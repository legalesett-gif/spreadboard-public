"""Bounded on-disk exact funding events, shared across successive refresh workers."""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

RETENTION_MS = 32 * 86_400_000


def read(path: Path, venue: str, symbol: str, now_ms: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as con:
            rows = con.execute(
                "SELECT ts,rate FROM funding_events WHERE venue=? AND symbol=? AND ts>? AND ts<=? ORDER BY ts",
                (venue, symbol, now_ms - RETENTION_MS, now_ms),
            ).fetchall()
        return [{"timestamp": ts, "fundingRate": rate} for ts, rate in rows]
    except sqlite3.OperationalError:
        return []


def merge(path: Path, venue: str, symbol: str, entries: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for entry in entries:
        try:
            ts, rate = int(entry["timestamp"]), float(entry["fundingRate"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if now_ms - RETENTION_MS < ts <= now_ms and math.isfinite(rate):
            rows.append((venue, symbol, ts, rate))
    with closing(sqlite3.connect(path, timeout=30)) as con, con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS funding_events (venue TEXT NOT NULL,symbol TEXT NOT NULL,ts INTEGER NOT NULL,rate REAL NOT NULL,PRIMARY KEY(venue,symbol,ts)) WITHOUT ROWID")
        con.execute("CREATE INDEX IF NOT EXISTS funding_events_ts ON funding_events(ts)")
        con.executemany("INSERT INTO funding_events VALUES (?,?,?,?) ON CONFLICT(venue,symbol,ts) DO UPDATE SET rate=excluded.rate", rows)
        # Retire old events even for delisted/disabled legs that are never
        # fetched again. The timestamp index avoids a full ledger scan.
        con.execute("DELETE FROM funding_events WHERE ts<=?", (now_ms - RETENTION_MS,))
    return read(path, venue, symbol, now_ms)
