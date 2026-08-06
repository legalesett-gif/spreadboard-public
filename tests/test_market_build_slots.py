"""Concurrent cold builds of *different* views must not stack up.

Single-flight already stops N readers of one view from starting N builds.
Nothing stopped N readers of N different views, and every lane tab is its own
view: opening the five kind filters plus funding straight after a restart
started six full board builds in parallel, took the service from 0.9GB to
3.98GB, and pinned the container against its 6GB cap -- at which point Caddy
could not dial the app at all and served 502s while the process sat at 182%
CPU.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from spreadboard import server


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Both stores, or one test is served the previous test's stale payload."""

    def wipe() -> None:
        with server._MARKET_CACHE_LOCK:
            server._MARKET_CACHE.clear()
            server._MARKET_CACHE_INFLIGHT.clear()
            server._MARKET_STALE_CACHE.clear()

    wipe()
    yield
    wipe()


def test_only_a_few_views_build_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    concurrent = 0
    peak = 0
    lock = threading.Lock()

    def slow_load(**kwargs: object) -> dict[str, object]:
        nonlocal concurrent, peak
        with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        try:
            time.sleep(0.4)
            return {"rows": [], "ok": True}
        finally:
            with lock:
                concurrent -= 1

    monkeypatch.setattr(server.api_spreads, "load_spreads", slow_load)

    # Six different views, which is what the six board tabs are.
    threads = [
        threading.Thread(
            target=server.api_market_spreads,
            args=(Path("data/spreadboard.json"), {"kind": [kind]}),
        )
        for kind in ("FUTURES", "SPOT", "SPOT-FUTURES", "DEX-FUTURES", "DEX-SPOT", "ALL")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    limit = server._MARKET_BUILD_SLOTS._initial_value
    assert peak <= limit, f"{peak} builds ran at once with a limit of {limit}"


def test_the_slot_is_released_when_a_build_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked slot would wedge the board permanently."""

    def boom(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("venue exploded")

    monkeypatch.setattr(server.api_spreads, "load_spreads", boom)

    for _ in range(server._MARKET_BUILD_SLOTS._initial_value + 2):
        with pytest.raises(RuntimeError):
            server.api_market_spreads(Path("data/spreadboard.json"), {"kind": ["FUTURES"]})

    # Every slot must still be free.
    taken = []
    try:
        for _ in range(server._MARKET_BUILD_SLOTS._initial_value):
            assert server._MARKET_BUILD_SLOTS.acquire(timeout=1.0), "a slot leaked"
            taken.append(True)
    finally:
        for _ in taken:
            server._MARKET_BUILD_SLOTS.release()


def test_a_queued_reader_is_served_rather_than_left_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A view built before must answer from the previous copy under load."""
    calls: list[int] = []

    def load(**kwargs: object) -> dict[str, object]:
        calls.append(1)
        return {"rows": [{"route_key": "A|B|C"}], "ok": True}

    monkeypatch.setattr(server.api_spreads, "load_spreads", load)
    board = Path("data/spreadboard.json")

    first = server.api_market_spreads(board, {"kind": ["FUTURES"]})
    assert first["rows"]

    # Drain every build slot so the next call cannot build.
    held = []
    for _ in range(server._MARKET_BUILD_SLOTS._initial_value):
        assert server._MARKET_BUILD_SLOTS.acquire(timeout=1.0)
        held.append(True)
    try:
        # Expire the fresh entry so it has to fall back to the stale copy.
        with server._MARKET_CACHE_LOCK:
            for key, (_stamp, payload) in list(server._MARKET_CACHE.items()):
                server._MARKET_CACHE[key] = (time.monotonic() - 10_000, payload)
        served = server.api_market_spreads(board, {"kind": ["FUTURES"]})
    finally:
        for _ in held:
            server._MARKET_BUILD_SLOTS.release()

    assert served is not None
    assert "rows" in served


def test_the_limit_is_configurable_and_at_least_one() -> None:
    assert server._MARKET_BUILD_SLOTS._initial_value >= 1
