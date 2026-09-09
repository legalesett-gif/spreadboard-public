"""A blended entry spread must lie between the tranches it blends.

Deriving the entry spread from the position's blended legs -- the ratio of the
two average prices -- is not an average of the tranche spreads when the tranches
were entered at different unit ratios. It can land outside their range entirely:

  SKHX  tranche 1  2.186 @ 1182.6257 vs 21.86 @ 156.3022  ->  32.1654%
        tranche 2  0.699 @ 1430.3000 vs  5.04 @ 198.1800  ->  38.5583%
        ratio of averages                                  ->  32.0972%   (!)

32.0972% is below BOTH, which no weighted average can be. The dollar-matched
tranche gave the long leg 24.2% of its units at the higher price but the short
leg only 18.7%, so the two averages moved by different factors and their ratio
fell. The operator added at 38.56% and saw his entry spread go DOWN. He said so.

The blend that means something is the one convergence actually realises: each
tranche's spread weighted by the size that is HEDGED in it (the rest is a
deliberate directional tilt, not spread exposure). For SKHX that is 33.3632%,
and it rises when you add above your average, as it must.

A blended row cannot recover its own tranche history, so the maintained value on
the position is the truth and deriving from legs is only the fallback for a row
that never had one.
"""

from __future__ import annotations

import pytest

from spreadboard import portfolio

SKHX = {
    "long_quantity": 2.885,
    "long_entry_price": 1242.6355632582324,
    "short_quantity": 26.90,
    "short_entry_price": 164.14886988847584,
    "conversion_ratio": 10.0,
    "entry_spread_pct": 33.3632,  # paired-size-weighted, maintained at scale-in
}


def test_the_maintained_blend_is_what_the_row_reports() -> None:
    assert portfolio.entry_spread_pct(SKHX) == pytest.approx(33.3632, abs=1e-3)


def test_the_blend_is_not_below_both_tranches() -> None:
    """The defect, stated as the property it violated."""

    assert 32.1654 <= portfolio.entry_spread_pct(SKHX) <= 38.5583


def test_a_row_without_a_maintained_blend_still_derives_one() -> None:
    """Single-tranche and hand-journaled rows keep working."""

    row = {"long_quantity": 2.0, "long_entry_price": 100.0,
           "short_quantity": 2.0, "short_entry_price": 110.0}

    assert portfolio.entry_spread_pct(row) == pytest.approx(10.0)


def test_deriving_still_respects_the_conversion_ratio() -> None:
    row = {"long_quantity": 2.186, "long_entry_price": 1182.6257,
           "short_quantity": 21.86, "short_entry_price": 156.3022,
           "conversion_ratio": 10.0}

    assert portfolio.entry_spread_pct(row) == pytest.approx(32.1654, abs=1e-3)
