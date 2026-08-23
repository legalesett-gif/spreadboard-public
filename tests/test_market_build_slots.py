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
    """Every test starts without a current payload or in-flight marker."""

    def wipe() -> None:
        with server._MARKET_CACHE_LOCK:
            server._MARKET_CACHE.clear()
            server._MARKET_CACHE_INFLIGHT.clear()

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


def test_a_queued_reader_gets_warming_instead_of_an_old_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity pressure cannot justify publishing an old matched spread."""
    calls: list[int] = []

    def load(**kwargs: object) -> dict[str, object]:
        calls.append(1)
        return {"rows": [{"route_key": "A|B|C"}], "ok": True}

    monkeypatch.setattr(server.api_spreads, "load_spreads", load)
    monkeypatch.setattr(server, "_MARKET_BUILD_SLOT_WAIT_SECONDS", 0.01)
    board = Path("data/spreadboard.json")

    first = server.api_market_spreads(board, {"kind": ["FUTURES"]})
    assert first["rows"]

    # Drain every build slot so the next call cannot build.
    held = []
    for _ in range(server._MARKET_BUILD_SLOTS._initial_value):
        assert server._MARKET_BUILD_SLOTS.acquire(timeout=1.0)
        held.append(True)
    try:
        # Expire the only current entry while no build slot is available.
        with server._MARKET_CACHE_LOCK:
            for key, (_stamp, payload) in list(server._MARKET_CACHE.items()):
                server._MARKET_CACHE[key] = (time.monotonic() - 10_000, payload)
        served = server.api_market_spreads(board, {"kind": ["FUTURES"]})
    finally:
        for _ in held:
            server._MARKET_BUILD_SLOTS.release()

    assert served.get("ok") is False
    assert served.get("status") == "warming"
    assert len(calls) == 1


def test_the_limit_is_configurable_and_at_least_one() -> None:
    assert server._MARKET_BUILD_SLOTS._initial_value >= 1


def test_production_serialises_full_market_view_builds() -> None:
    """Parallel full builds add stalls and can exhaust the app container."""
    import re

    compose = (Path(__file__).resolve().parents[1] / "compose.production.yml").read_text()
    match = re.search(r'SPREADBOARD_MARKET_BUILD_SLOTS:\s*"(\d+)"', compose)
    assert match is not None
    assert int(match.group(1)) == 1


def test_admission_control_fails_fast() -> None:
    """It must not add a second long wait on top of the in-flight gate.

    A request could spend 25s waiting for the in-flight event and another 25s
    waiting for a build slot: ten lane tabs opened together produced a 60s hang
    and a dropped connection rather than a prompt "still warming".
    """
    assert server._MARKET_BUILD_SLOT_WAIT_SECONDS < server._MARKET_BUILD_WAIT_SECONDS
    assert server._MARKET_BUILD_WAIT_SECONDS <= 5
    assert server._MARKET_BUILD_SLOT_WAIT_SECONDS <= 2

    import inspect

    source = inspect.getsource(server.api_market_spreads)
    assert "_MARKET_BUILD_SLOTS.acquire(timeout=_market_build_slot_wait_seconds())" in source
    # Warmers retain the original fast admission boundary; only a real member
    # request can wait for the single current owner instead of receiving a
    # false empty market.
    assert server._MARKET_BUILD_SLOT_WAIT_SECONDS <= 2
    assert server._MARKET_FOREGROUND_BUILD_SLOT_WAIT_SECONDS <= 20
