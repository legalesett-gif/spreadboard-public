"""Leakage-safe daily market features reconstructed from public route history.

This dataset is deliberately separate from the live, versioned research-score
observations.  Historical ``route_points`` do not preserve identity, rail or
exact account-cost evidence, so this backfill is CEX-only and may support only
public-market funding/convergence experiments.  It never satisfies the exact-
cost gate and can never activate a model by itself.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import market_history, research_calibration

METHOD = "market_core_daily_v1"
FEATURE_SCHEMA = "past_24h_public_cex_v1"
SELECTION_POLICY = "past_only_signal_top2_per_token_v1"
DAY_US = 86_400_000_000
HOUR_US = 3_600_000_000
LOOKBACK_HOURS = 24
HORIZON_HOURS = 24
DEFAULT_DAILY_LIMIT = 400
DEFAULT_PER_TOKEN_LIMIT = 2
MIN_FEATURE_SAMPLES = 12
MIN_OUTCOME_SAMPLES = 12
MAX_TARGET_GAP_US = 90 * 60 * 1_000_000
MAX_WINDOW_EDGE_GAP_US = 3 * HOUR_US
MAX_INTERNAL_GAP_HOURS = 6.0


def initialize(db_path: Path | str = research_calibration.DEFAULT_DB_PATH) -> None:
    connection = research_calibration._connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_core_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_key TEXT NOT NULL,
                token TEXT NOT NULL,
                observed_day_us INTEGER NOT NULL,
                method TEXT NOT NULL,
                feature_schema TEXT NOT NULL,
                selection_policy TEXT NOT NULL,
                route_kind TEXT NOT NULL,
                feature_start_us INTEGER NOT NULL,
                feature_end_us INTEGER NOT NULL,
                feature_sample_count INTEGER NOT NULL,
                historical_identity_status TEXT NOT NULL,
                cost_scope TEXT NOT NULL,
                feature_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(route_key, observed_day_us, method)
            );
            CREATE TABLE IF NOT EXISTS market_core_outcomes (
                observation_id INTEGER NOT NULL
                    REFERENCES market_core_observations(id) ON DELETE CASCADE,
                horizon_hours INTEGER NOT NULL CHECK (horizon_hours = 24),
                outcome_ts_us INTEGER NOT NULL,
                end_basis_pct REAL NOT NULL,
                convergence_capture_pct REAL NOT NULL,
                max_adverse_basis_widening_pct REAL NOT NULL,
                end_funding_daily_pct REAL,
                mean_funding_daily_pct REAL,
                funding_positive_fraction REAL,
                long_max_adverse_move_pct REAL,
                short_max_adverse_move_pct REAL,
                sample_count INTEGER NOT NULL,
                labeled_at TEXT NOT NULL,
                PRIMARY KEY (observation_id, horizon_hours)
            );
            CREATE INDEX IF NOT EXISTS market_core_observations_day
                ON market_core_observations(method, observed_day_us, id);
            CREATE TABLE IF NOT EXISTS market_core_days (
                method TEXT NOT NULL,
                observed_day_us INTEGER NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (method, observed_day_us)
            );
            CREATE TABLE IF NOT EXISTS market_core_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                method TEXT NOT NULL,
                source_first_us INTEGER,
                source_last_us INTEGER,
                first_day_us INTEGER,
                last_day_us INTEGER,
                days_processed INTEGER NOT NULL,
                observations_inserted INTEGER NOT NULL,
                outcomes_inserted INTEGER NOT NULL,
                skipped_missing_outcome INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def backfill(
    *,
    db_path: Path | str = research_calibration.DEFAULT_DB_PATH,
    history_db_path: Path | str = market_history.DEFAULT_DB_PATH,
    daily_limit: int = DEFAULT_DAILY_LIMIT,
    per_token_limit: int = DEFAULT_PER_TOKEN_LIMIT,
    max_days: int | None = None,
    start_us: int | None = None,
    end_us: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Materialize missing daily observations using only information known then."""

    history_path = Path(history_db_path)
    if not history_path.exists():
        return {"status": "history_database_missing", **status(db_path)}
    initialize(db_path)
    history = market_history._connect_readonly(history_path)
    output = research_calibration._connect(db_path)
    days_processed = 0
    observations_inserted = 0
    outcomes_inserted = 0
    skipped_missing_outcome = 0
    processed_days: list[int] = []
    try:
        bounds = history.execute(
            "SELECT MIN(quote_ts_us), MAX(quote_ts_us) FROM route_points"
        ).fetchone()
        source_first = int(bounds[0] or 0)
        source_last = int(bounds[1] or 0)
        if not source_first or not source_last:
            return {"status": "history_database_empty", **status(db_path)}
        first_eligible = _ceil_day(source_first + LOOKBACK_HOURS * HOUR_US)
        last_eligible = _floor_day(source_last - HORIZON_HOURS * HOUR_US)
        if start_us is not None:
            first_eligible = max(first_eligible, _ceil_day(int(start_us)))
        if end_us is not None:
            last_eligible = min(last_eligible, _floor_day(int(end_us)))
        existing = {
            int(row[0])
            for row in output.execute(
                "SELECT observed_day_us FROM market_core_days WHERE method = ?",
                (METHOD,),
            )
        }
        days = [
            day for day in range(first_eligible, last_eligible + 1, DAY_US) if day not in existing
        ]
        if max_days is not None:
            days = days[: max(0, int(max_days))]
        for observed_day_us in days:
            selected = _select_candidates(
                history,
                observed_day_us=observed_day_us,
                daily_limit=max(1, int(daily_limit)),
                per_token_limit=max(1, int(per_token_limit)),
            )
            if not selected:
                output.execute(
                    "INSERT OR IGNORE INTO market_core_days VALUES (?, ?, ?)",
                    (
                        METHOD,
                        observed_day_us,
                        _utc_iso(time.time() if now is None else float(now)),
                    ),
                )
                output.commit()
                processed_days.append(observed_day_us)
                days_processed += 1
                continue
            keys = [str(row["route_key"]) for row in selected]
            past = _load_route_rows(
                history,
                keys,
                after_us=observed_day_us - LOOKBACK_HOURS * HOUR_US,
                through_us=observed_day_us,
            )
            after = _load_route_rows(
                history,
                keys,
                after_us=observed_day_us,
                through_us=observed_day_us + HORIZON_HOURS * HOUR_US + MAX_TARGET_GAP_US,
            )
            past_by_route = _hourly_by_route(past)
            after_by_route = _hourly_by_route(after)
            for candidate in selected:
                key = str(candidate["route_key"])
                features = _features(
                    past_by_route.get(key, []),
                    observed_day_us=observed_day_us,
                    selection_score=_number(candidate.get("selection_score")),
                )
                if features is None:
                    continue
                output.execute(
                    """INSERT OR IGNORE INTO market_core_observations (
                           route_key, token, observed_day_us, method,
                           feature_schema, selection_policy, route_kind,
                           feature_start_us, feature_end_us, feature_sample_count,
                           historical_identity_status, cost_scope, feature_json,
                           created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key,
                        str(candidate.get("token") or "").upper(),
                        observed_day_us,
                        METHOD,
                        FEATURE_SCHEMA,
                        SELECTION_POLICY,
                        str(candidate.get("route_kind") or "UNKNOWN").upper(),
                        int(features["feature_start_us"]),
                        observed_day_us,
                        int(features["sample_count"]),
                        "not_preserved_in_historical_source_cex_only",
                        "public_market_only_no_account_costs",
                        json.dumps(features, sort_keys=True, separators=(",", ":")),
                        _utc_iso(time.time() if now is None else float(now)),
                    ),
                )
                inserted = int(output.execute("SELECT changes()").fetchone()[0] > 0)
                observations_inserted += inserted
                observation = output.execute(
                    """SELECT id FROM market_core_observations
                       WHERE route_key = ? AND observed_day_us = ? AND method = ?""",
                    (key, observed_day_us, METHOD),
                ).fetchone()
                outcome = _outcome(
                    after_by_route.get(key, []),
                    observed_day_us=observed_day_us,
                    entry_basis=_number(features.get("entry_basis_pct")),
                    entry_long_price=_number(features.get("entry_long_price")),
                    entry_short_price=_number(features.get("entry_short_price")),
                )
                if outcome is None:
                    skipped_missing_outcome += 1
                    continue
                output.execute(
                    """INSERT OR IGNORE INTO market_core_outcomes (
                           observation_id, horizon_hours, outcome_ts_us,
                           end_basis_pct, convergence_capture_pct,
                           max_adverse_basis_widening_pct,
                           end_funding_daily_pct, mean_funding_daily_pct,
                           funding_positive_fraction, long_max_adverse_move_pct,
                           short_max_adverse_move_pct, sample_count, labeled_at
                       ) VALUES (?, 24, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(observation["id"]),
                        int(outcome["outcome_ts_us"]),
                        outcome["end_basis_pct"],
                        outcome["convergence_capture_pct"],
                        outcome["max_adverse_basis_widening_pct"],
                        outcome["end_funding_daily_pct"],
                        outcome["mean_funding_daily_pct"],
                        outcome["funding_positive_fraction"],
                        outcome["long_max_adverse_move_pct"],
                        outcome["short_max_adverse_move_pct"],
                        int(outcome["sample_count"]),
                        _utc_iso(time.time() if now is None else float(now)),
                    ),
                )
                outcomes_inserted += int(output.execute("SELECT changes()").fetchone()[0] > 0)
            processed_days.append(observed_day_us)
            days_processed += 1
            output.execute(
                "INSERT OR IGNORE INTO market_core_days VALUES (?, ?, ?)",
                (
                    METHOD,
                    observed_day_us,
                    _utc_iso(time.time() if now is None else float(now)),
                ),
            )
            output.commit()
        output.execute(
            """INSERT INTO market_core_runs (
                   method, source_first_us, source_last_us, first_day_us,
                   last_day_us, days_processed, observations_inserted,
                   outcomes_inserted, skipped_missing_outcome, completed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                METHOD,
                source_first,
                source_last,
                min(processed_days) if processed_days else None,
                max(processed_days) if processed_days else None,
                days_processed,
                observations_inserted,
                outcomes_inserted,
                skipped_missing_outcome,
                _utc_iso(time.time() if now is None else float(now)),
            ),
        )
        output.commit()
    finally:
        history.close()
        output.close()
    return {
        "status": "ok",
        "days_processed": days_processed,
        "observations_inserted": observations_inserted,
        "outcomes_inserted": outcomes_inserted,
        "skipped_missing_outcome": skipped_missing_outcome,
        **status(db_path),
    }


def status(db_path: Path | str = research_calibration.DEFAULT_DB_PATH) -> dict[str, Any]:
    path = Path(db_path)
    base = {
        "method": METHOD,
        "feature_schema": FEATURE_SCHEMA,
        "selection_policy": SELECTION_POLICY,
        "activation_eligible": False,
        "activation_exclusion": "public_market_only_no_identity_or_exact_account_costs",
    }
    if not path.exists():
        return {**base, "initialized": False, "observations": 0, "outcomes": 0}
    initialize(path)
    connection = research_calibration._connect(path)
    try:
        row = connection.execute(
            """SELECT COUNT(*) observations, COUNT(DISTINCT route_key) routes,
                      COUNT(DISTINCT token) tokens, MIN(observed_day_us) first_us,
                      MAX(observed_day_us) last_us
               FROM market_core_observations WHERE method = ?""",
            (METHOD,),
        ).fetchone()
        outcomes = int(
            connection.execute(
                """SELECT COUNT(*) FROM market_core_outcomes x
                   JOIN market_core_observations o ON o.id=x.observation_id
                   WHERE o.method = ?""",
                (METHOD,),
            ).fetchone()[0]
        )
        dex_rows = int(
            connection.execute(
                """SELECT COUNT(*) FROM market_core_observations
                   WHERE method = ? AND route_kind LIKE 'DEX-%'""",
                (METHOD,),
            ).fetchone()[0]
        )
        feature_rows = connection.execute(
            "SELECT feature_json FROM market_core_observations WHERE method = ?",
            (METHOD,),
        ).fetchall()
    finally:
        connection.close()
    leakage_hits = _feature_key_leakage_hits(feature_rows)
    first_us = int(row["first_us"] or 0)
    last_us = int(row["last_us"] or 0)
    return {
        **base,
        "initialized": True,
        "observations": int(row["observations"] or 0),
        "outcomes": outcomes,
        "routes": int(row["routes"] or 0),
        "tokens": int(row["tokens"] or 0),
        "span_days": round((last_us - first_us) / DAY_US, 2) if first_us and last_us else 0.0,
        "first_observed_at": _utc_iso(first_us / 1_000_000) if first_us else None,
        "last_observed_at": _utc_iso(last_us / 1_000_000) if last_us else None,
        "dex_rows": dex_rows,
        "feature_leakage_scan": {
            "passed": not leakage_hits,
            "hits": leakage_hits[:20],
        },
    }


def _select_candidates(
    connection: sqlite3.Connection,
    *,
    observed_day_us: int,
    daily_limit: int,
    per_token_limit: int,
) -> list[dict[str, Any]]:
    start_us = observed_day_us - LOOKBACK_HOURS * HOUR_US
    pool_limit = max(daily_limit * 10, daily_limit)
    rows = connection.execute(
        """WITH sampled AS (
               SELECT route_key, quote_ts_us, token, route_kind,
                      long_venue, long_market_type, short_venue,
                      short_market_type, executable_spread_pct,
                      depth_weighted_spread_pct, funding_daily_pct,
                      ROW_NUMBER() OVER (
                          PARTITION BY route_key, CAST(quote_ts_us / ? AS INTEGER)
                          ORDER BY quote_ts_us DESC
                      ) AS sample_rank
               FROM route_points
               WHERE quote_ts_us > ? AND quote_ts_us <= ?
                 AND route_kind NOT LIKE 'DEX-%'
           )
           SELECT route_key, MAX(token) token, MAX(route_kind) route_kind,
                  MAX(long_venue) long_venue,
                  MAX(long_market_type) long_market_type,
                  MAX(short_venue) short_venue,
                  MAX(short_market_type) short_market_type,
                  COUNT(*) sample_count,
                  MAX(MAX(
                      ABS(COALESCE(depth_weighted_spread_pct,
                                   executable_spread_pct, 0)),
                      ABS(COALESCE(funding_daily_pct, 0))
                  )) selection_score
           FROM sampled
           WHERE sample_rank = 1
           GROUP BY route_key
           HAVING COUNT(*) >= ?
           ORDER BY selection_score DESC, route_key
           LIMIT ?""",
        (HOUR_US, start_us, observed_day_us, MIN_FEATURE_SAMPLES, pool_limit),
    ).fetchall()
    selected: list[dict[str, Any]] = []
    per_token: defaultdict[str, int] = defaultdict(int)
    for item in rows:
        row = dict(item)
        token = str(row.get("token") or "").upper()
        if not token or per_token[token] >= per_token_limit:
            continue
        selected.append(row)
        per_token[token] += 1
        if len(selected) >= daily_limit:
            break
    return selected


def _load_route_rows(
    connection: sqlite3.Connection,
    keys: list[str],
    *,
    after_us: int,
    through_us: int,
) -> list[dict[str, Any]]:
    if not keys:
        return []
    placeholders = ",".join("?" for _ in keys)
    rows = connection.execute(
        f"""SELECT route_key, quote_ts_us, token, route_kind,
                   long_venue, long_market_type, short_venue, short_market_type,
                   executable_spread_pct, depth_weighted_spread_pct,
                   funding_daily_pct, long_price, short_price,
                   target_notional_usd
            FROM route_points
            WHERE route_key IN ({placeholders})
              AND quote_ts_us > ? AND quote_ts_us <= ?
            ORDER BY route_key, quote_ts_us""",
        [*keys, int(after_us), int(through_us)],
    ).fetchall()
    return [dict(row) for row in rows]


def _hourly_by_route(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("route_key") or "")
        timestamp = _timestamp(row)
        if key and timestamp:
            buckets[(key, timestamp // HOUR_US)] = row
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for (key, _), row in buckets.items():
        grouped[key].append(row)
    for values in grouped.values():
        values.sort(key=_timestamp)
    return dict(grouped)


def _features(
    rows: list[dict[str, Any]],
    *,
    observed_day_us: int,
    selection_score: float | None,
) -> dict[str, Any] | None:
    rows = sorted((row for row in rows if _timestamp(row) <= observed_day_us), key=_timestamp)
    if len(rows) < MIN_FEATURE_SAMPLES:
        return None
    latest = rows[-1]
    basis = [_basis(row) for row in rows]
    basis_values = [value for value in basis if value is not None]
    funding_values = [
        value for row in rows if (value := _number(row.get("funding_daily_pct"))) is not None
    ]
    entry_basis = _basis(latest)
    if entry_basis is None or len(basis_values) < MIN_FEATURE_SAMPLES:
        return None
    timestamps = [_timestamp(row) for row in rows]
    gaps = [(right - left) / HOUR_US for left, right in pairwise(timestamps) if right > left]
    if (
        observed_day_us - timestamps[-1] > MAX_TARGET_GAP_US
        or timestamps[0] - (observed_day_us - LOOKBACK_HOURS * HOUR_US) > MAX_WINDOW_EDGE_GAP_US
        or not gaps
        or max(gaps) > MAX_INTERNAL_GAP_HOURS
    ):
        return None
    long_prices = [_number(row.get("long_price")) for row in rows]
    short_prices = [_number(row.get("short_price")) for row in rows]
    long_returns = _normalized_log_returns(timestamps, long_prices)
    short_returns = _normalized_log_returns(timestamps, short_prices)
    paired_returns = _paired_normalized_log_returns(timestamps, long_prices, short_prices)
    notionals = [
        value for row in rows if (value := _number(row.get("target_notional_usd"))) is not None
    ]
    return {
        "as_of_us": observed_day_us,
        "feature_start_us": timestamps[0],
        "lookback_hours": LOOKBACK_HOURS,
        "sample_count": len(rows),
        "coverage_fraction": round(min(1.0, len(rows) / LOOKBACK_HOURS), 6),
        "max_sample_gap_hours": round(max(gaps), 6) if gaps else None,
        "route_kind": str(latest.get("route_kind") or "UNKNOWN").upper(),
        "long_venue": latest.get("long_venue"),
        "long_market_type": latest.get("long_market_type"),
        "short_venue": latest.get("short_venue"),
        "short_market_type": latest.get("short_market_type"),
        "entry_basis_pct": entry_basis,
        "basis_24h_mean_pct": _mean(basis_values),
        "basis_24h_min_pct": min(basis_values),
        "basis_24h_max_pct": max(basis_values),
        "basis_24h_std_pct": _pstdev(basis_values),
        "current_funding_daily_pct": _number(latest.get("funding_daily_pct")),
        "funding_24h_mean_daily_pct": _mean(funding_values),
        "funding_24h_min_daily_pct": min(funding_values) if funding_values else None,
        "funding_24h_max_daily_pct": max(funding_values) if funding_values else None,
        "funding_24h_std_daily_pct": _pstdev(funding_values),
        "funding_24h_positive_fraction": (
            sum(value > 0 for value in funding_values) / len(funding_values)
            if funding_values
            else None
        ),
        "funding_sample_count": len(funding_values),
        "entry_long_price": _number(latest.get("long_price")),
        "entry_short_price": _number(latest.get("short_price")),
        "long_realized_volatility_24h_pct": _realized_volatility(long_returns),
        "short_realized_volatility_24h_pct": _realized_volatility(short_returns),
        "leg_return_correlation_24h": _correlation(paired_returns),
        "long_past_drawdown_pct": _past_drawdown(long_prices),
        "short_past_rise_pct": _past_rise(short_prices),
        "target_notional_usd": statistics.median(notionals) if notionals else None,
        "selection_score": selection_score,
        "historical_identity_present": False,
        "exact_account_costs_present": False,
    }


def _outcome(
    rows: list[dict[str, Any]],
    *,
    observed_day_us: int,
    entry_basis: float | None,
    entry_long_price: float | None,
    entry_short_price: float | None,
) -> dict[str, Any] | None:
    target_us = observed_day_us + HORIZON_HOURS * HOUR_US
    candidates = [
        row for row in rows if observed_day_us < _timestamp(row) <= target_us + MAX_TARGET_GAP_US
    ]
    if entry_basis is None or not candidates:
        return None
    end = min(candidates, key=lambda row: abs(_timestamp(row) - target_us))
    if abs(_timestamp(end) - target_us) > MAX_TARGET_GAP_US:
        return None
    end_basis = _basis(end)
    if end_basis is None:
        return None
    in_window = [row for row in candidates if _timestamp(row) <= _timestamp(end)]
    timestamps = [_timestamp(row) for row in in_window]
    gaps = [(right - left) / HOUR_US for left, right in pairwise(timestamps) if right > left]
    if (
        len(in_window) < MIN_OUTCOME_SAMPLES
        or timestamps[0] - observed_day_us > MAX_WINDOW_EDGE_GAP_US
        or not gaps
        or max(gaps) > MAX_INTERNAL_GAP_HOURS
    ):
        return None
    bases = [value for row in in_window if (value := _basis(row)) is not None]
    funding = [
        value for row in in_window if (value := _number(row.get("funding_daily_pct"))) is not None
    ]
    direction = 1.0 if entry_basis >= 0 else -1.0
    long_prices = [
        value for row in in_window if (value := _number(row.get("long_price"))) is not None
    ]
    short_prices = [
        value for row in in_window if (value := _number(row.get("short_price"))) is not None
    ]
    return {
        "outcome_ts_us": _timestamp(end),
        "end_basis_pct": end_basis,
        "convergence_capture_pct": direction * (entry_basis - end_basis),
        "max_adverse_basis_widening_pct": max(
            [0.0, *(direction * (value - entry_basis) for value in bases)]
        ),
        "end_funding_daily_pct": _number(end.get("funding_daily_pct")),
        "mean_funding_daily_pct": _mean(funding),
        "funding_positive_fraction": (
            sum(value > 0 for value in funding) / len(funding) if funding else None
        ),
        "long_max_adverse_move_pct": _long_adverse(entry_long_price, long_prices),
        "short_max_adverse_move_pct": _short_adverse(entry_short_price, short_prices),
        "sample_count": len(in_window),
    }


def _normalized_log_returns(timestamps: list[int], prices: list[float | None]) -> list[float]:
    output: list[float] = []
    for left_ts, right_ts, left, right in zip(
        timestamps, timestamps[1:], prices, prices[1:], strict=False
    ):
        hours = (right_ts - left_ts) / HOUR_US
        if left and right and left > 0 and right > 0 and 0 < hours <= 6:
            output.append(math.log(right / left) / math.sqrt(hours))
    return output


def _paired_normalized_log_returns(
    timestamps: list[int], long_prices: list[float | None], short_prices: list[float | None]
) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    for left_ts, right_ts, long_left, long_right, short_left, short_right in zip(
        timestamps,
        timestamps[1:],
        long_prices,
        long_prices[1:],
        short_prices,
        short_prices[1:],
        strict=False,
    ):
        hours = (right_ts - left_ts) / HOUR_US
        if (
            long_left
            and long_right
            and short_left
            and short_right
            and min(long_left, long_right, short_left, short_right) > 0
            and 0 < hours <= 6
        ):
            output.append(
                (
                    math.log(long_right / long_left) / math.sqrt(hours),
                    math.log(short_right / short_left) / math.sqrt(hours),
                )
            )
    return output


def _realized_volatility(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    return statistics.pstdev(returns) * math.sqrt(24) * 100


def _correlation(values: list[tuple[float, float]]) -> float | None:
    if len(values) < 3:
        return None
    left = [item[0] for item in values]
    right = [item[1] for item in values]
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    covariance = statistics.fmean((x - left_mean) * (y - right_mean) for x, y in values)
    denominator = statistics.pstdev(left) * statistics.pstdev(right)
    return covariance / denominator if denominator > 0 else None


def _past_drawdown(prices: list[float | None]) -> float | None:
    peak: float | None = None
    worst = 0.0
    found = False
    for price in prices:
        if price is None or price <= 0:
            continue
        found = True
        peak = price if peak is None else max(peak, price)
        worst = max(worst, (peak - price) / peak * 100)
    return worst if found else None


def _past_rise(prices: list[float | None]) -> float | None:
    trough: float | None = None
    worst = 0.0
    found = False
    for price in prices:
        if price is None or price <= 0:
            continue
        found = True
        trough = price if trough is None else min(trough, price)
        worst = max(worst, (price - trough) / trough * 100)
    return worst if found else None


def _long_adverse(entry: float | None, prices: list[float]) -> float | None:
    if entry is None or entry <= 0 or not prices:
        return None
    return max(0.0, (entry - min(prices)) / entry * 100)


def _short_adverse(entry: float | None, prices: list[float]) -> float | None:
    if entry is None or entry <= 0 or not prices:
        return None
    return max(0.0, (max(prices) - entry) / entry * 100)


def _basis(row: dict[str, Any]) -> float | None:
    depth_weighted = _number(row.get("depth_weighted_spread_pct"))
    return (
        depth_weighted if depth_weighted is not None else _number(row.get("executable_spread_pct"))
    )


def _timestamp(row: dict[str, Any]) -> int:
    try:
        return int(row.get("quote_ts_us") or 0)
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _feature_key_leakage_hits(rows: Iterable[Any]) -> list[str]:
    forbidden = (
        "future",
        "outcome",
        "label",
        "end_basis",
        "convergence_capture",
        "labeled_at",
    )
    hits: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(str(row["feature_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
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


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _pstdev(values: list[float]) -> float | None:
    return statistics.pstdev(values) if len(values) >= 2 else None


def _ceil_day(timestamp_us: int) -> int:
    return ((int(timestamp_us) + DAY_US - 1) // DAY_US) * DAY_US


def _floor_day(timestamp_us: int) -> int:
    return int(timestamp_us) // DAY_US * DAY_US


def _utc_iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, tz=UTC).replace(microsecond=0).isoformat()
