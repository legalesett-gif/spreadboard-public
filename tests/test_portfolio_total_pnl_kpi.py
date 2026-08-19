"""The portfolio must show a total that includes closed positions.

Reported: "the pnl from the closed position is not adding to the total PNL at
the top in the portfolio... We should added a total PNL."

Both numbers already existed in the summary -- `realized_pnl_usd` for closed
positions and `price_and_funding_pnl_usd` for everything -- and neither was
rendered. The KPI row showed only "Open-position PnL", so a member who closed a
position watched its result vanish from the top of the page even though it had
been computed and summed correctly underneath.
"""

from __future__ import annotations

import inspect

from spreadboard import portfolio, server


def _summary():
    return portfolio._portfolio_totals(
        [
            {"status": "closed", "total_pnl_usd": 44.12,
             "funding_known": True, "funding_income_usd": 63.36},
            {"status": "open", "total_pnl_usd": 74.33,
             "funding_known": True, "funding_income_usd": 98.84},
        ],
        None,
    )


def test_the_totals_already_carry_the_closed_result() -> None:
    """The arithmetic was never the problem."""
    summary = _summary()

    assert abs(summary["realized_pnl_usd"] - 44.12) < 1e-9
    assert abs(summary["unrealized_pnl_usd"] - 74.33) < 1e-9
    assert abs(summary["price_and_funding_pnl_usd"] - 118.45) < 1e-9


def test_the_page_renders_a_total_that_includes_closed_positions() -> None:
    source = inspect.getsource(server.render_account_page)

    assert "price_and_funding_pnl_usd" in source, "no total PnL is rendered"
    assert "Total PnL" in source


def test_the_page_renders_the_realised_figure_too() -> None:
    """Open and closed must be separable, or the total is unexplainable."""
    source = inspect.getsource(server.render_account_page)

    assert "realized_pnl_usd" in source


def test_total_leads_the_kpi_row() -> None:
    """It is the number a member opens this page for."""
    source = inspect.getsource(server.render_account_page)
    total = source.index("Total PnL")
    for later in ("Open positions", "Capital committed", "Matched notional"):
        assert total < source.index(later), f"{later} still precedes the total"
