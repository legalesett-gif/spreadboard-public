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
        # _entrance_spread reads the displayed edge; derive it from the legs so
        # the fixture stays self-consistent.
        "displayed_open_spread_pct": None,
        "executable_spread_pct": None,
        "depth_weighted_spread_pct": None,
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        object.__setattr__(row, key, value)
    if row.displayed_open_spread_pct is None and row.long_ask:
        object.__setattr__(
            row, "displayed_open_spread_pct", (row.short_bid / row.long_ask - 1.0) * 100.0
        )
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


def test_a_small_ticker_priced_edge_is_believable() -> None:
    """Binance's ASR is quoted this way and is the reference top row at 0.21%.

    Rejecting every ticker-priced row dropped exactly the tight liquid band the
    reference Spot-Spot lane is made of.
    """
    from spreadboard.api_spreads import spread_is_untrustworthy

    asr = _row(long_bid=0.856, long_ask=0.856, short_bid=0.857, short_ask=0.857)
    assert spread_is_untrustworthy(asr) is False


def test_a_large_ticker_priced_edge_is_not() -> None:
    """Upbit BIO printed +45.97% off a last trade; the real trade is about -55%."""
    from spreadboard.api_spreads import spread_is_untrustworthy

    bio = _row(long_bid=0.01825, long_ask=0.01825, short_bid=0.02664, short_ask=0.02664)
    assert spread_is_untrustworthy(bio) is True


def test_a_large_edge_backed_by_a_real_book_is_kept() -> None:
    """ESPORTS at 150% and VANRY at 100% have genuine two-sided books."""
    from spreadboard.api_spreads import spread_is_untrustworthy

    real = _row(long_bid=0.0170, long_ask=0.0171, short_bid=0.0428, short_ask=0.0430)
    assert spread_is_untrustworthy(real) is False


def test_a_spot_route_into_a_shut_deposit_is_not_a_spread() -> None:
    """ESPORTS printed 150% into a Mexc deposit that was closed.

    A spot arb needs the coin delivered, so a shut rail is why the gap exists
    rather than an opportunity. Every spot row the reference product lists
    shows both rails open.
    """
    from spreadboard.api_spreads import route_deliverable

    row = _row()
    object.__setattr__(row, "route_kind", "SPOT")
    object.__setattr__(row, "long_venue", "Kraken")
    object.__setattr__(row, "short_venue", "Mexc")
    object.__setattr__(row, "long_withdraw_enabled", True)
    object.__setattr__(row, "short_deposit_enabled", False)

    assert route_deliverable(row) is False


def test_an_unknown_rail_is_not_treated_as_shut() -> None:
    from spreadboard.api_spreads import route_deliverable

    row = _row()
    object.__setattr__(row, "route_kind", "SPOT")
    object.__setattr__(row, "long_venue", "Kraken")
    object.__setattr__(row, "short_venue", "Mexc")
    object.__setattr__(row, "long_withdraw_enabled", True)
    object.__setattr__(row, "short_deposit_enabled", None)

    assert route_deliverable(row) is None
