"""Persist short-lived live spread transitions independently of page views."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_PATH = RUNTIME_DIR / "spread_opportunity_journal.sqlite3"
MIN_SPREAD_PCT = float(os.environ.get("SPREADBOARD_OPPORTUNITY_JOURNAL_MIN_SPREAD_PCT", "0.5"))
PEAK_STEP_PCT = float(os.environ.get("SPREADBOARD_OPPORTUNITY_JOURNAL_PEAK_STEP_PCT", "0.1"))


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS opportunity_state (
            route_key TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            route_kind TEXT NOT NULL,
            active INTEGER NOT NULL,
            opened_at_unix REAL NOT NULL,
            last_seen_at_unix REAL NOT NULL,
            last_spread_pct REAL NOT NULL,
            peak_spread_pct REAL NOT NULL,
            quote_ts_us INTEGER,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS opportunity_state_kind_active
            ON opportunity_state(route_kind, active);
        CREATE TABLE IF NOT EXISTS opportunity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_key TEXT NOT NULL,
            token TEXT NOT NULL,
            route_kind TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            observed_at_unix REAL NOT NULL,
            spread_pct REAL NOT NULL,
            quote_ts_us INTEGER,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS opportunity_events_observed
            ON opportunity_events(observed_at_unix);
        """
    )
    return connection


def _event(
    connection: sqlite3.Connection,
    *,
    route: dict[str, Any],
    event_kind: str,
    observed_at: float,
    spread: float,
    payload_json: str,
) -> None:
    connection.execute(
        """INSERT INTO opportunity_events (
               route_key, token, route_kind, event_kind, observed_at_unix,
               spread_pct, quote_ts_us, payload_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(route.get("route_key") or ""),
            str(route.get("token") or "").upper(),
            str(route.get("route_kind") or "").upper(),
            event_kind,
            observed_at,
            spread,
            int(route.get("quote_ts_us") or 0) or None,
            payload_json,
        ),
    )


def record_snapshot(
    rows: Iterable[dict[str, Any]],
    *,
    observed_route_kinds: Iterable[str],
    path: Path | str = DEFAULT_PATH,
    now: float | None = None,
) -> dict[str, int]:
    """Record open/peak/close transitions for one freshly observed route lane."""

    observed_at = time.time() if now is None else float(now)
    kinds = {str(value).strip().upper() for value in observed_route_kinds if str(value).strip()}
    current: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("route_key") or "")
        try:
            spread = float(row.get("spread_pct"))
        except (TypeError, ValueError, OverflowError):
            continue
        if key and str(row.get("route_kind") or "").upper() in kinds and spread >= MIN_SPREAD_PCT:
            current[key] = {**row, "spread_pct": spread}
    opened = peaked = closed = 0
    connection = _connect(Path(path))
    try:
        placeholders = ",".join("?" for _ in kinds)
        active_rows = (
            connection.execute(
                f"""SELECT route_key, peak_spread_pct, last_spread_pct, payload_json
                    FROM opportunity_state
                    WHERE active = 1 AND route_kind IN ({placeholders})""",
                tuple(sorted(kinds)),
            ).fetchall()
            if kinds
            else []
        )
        active = {str(row[0]): row for row in active_rows}
        for key, route in current.items():
            spread = float(route["spread_pct"])
            payload_json = json.dumps(route, sort_keys=True, separators=(",", ":"), default=str)
            previous = active.get(key)
            if previous is None:
                opened += 1
                _event(
                    connection,
                    route=route,
                    event_kind="opened",
                    observed_at=observed_at,
                    spread=spread,
                    payload_json=payload_json,
                )
                opened_at = observed_at
                peak = spread
            else:
                opened_at_row = connection.execute(
                    "SELECT opened_at_unix FROM opportunity_state WHERE route_key = ?", (key,)
                ).fetchone()
                opened_at = float(opened_at_row[0]) if opened_at_row else observed_at
                peak = max(float(previous[1]), spread)
                if spread >= float(previous[1]) + PEAK_STEP_PCT:
                    peaked += 1
                    _event(
                        connection,
                        route=route,
                        event_kind="new_peak",
                        observed_at=observed_at,
                        spread=spread,
                        payload_json=payload_json,
                    )
            connection.execute(
                """INSERT INTO opportunity_state (
                       route_key, token, route_kind, active, opened_at_unix,
                       last_seen_at_unix, last_spread_pct, peak_spread_pct,
                       quote_ts_us, payload_json
                   ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(route_key) DO UPDATE SET
                       token = excluded.token,
                       route_kind = excluded.route_kind,
                       active = 1,
                       last_seen_at_unix = excluded.last_seen_at_unix,
                       last_spread_pct = excluded.last_spread_pct,
                       peak_spread_pct = excluded.peak_spread_pct,
                       quote_ts_us = excluded.quote_ts_us,
                       payload_json = excluded.payload_json""",
                (
                    key,
                    str(route.get("token") or "").upper(),
                    str(route.get("route_kind") or "").upper(),
                    opened_at,
                    observed_at,
                    spread,
                    peak,
                    int(route.get("quote_ts_us") or 0) or None,
                    payload_json,
                ),
            )
        for key, previous in active.items():
            if key in current:
                continue
            closed += 1
            try:
                route = json.loads(str(previous[3]))
            except json.JSONDecodeError:
                route = {"route_key": key, "token": "", "route_kind": ""}
            spread = float(previous[2])
            payload_json = json.dumps(route, sort_keys=True, separators=(",", ":"), default=str)
            _event(
                connection,
                route=route,
                event_kind="closed",
                observed_at=observed_at,
                spread=spread,
                payload_json=payload_json,
            )
            connection.execute(
                "UPDATE opportunity_state SET active = 0, last_seen_at_unix = ? WHERE route_key = ?",
                (observed_at, key),
            )
        connection.execute(
            "DELETE FROM opportunity_events WHERE observed_at_unix < ?",
            (observed_at - 30 * 86400,),
        )
        connection.commit()
    finally:
        connection.close()
    return {"opened": opened, "new_peaks": peaked, "closed": closed, "active": len(current)}


def recent_events(
    *,
    token: str | None = None,
    since_seconds: float = 1800.0,
    limit: int = 50,
    path: Path | str = DEFAULT_PATH,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Read a bounded recent radar trail without claiming it is live now."""

    journal_path = Path(path)
    if not journal_path.exists():
        return []
    observed_at = time.time() if now is None else float(now)
    where = "observed_at_unix >= ?"
    params: list[Any] = [observed_at - max(1.0, float(since_seconds))]
    normalized_token = str(token or "").strip().upper()
    if normalized_token:
        where += " AND token = ?"
        params.append(normalized_token)
    params.append(max(1, min(500, int(limit))))
    connection = sqlite3.connect(journal_path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"""SELECT route_key, token, route_kind, event_kind,
                       observed_at_unix, spread_pct, quote_ts_us, payload_json
                FROM opportunity_events
                WHERE {where}
                ORDER BY observed_at_unix DESC, id DESC LIMIT ?""",
            params,
        ).fetchall()
    finally:
        connection.close()
    output: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            payload = {}
        output.append(
            {
                "route_key": row["route_key"],
                "token": row["token"],
                "route_kind": row["route_kind"],
                "event_kind": row["event_kind"],
                "observed_at_unix": row["observed_at_unix"],
                "spread_pct": row["spread_pct"],
                "quote_ts_us": row["quote_ts_us"],
                "historical_observation_only": True,
                "long_venue": payload.get("long_venue"),
                "short_venue": payload.get("short_venue"),
            }
        )
    return output
