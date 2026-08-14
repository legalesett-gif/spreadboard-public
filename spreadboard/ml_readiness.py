"""Fail-closed readiness gates for a future measurable-outcome ML layer.

There is intentionally no model training or inference here.  The module proves
whether the shadow calibration data is mature enough to begin a walk-forward
experiment and refuses activation unless an eventual candidate beats simple
time-split baselines, is calibrated, drift-checked, and has a tested
deterministic fallback.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from . import market_core_backfill, research_calibration

MIN_OUTCOMES = 5_000
MIN_ROUTES = 100
MIN_SPAN_DAYS = 30.0
MIN_COST_COMPLETE = 0.80
MIN_CLASS_SHARE = 0.10
SPLIT_EMBARGO_US = 24 * 3_600_000_000


def assess(
    db_path: Path | str = research_calibration.DEFAULT_DB_PATH,
    *,
    method: str | None = None,
    candidate_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return _empty("calibration_database_missing")
    research_calibration.initialize(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        available_methods = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT method, COUNT(*) FROM score_observations GROUP BY method ORDER BY method"
            )
        }
        selected_method = str(method or "").strip()
        if not selected_method:
            latest = connection.execute(
                """SELECT method FROM score_observations
                   ORDER BY observed_hour_us DESC, id DESC LIMIT 1"""
            ).fetchone()
            selected_method = str(latest[0]) if latest else ""
        observations = connection.execute(
            """SELECT * FROM score_observations
               WHERE method = ? ORDER BY observed_hour_us, id""",
            (selected_method,),
        ).fetchall()
        outcomes = connection.execute(
            """SELECT x.*, o.route_key, o.observed_hour_us, o.cost_status,
                      o.feature_json
               FROM score_outcomes x JOIN score_observations o ON o.id=x.observation_id
               WHERE x.horizon_hours=24 AND o.method = ?
               ORDER BY o.observed_hour_us, o.id""",
            (selected_method,),
        ).fetchall()
    finally:
        connection.close()

    routes = len({str(row["route_key"]) for row in outcomes})
    span_days = (
        (int(outcomes[-1]["observed_hour_us"]) - int(outcomes[0]["observed_hour_us"]))
        / 86_400_000_000
        if len(outcomes) >= 2
        else 0.0
    )
    cost_complete = (
        sum(str(row["cost_status"]) in {"known", "observed_route_median"} for row in outcomes)
        / len(outcomes)
        if outcomes
        else 0.0
    )
    funding_labels = [
        float(row["funding_positive_fraction"]) >= 0.999
        for row in outcomes
        if row["funding_positive_fraction"] is not None
    ]
    spread_labels = [
        float(row["convergence_capture_pct"]) > 0
        for row in outcomes
        if row["convergence_capture_pct"] is not None
    ]
    funding_balance = _minority_share(funding_labels)
    spread_balance = _minority_share(spread_labels)
    leakage_hits = _leakage_hits(observations)

    data_gates = {
        "version_selected": {
            "passed": bool(selected_method) and selected_method in available_methods,
            "selected": selected_method or None,
            "available": available_methods,
        },
        "outcomes": {
            "passed": len(outcomes) >= MIN_OUTCOMES,
            "actual": len(outcomes),
            "required": MIN_OUTCOMES,
        },
        "routes": {"passed": routes >= MIN_ROUTES, "actual": routes, "required": MIN_ROUTES},
        "span_days": {
            "passed": span_days >= MIN_SPAN_DAYS,
            "actual": round(span_days, 2),
            "required": MIN_SPAN_DAYS,
        },
        "cost_complete_fraction": {
            "passed": cost_complete >= MIN_COST_COMPLETE,
            "actual": round(cost_complete, 4),
            "required": MIN_COST_COMPLETE,
        },
        "funding_class_balance": {
            "passed": funding_balance >= MIN_CLASS_SHARE,
            "actual": round(funding_balance, 4),
            "required": MIN_CLASS_SHARE,
        },
        "spread_class_balance": {
            "passed": spread_balance >= MIN_CLASS_SHARE,
            "actual": round(spread_balance, 4),
            "required": MIN_CLASS_SHARE,
        },
        "feature_leakage_scan": {"passed": not leakage_hits, "hits": leakage_hits[:20]},
    }

    baselines = _time_split_baselines(outcomes)
    candidate = _candidate_gates(candidate_metrics, baselines)
    data_ready = all(item["passed"] for item in data_gates.values())
    activation_allowed = (
        data_ready and baselines["valid"] and all(item["passed"] for item in candidate.values())
    )
    return {
        "mode": "shadow_only",
        "model_configured": False,
        "activation_allowed": activation_allowed,
        "data_ready": data_ready,
        "selected_method": selected_method or None,
        "available_method_versions": available_methods,
        "excluded_observations_from_other_versions": sum(available_methods.values())
        - len(observations),
        "historical_market_core": market_core_backfill.status(path),
        "data_gates": data_gates,
        "time_split": baselines,
        "candidate_gates": candidate,
        "targets": [
            "funding_positive_over_8h",
            "funding_positive_over_24h",
            "net_carry_after_entered_costs",
            "spread_convergence_over_24h",
            "p95_basis_widening_and_adverse_leg_excursion",
        ],
        "explicitly_excluded": [
            "LLM-generated margin or liquidation predictions",
            "random train/test splits across time",
            "activation without calibrated probabilities and deterministic fallback",
            "historical public-market backfill as proof of exact costs, identity, or activation readiness",
        ],
    }


def _time_split_baselines(rows: list[sqlite3.Row]) -> dict[str, Any]:
    if len(rows) < 100:
        return {"valid": False, "reason": "at_least_100_labeled_24h_outcomes_required"}
    timestamps = sorted({int(row["observed_hour_us"]) for row in rows})
    if len(timestamps) < 5:
        return {
            "valid": False,
            "reason": "distinct_observation_times_required_for_purged_split",
        }
    train_boundary = timestamps[max(1, int(len(timestamps) * 0.60))]
    calibration_boundary = timestamps[max(2, int(len(timestamps) * 0.80))]
    train = [row for row in rows if int(row["observed_hour_us"]) < train_boundary]
    calibration = [
        row
        for row in rows
        if train_boundary + SPLIT_EMBARGO_US <= int(row["observed_hour_us"]) < calibration_boundary
    ]
    test = [
        row
        for row in rows
        if int(row["observed_hour_us"]) >= calibration_boundary + SPLIT_EMBARGO_US
    ]
    funding_train = _labels(train, "funding")
    funding_calibration = _labels(calibration, "funding")
    funding_test = _labels(test, "funding")
    spread_train = _labels(train, "spread")
    spread_calibration = _labels(calibration, "spread")
    spread_test = _labels(test, "spread")
    if not all(
        (
            funding_train,
            funding_calibration,
            funding_test,
            spread_train,
            spread_calibration,
            spread_test,
        )
    ):
        return {
            "valid": False,
            "reason": "all_purged_splits_require_both_non_null_targets",
        }
    funding_prevalence = sum(funding_train) / len(funding_train)
    spread_prevalence = sum(spread_train) / len(spread_train)
    return {
        "valid": True,
        "strategy": "purged_chronological_60_train_20_calibration_20_test",
        "embargo_hours": 24,
        "train_latest_observed_us": max(int(row["observed_hour_us"]) for row in train),
        "calibration_earliest_observed_us": min(
            int(row["observed_hour_us"]) for row in calibration
        ),
        "calibration_latest_observed_us": max(int(row["observed_hour_us"]) for row in calibration),
        "test_earliest_observed_us": min(int(row["observed_hour_us"]) for row in test),
        "purged": len(rows) - len(train) - len(calibration) - len(test),
        "train": len(train),
        "calibration": len(calibration),
        "test": len(test),
        "funding_prevalence_baseline_brier": round(
            sum((funding_prevalence - value) ** 2 for value in funding_test) / len(funding_test), 8
        ),
        "spread_prevalence_baseline_brier": round(
            sum((spread_prevalence - value) ** 2 for value in spread_test) / len(spread_test), 8
        ),
    }


def _candidate_gates(metrics: dict[str, Any] | None, baselines: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metrics, dict) or not metrics:
        return {"candidate_present": {"passed": False, "reason": "no_candidate_model"}}
    funding_baseline = _float(baselines.get("funding_prevalence_baseline_brier"))
    spread_baseline = _float(baselines.get("spread_prevalence_baseline_brier"))
    funding_brier = _float(metrics.get("funding_brier"))
    spread_brier = _float(metrics.get("spread_brier"))
    return {
        "candidate_present": {"passed": True},
        "beats_funding_baseline": {
            "passed": _beats(funding_brier, funding_baseline),
            "actual": funding_brier,
        },
        "beats_spread_baseline": {
            "passed": _beats(spread_brier, spread_baseline),
            "actual": spread_brier,
        },
        "funding_calibration_ece": {"passed": _at_most(metrics.get("funding_ece"), 0.05)},
        "spread_calibration_ece": {"passed": _at_most(metrics.get("spread_ece"), 0.05)},
        "walk_forward_folds": {"passed": int(_float(metrics.get("walk_forward_folds")) or 0) >= 3},
        "purged_time_splits": {"passed": metrics.get("purged_time_splits") is True},
        "probability_calibration": {
            "passed": str(metrics.get("calibration_method") or "").casefold()
            in {"isotonic", "platt", "beta"}
        },
        "drift_psi": {"passed": _at_most(metrics.get("drift_psi_max"), 0.20)},
        "drift_monitor": {"passed": metrics.get("drift_monitor_configured") is True},
        "latency": {"passed": _at_most(metrics.get("p95_inference_ms"), 100.0)},
        "deterministic_fallback": {"passed": metrics.get("fallback_tested") is True},
        "shadow_period": {"passed": int(_float(metrics.get("shadow_days")) or 0) >= 14},
    }


def _labels(rows: list[sqlite3.Row], target: str) -> list[int]:
    if target == "funding":
        return [
            int(float(row["funding_positive_fraction"]) >= 0.999)
            for row in rows
            if row["funding_positive_fraction"] is not None
        ]
    return [
        int(float(row["convergence_capture_pct"]) > 0)
        for row in rows
        if row["convergence_capture_pct"] is not None
    ]


def _minority_share(values: list[bool]) -> float:
    if not values:
        return 0.0
    positive = sum(values) / len(values)
    return min(positive, 1.0 - positive)


def _leakage_hits(rows: list[sqlite3.Row]) -> list[str]:
    forbidden = ("future", "outcome", "label", "end_basis", "convergence_capture", "labeled_at")
    hits: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(str(row["feature_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            hits.add("invalid_feature_json")
            continue
        stack: list[tuple[str, Any]] = [("", payload)]
        while stack:
            path, value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    child_path = f"{path}.{key}" if path else str(key)
                    if any(token in str(key).casefold() for token in forbidden):
                        hits.add(child_path)
                    stack.append((child_path, child))
            elif isinstance(value, list):
                stack.extend((path, child) for child in value)
    return sorted(hits)


def _beats(candidate: float | None, baseline: float | None) -> bool:
    return candidate is not None and baseline is not None and candidate <= baseline * 0.95


def _at_most(value: Any, maximum: float) -> bool:
    number = _float(value)
    return number is not None and number <= maximum


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _empty(reason: str) -> dict[str, Any]:
    return {
        "mode": "shadow_only",
        "model_configured": False,
        "activation_allowed": False,
        "data_ready": False,
        "reason": reason,
    }
