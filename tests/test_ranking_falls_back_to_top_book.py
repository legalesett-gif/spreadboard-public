"""The board must rank by the number it shows, not by one it rarely has.

`current_rankable` demanded a route be both currently quoted and matched-depth
verified. The pipeline almost never produces both at once: the websocket books
reprice every minute but publish `depth_unverified: true` with no matched VWAP,
while the $500 matched probes run on the slow path and are ~2 hours old by the
time a page is served. Measured on production, 446 of 500 board groups had
`best_edge_pct: None` and therefore all sorted at 0.0 -- a handful of ranked
rows above a mass in arbitrary order.

The display already falls back: it renders "$500 VWAP · X% top book" when a
probe exists and "X% top book · target depth unavailable" when it does not. So
those 446 groups showed a number while ranking as though they had none.

The fallback rides on `spread_evidence_state`, which is the product's own trust
test: `verified` fills the matched probe, `research` is "current and
structurally plausible" top book, and `excluded` already rejects ticker-derived
wide rows, implausible price ratios, thin books and quote mismatches.
"""

from __future__ import annotations

import time

from spreadboard import api_spreads, server


def _route(**over) -> dict:
    now_us = int(time.time() * 1_000_000)
    route = {
        "token": "T",
        "route_key": "T-route",
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "short_venue": "Mexc",
        "short_market_type": "Futures",
        "route_kind": "FUTURES",
        "long_price": 100.0,
        "short_price": 101.0,
        "long_bid": 99.9,
        "long_ask": 100.0,
        "short_bid": 101.0,
        "short_ask": 101.1,
        "long_volume_24h_usd": 5_000_000.0,
        "short_volume_24h_usd": 5_000_000.0,
        "executable_spread_pct": 1.0,
        "displayed_open_spread_pct": 1.0,
        "quote_ts_us": now_us,
        "age_min": 0.0,
        "funding_daily_pct": 0.0,
        "deliverable": True,
    }
    route.update(over)
    return route


def _verified(**over) -> dict:
    return _route(
        depth_weighted_spread_pct=0.2,
        matched_size_notional_usd=500.0,
        target_notional_usd=500.0,
        depth_usd=500.0,
        live_book=True,
        depth_unverified=False,
        **over,
    )


def _research(**over) -> dict:
    """Fresh from the websocket books: current, but no matched probe."""

    return _route(depth_unverified=True, live_book=True, **over)


def _payload(*routes: dict) -> dict:
    return {
        "filters": {"sort": "edge", "direction": "desc"},
        "groups": [
            {"token": route["token"], "routes": [route], "best_route": route}
            for route in routes
        ],
    }


def _edges(payload: dict) -> list:
    return [group.get("best_edge_pct") for group in payload["groups"]]


def test_a_matched_route_still_ranks_on_its_matched_spread() -> None:
    payload = _payload(_verified())

    server._apply_spread_freshness(payload)

    assert _edges(payload) == [0.2]
    assert payload["groups"][0]["best_edge_basis"] == "matched_vwap"


def test_a_current_route_with_no_probe_ranks_on_its_top_book() -> None:
    """This is the 446. They were shown with a number and ranked without one."""

    payload = _payload(_research())

    server._apply_spread_freshness(payload)

    assert _edges(payload) == [1.0]
    assert payload["groups"][0]["best_edge_basis"] == "top_book"


def test_a_thin_book_route_still_ranks_on_nothing() -> None:
    payload = _payload(_research(thin_book=True))

    server._apply_spread_freshness(payload)

    assert _edges(payload) == [None]


def test_a_ticker_derived_wide_row_still_ranks_on_nothing() -> None:
    """bid == ask with a large edge is how a dead or crossed book reads.

    `vntl:OPENAI` published bid == ask == 1336.2 and invented a 57.85% spread.
    The fallback must not promote that class of row.
    """

    payload = _payload(
        _research(
            long_bid=100.0,
            long_ask=100.0,
            short_bid=145.0,
            short_ask=145.0,
            short_price=145.0,
            executable_spread_pct=45.0,
            displayed_open_spread_pct=45.0,
        )
    )

    server._apply_spread_freshness(payload)

    assert _edges(payload) == [None]


def test_a_stale_route_still_ranks_on_nothing() -> None:
    """Two hours old is not a current quote, matched probe or not."""

    stale_us = int((time.time() - 7200) * 1_000_000)
    payload = _payload(_verified(quote_ts_us=stale_us, age_min=120.0))

    server._apply_spread_freshness(payload)

    assert _edges(payload) == [None]


def test_the_evidence_classifier_agrees_with_the_fixtures() -> None:
    """Guard the fixtures themselves: if these drift the tests above go quiet."""

    assert api_spreads.spread_evidence_state(_verified()) != "excluded"
    assert api_spreads.spread_evidence_state(_research()) != "excluded"
    assert api_spreads.spread_evidence_state(_research(thin_book=True)) == "excluded"
