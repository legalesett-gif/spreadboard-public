"""Chart resolution comes from how often a point is recorded.

Measured on production history: consecutive samples for a charted route sit a
median of 17.7 MINUTES apart, p90 74 minutes, and a one-hour window returned
zero points. Points were only written when the discovery snapshot published,
which is roughly hourly.

That is the whole "spiky charts" complaint. A line drawn through points 18
minutes apart is jagged because the samples are sparse, not because the market
moved that way, and a short window is simply empty.

The fast-quote cycle already reprices ~175 selected routes every few minutes
and its own log line said `history_inserted=deferred`. Recording those gives
the routes people actually chart a sample every cycle. The full 25k-row
snapshot stays off this path on purpose -- it cost 45-60s and starved the DEX
rotation.
"""

from __future__ import annotations

import json

from spreadboard import market_history


def _row(spread: float, ts: int) -> dict:
    return {
        "token": "COTI",
        "long_venue": "Gate", "long_market_type": "Spot",
        "short_venue": "Bybit", "short_market_type": "Futures",
        "long_market_symbol": "COTI/USDT",
        "short_market_symbol": "COTI/USDT:USDT",
        "route_kind": "SPOT-FUTURES",
        "executable_spread_pct": spread,
        "quote_ts_us": ts,
    }


def test_fast_quote_rows_are_recorded_as_history(tmp_path) -> None:
    db = tmp_path / "history.sqlite3"
    delta = tmp_path / "api_discovery_fast_quotes.json"
    delta.write_text(json.dumps({"rows": [_row(2.5, 1_700_000_000_000_000)]}))

    inserted = market_history.record_fast_quotes(delta, db_path=db)

    assert inserted == 1


def test_each_cycle_adds_its_own_point(tmp_path) -> None:
    """Two cycles minutes apart must both survive, or resolution is unchanged."""
    db = tmp_path / "history.sqlite3"
    delta = tmp_path / "api_discovery_fast_quotes.json"

    delta.write_text(json.dumps({"rows": [_row(2.5, 1_700_000_000_000_000)]}))
    market_history.record_fast_quotes(delta, db_path=db)
    delta.write_text(json.dumps({"rows": [_row(2.9, 1_700_000_240_000_000)]}))
    market_history.record_fast_quotes(delta, db_path=db)

    import sqlite3

    rows = sqlite3.connect(db).execute(
        "select quote_ts_us, executable_spread_pct from route_points order by quote_ts_us"
    ).fetchall()
    assert len(rows) == 2
    assert [r[1] for r in rows] == [2.5, 2.9]


def test_a_missing_or_broken_delta_never_raises(tmp_path) -> None:
    """This runs on the quote path; it must not be able to stop a cycle."""
    db = tmp_path / "history.sqlite3"

    assert market_history.record_fast_quotes(tmp_path / "absent.json", db_path=db) == 0

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert market_history.record_fast_quotes(broken, db_path=db) == 0


def test_rows_without_a_price_are_not_recorded(tmp_path) -> None:
    db = tmp_path / "history.sqlite3"
    delta = tmp_path / "d.json"
    row = _row(2.5, 1_700_000_000_000_000)
    row["executable_spread_pct"] = None
    delta.write_text(json.dumps({"rows": [row]}))

    assert market_history.record_fast_quotes(delta, db_path=db) == 0
