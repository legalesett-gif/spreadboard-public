"""Only linear perpetuals belong on the board.

Kraken lists XRP as PF_XRPUSD (linear perp), PI_XRPUSD (inverse perp) and
FI_XRPUSD_260828 (a dated future). Accepting all three put the inverse contract
on the funding board at -0.4409%/h against a real -0.00073%/h, headlining XRP
at +10.61%/day (3,869% APR). Inverse contracts quote funding against the base
currency, so linear arithmetic is the wrong formula for them; dated futures
have no funding at all.
"""

from __future__ import annotations

from src.spreadarb.api_discovery.sources import _market_matches_type


def _market(**overrides):
    base = {"swap": True, "linear": True, "inverse": False, "spot": False}
    base.update(overrides)
    return base


def test_linear_perpetual_is_accepted() -> None:
    assert _market_matches_type(_market(), "XRP/USD:USD", "Futures") is True


def test_inverse_perpetual_is_rejected() -> None:
    market = _market(linear=False, inverse=True)
    assert _market_matches_type(market, "XRP/USD:XRP", "Futures") is False


def test_dated_future_is_rejected() -> None:
    dated = _market(swap=False, future=True, expiry=1788000000000)
    assert _market_matches_type(dated, "XRP/USD:XRP-260828", "Futures") is False


def test_dated_future_rejected_even_when_the_venue_marks_it_a_swap() -> None:
    # Some venues leave `swap` set on quarterly contracts but always publish an
    # expiry; the expiry is the reliable signal.
    assert _market_matches_type(_market(expiry=1788000000000), "BTC/USD:USD-261225", "Futures") is False


def test_spot_side_is_unaffected() -> None:
    assert _market_matches_type({"spot": True}, "XRP/USDT", "Spot") is True
    assert _market_matches_type(_market(), "XRP/USD:USD", "Spot") is False


def _row(**overrides):
    from spreadboard.api_spreads import SpreadTerminalRow

    row = SpreadTerminalRow.__new__(SpreadTerminalRow)
    defaults = {
        "long_market_type": "Futures",
        "short_market_type": "Futures",
        "long_market_symbol": "XRP/USDT:USDT",
        "short_market_symbol": "XRP/USDT:USDT",
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        object.__setattr__(row, key, value)
    return row


def test_serving_guard_keeps_linear_perpetuals() -> None:
    from spreadboard.api_spreads import is_non_perpetual_or_inverse

    assert is_non_perpetual_or_inverse(_row()) is False
    assert is_non_perpetual_or_inverse(_row(long_market_symbol="XRP/USD:USD")) is False
    assert is_non_perpetual_or_inverse(_row(long_market_symbol="XRP/USDC:USDC")) is False


def test_serving_guard_rejects_the_kraken_inverse_contract() -> None:
    """The exact route that headlined XRP at +10.61%/day."""
    from spreadboard.api_spreads import is_non_perpetual_or_inverse

    assert is_non_perpetual_or_inverse(_row(long_market_symbol="XRP/USD:XRP")) is True


def test_serving_guard_rejects_dated_futures() -> None:
    from spreadboard.api_spreads import is_non_perpetual_or_inverse

    assert is_non_perpetual_or_inverse(_row(long_market_symbol="XRP/USD:XRP-260828")) is True
    assert is_non_perpetual_or_inverse(_row(short_market_symbol="BTC/USDT:USDT-261225")) is True


def test_serving_guard_ignores_spot_legs() -> None:
    """A spot symbol has no settle asset and must not trip the inverse check."""
    from spreadboard.api_spreads import is_non_perpetual_or_inverse

    spot = _row(long_market_type="Spot", long_market_symbol="XRP/USDT")
    assert is_non_perpetual_or_inverse(spot) is False
