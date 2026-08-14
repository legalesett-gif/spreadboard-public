"""The board must not go cold every time the snapshot is rewritten.

The market cache key includes the snapshot's file signature, and the funding
sweep rewrites that snapshot every couple of minutes -- so every warmed view
was invalidated together far more often than the discovery scan runs, and the
next visitor paid a full rebuild. Serving the previous payload while the new
one builds is what removes the cold window.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from spreadboard import server


def test_fast_quote_delta_changes_the_market_cache_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text("{}")
    monkeypatch.setattr(server.api_spreads, "DEFAULT_API_DISCOVERY_PATH", snapshot)

    first = server._market_cache_key(tmp_path / "board.jsonl", {})
    (tmp_path / "api_discovery_fast_quotes.json").write_text("{}")
    second = server._market_cache_key(tmp_path / "board.jsonl", {})

    assert first != second


def test_a_moved_snapshot_serves_the_previous_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    board = Path("board.jsonl")
    builds: list[int] = []

    def slow_build(**_kwargs):
        builds.append(1)
        time.sleep(0.05)
        return {"rows": [], "groups": [], "build": len(builds)}

    monkeypatch.setattr(server.api_spreads, "load_spreads", slow_build)

    signature = {"value": 1}
    monkeypatch.setattr(server, "_file_signature", lambda _p: signature["value"])
    monkeypatch.setattr(Path, "resolve", lambda self: self)

    first = server.api_market_spreads(board, {})
    assert first["build"] == 1

    # The snapshot is rewritten: every cache key changes at once.
    signature["value"] = 2
    second = server.api_market_spreads(board, {})

    # The visitor is handed the payload already built, not made to wait.
    assert second["build"] == 1, "a moved snapshot must not cost the visitor a rebuild"

    # And a refresh runs behind them.
    deadline = time.time() + 3
    while len(builds) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert len(builds) >= 2, "the refresh must still happen"


def test_a_payload_older_than_the_stale_limit_is_not_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving something indefinitely would be worse than a slow page."""
    monkeypatch.setattr(server, "_MARKET_STALE_MAX_SECONDS", 0.0)
    key = ("board", "sig", (("kind", ("SPOT",)),))
    with server._MARKET_CACHE_LOCK:
        server._MARKET_STALE_CACHE[server._market_stale_key(key)] = (
            time.monotonic() - 10.0,
            {"rows": []},
        )

    assert server._market_cache_stale_get(key) is None


def test_the_stale_index_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each payload is large; the second index must not grow without limit."""
    monkeypatch.setattr(server, "_MARKET_CACHE_MAX_ENTRIES", 3)
    with server._MARKET_CACHE_LOCK:
        server._MARKET_STALE_CACHE.clear()
    for index in range(8):
        server._market_cache_finish(("board", f"sig{index}", ((f"q{index}", ()),)), {"n": index})

    assert len(server._MARKET_STALE_CACHE) <= 3


def test_a_waiter_never_starts_a_second_build_of_the_same_view(monkeypatch) -> None:
    """The stampede that kept killing the container.

    A cold build takes 30-60s on two cores; waiters gave up after fifteen and
    built their own copy. After a restart the threads went 14 -> 40 and the
    process 0.5GB -> 4.2GB with every cache still empty.
    """
    import threading
    import time

    from spreadboard import api_spreads, server

    monkeypatch.setattr(server, "_MARKET_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_STALE_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_CACHE_INFLIGHT", {})
    monkeypatch.setattr(server, "_MARKET_BUILD_WAIT_SECONDS", 1.0)

    builds = []
    started = threading.Event()

    def slow_build(**kwargs):
        builds.append(kwargs)
        started.set()
        time.sleep(2.0)
        return {"ok": True, "groups": [], "rows": [], "summary": {}}

    monkeypatch.setattr(api_spreads, "load_spreads", slow_build)

    board = Path("board.json")
    owner = threading.Thread(
        target=lambda: server.api_market_spreads(board, {}), daemon=True
    )
    owner.start()
    assert started.wait(timeout=5.0)

    # A second request arrives while the first is still building.
    payload = server.api_market_spreads(board, {})
    owner.join(timeout=10.0)

    assert payload.get("ok") is False
    assert payload.get("status") == "warming"
    assert len(builds) == 1, f"the waiter started its own build: {len(builds)}"


def test_the_readiness_probe_never_builds_the_board(monkeypatch) -> None:
    """It ran a 14s build against a 12s probe timeout, so the container was
    reported unhealthy while serving pages in two seconds."""
    from spreadboard import api_spreads, server

    monkeypatch.setitem(server._HEALTH_CACHE, "payload", None)
    monkeypatch.setitem(server._HEALTH_CACHE, "at", 0.0)

    builds = []

    def build(**kwargs):
        builds.append(kwargs)
        return {"ok": True, "summary": {}, "source_health": {"canonical_api": {}}}

    monkeypatch.setattr(api_spreads, "load_spreads", build)

    first = server.api_source_health(Path("board.json"), {})
    second = server.api_source_health(Path("board.json"), {})

    assert first is not None and second is not None
    assert len(builds) == 0, "an HTTP probe must never build the grouped board"

    # And while another thread holds the build, it answers without waiting.
    server._HEALTH_BUILD_LOCK.acquire()
    try:
        monkeypatch.setitem(server._HEALTH_CACHE, "at", 0.0)
        answer = server.api_source_health(Path("board.json"), {})
    finally:
        server._HEALTH_BUILD_LOCK.release()
    assert len(builds) == 0
    assert answer is not None


def test_fast_quote_timestamp_completes_the_cheap_health_answer(monkeypatch) -> None:
    from spreadboard import api_spreads, server

    monkeypatch.setattr(
        api_spreads,
        "fast_quote_health",
        lambda: {
            "updated_at": "2026-08-14T17:29:30Z",
            "age_min": 0.25,
            "lane_token_counts": {"DEX-FUTURES": 40, "DEX-SPOT": 31},
        },
    )

    health = server._health_with_fast_quote_state(
        {"ok": True, "canonical_api": {"status": "warming"}, "market": {}}
    )

    assert health["canonical_api"]["updated_at"] == "2026-08-14T17:29:30Z"
    assert health["canonical_api"]["age_min"] == 0.25


def test_a_waiter_serves_stale_immediately_instead_of_waiting(monkeypatch) -> None:
    """It must not wait out someone else's build when it already has an answer.

    Checking stale only after the wait expired put a 25s pause in front of every
    page served while the warmer held the build: /free measured 26s three times
    running with the answer sitting in the stale cache the whole time.
    """
    import threading
    import time

    from spreadboard import api_spreads, server

    monkeypatch.setattr(server, "_MARKET_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_STALE_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_CACHE_INFLIGHT", {})
    monkeypatch.setattr(server, "_MARKET_BUILD_WAIT_SECONDS", 30.0)

    builds = []
    started = threading.Event()

    def slow_build(**kwargs):
        builds.append(kwargs)
        started.set()
        time.sleep(3.0)
        return {"ok": True, "groups": [], "rows": [], "summary": {"generation": len(builds)}}

    monkeypatch.setattr(api_spreads, "load_spreads", slow_build)

    board = Path("board.json")
    # The stale key is (first, last) of the cache key -- the snapshot signature
    # sits in the middle, so it changes while the stale key stays put.
    monkeypatch.setattr(server, "_market_cache_key", lambda path, query: ("board", "sig1", "view"))
    server.api_market_spreads(board, {})
    assert len(builds) == 1

    # The snapshot moves, so the exact key misses while a rebuild is in flight.
    monkeypatch.setattr(server, "_market_cache_key", lambda path, query: ("board", "sig2", "view"))
    owner = threading.Thread(target=lambda: server.api_market_spreads(board, {}), daemon=True)
    owner.start()
    assert started.wait(timeout=5.0)

    began = time.monotonic()
    payload = server.api_market_spreads(board, {})
    elapsed = time.monotonic() - began

    owner.join(timeout=10.0)
    assert payload.get("ok") is True, "a waiter with a stale copy got the warming page"
    assert elapsed < 1.0, f"the waiter sat for {elapsed:.1f}s with an answer in hand"
