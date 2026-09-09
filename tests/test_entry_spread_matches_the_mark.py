"""Entry and marked spread must be measured the same way, or neither means anything.

The owner's OPENAI position is 1.478 Mexc long at 1442.2675 against 1.424
Hyperliquid `io:OAI` short at 1539.1534 -- quantities deliberately unequal
because the scale-in targeted equal DOLLAR notionals.

The page showed entry 6.7176% against a marked 5.9355% -- a widening that read
as a 0.78pp NARROWING beside a $79.18 loss, because entry was stored per-unit
and the mark was computed on notionals.

Aligning entry to the notional mark fixed the comparison and broke the number:
notionals divide by the ratio HELD, so a deliberately dollar-matched position
(1.424/1.478 = 0.96346) showed 2.8186% for a 6.7176% spread. The operator, who
had entered near 5.3% and watched it since, said plainly that 2.8% was wrong.

Both legs are the same asset, one unit for one unit, so the conversion ratio is
1 and the per-unit figure was right all along. Entry and mark now share that
basis instead of the ratio held. See
tests/test_spread_basis_is_the_conversion_ratio.py.
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
    assert abs(derived - 6.7176) < 0.01, derived


def test_a_deliberate_dollar_matched_tilt_does_not_move_the_entry() -> None:
    """Scaling in at equal USD must not restate the spread already entered."""

    tilted = dict(OPENAI, long_quantity=1.9)

    assert portfolio.entry_spread_pct(tilted) == portfolio.entry_spread_pct(OPENAI)


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

    assert abs(row["entry_spread_pct"] - 6.7176) < 0.01, row["entry_spread_pct"]
