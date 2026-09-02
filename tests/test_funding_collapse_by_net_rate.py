"""Collapse on the NET RATE, not on the leg's market type.

My first rule merged every route sharing a short leg, which was wrong: net carry
is short minus long, so futures-futures routes with different longs carry
genuinely different rates.

My second rule keyed on market type -- collapse only when the long is spot/DEX --
which was wrong in the other direction. It left 373 surplus rows on the board
whose short leg AND rate were identical, and it grew the catalogue to 384.5MB,
larger than the 259.9MB it started at.

What actually makes a row redundant is carrying the same net rate for the same
short leg. That is the rule, and it needs no knowledge of leg types.

A token listed on fifteen futures venues has 15x14 ordered pairs, all with
distinct rates, so distinctness alone cannot bound the catalogue. Longs per short
leg are capped as well: the ranked page shows a handful, and the rest is weight.
"""

from __future__ import annotations

from spreadboard import funding_catalog


def _r(short, *, long_venue, rate, spread=0.5, long_type="Spot"):
    return {
        "token": "TKN",
        "long_venue": long_venue,
        "long_market_type": long_type,
        "long_market_symbol": f"TKN/USDT@{long_venue}",
        "short_venue": short,
        "short_market_type": "Futures",
        "short_market_symbol": f"TKN/USDT:USDT@{short}",
        "funding_apr_pct": rate,
        "displayed_open_spread_pct": spread,
    }


def test_identical_rates_on_one_short_leg_collapse() -> None:
    routes = [
        _r("Aster", long_venue="Gate", rate=116.797, spread=0.1),
        _r("Aster", long_venue="Mexc", rate=116.797, spread=0.9),
        _r("Aster", long_venue="HTX", rate=116.797, spread=0.4),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 1
    assert kept[0]["long_venue"] == "Mexc", "kept a worse entry than it had to"


def test_identical_rates_collapse_even_on_futures_longs() -> None:
    """The 373 rows my market-type rule let through."""

    routes = [
        _r("Aster", long_venue="Gate", rate=100.0, long_type="Futures", spread=0.1),
        _r("Aster", long_venue="Mexc", rate=100.0, long_type="Futures", spread=0.7),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 1


def test_different_rates_on_one_short_leg_are_all_kept() -> None:
    """Production 龙虾: a Gate short at 91.76%, 86.29% and 80.81%."""

    routes = [
        _r("Gate", long_venue="Mexc", rate=91.761, long_type="Futures"),
        _r("Gate", long_venue="HTX", rate=86.286, long_type="Futures"),
        _r("Gate", long_venue="Bitget", rate=80.811, long_type="Futures"),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 3
    assert {round(r["funding_apr_pct"], 3) for r in kept} == {91.761, 86.286, 80.811}


def test_longs_per_short_leg_are_bounded() -> None:
    """Distinctness alone cannot bound a token on fifteen futures venues."""

    routes = [
        _r("Aster", long_venue=f"V{i}", rate=100.0 - i, long_type="Futures", spread=i / 10)
        for i in range(12)
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) <= funding_catalog.LONGS_PER_SHORT_LEG
    # The survivors are the best paying, not an arbitrary slice.
    assert max(r["funding_apr_pct"] for r in kept) == 100.0


def test_a_rate_that_is_unknown_does_not_merge_with_a_known_one() -> None:
    """None is "we do not know", not "the same as that one"."""

    routes = [
        _r("Aster", long_venue="Gate", rate=None),
        _r("Aster", long_venue="Mexc", rate=100.0),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 2


def test_two_unknown_rates_are_not_assumed_equal() -> None:
    """Two rates we cannot read might differ.

    Keying them both as None merges them and reports one route's unknown as the
    other's, which is a claim neither row supports.
    """

    routes = [
        _r("Aster", long_venue="Gate", rate=None, spread=0.1),
        _r("Aster", long_venue="Mexc", rate=None, spread=0.9),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 2, "merged two routes whose rates are simply unknown"
