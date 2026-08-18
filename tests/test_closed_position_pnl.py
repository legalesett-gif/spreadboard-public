"""Closing a position must produce a PnL from the fills you entered.

Reported: "after i closed my position in portfolio, it fails to calculate the
pnl and add it to the total pnl. also, does not show the accrued funding".

The cause is one condition:

    total_pnl = (price_pnl + funding_usd - total_costs
                 if price_pnl is not None and funding_usd is not None
                 and settled_funding.get("known") else None)

Exact settled funding is imported from a connected exchange. A member who
journals a position by hand and closes it by hand has no such import, so
`known` is False and the WHOLE result was discarded -- including the price PnL,
which is fully determined by the entry and exit fills they just typed in.

Funding may legitimately be unknown. The price result never is. So the total is
computed from what is known, and says so when funding is not part of it.
"""

from __future__ import annotations

from spreadboard import portfolio, portfolio_funding


def _closed(**kw):
    base = {
        "id": 1, "token": "ESPORTS", "status": "closed",
        "long_venue": "Mexc", "short_venue": "Gate",
        "long_market_type": "Spot", "short_market_type": "Futures",
        "long_quantity": 100.0, "long_entry_price": 2.00, "long_exit_price": 2.20,
        "short_quantity": 100.0, "short_entry_price": 2.05, "short_exit_price": 2.10,
        "entry_fees_usd": 1.00, "exit_fees_usd": 3.20,
        "capital_usd": 200.0, "opened_at": "2026-08-01T00:00:00Z",
    }
    base.update(kw)
    return base


def _hydrate(position, monkeypatch, *, funding_known=False, funding_usd=None):
    monkeypatch.setattr(
        portfolio_funding, "exact_funding",
        lambda *_a, **_k: {"known": funding_known, "amount_usd": funding_usd},
    )
    return portfolio._hydrate_position(
        position, [], books={}, funding_legs={}, catalogue={},
        market_index={}, funding_snapshot={},
    )


def test_a_closed_position_reports_its_price_pnl_without_imported_funding(monkeypatch):
    """long +20.00, short -5.00, fees -4.20 => +10.80."""
    row = _hydrate(_closed(), monkeypatch)

    assert abs(row["price_pnl_usd"] - 15.0) < 1e-9
    assert row["total_pnl_usd"] is not None
    assert abs(row["total_pnl_usd"] - 10.80) < 1e-9


def test_it_says_the_total_leaves_funding_out(monkeypatch):
    """Showing a number without saying what it excludes would be the worse bug."""
    row = _hydrate(_closed(), monkeypatch)

    assert row["funding_known"] is False
    assert row["total_pnl_excludes_funding"] is True


def test_imported_funding_is_included_and_flagged_as_complete(monkeypatch):
    row = _hydrate(_closed(), monkeypatch, funding_known=True, funding_usd=6.20)

    assert abs(row["total_pnl_usd"] - (15.0 + 6.20 - 4.20)) < 1e-9
    assert row["total_pnl_excludes_funding"] is False


def test_a_closed_position_counts_toward_the_portfolio_total(monkeypatch):
    """It was excluded from the total precisely because its PnL was None."""
    row = _hydrate(_closed(), monkeypatch)
    totals = portfolio._portfolio_totals([row], None)

    # A closed position is realised, and it was being dropped from this sum
    # entirely because its total_pnl_usd was None.
    assert totals["realized_pnl_usd"] is not None
    assert abs(totals["realized_pnl_usd"] - 10.80) < 1e-9
    assert abs(totals["price_and_funding_pnl_usd"] - 10.80) < 1e-9


def test_a_position_with_no_exit_fills_still_reports_nothing(monkeypatch):
    """No invention: without an exit there is no realised price result."""
    row = _hydrate(_closed(long_exit_price=None, short_exit_price=None), monkeypatch)

    assert row["price_pnl_usd"] is None
    assert row["total_pnl_usd"] is None


def test_the_card_says_when_a_total_is_price_only() -> None:
    """Otherwise the number reads as the complete result."""
    import inspect

    from spreadboard import server

    source = inspect.getsource(server.render_position_card)
    assert "total_pnl_excludes_funding" in source
    assert "funding not imported" in source


def test_the_card_does_not_print_a_funding_figure_it_does_not_have() -> None:
    from spreadboard import server

    html = server.render_position_card({
        "id": 1, "token": "ESPORTS", "status": "closed",
        "long_venue": "Mexc", "short_venue": "Gate",
        "long_market_type": "Spot", "short_market_type": "Futures",
        "total_pnl_usd": 10.80, "funding_known": False,
        "funding_income_usd": None, "total_pnl_excludes_funding": True,
        "opened_at": "2026-08-01T00:00:00Z",
    })

    assert "not imported" in html
