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
