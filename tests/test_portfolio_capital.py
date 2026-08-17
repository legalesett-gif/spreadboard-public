"""Capital deployed per position, beside the notional it controls.

Return on capital is the number that decides whether a farm was worth holding,
and it was only computable per position. The summary divided total PnL by a
configured monthly figure, which answers a different question: "how did the
month go" rather than "what is the money currently at work actually earning".

Notional and capital are deliberately separate. A delta-neutral pair at 2x
controls twice the notional its capital would suggest, and conflating the two
overstates efficiency exactly where it matters most.
"""

from __future__ import annotations

from spreadboard import portfolio


def _position(**overrides):
    base = {
        "status": "open",
        "long_quantity": 100.0, "long_entry_price": 2.0,
        "short_quantity": 100.0, "short_entry_price": 2.0,
        "capital_usd": 200.0,
        "total_pnl_usd": 20.0,
    }
    base.update(overrides)
    return base


def test_a_position_reports_the_notional_each_leg_controls() -> None:
    row = portfolio.capital_metrics(_position())

    assert row["long_notional_usd"] == 200.0
    assert row["short_notional_usd"] == 200.0


def test_the_position_notional_is_the_larger_leg_not_their_sum() -> None:
    """A hedged pair controls one position, not two. Summing double-counts."""
    row = portfolio.capital_metrics(_position(short_quantity=50.0))

    assert row["notional_usd"] == 200.0


def test_capital_efficiency_shows_how_hard_the_money_works() -> None:
    """$200 controlling $200 is 1x; the same capital at 2x controls $400."""
    row = portfolio.capital_metrics(_position(capital_usd=100.0))

    assert row["capital_efficiency_x"] == 2.0


def test_return_on_capital_is_measured_against_capital_not_notional() -> None:
    row = portfolio.capital_metrics(_position())

    assert row["return_on_capital_pct"] == 10.0


def test_a_position_without_capital_reports_no_return_rather_than_zero() -> None:
    """Unknown capital and a zero return are opposite conclusions."""
    row = portfolio.capital_metrics(_position(capital_usd=None))

    assert row["return_on_capital_pct"] is None
    assert row["capital_efficiency_x"] is None


def test_zero_capital_never_divides() -> None:
    row = portfolio.capital_metrics(_position(capital_usd=0.0))

    assert row["return_on_capital_pct"] is None


# --------------------------------------------------------------------------
# Across everything held
# --------------------------------------------------------------------------


def test_deployed_capital_counts_only_open_positions() -> None:
    """Closed positions returned their capital; it is no longer at work."""
    totals = portfolio.deployed_capital_summary([
        _position(capital_usd=200.0),
        _position(status="closed", capital_usd=500.0, total_pnl_usd=50.0),
    ])

    assert totals["deployed_capital_usd"] == 200.0


def test_return_on_deployed_capital_spans_every_open_position() -> None:
    totals = portfolio.deployed_capital_summary([
        _position(capital_usd=100.0, total_pnl_usd=10.0),
        _position(capital_usd=300.0, total_pnl_usd=20.0),
    ])

    assert totals["deployed_capital_usd"] == 400.0
    assert totals["open_return_on_capital_pct"] == 7.5


def test_deployed_notional_is_reported_beside_the_capital() -> None:
    totals = portfolio.deployed_capital_summary([_position(capital_usd=100.0)])

    assert totals["deployed_notional_usd"] == 200.0
    assert totals["portfolio_efficiency_x"] == 2.0


def test_nothing_open_reports_no_return_rather_than_zero() -> None:
    totals = portfolio.deployed_capital_summary([
        _position(status="closed", total_pnl_usd=5.0)
    ])

    assert totals["deployed_capital_usd"] == 0.0
    assert totals["open_return_on_capital_pct"] is None


def test_the_portfolio_page_shows_capital_and_notional() -> None:
    """The number that decides whether a farm was worth holding must be visible."""
    import inspect

    from spreadboard import server

    source = inspect.getsource(server.render_account_page)
    assert "deployed_capital_usd" in source
    assert "deployed_notional_usd" in source
    assert "open_return_on_capital_pct" in source
