from __future__ import annotations

import json

from spreadboard import ml_readiness, research_calibration


def test_missing_calibration_data_fails_closed(tmp_path) -> None:
    result = ml_readiness.assess(tmp_path / "missing.sqlite3")
    assert result["activation_allowed"] is False
    assert result["model_configured"] is False
    assert result["reason"] == "calibration_database_missing"


def test_small_dataset_cannot_activate_even_with_claimed_candidate_metrics(tmp_path) -> None:
    db = tmp_path / "calibration.sqlite3"
    research_calibration.initialize(db)
    connection = research_calibration._connect(db)
    try:
        for index in range(120):
            cursor = connection.execute(
                """INSERT INTO score_observations (
                       route_key, token, observed_hour_us, method, funding_score,
                       spread_score, funding_confidence, spread_confidence,
                       entry_basis_pct, expected_funding_24h_pct, funding_regime,
                       cost_status, feature_json, created_at
                   ) VALUES (?, 'X', ?, 'v1', 50, 50, 80, 80, 1, .2,
                             'positive', 'known', ?, '2026-01-01T00:00:00+00:00')""",
                (f"route-{index % 10}", index * 3_600_000_000, json.dumps({"carry": index % 5})),
            )
            connection.execute(
                """INSERT INTO score_outcomes (
                       observation_id, horizon_hours, outcome_ts_us,
                       end_basis_pct, convergence_capture_pct,
                       max_adverse_basis_widening_pct, end_funding_daily_pct,
                       funding_positive_fraction, sample_count, labeled_at
                   ) VALUES (?, 24, ?, .5, ?, .2, .2, ?, 24,
                             '2026-01-02T00:00:00+00:00')""",
                (cursor.lastrowid, index * 3_600_000_000 + 86_400_000_000,
                 0.5 if index % 2 else -0.5, 1.0 if index % 3 else 0.0),
            )
        connection.commit()
    finally:
        connection.close()

    result = ml_readiness.assess(
        db,
        candidate_metrics={
            "funding_brier": 0.01,
            "spread_brier": 0.01,
            "funding_ece": 0.01,
            "spread_ece": 0.01,
            "drift_psi_max": 0.01,
            "p95_inference_ms": 10,
            "fallback_tested": True,
            "shadow_days": 30,
        },
    )

    assert result["time_split"]["valid"] is True
    assert result["data_gates"]["outcomes"]["passed"] is False
    assert result["data_gates"]["routes"]["passed"] is False
    assert result["activation_allowed"] is False


def test_feature_leakage_names_block_readiness(tmp_path) -> None:
    db = tmp_path / "calibration.sqlite3"
    research_calibration.initialize(db)
    connection = research_calibration._connect(db)
    try:
        connection.execute(
            """INSERT INTO score_observations (
                   route_key, token, observed_hour_us, method, funding_confidence,
                   spread_confidence, funding_regime, cost_status, feature_json,
                   created_at
               ) VALUES ('r', 'X', 1, 'v1', 1, 1, 'mixed', 'known', ?, 'x')""",
            (json.dumps({"future_funding": 1}),),
        )
        connection.commit()
    finally:
        connection.close()
    result = ml_readiness.assess(db)
    assert result["data_gates"]["feature_leakage_scan"]["passed"] is False
    assert "future_funding" in result["data_gates"]["feature_leakage_scan"]["hits"]
