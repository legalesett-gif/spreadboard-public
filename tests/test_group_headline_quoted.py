"""A token's headline must show a number when the token has one.

Seven groups on the live board displayed "— waiting for a two-leg matched
quote" while holding 14 to 28 routes that were quoted right then. Every one had
picked a DEX route as its headline: the widest raw spread wins the ranking, and
a DEX leg's on-chain quote goes stale between samples, so the freshness gate
then blanked the number.

The ranking filters exist for good reasons -- a shut rail or a ticker collision
must not buy a top slot. But when nothing survives them the code fell back to
"widest raw", which is exactly the unquotable route. Falling back to a route
that can actually be priced costs nothing and is the difference between a board
that always shows a price and one with holes in it.
"""

from __future__ import annotations

from spreadboard import api_spreads


def _row(**kw):
    """A terminal row with only the fields the headline choice reads."""
    from dataclasses import fields

    base = {f.name: None for f in fields(api_spreads.SpreadTerminalRow)}
    base.update({
        "token": "BICO", "long_venue": "A", "short_venue": "B",
        "long_market_type": "Futures", "short_market_type": "Futures",
        "age_min": 0.5, "quote_ts_us": None, "blockers": [],
        "freshness": "fresh", "status": "live",
    })
    base.update(kw)
    return api_spreads.SpreadTerminalRow(**base)


def test_the_headline_prefers_a_route_that_can_be_priced() -> None:
    """The production shape: nothing survives the tradeable filters.

    Both routes are mirage-guarded, so `tradeable_rows` is empty and the code
    falls back across every route by widest raw spread -- which is the stale DEX
    leg that cannot render a number.
    """
    stale_dex = _row(
        long_venue="OKX DEX 1", executable_spread_pct=9.0,
        depth_weighted_spread_pct=None, age_min=90.0,
        blockers=["mirage_guard:ratio"],
    )
    quoted = _row(
        long_venue="Bitget", executable_spread_pct=0.27,
        depth_weighted_spread_pct=0.27, age_min=0.5,
        blockers=["mirage_guard:ratio"],
    )

    groups = api_spreads._group_rows([stale_dex, quoted])

    assert len(groups) == 1
    best = groups[0]["best_route"]
    assert best["long_venue"] == "Bitget", "headline still points at an unquotable route"


def test_a_group_with_nothing_quotable_still_produces_a_headline() -> None:
    """Never crash or drop the token just because everything is stale."""
    only_stale = _row(long_venue="OKX DEX 1", executable_spread_pct=9.0, age_min=90.0)

    groups = api_spreads._group_rows([only_stale])

    assert len(groups) == 1
    assert groups[0]["best_route"] is not None
