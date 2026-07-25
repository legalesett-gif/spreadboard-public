#!/usr/bin/env python3
"""Refresh public token names and transfer rails in an isolated process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spreadboard import public_rails, token_metadata  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-path", type=Path, required=True)
    parser.add_argument("--rail-workers", type=int, default=1)
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot_path.read_text(encoding="utf-8"))
    symbols = {
        str(row.get("token") or "").upper()
        for bucket in ("api_discovered_rows", "dex_discovered_rows")
        for row in snapshot.get(bucket) or []
        if isinstance(row, dict) and row.get("token")
    }
    results: dict[str, str] = {}
    try:
        token_metadata.refresh_token_metadata(symbols)
        results["token_names"] = "ok"
    except Exception as exc:  # noqa: BLE001 - enrichment is best effort.
        results["token_names"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        public_rails.refresh_public_rails(
            snapshot,
            max_workers=max(1, args.rail_workers),
        )
        results["transfer_rails"] = "ok"
    except Exception as exc:  # noqa: BLE001 - enrichment is best effort.
        results["transfer_rails"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    print(json.dumps(results, sort_keys=True))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SPREADBOARD_PUBLIC_MODE", "1")
    raise SystemExit(main())
