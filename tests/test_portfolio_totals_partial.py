"""A total must sum what is known and say what it left out.

Live portfolio, four positions: ESPORTS +80.41 and a closed BTW +44.12 are both
fully marked; GUA and a second BTW each have one leg the market cannot price
right now. Every aggregate was written all-or-nothing --

    total = sum(known) if len(known) == len(positions) else None

-- so two perfectly good results were discarded and the member saw "Total PnL —"
sitting directly beside "Realised PnL +44.12 USD". That is the reported bug:
"the pnl from the closed position is not adding to the total PNL at the top".

One unpriceable leg must not erase the rest of the book. The totals now sum what
is known and carry a count of what is excluded, so the figure is never silently
partial.
"""

from __future__ import annotations

from spreadboard import portfolio


def _p(status, pnl, funding=None, known=True):
    return {"status": status, "total_pnl_usd": pnl,
            "funding_income_usd": funding, "funding_known": known}


def _live_shape():
    """The four positions actually in the account."""
    return [
        _p("open", 80.41, 98.84),
        _p("open", None, None, known=False),      # GUA, long leg unpriced
        _p("closed", 44.12, 63.36),
        _p("open", None, None, known=False),      # BTW open, short leg unpriced
    ]


def test_the_total_sums_the_positions_it_can_price() -> None:
    totals = portfolio._portfolio_totals(_live_shape(), None)

    assert totals["price_and_funding_pnl_usd"] is not None
    assert abs(totals["price_and_funding_pnl_usd"] - 124.53) < 1e-9


def test_the_realised_figure_is_unaffected() -> None:
    totals = portfolio._portfolio_totals(_live_shape(), None)

    assert abs(totals["realized_pnl_usd"] - 44.12) < 1e-9


def test_the_open_total_sums_the_open_positions_it_can_price() -> None:
    totals = portfolio._portfolio_totals(_live_shape(), None)

    assert abs(totals["unrealized_pnl_usd"] - 80.41) < 1e-9


def test_the_summary_says_how_many_it_could_not_price() -> None:
    """A partial total that does not admit it is partial is the worse bug."""
    totals = portfolio._portfolio_totals(_live_shape(), None)

    assert totals["unpriced_positions"] == 2


def test_nothing_priceable_still_reports_nothing() -> None:
    """No invention: with no usable input there is no total."""
    totals = portfolio._portfolio_totals(
        [_p("open", None, None, known=False)], None
    )

    assert totals["price_and_funding_pnl_usd"] is None
    assert totals["unpriced_positions"] == 1


def test_a_fully_priced_book_reports_no_exclusions() -> None:
    totals = portfolio._portfolio_totals(
        [_p("open", 10.0, 1.0), _p("closed", 5.0, 2.0)], None
    )

    assert abs(totals["price_and_funding_pnl_usd"] - 15.0) < 1e-9
    assert totals["unpriced_positions"] == 0


def test_the_kpi_discloses_that_a_total_is_partial() -> None:
    """The caption must change when positions are left out."""
    import inspect

    from spreadboard import server

    source = inspect.getsource(server.render_account_page)
    assert "unpriced_positions" in source
    assert "not priced yet" in source
