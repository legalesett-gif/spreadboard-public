"""A headline spread must be reproducible from the legs printed beside it.

A short leg is opened by SELLING into the bid. Some producers derived the
headline as ``(short_price / long_ask - 1)`` using the venue's LAST price,
which sits inside the spread -- so the board printed an edge nobody could
take, and when the last trade printed above the bid it printed a POSITIVE
spread on a route that was negative at the touch.

Both numbers below were read off production on 2026-08-29 from the
OKX DEX -> Futures lane, the same shape as the owner's own DEX-long /
CEX-short farms.
"""

from __future__ import annotations

from dataclasses import replace

from spreadboard import api_spreads


def _row(*, long_ask: float, short_bid: float, producer_headline: float):
    return api_spreads._row_from_api(
        {
            "token": "PEPE",
            "long_venue": "OKX DEX 1",
            "long_market_type": "Spot",
            "long_market_symbol": "PEPE",
            "short_venue": "Kraken Futures",
            "short_market_type": "Futures",
            "short_market_symbol": "PEPE/USD:USD",
            "executable_spread_pct": producer_headline,
            "notes": {
                "route_inputs": {
                    "long": {"symbol": "PEPE", "ask": long_ask},
                    "short": {"symbol": "PEPE/USD:USD", "bid": short_bid},
                }
            },
        },
        bucket="api_discovered",
        now=1.0,
    )


def test_a_last_price_headline_is_replaced_by_the_bid_it_must_sell_into() -> None:
    """The exact production PEPE row: +0.395% shown, +0.340% at the bid."""

    long_ask = 3.6311664304367705e-06
    short_bid = 3.6435e-06
    row = _row(long_ask=long_ask, short_bid=short_bid, producer_headline=0.3947373340721594)

    at_the_bid = (short_bid / long_ask - 1.0) * 100.0
    assert abs(at_the_bid - 0.3398) < 0.001, "guard the fixture itself"
    assert row.displayed_open_spread_pct is not None
    assert abs(row.displayed_open_spread_pct - at_the_bid) < 1e-9, (
        "the headline must be the spread the published legs support"
    )
    assert abs(row.executable_spread_pct - at_the_bid) < 1e-9


def test_a_positive_headline_is_corrected_to_negative_when_the_bid_says_so() -> None:
    """Production token "4": +0.071% displayed, -0.037% selling into the bid.

    This is the case that matters. A member sorting by spread saw a route
    paying 7bp; taking it at the touch lost 4bp.
    """

    long_ask = 0.018576842449894347
    short_bid = 0.01857
    row = _row(long_ask=long_ask, short_bid=short_bid, producer_headline=0.07082769927742039)

    assert row.displayed_open_spread_pct is not None
    assert row.displayed_open_spread_pct < 0.0, (
        "a route that is negative at the touch must not print a positive edge"
    )
    assert abs(row.displayed_open_spread_pct - (-0.0368)) < 0.001


def test_a_coherent_row_is_left_exactly_as_the_producer_measured_it() -> None:
    """The common case must not be rewritten, not even by float churn."""

    long_ask = 0.04198878169187255
    short_bid = 0.0421
    headline = (short_bid / long_ask - 1.0) * 100.0
    row = _row(long_ask=long_ask, short_bid=short_bid, producer_headline=headline)

    assert row.executable_spread_pct == headline


def test_a_row_without_published_legs_keeps_the_producer_headline() -> None:
    """With no legs to check against, the measurement stands unaltered."""

    row = api_spreads._row_from_api(
        {
            "token": "PEPE",
            "long_venue": "OKX DEX 1",
            "long_market_type": "Spot",
            "short_venue": "Kraken Futures",
            "short_market_type": "Futures",
            "executable_spread_pct": 0.3947373340721594,
            "notes": {"route_inputs": {"long": {}, "short": {}}},
        },
        bucket="api_discovered",
        now=1.0,
    )

    assert row.executable_spread_pct == 0.3947373340721594


def test_the_published_row_is_coherent_whatever_produced_it() -> None:
    """The last gate before serving, on the fields the reader actually sees.

    Rows reach the member from several producers and from a merge that can pair
    one generation's spread with another generation's legs. Production served
    HFT Kraken->Kraken Futures at +2.213% while its own published legs implied
    -4.194%: a member sorting by spread saw a route paying 221bp on a route
    that was 419bp underwater at the touch.
    """

    long_ask, short_bid = 1.0, 0.95806
    row = api_spreads._row_from_api(
        {
            "token": "HFT",
            "long_venue": "Kraken",
            "long_market_type": "Spot",
            "short_venue": "Kraken Futures",
            "short_market_type": "Futures",
            "executable_spread_pct": 2.213,
            "notes": {"route_inputs": {"long": {}, "short": {}}},
        },
        bucket="api_discovered",
        now=1.0,
    )
    # The legs arrive from a producer that does not populate route_inputs.
    row = replace(row, long_ask=long_ask, short_bid=short_bid)

    payload = api_spreads._public_row(row)

    implied = (short_bid / long_ask - 1.0) * 100.0
    assert implied < 0
    assert payload["displayed_open_spread_pct"] < 0.0, (
        "a route underwater at the touch must not print a positive headline"
    )
    assert abs(payload["displayed_open_spread_pct"] - implied) < 1e-9


def test_a_published_row_without_legs_keeps_its_measured_headline() -> None:
    row = api_spreads._row_from_api(
        {
            "token": "HFT",
            "long_venue": "Kraken",
            "long_market_type": "Spot",
            "short_venue": "Bybit",
            "short_market_type": "Futures",
            "executable_spread_pct": 2.213,
            "notes": {"route_inputs": {"long": {}, "short": {}}},
        },
        bucket="api_discovered",
        now=1.0,
    )

    payload = api_spreads._public_row(row)

    assert payload["displayed_open_spread_pct"] == 2.213
