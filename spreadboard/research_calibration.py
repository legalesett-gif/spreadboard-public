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

from . import funding_radar, market_history, research_score


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_DB_PATH = RUNTIME_DIR / "spreadboard_research_calibration.sqlite3"
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
    db_path: Path | str = DEFAULT_DB_PATH,
    history_db_path: Path | str = market_history.DEFAULT_DB_PATH,
) -> dict[str, int]:
    """Freeze a bounded hourly set of the strongest public candidates."""
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
            observed_route = {**route, "spread_quote_current": True}
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
            inserted += int(connection.execute("SELECT changes()").fetchone()[0] > 0)
        connection.commit()
    finally:
        connection.close()
    return {"considered": len(ranked), "inserted": inserted}


def label_matured(
    *,
    now: float | None = None,
    limit: int = 240,
    db_path: Path | str = DEFAULT_DB_PATH,
    history_db_path: Path | str = market_history.DEFAULT_DB_PATH,
) -> dict[str, int]:
    moment = time.time() if now is None else float(now)
    now_us = int(moment * 1_000_000)
    initialize(db_path)
    connection = _connect(db_path)
    labeled = 0
    insufficient = 0
    try:
        rows = connection.execute(
            """SELECT o.* FROM score_observations o
               WHERE o.observed_hour_us <= ?
                 AND (
                   NOT EXISTS (SELECT 1 FROM score_outcomes x WHERE x.observation_id=o.id AND x.horizon_hours=8)
                   OR NOT EXISTS (SELECT 1 FROM score_outcomes x WHERE x.observation_id=o.id AND x.horizon_hours=24)
                 )
               ORDER BY o.observed_hour_us, o.id LIMIT ?""",
            (now_us - 8 * 3600 * 1_000_000, max(1, int(limit))),
        ).fetchall()
        for observation in rows:
            for horizon in HORIZONS:
                target = int(observation["observed_hour_us"]) + horizon * 3600 * 1_000_000
                if target > now_us:
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM score_outcomes WHERE observation_id = ? AND horizon_hours = ?",
                    (observation["id"], horizon),
                ).fetchone()
                if exists:
                    continue
                history = market_history.load_history(
                    route_key=str(observation["route_key"]),
                    since_us=int(observation["observed_hour_us"]),
                    max_points=2_000,
                    db_path=history_db_path,
                )
                outcome = _outcome(
                    history,
                    entry_basis=_number(observation["entry_basis_pct"]),
                    target_us=target,
                )
                if outcome is None:
                    insufficient += 1
                    continue
                connection.execute(
                    """INSERT INTO score_outcomes (
                           observation_id, horizon_hours, outcome_ts_us,
                           end_basis_pct, convergence_capture_pct,
                           max_adverse_basis_widening_pct, end_funding_daily_pct,
                           funding_positive_fraction, sample_count, labeled_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        observation["id"], horizon, outcome["outcome_ts_us"],
                        outcome["end_basis_pct"], outcome["convergence_capture_pct"],
                        outcome["max_adverse_basis_widening_pct"],
                        outcome["end_funding_daily_pct"], outcome["funding_positive_fraction"],
                        outcome["sample_count"], _utc_iso(moment),
                    ),
                )
                labeled += 1
        connection.commit()
    finally:
        connection.close()
    return {"labeled": labeled, "insufficient_history": insufficient}


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
