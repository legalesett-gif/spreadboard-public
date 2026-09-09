"""Entry and marked spread must be measured the same way, or neither means anything.

The owner's OPENAI position is 1.478 Mexc long at 1442.2675 against 1.424
Hyperliquid `io:OAI` short at 1539.1534 -- quantities deliberately unequal
because the scale-in targeted equal DOLLAR notionals.

`paired_spread_pct` marks it on notionals (2026-08-31, the SKHX fix), but the
stored `entry_spread_pct` of 6.7176% is the PER-UNIT figure, written straight to
the row by an accounting reconciliation that bypassed the save path's formula.
The page then showed entry 6.7176% against marked 5.9355% -- which reads as the
spread NARROWING by 0.78pp while the position lost $79.18.

It had in fact widened, on both bases: per-unit 6.72% -> 9.95%, notional
2.82% -> 5.94%. The loss was right and the comparison was not.

Deriving entry from the position's own legs makes the two always comparable and
self-corrects every historical row, whatever basis it was written on.
"""

from __future__ import annotations

from spreadboard import portfolio

OPENAI = {
    "long_quantity": 1.478,
    "long_entry_price": 1442.2675236806494,
    "short_quantity": 1.424,
    "short_entry_price": 1539.153441011236,
    "entry_spread_pct": 6.717610688711531,  # per-unit, from the reconciliation
}


def test_entry_spread_is_derived_on_the_same_basis_as_the_mark() -> None:
    derived = portfolio.entry_spread_pct(OPENAI)
    marked = portfolio.paired_spread_pct(
        OPENAI,
        long_price=OPENAI["long_entry_price"],
        short_price=OPENAI["short_entry_price"],
    )

    assert derived == marked
    assert abs(derived - 2.818) < 0.01, derived


def test_the_stored_per_unit_figure_is_not_used_when_legs_are_present() -> None:
    """6.7176% against a notional mark is what made a widening look like a gain."""

    assert abs(portfolio.entry_spread_pct(OPENAI) - 6.7176) > 1.0


def test_equal_quantities_are_unchanged() -> None:
    """The two bases agree whenever the legs are 1:1, which is most positions."""

    equal = {
        "long_quantity": 2.0,
        "long_entry_price": 100.0,
        "short_quantity": 2.0,
        "short_entry_price": 105.0,
        "entry_spread_pct": 5.0,
    }

    assert abs(portfolio.entry_spread_pct(equal) - 5.0) < 1e-9


def test_a_hand_journaled_row_keeps_its_stored_value() -> None:
    """Without quantities there is nothing to derive from; do not invent one."""

    journaled = {"entry_spread_pct": 4.25}

    assert portfolio.entry_spread_pct(journaled) == 4.25


def test_a_row_with_neither_reports_nothing() -> None:
    assert portfolio.entry_spread_pct({}) is None


def test_the_row_reports_the_derived_entry_spread(monkeypatch) -> None:
    """A helper the row never calls fixes nothing -- this is the actual defect.

    The page reads `entry_spread_pct` off the row and puts it beside the marked
    spread, so the row is where the two bases have to agree.
    """

    from spreadboard import portfolio_funding

    monkeypatch.setattr(
        portfolio_funding,
        "exact_funding",
        lambda *_a, **_k: {"known": False, "amount_usd": None},
    )
    position = {
        "id": 47,
        "token": "OPENAI",
        "status": "open",
        "long_venue": "Mexc",
        "long_market_type": "Futures",
        "short_venue": "Hyperliquid",
        "short_market_type": "Futures",
        "opened_at": "2026-09-04T01:21:07Z",
        **OPENAI,
    }

    row = portfolio._hydrate_position(
        position, [], books={}, funding_legs={}, catalogue={},
        market_index={}, funding_snapshot={},
    )

    assert abs(row["entry_spread_pct"] - 2.818) < 0.01, row["entry_spread_pct"]
