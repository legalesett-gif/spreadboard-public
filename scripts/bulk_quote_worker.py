#!/usr/bin/env python3
"""Re-price the board from one bulk call per venue, in a process that exits.

The sweep holds a loaded ccxt client per venue -- 4,552 markets for Binance,
6,297 for Gate -- and one full pass measures ~595MB of quotes plus ~466MB of
funding. Run as a thread inside the server that memory never came back: each
pass added tens of megabytes that no `gc.collect()` returned, and the service
crossed its 6GB cgroup and was OOM-killed every hour or two, taking the running
discovery scan and the site down with it.

Here the whole pass costs one process that exits, so the kernel reclaims all of
it, and none of the work holds the GIL against a page load. This is the pattern
the scan, the websocket books, the chart catalog and the fast quotes already
use; the sweep was the one heavy thing left inside the server.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
while str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from spreadboard import bulk_quotes  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-seconds", type=float, default=180.0)
    parser.add_argument(
        "--funding-budget-seconds",
        type=float,
        default=180.0,
        help="0 skips the funding sweep for this pass.",
    )
    args = parser.parse_args()

    summary = {"quotes": bulk_quotes.sweep(budget_seconds=args.budget_seconds)}
    if args.funding_budget_seconds > 0:
        summary["funding"] = bulk_quotes.sweep_funding(
            budget_seconds=args.funding_budget_seconds
        )
    print(json.dumps(summary, sort_keys=True, default=str), flush=True)
    # The clients hold hundreds of megabytes and the interpreter is about to be
    # thrown away; unwinding them buys nothing.
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
