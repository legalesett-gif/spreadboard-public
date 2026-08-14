#!/usr/bin/env python3
"""Incrementally materialize leakage-safe public market-core research data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spreadboard import market_core_backfill, market_history, research_calibration


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=research_calibration.DEFAULT_DB_PATH)
    parser.add_argument(
        "--history-db-path",
        type=Path,
        default=market_history.DEFAULT_DB_PATH,
    )
    parser.add_argument("--daily-limit", type=int, default=market_core_backfill.DEFAULT_DAILY_LIMIT)
    parser.add_argument(
        "--per-token-limit", type=int, default=market_core_backfill.DEFAULT_PER_TOKEN_LIMIT
    )
    parser.add_argument("--max-days", type=int, default=3)
    args = parser.parse_args()
    result = market_core_backfill.backfill(
        db_path=args.db_path,
        history_db_path=args.history_db_path,
        daily_limit=args.daily_limit,
        per_token_limit=args.per_token_limit,
        max_days=args.max_days,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"ok", "history_database_empty"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
