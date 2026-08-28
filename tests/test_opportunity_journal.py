from __future__ import annotations

import sqlite3

from spreadboard import opportunity_journal


def _row(spread: float) -> dict:
    return {
        "route_key": "GUA-route",
        "token": "GUA",
        "route_kind": "FUTURES",
        "long_venue": "Mexc",
        "short_venue": "Gate",
        "spread_pct": spread,
        "quote_ts_us": 1_000_000,
    }


def test_short_lived_opportunity_is_journaled_open_peak_and_closed(tmp_path) -> None:
    path = tmp_path / "journal.sqlite3"
    opened = opportunity_journal.record_snapshot(
        [_row(0.6)], observed_route_kinds={"FUTURES"}, path=path, now=1.0
    )
    quiet = opportunity_journal.record_snapshot(
        [_row(0.65)], observed_route_kinds={"FUTURES"}, path=path, now=2.0
    )
    peak = opportunity_journal.record_snapshot(
        [_row(0.8)], observed_route_kinds={"FUTURES"}, path=path, now=3.0
    )
    closed = opportunity_journal.record_snapshot(
        [], observed_route_kinds={"FUTURES"}, path=path, now=4.0
    )

    connection = sqlite3.connect(path)
    try:
        events = [
            row[0]
            for row in connection.execute(
                "SELECT event_kind FROM opportunity_events ORDER BY id"
            ).fetchall()
        ]
        active = connection.execute(
            "SELECT active FROM opportunity_state WHERE route_key = 'GUA-route'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert opened == {"opened": 1, "new_peaks": 0, "closed": 0, "active": 1}
    assert quiet["new_peaks"] == 0
    assert peak["new_peaks"] == 1
    assert closed["closed"] == 1
    assert events == ["opened", "new_peak", "closed"]
    assert active == 0
    recent = opportunity_journal.recent_events(
        token="GUA", since_seconds=10, limit=10, path=path, now=5.0
    )
    assert [event["event_kind"] for event in recent] == [
        "closed",
        "new_peak",
        "opened",
    ]
    assert all(event["historical_observation_only"] for event in recent)
