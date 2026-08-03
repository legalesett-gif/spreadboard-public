"""Carry and spread are separate mechanisms.

A farm whose basis never converges can still pay well. The reference product's
entire futures lane runs on negative open spreads (-0.15% to -0.52%) while
paying 100-380% APR, and a spread floor applied before the funding test dropped
exactly those rows.
"""

from __future__ import annotations

from dataclasses import fields

from spreadboard import api_spreads


def _row(*, spread: float, funding: float) -> api_spreads.SpreadTerminalRow:
    row = api_spreads.SpreadTerminalRow.__new__(api_spreads.SpreadTerminalRow)
    values = {f.name: None for f in fields(api_spreads.SpreadTerminalRow)}
    values.update(
        token="T",
        route_key=f"T|A|Futures|B|Futures|{spread}",
        route_kind="FUTURES",
        long_venue="A", long_market_type="Futures", long_market_symbol="T/USDT:USDT",
        short_venue="B", short_market_type="Futures", short_market_symbol="T/USDT:USDT",
        displayed_open_spread_pct=spread,
        executable_spread_pct=spread,
        depth_weighted_spread_pct=spread,
        funding_24h_pct=funding,
        funding_daily_pct=funding,
        freshness="fresh",
        blockers=[],
    )
    for key, value in values.items():
        object.__setattr__(row, key, value)
    return row


def test_a_negative_spread_farm_survives_the_funding_lane() -> None:
    paying = _row(spread=-0.35, funding=0.45)

    kept = api_spreads._filter_rows(
        [paying], q=None, exchange=None, kind=None, source=None,
        min_spread_pct=0.5, min_abs_funding_24h_pct=None, funding_only=True, include_stale=True,
    )

    assert kept == [paying], "a spread floor must not decide a funding row"


def test_the_spread_floor_still_applies_to_the_spread_lane() -> None:
    thin = _row(spread=0.1, funding=0.0)

    kept = api_spreads._filter_rows(
        [thin], q=None, exchange=None, kind=None, source=None,
        min_spread_pct=0.5, min_abs_funding_24h_pct=None, funding_only=False, include_stale=True,
    )

    assert kept == []


def test_a_funding_lane_still_requires_carry_it_receives() -> None:
    paying_out = _row(spread=-0.35, funding=-0.45)

    kept = api_spreads._filter_rows(
        [paying_out], q=None, exchange=None, kind=None, source=None,
        min_spread_pct=None, min_abs_funding_24h_pct=None, funding_only=True, include_stale=True,
    )

    assert kept == []
