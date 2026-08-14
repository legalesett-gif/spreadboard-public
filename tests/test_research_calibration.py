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


def test_shadow_followup_keeps_dropped_leader_and_records_one_exact_dex_point_per_hour(
    tmp_path,
) -> None:
    hour = int(time.time() // 3600 * 3600)
    observed = hour + 60
    calibration_db = tmp_path / "calibration.sqlite3"
    history_db = tmp_path / "history.sqlite3"
    route = {
        **_route(observed * 1_000_000, 1.2, 0.4),
        "token": "DEXCAL",
        "route_key": "DEXCAL|OKX DEX 56|DEX|Bybit|Futures",
        "long_venue": "OKX DEX 56",
        "long_market_type": "DEX",
        "route_kind": "DEX-FUTURES",
        "dex_chain": "56",
        "dex_contract": "0xabc",
        "dex_quote_source": "okx_dex_quote",
        "dex_price_impact_pct": -0.02,
        "dex_gas_estimate_usd": 0.01,
        "dex_mev_protection": "not_enabled_quote_only",
        "deliverable": True,
    }
    research_calibration.capture_routes(
        [route],
        now=observed,
        db_path=calibration_db,
        history_db_path=history_db,
    )

    keys = research_calibration.shadow_followup_route_keys(
        [], now=observed + 3600, db_path=calibration_db
    )
    first = market_history.record_research_routes_hourly(
        [route], route_keys=keys, now=observed, db_path=history_db
    )
    later_same_hour = {**route, "quote_ts_us": (observed + 120) * 1_000_000}
    duplicate = market_history.record_research_routes_hourly(
        [later_same_hour], route_keys=keys, now=observed + 120, db_path=history_db
    )
    point = market_history.load_history(route_key=route["route_key"], db_path=history_db)[0]

    assert route["route_key"] in keys
    assert first["inserted"] == 1
    assert duplicate["inserted"] == 0
    assert duplicate["already_recorded"] == 1
    assert point["dex_chain"] == "56"
    assert point["dex_contract"] == "0xabc"
    assert point["dex_quote_source"] == "okx_dex_quote"
    assert point["deliverable"] == 1
    assert route["route_key"] not in research_calibration.shadow_followup_route_keys(
        [], now=observed + 27 * 3600, db_path=calibration_db
    )


def test_dex_outcome_uses_only_the_frozen_chain_and_contract(tmp_path) -> None:
    now = time.time()
    start = int(((now - 25 * 3600) // 3600) * 3600 + 60)
    calibration_db = tmp_path / "calibration.sqlite3"
    history_db = tmp_path / "history.sqlite3"
    route = {
        **_route(start * 1_000_000, 2.0, 0.4),
        "token": "DEXCAL",
        "route_key": "DEXCAL|OKX DEX 56|DEX|Bybit|Futures",
        "long_venue": "OKX DEX 56",
        "long_market_type": "DEX",
        "route_kind": "DEX-FUTURES",
        "dex_chain": "56",
        "dex_contract": "0xcorrect",
    }
    research_calibration.capture_routes(
        [route], now=start, db_path=calibration_db, history_db_path=history_db
    )
    for horizon in (8, 24):
        target = start + horizon * 3600
        wrong = {
            **route,
            "quote_ts_us": target * 1_000_000,
            "depth_weighted_spread_pct": 99.0,
            "dex_contract": "0xwrong",
        }
        correct = {
            **route,
            "quote_ts_us": (target + 60) * 1_000_000,
            "depth_weighted_spread_pct": 1.0,
        }
        market_history.record_route(wrong, db_path=history_db)
        market_history.record_route(correct, db_path=history_db)

    labeled = research_calibration.label_matured(
        now=now, db_path=calibration_db, history_db_path=history_db
    )
    connection = research_calibration._connect(calibration_db)
    try:
        outcomes = connection.execute(
            "SELECT horizon_hours, end_basis_pct FROM score_outcomes ORDER BY horizon_hours"
        ).fetchall()
    finally:
        connection.close()

    assert labeled["labeled"] == 2
    assert [(row["horizon_hours"], row["end_basis_pct"]) for row in outcomes] == [
        (8, 1.0),
        (24, 1.0),
    ]
