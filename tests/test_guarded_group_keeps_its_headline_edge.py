"""A guarded route must still give its group the edge it ranks on.

`_group_sort_value` was fixed on 2026-08-31 so a guarded group "ranks on its
real edge, badged, rather than being forced to the tail" -- guarded is a claim
about identity evidence, not about size. `_apply_spread_freshness` was never
updated to match: `current_rankable` rejects any route carrying
`mirage_guarded`, so the group's `best_edge_pct` is set to None, and the fixed
sort then reads None and ranks it at 0.0. The value the sort was repaired to
use is discarded just before it is read.

Traced on production 2026-09-03. OPENAI carries a matched, quote-current
Ourbit-spot to Binance-futures route at 64.29%, not identity-mismatched and not
thin. Its only disqualifier is the guard, which is set because the tokenised
registry has no OPENAI entry. The token therefore sat mid-board at an effective
0.0 among the near-zero spreads -- which is also why the board reads as a run of
large spreads, then small ones, then large ones again.
"""

from __future__ import annotations

import time

from spreadboard import server


def _route(spread: float, **flags) -> dict:
    now_us = int(time.time() * 1_000_000)
    route = {
        "token": "OPENAI",
        "route_key": f"OPENAI-{spread}",
        "long_venue": "Ourbit",
        "long_market_type": "Spot",
        "short_venue": "Binance",
        "short_market_type": "Futures",
        "depth_weighted_spread_pct": spread,
        "executable_spread_pct": spread,
        "matched_size_notional_usd": 500.0,
        "target_notional_usd": 500.0,
        "depth_usd": 500.0,
        "live_book": True,
        "depth_unverified": False,
        "quote_ts_us": now_us,
        "age_min": 0.0,
        "funding_daily_pct": 0.0,
    }
    route.update(flags)
    return route


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


def test_a_guarded_route_still_supplies_the_headline_edge() -> None:
    payload = _payload(_route(64.29, mirage_guarded=True))

    server._apply_spread_freshness(payload)

    assert _edges(payload) == [64.29]


def test_an_identity_mismatch_still_does_not() -> None:
    """Two legs that may be different assets do not have a spread between them."""

    payload = _payload(_route(64.29, identity_mismatch=True))

    server._apply_spread_freshness(payload)

    assert _edges(payload) == [None]


def test_a_thin_book_still_does_not() -> None:
    """The number is real but not available at the probe size."""

    payload = _payload(_route(64.29, thin_book=True))

    server._apply_spread_freshness(payload)

    assert _edges(payload) == [None]


def test_an_explicit_do_not_rank_is_still_honoured() -> None:
    payload = _payload(
        _route(64.29, mirage_guarded=True, tokenized_guard={"rankable": False})
    )

    server._apply_spread_freshness(payload)

    assert _edges(payload) == [None]


def test_a_wide_guarded_group_sorts_above_a_narrow_clean_one() -> None:
    """The reader's complaint: large spreads sat below small ones."""

    wide = _route(64.29, mirage_guarded=True)
    wide["token"] = "OPENAI"
    narrow = _route(0.2)
    narrow["token"] = "CLEAN"
    payload = _payload(narrow, wide)

    server._apply_spread_freshness(payload)

    assert [group["token"] for group in payload["groups"]] == ["OPENAI", "CLEAN"]
