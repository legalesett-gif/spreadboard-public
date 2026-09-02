"""The headline return divided by half the money that was actually at risk.

`capital_metrics` already gets this right per position: both legs are funded,
so both legs are capital, and the form's `capital_usd` column is labelled "per
leg". The per-row `return_pct` was fixed to use the committed figure. The
SUMMARY at the top of the page was not -- it still summed the raw per-leg
column -- so the headline read roughly twice every row beneath it.
"""

from __future__ import annotations

from spreadboard import portfolio


def _position(**kw):
    base = {
        "status": "open",
        "long_venue": "Gate",
        "short_venue": "Aster",
        "long_quantity": 100.0,
        "long_entry_price": 10.0,
        "short_quantity": 100.0,
        "short_entry_price": 10.0,
        "capital_usd": 1000.0,
        "total_pnl_usd": 100.0,
    }
    base.update(kw)
    return base


def test_the_headline_uses_both_funded_legs() -> None:
    """$1,000 per leg is $2,000 at work, so $100 is 5%, not 10%."""

    totals = portfolio._portfolio_totals([_position()], None)

    assert totals["monthly_capital_usd"] == 2000.0, (
        f"capital basis is {totals['monthly_capital_usd']}; the per-leg column "
        "was summed as though it were the position total"
    )
    assert totals["monthly_return_pct"] == 5.0


def test_the_headline_agrees_with_the_rows_beneath_it() -> None:
    position = _position()
    row_pct = portfolio.capital_metrics(position)["return_on_capital_pct"]

    totals = portfolio._portfolio_totals([position], None)

    assert totals["monthly_return_pct"] == row_pct, (
        "the summary and the single row it summarises disagree"
    )


def test_a_configured_monthly_capital_still_wins() -> None:
    """An operator-set figure is a deliberate statement about the book."""

    totals = portfolio._portfolio_totals([_position()], 5000.0)

    assert totals["monthly_capital_usd"] == 5000.0
    assert totals["capital_basis"] == "configured_monthly"
    assert totals["monthly_return_pct"] == 2.0


def test_positions_without_a_capital_figure_do_not_zero_the_basis() -> None:
    totals = portfolio._portfolio_totals(
        [_position(), _position(capital_usd=None, total_pnl_usd=50.0)], None
    )

    assert totals["monthly_capital_usd"] and totals["monthly_capital_usd"] > 0
