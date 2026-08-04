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
