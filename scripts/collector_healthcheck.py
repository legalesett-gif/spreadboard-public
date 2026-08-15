#!/usr/bin/env python3
"""Fail when the market collector has stopped advancing shared artifacts."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _age_seconds(timestamp: Any, *, now: float) -> float | None:
    if timestamp is None:
        return None
    try:
        if isinstance(timestamp, (int, float)):
            value = float(timestamp)
        else:
            text = str(timestamp).strip().replace("Z", "+00:00")
            value = datetime.fromisoformat(text).astimezone(UTC).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, now - value)


def _file_age(path: Path, *, now: float) -> float | None:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def collector_health(
    data_dir: Path,
    *,
    now: float | None = None,
    snapshot_max_age_seconds: float = 3600.0,
    generation_max_age_seconds: float = 180.0,
    live_book_max_age_seconds: float = 180.0,
    fast_cycle_max_age_seconds: float = 900.0,
    min_current_bulk_books: int = 1000,
) -> dict[str, Any]:
    """Return bounded, credential-free health evidence for Docker."""

    moment = time.time() if now is None else now
    snapshot_age = _file_age(data_dir / "api_discovery_latest.json", now=moment)

    generation = _read_json(data_dir / "market_generation.json")
    generation_age = _age_seconds(generation.get("updated_at_unix"), now=moment)
    generation_schema_ok = (
        generation.get("schema") == "spreadboard.market_generation.v1"
    )

    book_count = 0
    latest_book_age: float | None = None
    current_bulk_book_count = 0
    latest_bulk_book_age: float | None = None
    book_db = data_dir / "spreadboard_live_books.sqlite3"
    try:
        with sqlite3.connect(
            f"file:{book_db}?mode=ro", uri=True, timeout=3
        ) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*),
                    MAX(quote_ts_us),
                    SUM(
                        CASE WHEN source = 'bulk_ticker' AND quote_ts_us >= ?
                        THEN 1 ELSE 0 END
                    ),
                    MAX(
                        CASE WHEN source = 'bulk_ticker' THEN quote_ts_us END
                    )
                FROM live_books
                """,
                (int((moment - live_book_max_age_seconds) * 1_000_000),),
            ).fetchone()
        book_count = int(row[0] or 0) if row else 0
        latest_us = int(row[1] or 0) if row else 0
        current_bulk_book_count = int(row[2] or 0) if row else 0
        latest_bulk_us = int(row[3] or 0) if row else 0
        latest_book_age = (
            _age_seconds(latest_us / 1_000_000.0, now=moment)
            if latest_us
            else None
        )
        latest_bulk_book_age = (
            _age_seconds(latest_bulk_us / 1_000_000.0, now=moment)
            if latest_bulk_us
            else None
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass

    delta = _read_json(data_dir / "api_discovery_fast_quotes.json")
    fast_meta = delta.get("fast_quote_refresh")
    fast_meta = fast_meta if isinstance(fast_meta, dict) else {}
    completed = fast_meta.get("last_completed_cycle")
    completed = completed if isinstance(completed, dict) else {}
    fast_cycle_age = _age_seconds(completed.get("updated_at"), now=moment)

    checks = {
        "snapshot_current": snapshot_age is not None
        and snapshot_age <= snapshot_max_age_seconds,
        "generation_current": generation_schema_ok
        and generation_age is not None
        and generation_age <= generation_max_age_seconds,
        "live_books_current": book_count > 0
        and latest_book_age is not None
        and latest_book_age <= live_book_max_age_seconds,
        # A few WebSocket leaders can stay current even when the complete
        # catalogue worker is dead. Require broad bulk-ticker evidence too.
        "bulk_books_current": current_bulk_book_count
        >= max(1, int(min_current_bulk_books))
        and latest_bulk_book_age is not None
        and latest_bulk_book_age <= live_book_max_age_seconds,
        "fast_cycle_current": fast_cycle_age is not None
        and fast_cycle_age <= fast_cycle_max_age_seconds,
    }
    return {
        "status": "ok" if all(checks.values()) else "stale",
        "checks": checks,
        "snapshot_age_seconds": snapshot_age,
        "generation_age_seconds": generation_age,
        "generation_kind": generation.get("kind"),
        "live_book_count": book_count,
        "latest_live_book_age_seconds": latest_book_age,
        "current_bulk_book_count": current_bulk_book_count,
        "latest_bulk_book_age_seconds": latest_bulk_book_age,
        "fast_cycle_age_seconds": fast_cycle_age,
    }


def main() -> int:
    data_dir = Path(os.environ.get("SPREADBOARD_DATA_DIR", "/app/runtime"))
    result = collector_health(
        data_dir,
        snapshot_max_age_seconds=float(
            os.environ.get("SPREADBOARD_COLLECTOR_SNAPSHOT_MAX_AGE_SECONDS", "3600")
        ),
        generation_max_age_seconds=float(
            os.environ.get("SPREADBOARD_COLLECTOR_GENERATION_MAX_AGE_SECONDS", "180")
        ),
        live_book_max_age_seconds=float(
            os.environ.get("SPREADBOARD_COLLECTOR_BOOK_MAX_AGE_SECONDS", "180")
        ),
        fast_cycle_max_age_seconds=float(
            os.environ.get("SPREADBOARD_COLLECTOR_FAST_MAX_AGE_SECONDS", "900")
        ),
        min_current_bulk_books=int(
            os.environ.get("SPREADBOARD_COLLECTOR_MIN_CURRENT_BULK_BOOKS", "1000")
        ),
    )
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
