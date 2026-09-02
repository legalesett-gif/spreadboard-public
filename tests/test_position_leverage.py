"""Leverage changes what capital a position actually locks up.

A 5x short controls its notional on a fifth of the money. Reporting the
notional as the capital understates the return by the leverage factor, which is
exactly where a levered farm looks least attractive and is in fact doing best.
The operator's SKHY position is run at 5x.

Leverage is per leg, not per position: a spot long is always 1x while the perp
short beside it may be 5x, and a single position-level factor cannot express
that.
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
        "total_pnl_usd": 100.0,
    }
    base.update(kw)
    return base


def test_unlevered_capital_is_both_notionals() -> None:
    metrics = portfolio.capital_metrics(_position())

    assert metrics["capital_committed_usd"] == 2000.0
    assert metrics["return_on_capital_pct"] == 5.0


def test_a_levered_short_locks_less_capital() -> None:
    """$1,000 long at 1x plus $1,000 short at 5x locks $1,200, not $2,000."""

    metrics = portfolio.capital_metrics(_position(short_leverage=5.0))

    assert metrics["capital_committed_usd"] == 1200.0
    assert metrics["return_on_capital_pct"] == round(100.0 / 1200.0 * 100.0, 4)


def test_leverage_on_both_legs_is_applied_per_leg() -> None:
    metrics = portfolio.capital_metrics(
        _position(long_leverage=2.0, short_leverage=5.0)
    )

    assert metrics["capital_committed_usd"] == 700.0


def test_notional_is_unchanged_by_leverage() -> None:
    """Leverage changes the capital, never the exposure.

    Conflating them is what makes a levered farm look like a bigger position
    than it is.
    """

    plain = portfolio.capital_metrics(_position())
    levered = portfolio.capital_metrics(_position(short_leverage=5.0))

    assert plain["matched_notional_usd"] == levered["matched_notional_usd"] == 1000.0
    assert plain["long_notional_usd"] == levered["long_notional_usd"] == 1000.0


def test_absent_or_nonsense_leverage_is_treated_as_unlevered() -> None:
    """A missing value must never divide capital toward zero and report an
    infinite return."""

    for value in (None, 0, -3, "", "abc"):
        metrics = portfolio.capital_metrics(_position(short_leverage=value))
        assert metrics["capital_committed_usd"] == 2000.0, f"leverage={value!r}"


def test_the_headline_summary_honours_leverage() -> None:
    totals = portfolio._portfolio_totals([_position(short_leverage=5.0)], None)

    assert totals["monthly_capital_usd"] == 1200.0
    # The summary reports unrounded; capital_metrics rounds to 4dp. Both are the
    # same number, and the renderer formats.
    assert totals["monthly_return_pct"] == 100.0 / 1200.0 * 100.0


def test_leverage_round_trips_through_the_journal(tmp_path) -> None:
    """Stated leverage must survive the write, or the capital figure silently
    reverts to unlevered on the next page load."""

    from spreadboard import accounts

    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path=db)
    user = accounts.create_user(
        email="lev@example.com", display_name="Lev", password="pw-1234567890ab", db_path=db
    )

    created = accounts.create_position(
        int(user["id"]),
        {
            "token": "SKHY",
            "long_venue": "Hyperliquid",
            "long_market_type": "Futures",
            "short_venue": "Hyperliquid",
            "short_market_type": "Futures",
            "long_quantity": 100.0,
            "long_entry_price": 10.0,
            "short_quantity": 100.0,
            "short_entry_price": 10.0,
            "short_leverage": 5.0,
        },
        db_path=db,
    )

    assert created["short_leverage"] == 5.0
    assert created["long_leverage"] is None

    stored = next(iter(accounts.list_positions(int(user["id"]), db_path=db)))
    assert stored["short_leverage"] == 5.0

    metrics = portfolio.capital_metrics(stored)
    assert metrics["capital_committed_usd"] == 1200.0
