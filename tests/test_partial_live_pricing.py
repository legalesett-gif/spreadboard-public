"""A route moves when either leg moves, not only when both do.

Requiring both legs to stream left the Futures-Spot lane at 0% live with 7 of
10 routes already having one live leg, and the DEX lanes at 0% permanently --
a DEX leg has no websocket to stream from, so those rows could never move.
"""

from __future__ import annotations

import pytest

from spreadboard import api_spreads, live_book_cache


def _route(**overrides) -> dict:
    route = {
        "route_key": "T|Gate|Spot|Bybit|Futures",
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "long_market_symbol": "T/USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "T/USDT:USDT",
        "long_ask": 1.00,
        "short_bid": 1.10,
        "funding_daily_pct": 0.2,
    }
    route.update(overrides)
    return route


class _Book:
    """A live book as api_spreads reads it: bids, asks and a quote timestamp."""

    def __init__(self, bid: float, ask: float) -> None:
        self.bids = [[bid, 100.0]]
        self.asks = [[ask, 100.0]]
        self.quote_ts_us = 1_700_000_100_000_000


def _book(bid: float, ask: float) -> _Book:
    return _Book(bid, ask)


def test_a_live_short_leg_reprices_against_the_stored_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DEX case: only the exchange leg can ever stream."""
    key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(api_spreads, "_live_books", lambda: {key: _book(1.20, 1.21)})

    priced = api_spreads.live_prices_for([_route()])

    # Sell at the live bid 1.20 against the stored ask 1.00.
    assert priced["T|Gate|Spot|Bybit|Futures"][0] == pytest.approx(20.0)


def test_a_live_long_leg_reprices_against_the_stored_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = live_book_cache.cache_key("Gate", "Spot", "T/USDT")
    monkeypatch.setattr(api_spreads, "_live_books", lambda: {key: _book(0.79, 0.80)})

    priced = api_spreads.live_prices_for([_route()])

    # Buy at the live ask 0.80 against the stored bid 1.10.
    assert priced["T|Gate|Spot|Bybit|Futures"][0] == pytest.approx(37.5)


def test_both_legs_live_still_uses_both(monkeypatch: pytest.MonkeyPatch) -> None:
    long_key = live_book_cache.cache_key("Gate", "Spot", "T/USDT")
    short_key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(
        api_spreads,
        "_live_books",
        lambda: {long_key: _book(0.99, 1.00), short_key: _book(1.50, 1.51)},
    )

    priced = api_spreads.live_prices_for([_route()])

    assert priced["T|Gate|Spot|Bybit|Futures"][0] == pytest.approx(50.0)


def test_no_live_leg_leaves_the_route_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_spreads, "_live_books", lambda: {})

    assert api_spreads.live_prices_for([_route()]) == {}


def test_the_board_filters_on_one_live_leg_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_live_books decides what the board ranks and filters on.

    Requiring both legs meant a route with one fresh leg was filtered on a
    price from the last scan while the stream showed something else -- the two
    paths disagreed about the same row.
    """
    from dataclasses import fields

    from spreadboard.api_spreads import SpreadTerminalRow, apply_live_books

    row = SpreadTerminalRow.__new__(SpreadTerminalRow)
    defaults = {f.name: None for f in fields(SpreadTerminalRow)}
    defaults.update(
        long_venue="Gate", long_market_type="Spot", long_market_symbol="T/USDT",
        short_venue="Bybit", short_market_type="Futures", short_market_symbol="T/USDT:USDT",
        long_ask=1.00, short_bid=1.10, long_price=1.00, short_price=1.10,
        displayed_open_spread_pct=10.0, executable_spread_pct=10.0,
        depth_weighted_spread_pct=10.0, quote_ts_us=1_700_000_000_000_000,
        blockers=[], live_book=False,
    )
    for key, value in defaults.items():
        object.__setattr__(row, key, value)

    key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    updated = apply_live_books([row], {key: _book(1.30, 1.31)}, now=1_700_000_100.0)

    # Sell at the live bid 1.30 against the stored ask 1.00.
    assert updated[0].displayed_open_spread_pct == pytest.approx(30.0)
    assert updated[0].live_book is True
