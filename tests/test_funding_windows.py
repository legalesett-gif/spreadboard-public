"""Realised funding per leg over 1d/7d/30d.

The reference product shows these beside every leg and it is how a member tells
a farm that has paid for a week from a rate that spiked this morning. Ours said
"history unavailable".
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from spreadboard import market_history


def _db(tmp_path: Path, samples: list[tuple]) -> Path:
    path = tmp_path / "history.sqlite3"
    connection = market_history._connect(path)
    connection.executemany(
        """
        INSERT INTO route_points (
            route_key, quote_ts_us, token, route_kind,
            long_current_funding_pct, long_funding_interval_hours,
            short_current_funding_pct, short_funding_interval_hours
        ) VALUES (?, ?, 'T', 'FUTURES', ?, ?, ?, ?)
        """,
        samples,
    )
    connection.commit()
    connection.close()
    return path


def _hourly(route: str, hours: int, *, long_pct: float, short_pct: float, now_us: int) -> list[tuple]:
    """One sample an hour, each leg on an 8h funding interval."""
    return [
        (route, now_us - h * 3_600_000_000, long_pct, 8.0, short_pct, 8.0)
        for h in range(hours, 0, -1)
    ]


def test_realised_funding_is_the_rate_integrated_over_time(tmp_path: Path) -> None:
    now = int(time.time() * 1_000_000)
    # 24 hourly samples at 0.01%/8h on the short leg is three settlements: 0.03%.
    path = _db(tmp_path, _hourly("R", 24, long_pct=0.0, short_pct=0.01, now_us=now))

    result = market_history.funding_windows(["R"], windows_days=(1,), db_path=path, now_us=now)

    day = result["R"]["1d"]
    assert day["short"] is not None
    assert abs(day["short"] - 0.03) < 0.005, day
    assert abs(day["net"] - 0.03) < 0.005


def test_net_is_short_minus_long(tmp_path: Path) -> None:
    now = int(time.time() * 1_000_000)
    path = _db(tmp_path, _hourly("R", 24, long_pct=0.01, short_pct=0.03, now_us=now))

    day = market_history.funding_windows(["R"], windows_days=(1,), db_path=path, now_us=now)["R"]["1d"]

    assert abs(day["net"] - (day["short"] - day["long"])) < 1e-9
    assert day["net"] > 0


def test_a_window_we_barely_observed_reports_nothing(tmp_path: Path) -> None:
    """Three hours of samples is not a 7d return, whatever the arithmetic says."""
    now = int(time.time() * 1_000_000)
    path = _db(tmp_path, _hourly("R", 3, long_pct=0.0, short_pct=0.01, now_us=now))

    result = market_history.funding_windows(["R"], windows_days=(7,), db_path=path, now_us=now)

    assert result["R"]["7d"]["short"] is None


def test_a_long_gap_is_not_counted_as_accrual(tmp_path: Path) -> None:
    """A route that left the sampling set did not keep earning unobserved."""
    now = int(time.time() * 1_000_000)
    samples = [
        ("R", now - 20 * 3_600_000_000, 0.0, 8.0, 1.0, 8.0),  # then a 19h gap
        ("R", now - 3_600_000_000, 0.0, 8.0, 1.0, 8.0),
    ]
    path = _db(tmp_path, samples)

    result = market_history.funding_windows(["R"], windows_days=(1,), db_path=path, now_us=now)

    # Coverage is capped at 1h per sample, far under the 12h a 1d window needs.
    assert result["R"]["1d"]["short"] is None


def test_unknown_routes_come_back_empty_not_missing(tmp_path: Path) -> None:
    now = int(time.time() * 1_000_000)
    path = _db(tmp_path, _hourly("R", 24, long_pct=0.0, short_pct=0.01, now_us=now))

    result = market_history.funding_windows(["R", "OTHER"], windows_days=(1,), db_path=path, now_us=now)

    assert result["OTHER"]["1d"] == {"long": None, "short": None, "net": None}
