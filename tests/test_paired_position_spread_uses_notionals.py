"""A paired position's spread is the ratio of its leg VALUES, not its prices.

The owner's SKHX position is 10:1 by construction -- long 2.186 units at
$1,182.625 against short 21.86 at $156.3022 -- because one leg is an ADR of the
other. Comparing the raw prices gives -86.78%. Comparing what each leg is
actually worth gives +32.17%, which is the real basis and what the closed twin
of this position had stored.

Editing the position recomputed the stored value and replaced the correct 32.17
with -86.78. For equal quantities the two formulas agree exactly, which is why
this stayed hidden until a ratio position was edited.
"""

from __future__ import annotations

import pytest

from spreadboard import accounts, portfolio

SKHX = {
    "token": "SKHX",
    "long_venue": "Hyperliquid",
    "long_market_type": "Futures",
    "short_venue": "Hyperliquid",
    "short_market_type": "Futures",
    "long_quantity": 2.186,
    "long_entry_price": 1182.625,
    "short_quantity": 21.86,
    "short_entry_price": 156.3022,
}


def test_entry_spread_is_the_ratio_of_leg_values() -> None:
    values = accounts._position_values(dict(SKHX))

    assert values["entry_spread_pct"] == pytest.approx(32.165, abs=0.01), (
        f"got {values['entry_spread_pct']}; comparing raw prices on a 10:1 "
        "position reports -86.78 and calls a 32% basis a 87% loss"
    )


def test_equal_quantities_are_unchanged() -> None:
    """The old formula was right for 1:1, and must stay right."""

    values = accounts._position_values(
        dict(SKHX, long_quantity=10.0, long_entry_price=100.0,
             short_quantity=10.0, short_entry_price=102.0)
    )

    assert values["entry_spread_pct"] == pytest.approx(2.0, abs=1e-9)


def test_the_marked_spread_uses_the_same_basis() -> None:
    """Entry and marked spread must be measured the same way, or the position
    appears to have moved when only the formula changed."""

    position = dict(SKHX, long_mark_price=1212.4, short_mark_price=164.27)
    marked = portfolio.paired_spread_pct(
        position, long_price=1212.4, short_price=164.27
    )

    # 21.86*164.27 / (2.186*1212.4) - 1
    assert marked == pytest.approx(35.492, abs=0.01)


def test_a_missing_quantity_falls_back_to_prices_rather_than_failing() -> None:
    marked = portfolio.paired_spread_pct(
        {"long_quantity": None, "short_quantity": None},
        long_price=100.0,
        short_price=102.0,
    )

    assert marked == pytest.approx(2.0, abs=1e-9)


def test_an_unknown_price_reports_nothing() -> None:
    assert portfolio.paired_spread_pct(SKHX, long_price=None, short_price=1.0) is None
    assert portfolio.paired_spread_pct(SKHX, long_price=1.0, short_price=None) is None
