#!/usr/bin/env python3
"""Build the files the board reads, in a process that exits.

Two jobs that have nothing to do with serving a request and everything to do
with the server's memory:

  chart-catalog     enumerates every market on every venue -- 22,309 of them --
                    which means a loaded ccxt client per venue, the same
                    hundreds of megabytes that made the quote sweep a worker.
  identity-registry parses the 40MB discovery snapshot, roughly a gigabyte of
                    Python objects, to decide which markets are the same asset.

Both only produce a file. Run inside the service they were most of the 2.2GB the
process reached within a minute of starting, on the way to the 4.3GB that had
the kernel kill it inside its 6GB cgroup. Run here they cost one process that
exits and the kernel takes it all back.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from spreadboard import chart_catalog, verified_identity  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", choices=("chart-catalog", "identity-registry"), required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--snapshot-path", type=Path)
    parser.add_argument("--output-path", type=Path)
    parser.add_argument("--static-registry-path", type=Path)
    parser.add_argument("--watchlist-path", type=Path)
    parser.add_argument("--rails-path", type=Path)
    args = parser.parse_args()

    if args.job == "chart-catalog":
        payload = chart_catalog.refresh(workers=args.workers)
        summary = {
            "status": "ok",
            "markets": payload.get("count", 0),
            "tokens": payload.get("token_count", 0),
        }
    else:
        payload = verified_identity.build_verified_identity_registry(
            static_registry_path=args.static_registry_path,
            watchlist_path=args.watchlist_path,
            rails_path=args.rails_path,
            snapshot_path=args.snapshot_path,
            output_path=args.output_path,
        )
        generation = payload.get("generation") or {}
        summary = {
            "status": "ok",
            "matches": generation.get("verified_matches", 0),
            "markets_added": generation.get("markets_added", 0),
        }

    print(json.dumps(summary, default=str), flush=True)
    # Whatever this job loaded goes back to the kernel with the process.
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
