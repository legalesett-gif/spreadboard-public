"""A pair's spread is set by what the assets convert at, not by how much is held.

`paired_spread_pct` took the position's own quantities as its statement of the
ratio. That is true only while the position is balanced. The owner scales in
DELIBERATELY dollar-matched -- equal USD on each leg, not equal units, because
he wants a net long tilt into upward pressure -- and every such tranche moves
the hedge ratio away from the conversion ratio. The displayed spread then gets
silently scaled by (hedge ratio / conversion ratio):

  OPENAI  1.424 / 1.478 = 0.96346  ->  6.7176% shown as 2.8186%
  SKHX    26.90 / 2.885 = 9.3241   -> 32.0974% shown as 23.1688%  (ratio 10)

Both readings were wrong in the same direction, and because entry and mark are
scaled by the same factor the error hides: the numbers stay self-consistent and
only the operator, who watched the real spread, could see it. He did, twice.

The conversion ratio is a property of the two ASSETS -- 1 for the same token on
two venues, 10 for SKHX against its SKHY ADR -- so it is stated on the position
and defaults to 1. Every live position is 1:1 except that ADR pair.
"""

from __future__ import annotations

import pytest

from spreadboard import portfolio

# The owner's real rows, as stored.
OPENAI = {
    "long_quantity": 1.478,
    "long_entry_price": 1442.2675236806494,
    "short_quantity": 1.424,
    "short_entry_price": 1539.153441011236,
}
SKHX = {
    "long_quantity": 2.885,
    "long_entry_price": 1242.6355632582324,
    "short_quantity": 26.90,
    "short_entry_price": 164.14886988847584,
    "conversion_ratio": 10.0,
}


def test_a_dollar_matched_tilt_does_not_move_the_openai_spread() -> None:
    assert portfolio.entry_spread_pct(OPENAI) == pytest.approx(6.7176, abs=1e-3)


def test_an_adr_pair_is_measured_at_its_conversion_ratio() -> None:
    assert portfolio.entry_spread_pct(SKHX) == pytest.approx(32.0974, abs=1e-3)


def test_the_conversion_ratio_defaults_to_one() -> None:
    """Every live position but the ADR pair is the same asset on two venues."""

    row = {"long_quantity": 5.0, "long_entry_price": 100.0,
           "short_quantity": 5.0, "short_entry_price": 110.0}
    assert portfolio.entry_spread_pct(row) == pytest.approx(10.0)


def test_holding_more_of_one_leg_is_not_a_narrower_spread() -> None:
    """The property that was violated: prices set the spread, size does not."""

    balanced = {"long_quantity": 10.0, "long_entry_price": 100.0,
                "short_quantity": 10.0, "short_entry_price": 110.0}
    tilted = dict(balanced, long_quantity=12.0)

    assert portfolio.entry_spread_pct(tilted) == portfolio.entry_spread_pct(balanced)


def test_entry_and_mark_are_measured_the_same_way() -> None:
    """A spread that widened must never read as narrowing (the OPENAI report)."""

    entry = portfolio.entry_spread_pct(OPENAI)
    mark = portfolio.paired_spread_pct(OPENAI, long_price=1500.0, short_price=1650.0)

    assert mark > entry, "10.0% mark against a 6.72% entry is a widening"


def test_a_stated_ratio_survives_a_missing_quantity() -> None:
    row = {"long_entry_price": 1242.64, "short_entry_price": 164.15, "conversion_ratio": 10.0}
    assert portfolio.entry_spread_pct(row) == pytest.approx(32.10, abs=1e-2)


def test_a_nonsense_ratio_falls_back_to_one() -> None:
    row = {"long_quantity": 1.0, "long_entry_price": 100.0, "short_quantity": 1.0,
           "short_entry_price": 110.0, "conversion_ratio": 0}
    assert portfolio.entry_spread_pct(row) == pytest.approx(10.0)
