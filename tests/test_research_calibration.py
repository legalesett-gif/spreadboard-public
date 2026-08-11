from __future__ import annotations

import time

from spreadboard import market_history, research_calibration


def _route(timestamp_us: int, basis: float, funding: float) -> dict:
    return {
        "token": "CAL",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "quote_ts_us": timestamp_us,
        "executable_spread_pct": basis,
        "depth_weighted_spread_pct": basis,
        "funding_daily_pct": funding,
        "funding_projected_24h_pct": funding,
        "depth_usd": 100_000,
        "freshness": "fresh",
        "notes": {
            "route_inputs": {
                "long": {"symbol": "CAL/USDT", "ask": 100, "bid": 99.9},
                "short": {"symbol": "CAL/USDT:USDT", "bid": 100 * (1 + basis / 100), "ask": 100.1},
            }
        },
        "blockers": [],
    }


def test_shadow_observations_are_hourly_versioned_and_labeled_without_lookahead(tmp_path) -> None:
    now = time.time()
    start = ((now - 25 * 3600) // 3600) * 3600 + 60
    history_db = tmp_path / "history.sqlite3"
    calibration_db = tmp_path / "calibration.sqlite3"
    rows = []
    for hour in range(26):
        row = _route(int((start + hour * 3600) * 1_000_000), 2.0 - hour * 0.04, 0.3)
        market_history.record_route(row, db_path=history_db)
        rows.append(row)
    leader = {**rows[0], "route_key": market_history.route_key_for(rows[0])}

    first = research_calibration.capture_routes(
        [leader], now=start, db_path=calibration_db, history_db_path=history_db
    )
    duplicate = research_calibration.capture_routes(
        [leader], now=start + 120, db_path=calibration_db, history_db_path=history_db
    )
    labeled = research_calibration.label_matured(
        now=now, db_path=calibration_db, history_db_path=history_db
    )
    status = research_calibration.status(calibration_db)

    assert first == {"considered": 1, "inserted": 1}
    assert duplicate == {"considered": 1, "inserted": 0}
    assert labeled["labeled"] == 2
    assert status["observations"] == 1
    assert status["outcomes"] == 2
    assert status["ml_ready"] is False
