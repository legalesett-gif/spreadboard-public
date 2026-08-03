"""A spread must come from prices someone could trade at.

A ticker-only quote fills bid and ask with the last traded price. Upbit's BIO
last traded at 0.01825 while its book was bid 0.02069 / ask 0.0588, so the row
printed +45.97% for a trade that is actually about -55%. 328 of the 379 routes
printing more than 20% were priced this way.
"""

from __future__ import annotations

from spreadboard.api_spreads import SpreadTerminalRow, spread_is_ticker_derived


def _row(**overrides) -> SpreadTerminalRow:
    row = SpreadTerminalRow.__new__(SpreadTerminalRow)
    defaults = {
        "long_bid": 1.0,
        "long_ask": 1.01,
        "short_bid": 1.10,
        "short_ask": 1.11,
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        object.__setattr__(row, key, value)
    return row


def test_a_real_book_is_kept() -> None:
    assert spread_is_ticker_derived(_row()) is False


def test_a_last_trade_masquerading_as_a_book_is_caught() -> None:
    """Upbit BIO: bid, ask and price all 0.01825."""
    assert spread_is_ticker_derived(_row(long_bid=0.01825, long_ask=0.01825)) is True


def test_either_leg_is_enough_to_disqualify_the_spread() -> None:
    assert spread_is_ticker_derived(_row(short_bid=2.0, short_ask=2.0)) is True


def test_a_missing_quote_is_not_treated_as_ticker_derived() -> None:
    """Absent prices are handled by the deliverability checks, not here."""
    assert spread_is_ticker_derived(_row(long_bid=None, long_ask=None)) is False
