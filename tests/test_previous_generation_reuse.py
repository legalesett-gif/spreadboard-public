"""A page must not rebuild the board because a price file was rewritten.

`_market_cache_key` carries the mtime and size of eight artefacts. Four of them
are prices and funding, which the collector rewrites every few seconds, so the
key changes far faster than the ten-to-twenty seconds a projection takes to
build. The previous-generation path exists for exactly that: reuse the last
structurally identical payload and let `_apply_spread_freshness_coalesced`
overlay current books on the way out.

It excluded `materialized_live_query_projection` payloads, which is the only
mode production builds. So `/free` rebuilt the whole projection on essentially
every request -- measured on production at 8.9s to 40.6s, never once fast.
"""

from __future__ import annotations

import pytest

from spreadboard import server

PROJECTION = "materialized_live_query_projection"


@pytest.fixture(autouse=True)
def _empty_cache():
    with server._MARKET_CACHE_LOCK:
        server._MARKET_CACHE.clear()
    yield
    with server._MARKET_CACHE_LOCK:
        server._MARKET_CACHE.clear()


def _key(*, fast_quotes: int = 1, discovery: int = 1) -> tuple:
    return (
        "/board.json",
        (10, 100),
        (discovery, 200),
        (fast_quotes, 300),
        (40, 400),
        (50, 500),
        (60, 600),
        (70, 700),
        (80, 800),
        (("limit", ("500",)),),
    )


def _store(key: tuple, payload: dict) -> None:
    server._market_cache_finish(key, payload)


def test_a_rewritten_price_file_reuses_the_last_projection() -> None:
    """Only the fast-quote signature moved, and prices are overlaid on return."""

    built = {"mode": PROJECTION, "groups": [{"token": "BP"}], "generation": "first"}
    _store(_key(fast_quotes=1), built)

    reused = server._market_cache_get(
        _key(fast_quotes=2), allow_previous_generation=True
    )

    assert reused is not None
    assert reused["generation"] == "first"


def test_a_new_structural_generation_is_never_reused() -> None:
    """A different discovery snapshot is a different set of routes."""

    _store(
        _key(discovery=1),
        {"mode": PROJECTION, "groups": [{"token": "BP"}], "generation": "old"},
    )

    assert (
        server._market_cache_get(_key(discovery=2), allow_previous_generation=True)
        is None
    )


def test_reuse_stays_opt_in() -> None:
    """The background builder asks for the exact generation and must not get another."""

    _store(
        _key(fast_quotes=1),
        {"mode": PROJECTION, "groups": [{"token": "BP"}], "generation": "old"},
    )

    assert (
        server._market_cache_get(_key(fast_quotes=2), allow_previous_generation=False)
        is None
    )


def test_an_exact_hit_is_still_served_directly() -> None:
    """The fast path must keep working; reuse is the fallback, not the route."""

    _store(
        _key(fast_quotes=1),
        {"mode": PROJECTION, "groups": [{"token": "BP"}], "generation": "same"},
    )

    hit = server._market_cache_get(_key(fast_quotes=1), allow_previous_generation=True)

    assert hit is not None and hit["generation"] == "same"


def test_an_empty_board_is_never_reused_as_a_generation() -> None:
    """A projection with no groups is correct for its own key and only that one."""

    _store(_key(fast_quotes=1), {"mode": PROJECTION, "groups": [], "generation": "empty"})

    assert (
        server._market_cache_get(_key(fast_quotes=2), allow_previous_generation=True)
        is None
    )


def test_eviction_keeps_the_generation_pages_are_reading() -> None:
    """The entry reuse depends on was the first one thrown away.

    Eviction picked the oldest *written* entry. A previous generation is by
    definition older than the keys arriving now, and serving it does not
    rewrite it, so unrelated traffic evicted exactly the payload that was
    keeping `/free` off the rebuild path. Production allows six entries and a
    500-row view is 21MB, so the cache cannot simply be made larger.
    """

    server._MARKET_CACHE_MAX_ENTRIES = 3
    try:
        for index in range(3):
            _store(
                _key(fast_quotes=index),
                {"mode": PROJECTION, "groups": [{"n": index}], "generation": str(index)},
            )
        # A page reads the oldest-written entry as its previous generation.
        assert (
            server._market_cache_get(_key(fast_quotes=0), allow_previous_generation=True)
            is not None
        )
        _store(
            _key(fast_quotes=9),
            {"mode": PROJECTION, "groups": [{"n": 9}], "generation": "9"},
        )

        with server._MARKET_CACHE_LOCK:
            surviving = {
                value[1]["generation"] for value in server._MARKET_CACHE.values()
            }
    finally:
        server._MARKET_CACHE_MAX_ENTRIES = 32

    assert "0" in surviving
    assert "1" not in surviving


def test_serving_an_earlier_generation_starts_building_the_current_one(
    monkeypatch, tmp_path
) -> None:
    """Otherwise nothing builds it: reuse satisfies the request, and the web
    role does not run the background warm. The board would sit on one structure
    until the entry aged out -- fifteen minutes in which a route that appeared
    could not show up on the page the operator is watching."""

    started: list[tuple] = []
    monkeypatch.setattr(
        server,
        "_schedule_market_generation_warm",
        lambda board_path, query, cache_key: started.append(cache_key) or True,
    )
    monkeypatch.setattr(server, "_market_cache_key", lambda board_path, query: _key(
        fast_quotes=2
    ))
    monkeypatch.setattr(server, "_exact_catalog_market_projection", lambda *a, **k: None)
    monkeypatch.setattr(server, "_sync_telegram_client_universe", lambda payload: payload)
    _store(
        _key(fast_quotes=1),
        {"mode": PROJECTION, "groups": [{"token": "BP"}], "generation": "earlier"},
    )

    served = server.api_market_spreads(tmp_path / "board.json", {})

    assert served["generation"] == "earlier"
    assert started == [_key(fast_quotes=2)]


def test_an_exact_generation_starts_nothing(monkeypatch, tmp_path) -> None:
    started: list[tuple] = []
    monkeypatch.setattr(
        server,
        "_schedule_market_generation_warm",
        lambda board_path, query, cache_key: started.append(cache_key) or True,
    )
    monkeypatch.setattr(server, "_market_cache_key", lambda board_path, query: _key())
    monkeypatch.setattr(server, "_exact_catalog_market_projection", lambda *a, **k: None)
    monkeypatch.setattr(server, "_sync_telegram_client_universe", lambda payload: payload)
    _store(_key(), {"mode": PROJECTION, "groups": [{"token": "BP"}], "generation": "now"})

    served = server.api_market_spreads(tmp_path / "board.json", {})

    assert served["generation"] == "now"
    assert started == []


def test_the_background_build_is_throttled_per_query(monkeypatch, tmp_path) -> None:
    """Every request would otherwise start its own rebuild of the same board."""

    threads: list[str] = []

    class _Recorded:
        def __init__(self, **kwargs) -> None:
            threads.append(kwargs.get("name", ""))

        def start(self) -> None:
            return None

    monkeypatch.setattr(server.threading, "Thread", _Recorded)
    server._MARKET_GENERATION_WARM_AT.clear()

    first = server._schedule_market_generation_warm(tmp_path, {}, _key(fast_quotes=1))
    second = server._schedule_market_generation_warm(tmp_path, {}, _key(fast_quotes=2))

    assert first is True
    assert second is False
    assert threads == ["market-generation-warm"]
