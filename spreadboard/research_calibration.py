"""Versioned shadow outcomes for funding and spread score calibration.

This is data collection, not an ML predictor.  Observations are frozen before
their future labels exist and outcomes are joined only after 8/24 hours, which
prevents look-ahead leakage and makes later walk-forward validation possible.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_DB_PATH = RUNTIME_DIR / "spreadboard_research_calibration.sqlite3"
DEFAULT_HISTORY_DB_PATH = RUNTIME_DIR / "spreadboard_market_history.sqlite3"
HORIZONS = (8, 24)


def initialize(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    connection = _connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS score_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_key TEXT NOT NULL,
                token TEXT NOT NULL,
                observed_hour_us INTEGER NOT NULL,
                method TEXT NOT NULL,
                funding_score REAL,
                spread_score REAL,
                funding_confidence INTEGER NOT NULL,
                spread_confidence INTEGER NOT NULL,
                entry_basis_pct REAL,
                expected_funding_24h_pct REAL,
                funding_regime TEXT NOT NULL,
                cost_status TEXT NOT NULL,
                feature_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(route_key, observed_hour_us, method)
            );
            CREATE TABLE IF NOT EXISTS score_outcomes (
                observation_id INTEGER NOT NULL REFERENCES score_observations(id) ON DELETE CASCADE,
                horizon_hours INTEGER NOT NULL CHECK (horizon_hours IN (8, 24)),
                outcome_ts_us INTEGER NOT NULL,
                end_basis_pct REAL,
                convergence_capture_pct REAL,
                max_adverse_basis_widening_pct REAL,
                end_funding_daily_pct REAL,
                funding_positive_fraction REAL,
                sample_count INTEGER NOT NULL,
                labeled_at TEXT NOT NULL,
                PRIMARY KEY (observation_id, horizon_hours)
            );
            CREATE INDEX IF NOT EXISTS score_observations_maturity
                ON score_observations(observed_hour_us, id);
            CREATE TABLE IF NOT EXISTS score_label_attempts (
                observation_id INTEGER NOT NULL REFERENCES score_observations(id) ON DELETE CASCADE,
                horizon_hours INTEGER NOT NULL CHECK (horizon_hours IN (8, 24)),
                attempt_count INTEGER NOT NULL,
                last_attempt_us INTEGER NOT NULL,
                next_attempt_us INTEGER NOT NULL,
                last_reason TEXT NOT NULL,
                PRIMARY KEY (observation_id, horizon_hours)
            );
            CREATE INDEX IF NOT EXISTS score_label_attempts_retry
                ON score_label_attempts(next_attempt_us, observation_id, horizon_hours);
            """
        )
        connection.commit()
    finally:
        connection.close()


def capture_routes(
    routes: Iterable[dict[str, Any]],
    *,
    now: float | None = None,
    limit: int = 60,
    account_evidence: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
    history_db_path: Path | str = DEFAULT_HISTORY_DB_PATH,
) -> dict[str, int]:
    """Freeze a bounded hourly set of the strongest public candidates."""
    from . import accounts, funding_radar, market_history, research_score

    moment = time.time() if now is None else float(now)
    hour_us = int(moment // 3600 * 3600 * 1_000_000)
    unique: dict[str, dict[str, Any]] = {}
    for route in routes:
        if not isinstance(route, dict):
            continue
        key = str(route.get("route_key") or "")
        if key:
            unique[key] = route
    ranked = sorted(
        unique.values(),
        key=lambda row: max(
            abs(_number(row.get("funding_24h_pct")) or 0.0),
            abs(_number(row.get("funding_projected_24h_pct")) or 0.0),
            abs(_number(row.get("executable_spread_pct")) or 0.0),
        ),
        reverse=True,
    )[: max(1, int(limit))]
    initialize(db_path)
    connection = _connect(db_path)
    inserted = 0
    cost_evidenced = 0
    transfer_evidenced = 0
    contributed = account_evidence or {}
    try:
        for route in ranked:
            key = str(route["route_key"])
            if connection.execute(
                "SELECT 1 FROM score_observations WHERE route_key = ? AND observed_hour_us = ?",
                (key, hour_us),
            ).fetchone():
                continue
            history = market_history.load_history(
                route_key=key,
                since_us=hour_us - 30 * 24 * 3600 * 1_000_000,
                bucket_seconds=3600,
                max_points=750,
                db_path=history_db_path,
            )
            windows = {label: funding_radar.window_value(route, label) for label in ("1d", "7d", "30d")}
            # The observation is evaluated as-of its capture hour. An offline
            # labeling run may execute days later; wall-clock quote freshness
            # must not erase the entry basis from the frozen feature row.
            observed_route = _route_with_account_evidence(
                {**route, "spread_quote_current": True},
                contributed.get(accounts.research_route_signature(route)) or {},
            )
            evaluation = research_score.evaluate(
                observed_route, windows=windows, history=history
            )
            funding = evaluation.get("funding_opportunity") or {}
            spread = evaluation.get("spread_opportunity") or {}
            economics = evaluation.get("route_economics") or {}
            outlook = funding.get("funding_outlook") or {}
            features = {
                "funding_components": funding.get("components") or {},
                "spread_components": spread.get("components") or {},
                "funding_outlook": outlook,
                "risk_data_quality": (evaluation.get("risk_estimate") or {}).get("data_quality") or {},
                "dex_evidence": evaluation.get("dex_evidence") or {},
                "account_cost_evidence": observed_route.get("account_cost_evidence") or {},
                "observed_transfer_evidence": observed_route.get("observed_transfer_evidence") or {},
            }
            connection.execute(
                """INSERT OR IGNORE INTO score_observations (
                       route_key, token, observed_hour_us, method,
                       funding_score, spread_score, funding_confidence,
                       spread_confidence, entry_basis_pct,
                       expected_funding_24h_pct, funding_regime, cost_status,
                       feature_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key, str(route.get("token") or "").upper(), hour_us,
                    str(evaluation.get("method") or "unknown"),
                    _number(funding.get("score")), _number(spread.get("score")),
                    int(funding.get("confidence") or 0), int(spread.get("confidence") or 0),
                    _number(spread.get("entry_spread_pct")),
                    _number(outlook.get("expected_24h_pct")),
                    str(outlook.get("regime") or "unavailable"),
                    str(economics.get("cost_status") or "unknown"),
                    json.dumps(features, sort_keys=True, separators=(",", ":")),
                    _utc_iso(moment),
                ),
            )
            was_inserted = int(connection.execute("SELECT changes()").fetchone()[0] > 0)
            inserted += was_inserted
            if was_inserted and observed_route.get("account_cost_evidence"):
                cost_evidenced += 1
            if was_inserted and observed_route.get("observed_transfer_evidence"):
                transfer_evidenced += 1
        connection.commit()
    finally:
        connection.close()
    return {
        "considered": len(ranked),
        "inserted": inserted,
        "cost_evidenced": cost_evidenced,
        "transfer_evidenced": transfer_evidenced,
    }


def _route_with_account_evidence(
    route: dict[str, Any], evidence: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Add anonymous historical evidence without overriding current rail state."""

    output = dict(route)
    long_type = str(route.get("long_market_type") or "").strip().casefold()
    short_type = str(route.get("short_market_type") or "").strip().casefold()
    is_dex = "dex" in {long_type, short_type} or "DEX" in str(route.get("route_kind") or "").upper()
    chain = str(route.get("dex_chain") or "").strip().casefold()
    contract = str(route.get("dex_contract") or "").strip().casefold()

    def matching(items: Any) -> dict[str, Any] | None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            item_chain = str(item.get("chain") or "").strip().casefold()
            item_contract = str(item.get("contract") or "").strip().casefold()
            if is_dex:
                if chain and contract and item_chain == chain and item_contract == contract:
                    return item
            elif not item_chain and not item_contract:
                return item
        return None

    cost = matching(evidence.get("costs"))
    if cost is not None:
        value = _number(cost.get("round_trip_cost_pct"))
        if value is not None and value >= 0:
            output["known_round_trip_cost_pct"] = value
            output["account_cost_evidence"] = {
                "source": "opt_in_completed_positions",
                "sample_count": int(cost.get("sample_count") or 0),
                "scope": "median_route_percentage_only",
                "includes": cost.get("includes"),
                "identity_match": "exact_chain_contract" if is_dex else "exact_route",
            }
    transfer = matching(evidence.get("transfers"))
    if transfer is not None and bool(route.get("requires_transfer")):
        value = _number(transfer.get("transfer_time_seconds"))
        if value is not None and value >= 0:
            output["dex_transfer_time_seconds"] = value
            output["dex_transfer_time_source"] = "opt_in_observed_median"
            output["observed_transfer_evidence"] = {
                "source": "opt_in_successful_transfers",
                "sample_count": int(transfer.get("sample_count") or 0),
                "identity_match": "exact_chain_contract",
                "does_not_override_current_rails": True,
            }
    return output


def label_matured(
    *,
    now: float | None = None,
    limit: int = 240,
    db_path: Path | str = DEFAULT_DB_PATH,
    history_db_path: Path | str = DEFAULT_HISTORY_DB_PATH,
) -> dict[str, int]:
    from . import market_history

    moment = time.time() if now is None else float(now)
    now_us = int(moment * 1_000_000)
    initialize(db_path)
    connection = _connect(db_path)
    labeled = 0
    insufficient = 0
    try:
        rows = connection.execute(
            """WITH horizons(horizon_hours) AS (VALUES (8), (24))
               SELECT o.*, h.horizon_hours,
                      COALESCE(a.attempt_count, 0) AS label_attempt_count
               FROM score_observations o
               CROSS JOIN horizons h
               LEFT JOIN score_outcomes x
                 ON x.observation_id = o.id AND x.horizon_hours = h.horizon_hours
               LEFT JOIN score_label_attempts a
                 ON a.observation_id = o.id AND a.horizon_hours = h.horizon_hours
               WHERE x.observation_id IS NULL
                 AND o.observed_hour_us + h.horizon_hours * 3600000000 <= ?
                 AND (a.next_attempt_us IS NULL OR a.next_attempt_us <= ?)
               ORDER BY COALESCE(a.next_attempt_us, 0), o.observed_hour_us,
                        o.id, h.horizon_hours
               LIMIT ?""",
            (now_us, now_us, max(1, int(limit))),
        ).fetchall()
        history_cache: dict[int, list[dict[str, Any]]] = {}
        for observation in rows:
            observation_id = int(observation["id"])
            horizon = int(observation["horizon_hours"])
            target = int(observation["observed_hour_us"]) + horizon * 3600 * 1_000_000
            if observation_id not in history_cache:
                history_cache[observation_id] = market_history.load_history(
                    route_key=str(observation["route_key"]),
                    since_us=int(observation["observed_hour_us"]),
                    max_points=2_000,
                    db_path=history_db_path,
                )
            outcome = _outcome(
                history_cache[observation_id],
                entry_basis=_number(observation["entry_basis_pct"]),
                target_us=target,
            )
            if outcome is None:
                insufficient += 1
                attempts = int(observation["label_attempt_count"] or 0) + 1
                retry_seconds = min(24 * 3600, 15 * 60 * (2 ** min(7, attempts - 1)))
                connection.execute(
                    """INSERT INTO score_label_attempts (
                           observation_id, horizon_hours, attempt_count,
                           last_attempt_us, next_attempt_us, last_reason
                       ) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(observation_id, horizon_hours) DO UPDATE SET
                           attempt_count = excluded.attempt_count,
                           last_attempt_us = excluded.last_attempt_us,
                           next_attempt_us = excluded.next_attempt_us,
                           last_reason = excluded.last_reason""",
                    (
                        observation_id,
                        horizon,
                        attempts,
                        now_us,
                        now_us + retry_seconds * 1_000_000,
                        "history_missing_or_target_gap",
                    ),
                )
                continue
            connection.execute(
                """INSERT INTO score_outcomes (
                       observation_id, horizon_hours, outcome_ts_us,
                       end_basis_pct, convergence_capture_pct,
                       max_adverse_basis_widening_pct, end_funding_daily_pct,
                       funding_positive_fraction, sample_count, labeled_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation_id, horizon, outcome["outcome_ts_us"],
                    outcome["end_basis_pct"], outcome["convergence_capture_pct"],
                    outcome["max_adverse_basis_widening_pct"],
                    outcome["end_funding_daily_pct"], outcome["funding_positive_fraction"],
                    outcome["sample_count"], _utc_iso(moment),
                ),
            )
            connection.execute(
                "DELETE FROM score_label_attempts WHERE observation_id = ? AND horizon_hours = ?",
                (observation_id, horizon),
            )
            labeled += 1
        connection.commit()
    finally:
        connection.close()
    return {
        "attempted": len(rows),
        "labeled": labeled,
        "insufficient_history": insufficient,
    }


def status(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {"initialized": False, "observations": 0, "outcomes": 0, "ml_ready": False}
    initialize(path)
    connection = _connect(path)
    try:
        observations = int(connection.execute("SELECT COUNT(*) FROM score_observations").fetchone()[0])
        outcomes = int(connection.execute("SELECT COUNT(*) FROM score_outcomes").fetchone()[0])
        routes = int(connection.execute("SELECT COUNT(DISTINCT route_key) FROM score_observations").fetchone()[0])
        versions = [row[0] for row in connection.execute("SELECT DISTINCT method FROM score_observations ORDER BY method")]
    finally:
        connection.close()
    return {
        "initialized": True,
        "observations": observations,
        "outcomes": outcomes,
        "routes": routes,
        "method_versions": versions,
        "ml_ready": outcomes >= 5_000 and routes >= 100,
        "ml_minimum_gate": {"outcomes": 5_000, "routes": 100},
        "mode": "deterministic_shadow_calibration_only",
    }


def _outcome(history: list[dict[str, Any]], *, entry_basis: float | None, target_us: int) -> dict[str, Any] | None:
    candidates = [row for row in history if _timestamp(row) <= target_us + 90 * 60 * 1_000_000]
    if entry_basis is None or not candidates:
        return None
    end = min(candidates, key=lambda row: abs(_timestamp(row) - target_us))
    if abs(_timestamp(end) - target_us) > 90 * 60 * 1_000_000:
        return None
    in_window = [row for row in candidates if _timestamp(row) >= target_us - 24 * 3600 * 1_000_000]
    bases = [value for row in in_window if (value := _basis(row)) is not None]
    end_basis = _basis(end)
    if end_basis is None:
        return None
    direction = 1.0 if entry_basis >= 0 else -1.0
    funding = [value for row in in_window if (value := _number(row.get("funding_daily_pct"))) is not None]
    return {
        "outcome_ts_us": _timestamp(end),
        "end_basis_pct": end_basis,
        "convergence_capture_pct": direction * (entry_basis - end_basis),
        "max_adverse_basis_widening_pct": max([0.0, *(direction * (basis - entry_basis) for basis in bases)]),
        "end_funding_daily_pct": _number(end.get("funding_daily_pct")),
        "funding_positive_fraction": sum(value > 0 for value in funding) / len(funding) if funding else None,
        "sample_count": len(in_window),
    }


def _connect(path: Path | str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _timestamp(row: dict[str, Any]) -> int:
    try:
        return int(row.get("quote_ts_us") or 0)
    except (TypeError, ValueError):
        return 0


def _basis(row: dict[str, Any]) -> float | None:
    depth_weighted = _number(row.get("depth_weighted_spread_pct"))
    return (
        depth_weighted
        if depth_weighted is not None
        else _number(row.get("executable_spread_pct"))
    )


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _utc_iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, tz=timezone.utc).replace(microsecond=0).isoformat()
