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
        "age_min": 0.1,
        # The stored depth a prior scan verified. Tied to the probe so the
        # fixture keeps representing "already verified at the current size".
        "depth_usd": api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD,
        "depth_unverified": False,
    }
    route.update(overrides)
    return route


class _Book:
    """A live book as api_spreads reads it: bids, asks and a quote timestamp."""

    def __init__(self, bid: float, ask: float) -> None:
        # Sized from the probe rather than a literal: a fixture holding $100 of
        # depth silently stops proving anything the moment the probe is raised
        # past it, and the failure looks like a pricing bug rather than a
        # too-small book.
        size = api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD * 2.0
        self.bids = [[bid, size / bid]]
        self.asks = [[ask, size / ask]]
        self.quote_ts_us = 1_700_000_100_000_000


class _TopOnlyBook(_Book):
    def __init__(self, bid: float, ask: float) -> None:
        super().__init__(bid, ask)
        self.bids = [[bid, 0.0]]
        self.asks = [[ask, 0.0]]


def _book(bid: float, ask: float) -> _Book:
    return _Book(bid, ask)


def test_a_live_short_leg_does_not_mix_with_a_stored_cex_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(live_book_cache, "load_live_books_by_keys", lambda *_args, **_kwargs: {key: _book(1.20, 1.21)})

    priced = api_spreads.live_prices_for([_route()])

    assert priced == {}


def test_a_live_long_leg_does_not_mix_with_a_stored_cex_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = live_book_cache.cache_key("Gate", "Spot", "T/USDT")
    monkeypatch.setattr(live_book_cache, "load_live_books_by_keys", lambda *_args, **_kwargs: {key: _book(0.79, 0.80)})

    priced = api_spreads.live_prices_for([_route()])

    assert priced == {}


def test_a_live_cex_leg_can_reprice_a_dex_route(monkeypatch: pytest.MonkeyPatch) -> None:
    key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(live_book_cache, "load_live_books_by_keys", lambda *_args, **_kwargs: {key: _book(1.20, 1.21)})

    priced = api_spreads.live_prices_for([
        _route(long_venue="Uniswap", long_market_type="DEX")
    ])

    assert priced["T|Gate|Spot|Bybit|Futures"][0] == pytest.approx(20.0)


def test_top_only_cex_fast_leg_keeps_a_verified_dex_depth_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(
        live_book_cache,
        "load_live_books_by_keys",
        lambda *_args, **_kwargs: {key: _TopOnlyBook(1.20, 1.21)},
    )
    route = _route(
        long_venue="Uniswap",
        long_market_type="DEX",
        depth_usd=None,
        matched_size_notional_usd=50.0,
        depth_weighted_spread_pct=9.5,
    )

    priced = api_spreads.live_prices_for([route])

    assert priced[route["route_key"]][0] == pytest.approx(9.5)


def test_a_live_cex_leg_cannot_renew_an_old_dex_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(live_book_cache, "load_live_books_by_keys", lambda *_args, **_kwargs: {key: _book(1.20, 1.21)})

    priced = api_spreads.live_prices_for([
        _route(long_venue="Uniswap", long_market_type="DEX", age_min=10.0)
    ])

    assert priced == {}


def test_both_legs_live_still_uses_both(monkeypatch: pytest.MonkeyPatch) -> None:
    long_key = live_book_cache.cache_key("Gate", "Spot", "T/USDT")
    short_key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(
        live_book_cache,
        "load_live_books_by_keys",
        lambda *_args, **_kwargs: {
            long_key: _book(0.99, 1.00),
            short_key: _book(1.50, 1.51),
        },
    )

    priced = api_spreads.live_prices_for([_route()])

    assert priced["T|Gate|Spot|Bybit|Futures"][0] == pytest.approx(50.0)


def test_live_route_update_uses_the_older_real_book_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_key = live_book_cache.cache_key("Gate", "Spot", "T/USDT")
    short_key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    long_book = _book(0.99, 1.00)
    short_book = _book(1.50, 1.51)
    long_book.quote_ts_us = 1_700_000_200_000_000
    short_book.quote_ts_us = 1_700_000_100_000_000
    monkeypatch.setattr(
        live_book_cache,
        "load_live_books_by_keys",
        lambda *_args, **_kwargs: {long_key: long_book, short_key: short_book},
    )

    update = api_spreads.live_route_updates_for([_route()])[
        "T|Gate|Spot|Bybit|Futures"
    ]

    assert update[0] == pytest.approx(50.0)
    assert update[2] == 1_700_000_100_000_000


def test_no_live_leg_leaves_the_route_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_book_cache, "load_live_books_by_keys", lambda *_args, **_kwargs: {})

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


def test_two_streamed_ladders_upgrade_ticker_depth_to_verified_vwap() -> None:
    from dataclasses import fields

    from spreadboard.api_spreads import SpreadTerminalRow, apply_live_books

    row = SpreadTerminalRow.__new__(SpreadTerminalRow)
    defaults = {field.name: None for field in fields(SpreadTerminalRow)}
    defaults.update(
        long_venue="Gate",
        long_market_type="Spot",
        long_market_symbol="T/USDT",
        short_venue="Bybit",
        short_market_type="Futures",
        short_market_symbol="T/USDT:USDT",
        long_ask=1.0,
        short_bid=1.1,
        long_price=1.0,
        short_price=1.1,
        displayed_open_spread_pct=10.0,
        executable_spread_pct=10.0,
        depth_weighted_spread_pct=10.0,
        quote_ts_us=1_700_000_000_000_000,
        blockers=["depth_unverified", "identity_unverified"],
        live_book=False,
    )
    for key, value in defaults.items():
        object.__setattr__(row, key, value)
    books = {
        live_book_cache.cache_key("Gate", "Spot", "T/USDT"): _book(0.99, 1.00),
        live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT"): _book(1.05, 1.06),
    }

    live = apply_live_books([row], books, now=1_700_000_100.0)[0]

    assert live.live_book is True
    assert live.depth_weighted_spread_pct == pytest.approx(5.0)
    assert live.depth_usd == api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD
    assert live.blockers == ["identity_unverified"]


def test_top_only_bulk_quotes_do_not_claim_matched_depth() -> None:
    from dataclasses import fields

    from spreadboard.api_spreads import SpreadTerminalRow, apply_live_books

    row = SpreadTerminalRow.__new__(SpreadTerminalRow)
    defaults = {field.name: None for field in fields(SpreadTerminalRow)}
    defaults.update(
        long_venue="Gate", long_market_type="Spot", long_market_symbol="T/USDT",
        short_venue="Bybit", short_market_type="Futures", short_market_symbol="T/USDT:USDT",
        long_ask=1.0, short_bid=1.1, long_price=1.0, short_price=1.1,
        displayed_open_spread_pct=10.0, executable_spread_pct=10.0,
        depth_weighted_spread_pct=10.0, depth_usd=50.0,
        quote_ts_us=1_700_000_000_000_000, blockers=[], live_book=False,
    )
    for key, value in defaults.items():
        object.__setattr__(row, key, value)
    books = {
        live_book_cache.cache_key("Gate", "Spot", "T/USDT"): _TopOnlyBook(0.99, 1.00),
        live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT"): _TopOnlyBook(1.05, 1.06),
    }

    live = apply_live_books([row], books, now=1_700_000_100.0)[0]

    assert live.executable_spread_pct == pytest.approx(5.0)
    assert live.depth_weighted_spread_pct is None
    assert live.depth_usd is None
    assert "depth_unverified" in live.blockers


def test_live_funding_recomputes_each_leg_on_its_own_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_book_cache, "load_live_books_by_keys", lambda *_args, **_kwargs: {})
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
