#!/usr/bin/env python3
"""Turn a finished scan into the published snapshot, in a process that exits.

The scan writes a 40MB JSON. Parsing it costs roughly a gigabyte of Python
objects, and the service did that twice at once -- the staging snapshot and the
published one, so the merge could see both -- then enriched funding over it,
recorded it to history, and parsed it a third time to rebuild the identity
registry. All inside the web server. Measured, the process reached 4.31GB five
minutes after every start and was OOM-killed inside its 6GB cgroup, which is
why the site kept going down and why no scan ever survived to land.

None of that work needs to be in the server. Here it costs one process that
exits, and the kernel takes all of it back.

Two stages, because they have different locking needs:

  enrich   Funding enrichment over the staging snapshot. Network-bound and slow,
           and it touches nothing the fast-quote cycle uses, so the parent runs
           it without holding anything.
  publish  Merge the newer fast quotes, write the published snapshot, record
           history. Fast, and the parent holds its snapshot and quote-cycle
           locks across it exactly as it did when this was inline.
  record   History alone, for the fast-quote cycle. That ran once a minute in
           the server and was what actually took it to 4.3GB.
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

from spreadboard import live, market_history  # noqa: E402

# The same functions the pipeline used when it ran inline, so the two cannot
# drift apart. Importing the service module costs its imports and nothing else;
# it starts nothing at import time.
sys.path.insert(0, str(ROOT / "scripts"))
from run_spreadboard_service import (  # noqa: E402
    _funding_refresh_route_keys,
    _merge_newer_fast_quotes,
)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("enrich", "publish", "record"), required=True)
    parser.add_argument("--staging-path", type=Path, required=True)
    parser.add_argument("--published-path", type=Path, required=True)
    parser.add_argument("--funding-workers", type=int, default=4)
    args = parser.parse_args()

    if args.stage == "record":
        # The published snapshot, straight to history. Once a minute in the
        # server this was a gigabyte of parsed JSON per cycle.
        published = _load(args.published_path)
        if not published:
            print(json.dumps({"status": "no_snapshot"}), flush=True)
            os._exit(1)
        inserted = market_history.record_snapshot(published)
        print(json.dumps({"status": "ok", "history_inserted": inserted}), flush=True)
        os._exit(0)

    snapshot = _load(args.staging_path)
    if not snapshot:
        print(json.dumps({"status": "no_snapshot"}), flush=True)
        os._exit(1)

    if args.stage == "enrich":
        summary = live.enrich_snapshot_funding_24h(
            snapshot,
            max_workers=args.funding_workers,
            route_keys=_funding_refresh_route_keys(snapshot),
        )
        _write_atomic(args.staging_path, snapshot)
        print(json.dumps({"status": "ok", "funding": summary}, default=str), flush=True)
        os._exit(0)

    published = _load(args.published_path)
    _merge_newer_fast_quotes(snapshot, published)
    # The published snapshot is what every page reads, so it is replaced whole
    # or not at all.
    del published
    _write_atomic(args.published_path, snapshot)
    inserted = market_history.record_snapshot(snapshot)
    refresh = snapshot.get("source_refresh") or {}
    routes = len(snapshot.get("api_discovered_rows") or []) + len(
        snapshot.get("dex_discovered_rows") or []
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "routes": routes,
                "history_inserted": inserted,
                "refresh_status": refresh.get("status"),
            },
            default=str,
        ),
        flush=True,
    )
    # Gigabytes of parsed snapshot are about to be discarded with the process.
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
