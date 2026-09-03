"""The chart-history writer must not pay for work it has already done.

A ninety-second CPU profile of the production web process attributed 63.7% of
its time to two lines of ``record_snapshot``: the retention ``DELETE`` (23.5%)
and the ``INSERT`` (40.2%).  Neither was doing useful work.

The ``DELETE`` has no index to use, so it scans all 14.5 million rows -- and
``record_route`` ran it for every single-row chart sample.  The ``INSERT`` is
``INSERT OR IGNORE``, and the fast-quote delta re-offers its whole row set on
every cycle even though only the freshly repriced routes carry a new
``quote_ts_us``; 123 rows landed in ten minutes out of tens of thousands of
attempts, each one walking a B-tree with a two-megabyte page cache.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from spreadboard import market_history

DAY_US = 86_400 * 1_000_000


def _now_us() -> int:
    """Retention is measured against the wall clock, so anchor on it."""

    return int(time.time() * 1_000_000)


def _row(route_key: str, quote_ts_us: int) -> dict[str, object]:
    return {
        "route_key": route_key,
        "quote_ts_us": quote_ts_us,
        "token": "TEST",
        "long_venue": "gate",
        "long_market_type": "spot",
        "short_venue": "mexc",
        "short_market_type": "swap",
        "executable_spread_pct": 1.25,
    }


def _tracing_connect(monkeypatch, statements: list[str]) -> None:
    original = market_history._connect

    def traced(path: Path | str) -> sqlite3.Connection:
        connection = original(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(market_history, "_connect", traced)


def test_single_route_write_does_not_run_retention(tmp_path) -> None:
    """One chart sample must not drag a full-table retention scan behind it."""

    db_path = tmp_path / "history.sqlite3"
    fresh_us = _now_us()
    stale_us = fresh_us - 40 * DAY_US

    market_history.record_snapshot(
        {"api_discovered_rows": [_row("retain|old", stale_us)]},
        db_path=db_path,
        prune=False,
    )
    market_history.record_route(_row("retain|new", fresh_us), db_path=db_path)

    surviving = market_history.load_history(route_key="retain|old", db_path=db_path)
    assert [point["quote_ts_us"] for point in surviving] == [stale_us]


def test_snapshot_writes_still_apply_retention(tmp_path) -> None:
    """Moving retention off the single-row path must not switch it off."""

    db_path = tmp_path / "history.sqlite3"
    fresh_us = _now_us()
    stale_us = fresh_us - 40 * DAY_US

    market_history.record_snapshot(
        {"api_discovered_rows": [_row("prune|old", stale_us)]},
        db_path=db_path,
        prune=False,
    )
    market_history.record_snapshot(
        {"api_discovered_rows": [_row("prune|new", fresh_us)]},
        db_path=db_path,
    )

    assert market_history.load_history(route_key="prune|old", db_path=db_path) == []


def test_a_point_already_written_is_not_offered_to_sqlite_again(
    tmp_path, monkeypatch
) -> None:
    """The second offer of an identical point must not reach the database."""

    db_path = tmp_path / "history.sqlite3"
    statements: list[str] = []
    _tracing_connect(monkeypatch, statements)
    snapshot = {"api_discovered_rows": [_row("repeat|route", 1_700_000_000_000_000)]}

    assert market_history.record_snapshot(snapshot, db_path=db_path, prune=False) == 1
    statements.clear()
    assert market_history.record_snapshot(snapshot, db_path=db_path, prune=False) == 0

    assert not [text for text in statements if "INSERT" in text.upper()]


def test_a_new_timestamp_on_a_written_route_is_still_recorded(tmp_path) -> None:
    """Skipping repeats must key on the point, never on the route alone."""

    db_path = tmp_path / "history.sqlite3"
    first_us = 1_700_000_100_000_000
    second_us = first_us + 60_000_000

    market_history.record_snapshot(
        {"api_discovered_rows": [_row("moving|route", first_us)]},
        db_path=db_path,
        prune=False,
    )
    market_history.record_snapshot(
        {"api_discovered_rows": [_row("moving|route", second_us)]},
        db_path=db_path,
        prune=False,
    )

    recorded = market_history.load_history(route_key="moving|route", db_path=db_path)
    assert sorted(point["quote_ts_us"] for point in recorded) == [first_us, second_us]


def test_the_written_point_cache_stays_bounded(tmp_path) -> None:
    """A long-lived writer must not accumulate one entry per point forever."""

    db_path = tmp_path / "history.sqlite3"
    limit = market_history._WRITTEN_POINT_LIMIT
    market_history.record_snapshot(
        {
            "api_discovered_rows": [
                _row(f"bounded|{index}", 1_700_000_000_000_000 + index)
                for index in range(limit + 50)
            ]
        },
        db_path=db_path,
        prune=False,
    )

    assert len(market_history._WRITTEN_POINTS) <= limit
