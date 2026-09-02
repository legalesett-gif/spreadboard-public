"""Return on capital counted only what the open positions had earned.

Live account, 2026-09-02: deployed capital $25,880 and total price-and-funding
PnL $634.71, which is 2.45%. The page showed 0.86%, because the numerator was
`open_position_pnl_usd` ($223.24) -- the open legs' mark-to-market and funding
only, discarding every dollar already realised and withdrawn from a closed
position.

The owner reads this figure as "what has this book earned me on the money I
have at work", and realised profit is unambiguously part of that. Excluding it
makes a book that closes its winners look worse the more often it takes profit.
"""

from __future__ import annotations

from spreadboard import portfolio


def _open(pnl, committed_per_leg=1000.0):
    return {
        "status": "open",
        "long_quantity": committed_per_leg / 10.0,
        "long_entry_price": 10.0,
        "short_quantity": committed_per_leg / 10.0,
        "short_entry_price": 10.0,
        "total_pnl_usd": pnl,
    }


def _closed(pnl):
    item = _open(pnl)
    item["status"] = "closed"
    return item


def test_realised_profit_counts_toward_return_on_capital() -> None:
    """$100 open + $400 realised on $2,000 deployed is 25%, not 5%."""

    summary = portfolio.deployed_capital_summary([_open(100.0), _closed(400.0)])

    assert summary["deployed_capital_usd"] == 2000.0, (
        "closed positions returned their capital and must not inflate the base"
    )
    assert summary["open_return_on_capital_pct"] == 25.0


def test_the_capital_base_is_still_only_what_is_deployed() -> None:
    """Closed positions contribute their PnL but not their capital.

    Including returned capital in the denominator would answer "how did the
    month go", which the monthly figure already answers.
    """

    summary = portfolio.deployed_capital_summary([_open(0.0), _closed(0.0), _closed(0.0)])

    assert summary["deployed_capital_usd"] == 2000.0


def test_a_book_with_nothing_open_reports_no_return_on_deployed_capital() -> None:
    """Dividing realised profit by zero deployed capital is not a percentage."""

    summary = portfolio.deployed_capital_summary([_closed(400.0)])

    assert summary["deployed_capital_usd"] == 0.0
    assert summary["open_return_on_capital_pct"] is None


def test_an_unpriced_open_position_does_not_blank_the_figure() -> None:
    summary = portfolio.deployed_capital_summary(
        [_open(100.0), _open(None)]
    )

    assert summary["open_return_on_capital_pct"] is not None
