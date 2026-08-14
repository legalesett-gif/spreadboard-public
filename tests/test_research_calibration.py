from __future__ import annotations

import json
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

    assert first == {
        "considered": 1,
        "inserted": 1,
        "cost_evidenced": 0,
        "transfer_evidenced": 0,
    }
    assert duplicate == {
        "considered": 1,
        "inserted": 0,
        "cost_evidenced": 0,
        "transfer_evidenced": 0,
    }
    assert labeled["labeled"] == 2
    assert status["observations"] == 1
    assert status["outcomes"] == 2
    assert status["ml_ready"] is False


def test_opt_in_dex_cost_and_transfer_evidence_requires_exact_current_identity(tmp_path) -> None:
    now = time.time()
    calibration_db = tmp_path / "calibration.sqlite3"
    history_db = tmp_path / "history.sqlite3"
    route = {
        **_route(int(now * 1_000_000), 1.2, 0.4),
        "token": "DEXCAL",
        "long_venue": "OKX DEX 56",
        "long_market_type": "DEX",
        "route_kind": "DEX-FUTURES",
        "route_key": "DEXCAL|OKX DEX 56|DEX|Bybit|Futures",
        "dex_chain": "Base",
        "dex_contract": "0xabc",
        "requires_transfer": True,
        "deliverable": False,
    }
    evidence = {
        "dexcal|okx dex 56|dex|bybit|futures": {
            "costs": [
                {
                    "chain": "base",
                    "contract": "0xabc",
                    "round_trip_cost_pct": 0.42,
                    "sample_count": 3,
                    "includes": "fees_borrow_gas_transfer_and_measured_slippage",
                }
            ],
            "transfers": [
                {
                    "chain": "base",
                    "contract": "0xabc",
                    "transfer_time_seconds": 95,
                    "sample_count": 2,
                }
            ],
        }
    }

    captured = research_calibration.capture_routes(
        [route],
        now=now,
        account_evidence=evidence,
        db_path=calibration_db,
        history_db_path=history_db,
    )
    connection = research_calibration._connect(calibration_db)
    try:
        row = connection.execute(
            "SELECT cost_status, feature_json FROM score_observations"
        ).fetchone()
    finally:
        connection.close()
    features = json.loads(row["feature_json"])

    assert captured["cost_evidenced"] == 1
    assert captured["transfer_evidenced"] == 1
    assert row["cost_status"] == "observed_route_median"
    assert features["account_cost_evidence"]["sample_count"] == 3
    assert features["dex_evidence"]["transfer"]["deliverable"] is False
    assert features["dex_evidence"]["transfer"]["status"] == "blocked"
    assert features["dex_evidence"]["transfer"]["time_status"] == "observed_opt_in_median"

    wrong_identity = research_calibration._route_with_account_evidence(
        {**route, "dex_contract": "0xdifferent"},
        evidence["dexcal|okx dex 56|dex|bybit|futures"],
    )
    assert "known_round_trip_cost_pct" not in wrong_identity
    assert "dex_transfer_time_seconds" not in wrong_identity


def test_missing_history_is_backed_off_instead_of_starving_later_routes(tmp_path) -> None:
    now = time.time()
    bad_observed = int(((now - 30 * 3600) // 3600) * 3600 * 1_000_000)
    good_observed = int(((now - 25 * 3600) // 3600) * 3600 * 1_000_000)
    history_db = tmp_path / "history.sqlite3"
    calibration_db = tmp_path / "calibration.sqlite3"
    research_calibration.initialize(calibration_db)
    connection = research_calibration._connect(calibration_db)
    try:
        for route_key, token, observed in (
            ("missing-route", "MISS", bad_observed),
            ("good-route", "GOOD", good_observed),
        ):
            connection.execute(
                """INSERT INTO score_observations (
                       route_key, token, observed_hour_us, method,
                       funding_confidence, spread_confidence, entry_basis_pct,
                       funding_regime, cost_status, feature_json, created_at
                   ) VALUES (?, ?, ?, 'v1', 50, 50, 2, 'positive', 'known', '{}', 'x')""",
                (route_key, token, observed),
            )
        connection.commit()
    finally:
        connection.close()

    for horizon in (8, 24):
        row = _route(good_observed + horizon * 3600 * 1_000_000, 1.0, 0.2)
        row["route_key"] = "good-route"
        market_history.record_route(row, db_path=history_db)

    blocked = research_calibration.label_matured(
        now=now, limit=2, db_path=calibration_db, history_db_path=history_db
    )
    progressed = research_calibration.label_matured(
        now=now, limit=2, db_path=calibration_db, history_db_path=history_db
    )

    assert blocked == {"attempted": 2, "labeled": 0, "insufficient_history": 2}
    assert progressed["labeled"] == 2
