"""Compact persistent history for canonical public-API spread routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_DB_PATH = RUNTIME_DIR / "spreadboard_market_history.sqlite3"


def record_snapshot(
    snapshot: dict[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    retention_days: int = 30,
) -> int:
    rows = [
        row
        for bucket in ("api_discovered_rows", "dex_discovered_rows")
        for row in (snapshot.get(bucket) or [])
        if isinstance(row, dict)
    ]
    if not rows:
        return 0
    connection = _connect(db_path)
    inserted = 0
    try:
        for row in rows:
            route_key = route_key_for(row)
            quote_ts_us = _int_or_none(row.get("quote_ts_us"))
            if not route_key or quote_ts_us is None:
                continue
            notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
            route_inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
            funding = notes.get("funding") if isinstance(notes.get("funding"), dict) else {}
            long_bid = _route_price(route_inputs, "long", "bid_vwap", "bid")
            long_ask = _route_price(route_inputs, "long", "ask_vwap", "ask")
            short_bid = _route_price(route_inputs, "short", "bid_vwap", "bid")
            short_ask = _route_price(route_inputs, "short", "ask_vwap", "ask")
            connection.execute(
                """
                INSERT OR IGNORE INTO route_points (
                    route_key, quote_ts_us, token, route_kind, long_venue, long_market_type,
                    short_venue, short_market_type, executable_spread_pct,
                    depth_weighted_spread_pct, funding_apr_pct, funding_daily_pct,
                    long_price, short_price, long_bid_price, long_ask_price,
                    short_bid_price, short_ask_price, exit_spread_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route_key,
                    quote_ts_us,
                    str(row.get("token") or "").upper(),
                    route_kind_for(row),
                    row.get("long_venue"),
                    row.get("long_market_type"),
                    row.get("short_venue"),
                    row.get("short_market_type"),
                    _float_or_none(row.get("executable_spread_pct")),
                    _float_or_none(row.get("depth_weighted_spread_pct")),
                    _float_or_none(row.get("funding_spread_apr_pct"), funding.get("net_apr_pct")),
                    _float_or_none(row.get("funding_daily_pct"), funding.get("net_daily_pct")),
                    long_ask,
                    short_bid,
                    long_bid,
                    long_ask,
                    short_bid,
                    short_ask,
                    _exit_spread_pct(long_bid, short_ask),
                ),
            )
            inserted += int(connection.execute("SELECT changes()").fetchone()[0] > 0)
        cutoff = int(
            (datetime.now(tz=timezone.utc) - timedelta(days=max(1, retention_days))).timestamp()
            * 1_000_000
        )
        connection.execute("DELETE FROM route_points WHERE quote_ts_us < ?", (cutoff,))
        connection.commit()
    finally:
        connection.close()
    return inserted


def load_history(
    *,
    route_key: str | None = None,
    token: str | None = None,
    route_kind: str | None = None,
    max_points: int = 240,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if route_key:
        clauses.append("route_key = ?")
        params.append(route_key)
    if token:
        clauses.append("token = ?")
        params.append(str(token).upper())
    if route_kind:
        clauses.append("route_kind = ?")
        params.append(str(route_kind).upper())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(5000, int(max_points))))
    try:
        rows = connection.execute(
            f"""
            SELECT route_key, quote_ts_us, token, route_kind, long_venue, long_market_type,
                   short_venue, short_market_type, executable_spread_pct,
                   depth_weighted_spread_pct, funding_apr_pct, funding_daily_pct,
                   long_price, short_price, long_bid_price, long_ask_price,
                   short_bid_price, short_ask_price, exit_spread_pct
            FROM route_points
            {where}
            ORDER BY quote_ts_us DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in reversed(rows)]


def route_key_for(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("token") or "").upper() or "?",
            str(row.get("long_venue") or "?"),
            str(row.get("long_market_type") or "?"),
            str(row.get("short_venue") or "?"),
            str(row.get("short_market_type") or "?"),
        ]
    )


def route_kind_for(row: dict[str, Any]) -> str:
    long_type = str(row.get("long_market_type") or "")
    short_type = str(row.get("short_market_type") or "")
    venues = f"{row.get('long_venue') or ''} {row.get('short_venue') or ''}".casefold()
    source_kind = str(row.get("source_kind") or "")
    is_dex = source_kind == "dex_discovered" or any(
        item in venues for item in ("dex", "jupiter", "0x", "hyperliquid", "aster")
    )
    if is_dex and "Futures" in {long_type, short_type}:
        return "DEX-FUTURES"
    if is_dex:
        return "DEX-SPOT"
    if long_type == "Futures" and short_type == "Futures":
        return "FUTURES"
    if long_type == "Spot" and short_type == "Futures":
        return "SPOT-FUTURES"
    if long_type == "Futures" and short_type == "Spot":
        return "FUTURES-SPOT"
    if long_type == "Spot" and short_type == "Spot":
        return "SPOT"
    return "UNKNOWN"


def _connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS route_points (
            route_key TEXT NOT NULL,
            quote_ts_us INTEGER NOT NULL,
            token TEXT NOT NULL,
            route_kind TEXT NOT NULL,
            long_venue TEXT,
            long_market_type TEXT,
            short_venue TEXT,
            short_market_type TEXT,
            executable_spread_pct REAL,
            depth_weighted_spread_pct REAL,
            funding_apr_pct REAL,
            funding_daily_pct REAL,
            long_price REAL,
            short_price REAL,
            long_bid_price REAL,
            long_ask_price REAL,
            short_bid_price REAL,
            short_ask_price REAL,
            exit_spread_pct REAL,
            PRIMARY KEY (route_key, quote_ts_us)
        )
        """
    )
    _ensure_columns(
        connection,
        {
            "long_bid_price": "REAL",
            "long_ask_price": "REAL",
            "short_bid_price": "REAL",
            "short_ask_price": "REAL",
            "exit_spread_pct": "REAL",
        },
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS route_points_token_ts ON route_points(token, quote_ts_us)"
    )
    return connection


def _ensure_columns(connection: sqlite3.Connection, columns: dict[str, str]) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(route_points)").fetchall()
    }
    for name, column_type in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE route_points ADD COLUMN {name} {column_type}")


def _route_price(route_inputs: Any, side: str, *keys: str) -> float | None:
    value = route_inputs.get(side) if isinstance(route_inputs, dict) else {}
    if not isinstance(value, dict):
        return None
    return _float_or_none(*(value.get(key) for key in keys))


def _exit_spread_pct(long_bid: float | None, short_ask: float | None) -> float | None:
    if long_bid is None or short_ask is None or short_ask <= 0:
        return None
    return (long_bid - short_ask) / short_ask * 100.0


def _float_or_none(*values: Any) -> float | None:
    for value in values:
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
