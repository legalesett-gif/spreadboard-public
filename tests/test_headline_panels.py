"""The headline panels must answer the same question as the lane beside them.

Top Arbitrage Edges ranked off a universe that applied only deliverability and
price sanity, so SHIB3S -- a 3x leveraged token the board itself refuses to
list -- headlined the page at +177%.
"""

from __future__ import annotations

from spreadboard.api_spreads import SpreadTerminalRow, row_is_presentable


def _row(**overrides) -> SpreadTerminalRow:
    row = SpreadTerminalRow.__new__(SpreadTerminalRow)
    defaults = {
        "token": "BTC",
        "long_venue": "Gate",
        "short_venue": "XT",
        "long_market_type": "Spot",
        "short_market_type": "Spot",
        "long_market_symbol": "BTC/USDT",
        "short_market_symbol": "BTC/USDT",
        "long_bid": 1.0,
        "long_ask": 1.01,
        "short_bid": 1.10,
        "short_ask": 1.11,
        "displayed_open_spread_pct": 8.9,
        "executable_spread_pct": 8.9,
        "depth_weighted_spread_pct": 8.9,
        "long_price": 1.0,
        "short_price": 1.10,
        "long_volume_24h_usd": 5_000_000.0,
        "short_volume_24h_usd": 5_000_000.0,
        "blockers": [],
        "route_kind": "SPOT",
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        object.__setattr__(row, key, value)
    return row


def test_a_normal_row_is_presentable() -> None:
    assert row_is_presentable(_row()) is True


def test_unmeasured_depth_never_headlines_a_matched_size_ranking() -> None:
    assert row_is_presentable(_row(blockers=["depth_unverified"])) is False


def test_a_leveraged_token_never_headlines() -> None:
    """SHIB3S led Top Arbitrage Edges at +177% while the lane rejected it."""
    assert row_is_presentable(_row(token="SHIB3S")) is False


def test_a_ticker_priced_mirage_never_headlines() -> None:
    mirage = _row(long_bid=0.01825, long_ask=0.01825, short_bid=0.02664, short_ask=0.02664)
    assert row_is_presentable(mirage) is False


def test_an_inverse_contract_never_headlines() -> None:
    inverse = _row(
        long_market_type="Futures",
        short_market_type="Futures",
        long_market_symbol="XRP/USD:XRP",
        short_market_symbol="XRP/USDT:USDT",
    )
    assert row_is_presentable(inverse) is False
