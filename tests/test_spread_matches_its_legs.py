"""A rendered spread must be reproducible from the legs shown beside it.

Measured on production before this fix: 871 of 2,861 served routes (30.4%)
carried an ``executable_spread_pct`` that could not be reproduced from their own
displayed leg prices by either bid/ask crossing or price-to-price, including
sign flips and one route overstating the edge by 3.5 percentage points --
``O HTX->Binance`` served +6.012% while its legs implied +2.465%. Every one of
them was marked ``freshness=fresh`` and ``spread_quote_current=True``.

Cause: ``_overlay`` wrote the fresh spread and a fresh ``quote_ts_us`` but never
the leg prices, so the headline came from the live book pass while the legs
stayed at the last structural generation. ``live_route_updates_for`` computed
the fresh ask/bid, derived the spread from them, and discarded them.
"""

from __future__ import annotations

import time

from spreadboard import api_spreads, warm_query_projection


def _row(**over):
    base = {
        "route_key": "T|Gate|Futures|Mexc|Futures",
        "token": "T",
        "route_kind": "FUTURES",
        "long_venue": "Gate", "long_market_type": "Futures",
        "short_venue": "Mexc", "short_market_type": "Futures",
        # Deliberately stale legs from an older structural generation.
        "long_ask": 100.0, "long_bid": 99.9,
        "short_bid": 101.0, "short_ask": 101.1,
        "executable_spread_pct": 1.0,
        "quote_ts_us": int(time.time() * 1_000_000),
    }
    base.update(over)
    return base


def _crossing(row) -> float:
    return (float(row["short_bid"]) / float(row["long_ask"]) - 1.0) * 100.0


def test_a_top_book_update_rewrites_the_legs_it_priced() -> None:
    """The exact production failure: fresh spread, stale legs."""

    now = time.time()
    ts = int(now * 1_000_000)
    # Live book moved: buy at 200, sell at 206 -> +3%.
    update = (3.0, None, ts, "top_book", 200.0, 206.0)

    out = warm_query_projection._overlay(_row(), update, now=now)

    assert out["executable_spread_pct"] == 3.0
    assert out["long_ask"] == 200.0, "the leg the spread was priced from must be shown"
    assert out["short_bid"] == 206.0
    assert abs(_crossing(out) - 3.0) < 1e-9, (
        f"headline {out['executable_spread_pct']}% must reproduce from its legs, "
        f"got {_crossing(out)}%"
    )


def test_a_matched_vwap_update_rewrites_the_legs_it_priced() -> None:
    now = time.time()
    ts = int(now * 1_000_000)
    update = (2.0, None, ts, "matched_vwap", 50.0, 51.0)

    out = warm_query_projection._overlay(_row(), update, now=now)

    assert out["depth_weighted_spread_pct"] == 2.0
    assert out["depth_unverified"] is False
    assert out["long_ask"] == 50.0 and out["short_bid"] == 51.0
    assert abs(_crossing(out) - 2.0) < 1e-9


def test_a_retained_measurement_does_not_claim_fresh_legs() -> None:
    """A retained prior spread must not overwrite legs with someone else's."""

    now = time.time()
    ts = int(now * 1_000_000)
    update = (1.0, None, ts, "retained_matched_vwap", None, None)

    out = warm_query_projection._overlay(_row(), update, now=now)

    assert out["long_ask"] == 100.0, "legs must be left as they were"
    assert out["short_bid"] == 101.0


def test_a_funding_only_update_leaves_the_price_legs_alone() -> None:
    now = time.time()
    update = (None, 0.05, None, None, None, None)

    out = warm_query_projection._overlay(_row(), update, now=now)

    assert out["long_ask"] == 100.0 and out["short_bid"] == 101.0
    assert out["funding_daily_pct"] == 0.05


def test_the_structural_seed_carries_its_own_legs() -> None:
    """Seeding must not produce a row whose legs the overlay cannot restore."""

    seeded = warm_query_projection._structural_route_update(_row())

    assert seeded is not None and len(seeded) >= 6
    assert seeded[4] == 100.0 and seeded[5] == 101.0


def test_merging_a_retained_price_keeps_that_price_s_legs() -> None:
    """Legs must travel with the spread chosen, never be mixed across sources."""

    now = time.time()
    ts = int(now * 1_000_000)
    prior = (4.0, None, ts, "top_book", 10.0, 10.4)
    observed = {"k": (None, 0.01, None, None, None, None)}

    merged = warm_query_projection._merge_live_updates(
        {"k": prior}, observed, route_keys={"k"}, now=now
    )["k"]

    assert merged[0] == 4.0, "the still-current prior price is retained"
    assert merged[4] == 10.0 and merged[5] == 10.4, "with the legs it belongs to"


def test_live_prices_for_still_reads_a_three_tuple() -> None:
    """The compatibility view slices [:3]; appending must not break it."""

    import inspect

    source = inspect.getsource(api_spreads.live_prices_for)
    assert "(spread, funding, _quote_ts_us)" in source


def test_a_matched_update_also_refreshes_the_ranking_key() -> None:
    """executable_spread_pct is the sort key; a matched row must not rank stale.

    Before this, a matched observation wrote depth_weighted_spread_pct and left
    executable_spread_pct at its structural value. 96 served rows ranked by a
    stale number while displaying a current one.
    """

    now = time.time()
    ts = int(now * 1_000_000)
    # matched spread 2.0% from VWAP legs; top of book was 1.5% in the same read.
    update = (2.0, None, ts, "matched_vwap", 50.0, 51.0, 1.5)

    out = warm_query_projection._overlay(_row(), update, now=now)

    assert out["depth_weighted_spread_pct"] == 2.0, "matched figure is displayed"
    assert out["executable_spread_pct"] == 1.5, (
        "the ranking key must come from the same observation, not the old row"
    )
    assert out["displayed_open_spread_pct"] == 1.5


def test_a_matched_update_without_a_top_book_reading_leaves_the_key_alone() -> None:
    now = time.time()
    ts = int(now * 1_000_000)
    update = (2.0, None, ts, "matched_vwap", 50.0, 51.0, None)

    out = warm_query_projection._overlay(_row(), update, now=now)

    assert out["depth_weighted_spread_pct"] == 2.0
    assert out["executable_spread_pct"] == 1.0, "unchanged when nothing was read"
