#!/usr/bin/env python3
"""Reprice one SpreadBoard route in an isolated process."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
while str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from spreadboard.fast_quotes import FastQuoteRefresher  # noqa: E402


def main() -> int:
    try:
        row = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        print(json.dumps({"status": "unavailable", "error": "invalid_route_payload"}), flush=True)
        return 2
    if not isinstance(row, dict):
        print(json.dumps({"status": "unavailable", "error": "invalid_route_payload"}), flush=True)
        return 2
    refresher = FastQuoteRefresher()
    try:
        result = refresher.quote_route(
            row,
            target_notional_usd=float(os.environ.get("SPREADBOARD_CHART_NOTIONAL_USD", "50")),
        )
    finally:
        refresher.close()
    print(json.dumps(result, separators=(",", ":"), default=str), flush=True)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
