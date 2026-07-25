#!/usr/bin/env python3
"""Refresh leading SpreadBoard routes in an isolated process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spreadboard.fast_quotes import FastQuoteRefresher  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-path", type=Path, required=True)
    parser.add_argument("--route-limit", type=int, default=6)
    args = parser.parse_args()

    refresher = FastQuoteRefresher()
    summary = refresher.refresh(args.snapshot_path, route_limit=args.route_limit)
    print(json.dumps(summary, sort_keys=True), flush=True)
    os._exit(0 if summary.get("status") == "ok" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
