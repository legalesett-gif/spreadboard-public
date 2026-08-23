"""Fresh-generation cache and single-flight safety for the market board."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from spreadboard import server


def test_live_book_reader_reuses_only_current_last_good_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spreadboard import api_spreads, live_book_cache

    path = tmp_path / "books.sqlite3"
    path.touch()
    recent = live_book_cache.CachedBook(
        bids=[[1.0, 100.0]],
        asks=[[1.01, 100.0]],
        quote_ts_us=int(time.time() * 1_000_000),
    )
    expired = live_book_cache.CachedBook(
        bids=[[2.0, 100.0]],
        asks=[[2.01, 100.0]],
        quote_ts_us=int(
            (time.time() - api_spreads.LIVE_BOOK_MAX_AGE_SECONDS - 1) * 1_000_000
        ),
    )
    monkeypatch.setattr(live_book_cache, "DEFAULT_PATH", path)
    monkeypatch.setattr(
        live_book_cache,
        "LiveBookStore",
        lambda: (_ for _ in ()).throw(RuntimeError("writer handoff")),
    )
    monkeypatch.setattr(
        api_spreads,
        "_LAST_GOOD_LIVE_BOOKS",
        {"recent": recent, "expired": expired},
    )

    assert api_spreads._live_books() == {"recent": recent}


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


def test_member_build_waits_longer_than_background_warm(monkeypatch) -> None:
    from types import SimpleNamespace

    from spreadboard import server

    monkeypatch.setattr(server, "_MARKET_BUILD_SLOT_WAIT_SECONDS", 1.5)
    monkeypatch.setattr(server, "_MARKET_FOREGROUND_BUILD_SLOT_WAIT_SECONDS", 20.0)
    monkeypatch.setattr(
        server.threading,
        "current_thread",
        lambda: SimpleNamespace(name="Thread-7 (process_request_thread)"),
    )
    assert server._market_build_slot_wait_seconds() == 20.0

    monkeypatch.setattr(
        server.threading,
        "current_thread",
        lambda: SimpleNamespace(name="spreadboard-telegram-startup-warm"),
    )
    assert server._market_build_slot_wait_seconds() == 1.5


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
