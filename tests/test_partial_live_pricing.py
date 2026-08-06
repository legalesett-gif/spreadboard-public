"""Live repricing must never manufacture a mixed-time CEX spread.

DEX routes are the intentional exception because the on-chain leg cannot have
a centralised-exchange websocket book.
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


def test_a_live_short_leg_does_not_mix_with_a_stored_cex_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(api_spreads, "_live_books", lambda: {key: _book(1.20, 1.21)})

    priced = api_spreads.live_prices_for([_route()])

    assert priced == {}


def test_a_live_long_leg_does_not_mix_with_a_stored_cex_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = live_book_cache.cache_key("Gate", "Spot", "T/USDT")
    monkeypatch.setattr(api_spreads, "_live_books", lambda: {key: _book(0.79, 0.80)})

    priced = api_spreads.live_prices_for([_route()])

    assert priced == {}


def test_a_live_cex_leg_can_reprice_a_dex_route(monkeypatch: pytest.MonkeyPatch) -> None:
    key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(api_spreads, "_live_books", lambda: {key: _book(1.20, 1.21)})

    priced = api_spreads.live_prices_for([
        _route(long_venue="Uniswap", long_market_type="DEX")
    ])

    assert priced["T|Gate|Spot|Bybit|Futures"][0] == pytest.approx(20.0)


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


def test_the_board_does_not_filter_on_one_live_cex_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_live_books decides what the board ranks and filters on.

    A current leg and an older leg are not a simultaneous executable spread.
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

    assert updated[0].displayed_open_spread_pct == pytest.approx(10.0)
    assert updated[0].live_book is False


def test_live_funding_recomputes_each_leg_on_its_own_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_spreads, "_live_books", lambda: {})
    monkeypatch.setattr(
        "spreadboard.bulk_quotes.load_funding",
        lambda: {
            "Gate|T/USDT": {"rate_pct": 0.01, "interval_hours": 1},
            "Bybit|T/USDT:USDT": {"rate_pct": 0.04, "interval_hours": 4},
        },
    )
    route = _route(long_market_type="Futures")

    priced = api_spreads.live_prices_for([route])

    # short: 0.04 * 6 = 0.24/day; long: 0.01 * 24 = 0.24/day.
    assert priced[route["route_key"]] == (None, pytest.approx(0.0))
