#!/usr/bin/env python3
"""Refresh leading SpreadBoard routes in an isolated process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
while str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from spreadboard.fast_quotes import FastQuoteRefresher  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-path", type=Path, required=True)
    parser.add_argument("--route-limit", type=int, default=100)
    # Stop quoting and write in time to beat the parent's hard timeout: a
    # killed cycle discards every quote it had already taken.
    parser.add_argument("--deadline-seconds", type=float, default=None)
    args = parser.parse_args()

    priority_path = Path(
        os.environ.get(
            "SPREADBOARD_OKX_DEX_PRIORITY_STATE_PATH",
            "/tmp/spreadboard-okx-dex-priority.state",
        )
    )
    owner = f"{os.getpid()}:{time.time()}"
    try:
        priority_path.write_text(owner, encoding="ascii")
    except OSError:
        owner = ""
    refresher = FastQuoteRefresher()
    try:
        summary = refresher.refresh(
            args.snapshot_path,
            route_limit=args.route_limit,
            deadline_seconds=args.deadline_seconds,
        )
    finally:
        refresher.close()
        if owner:
            try:
                if priority_path.read_text(encoding="ascii") == owner:
                    priority_path.unlink(missing_ok=True)
            except OSError:
                pass
    print(json.dumps(summary, sort_keys=True), flush=True)
    os._exit(0 if summary.get("status") == "ok" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
