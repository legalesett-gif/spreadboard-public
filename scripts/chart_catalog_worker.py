#!/usr/bin/env python3
"""Isolated venue catalogue load so one adapter cannot stall the service."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from spreadboard.chart_catalog import _load_venue  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        markets = _load_venue(sys.argv[1], sys.argv[2])
        print(json.dumps({"status": "ok", "markets": markets}, separators=(",", ":")))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "unavailable", "error": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
