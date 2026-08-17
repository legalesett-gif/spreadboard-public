"""Capital committed per position, beside the notional each leg controls.

A delta-neutral farm funds BOTH legs. At 1x the long costs its full notional and
the short ties up its full entry notional as margin, so the capital committed is
their sum -- roughly twice the per-leg notional. Quoting a return against one
leg overstates it by about 2x.

The stored `capital_usd` column is labelled "Allocated capital per leg" in the
position form, and it is exactly that: one leg. Treating it as the position
total was the bug this file exists to prevent. It made ESPORTS report $2,754
of capital controlling $2,757 of notional -- a 1.0x ratio that cannot happen on
a hedged pair, and which hid that the real denominator was $5,506.

The matched notional is the SMALLER leg: that is the part actually hedged.
Whatever the larger leg carries beyond it is unhedged residual, not farm size.
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


def test_capital_committed_is_both_legs_because_both_are_funded() -> None:
    """The whole correction: $200 a side is $400 tied up, not $200."""
    row = portfolio.capital_metrics(_position())

    assert row["capital_committed_usd"] == 400.0


def test_capital_is_about_twice_the_per_leg_notional() -> None:
    """The sanity check the operator applied by eye, pinned as arithmetic."""
    row = portfolio.capital_metrics(_position())

    assert row["capital_committed_usd"] == 2 * row["matched_notional_usd"]


def test_the_matched_notional_is_the_hedged_leg_not_the_larger_one() -> None:
    """Only the smaller leg is hedged; the excess is naked exposure."""
    row = portfolio.capital_metrics(_position(short_quantity=50.0))

    assert row["matched_notional_usd"] == 100.0
    assert row["unhedged_notional_usd"] == 100.0


def test_a_balanced_pair_carries_no_unhedged_residual() -> None:
    row = portfolio.capital_metrics(_position())

    assert row["unhedged_notional_usd"] == 0.0


def test_capital_committed_still_counts_both_legs_when_they_differ() -> None:
    """Capital follows what was actually funded, not the hedged portion."""
    row = portfolio.capital_metrics(_position(short_quantity=50.0))

    assert row["capital_committed_usd"] == 300.0


def test_the_per_leg_allocation_is_kept_but_never_used_as_the_total() -> None:
    """The operator's own figure survives, correctly labelled as one leg."""
    row = portfolio.capital_metrics(_position(capital_usd=200.0))

    assert row["allocated_capital_per_leg_usd"] == 200.0
    assert row["capital_committed_usd"] == 400.0


def test_return_on_capital_is_measured_against_both_legs() -> None:
    """$20 on $400 committed is 5%, not the 10% one leg would suggest."""
    row = portfolio.capital_metrics(_position())

    assert row["return_on_capital_pct"] == 5.0


def test_capital_falls_back_to_twice_the_allocation_when_fills_are_missing() -> None:
    """A position saved without quantities still has a usable denominator."""
    row = portfolio.capital_metrics(
        _position(
            long_quantity=None, long_entry_price=None,
            short_quantity=None, short_entry_price=None,
            capital_usd=250.0,
        )
    )

    assert row["capital_committed_usd"] == 500.0


def test_a_position_with_no_capital_at_all_reports_none_rather_than_zero() -> None:
    """Unknown capital and a zero return are opposite conclusions."""
    row = portfolio.capital_metrics(
        _position(
            long_quantity=None, long_entry_price=None,
            short_quantity=None, short_entry_price=None,
            capital_usd=None,
        )
    )

    assert row["capital_committed_usd"] is None
    assert row["return_on_capital_pct"] is None


def test_zero_capital_never_divides() -> None:
    row = portfolio.capital_metrics(
        _position(
            long_quantity=0.0, long_entry_price=0.0,
            short_quantity=0.0, short_entry_price=0.0,
            capital_usd=0.0,
        )
    )

    assert row["return_on_capital_pct"] is None


# --------------------------------------------------------------------------
# Across everything held
# --------------------------------------------------------------------------


def test_deployed_capital_counts_only_open_positions() -> None:
    """Closed positions returned their capital; it is no longer at work."""
    totals = portfolio.deployed_capital_summary([
        _position(),
        _position(status="closed", total_pnl_usd=50.0),
    ])

    assert totals["deployed_capital_usd"] == 400.0


def test_return_on_deployed_capital_spans_every_open_position() -> None:
    totals = portfolio.deployed_capital_summary([
        _position(total_pnl_usd=10.0),
        _position(long_quantity=200.0, short_quantity=200.0, total_pnl_usd=20.0),
    ])

    # 400 + 800 committed, 30 earned.
    assert totals["deployed_capital_usd"] == 1200.0
    assert totals["open_return_on_capital_pct"] == 2.5


def test_deployed_notional_sums_the_hedged_size_of_each_farm() -> None:
    totals = portfolio.deployed_capital_summary([_position()])

    assert totals["deployed_notional_usd"] == 200.0


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


def test_each_position_card_shows_its_capital_and_per_leg_notional() -> None:
    """The operator asked to see capital against the notional of each leg."""
    import inspect

    from spreadboard import server

    source = inspect.getsource(server.render_position_card)
    assert "capital_committed_usd" in source
    assert "long_notional_usd" in source
    assert "short_notional_usd" in source


def test_the_page_no_longer_calls_one_leg_the_capital_deployed() -> None:
    """The label that made a 1.0x ratio look plausible."""
    import inspect

    from spreadboard import server

    source = inspect.getsource(server.render_account_page)
    assert "Capital committed" in source
    assert "larger leg per position" not in source
