#!/usr/bin/env python3
"""Cross-check displayed spreads against live order books.

A spread is only real if you could buy the long leg and sell the short leg at
the prices shown. This re-quotes both legs from the venue right now at the same
matched $50 notional the board ranks and compares like with like.

    python scripts/audit_spread_accuracy.py --top 10
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from spreadboard.fast_quotes import FastQuoteRefresher  # noqa: E402
from spreadboard.server import api_market_spreads, _query_lists_with  # noqa: E402


LANES = (
    ("FUTURES", {"kind": "FUTURES"}),
    ("FUTURES-SPOT", {"kind": "FUTURES-SPOT-PAIR"}),
    ("SPOT", {"kind": "SPOT"}),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--tolerance", type=float, default=0.25)
    args = parser.parse_args()

    board_path = Path(
        os.environ.get("SPREADBOARD_BOARD_PATH", str(ROOT / "runtime" / "board_latest.json"))
    )
    refresher = FastQuoteRefresher()
    totals = {"ok": 0, "mismatch": 0, "unverifiable": 0}
    try:
        for lane_name, lane in LANES:
            query = _query_lists_with({}, sort="edge", direction="desc", limit="25", **lane)
            groups = (api_market_spreads(board_path, query).get("groups") or [])[: args.top]
            print(f"\n== {lane_name} ==")
            print(f"   {'TOKEN':<12} {'OURS $50':>9} {'LIVE $50':>9} {'DELTA':>8}  ROUTE")
            for group in groups:
                route = group.get("best_route") or {}
                ours = float(group.get("best_edge_pct") or 0.0)
                quoted = (
                    {"status": "unavailable"}
                    if route.get("depth_unverified")
                    else refresher.quote_route(route, target_notional_usd=50.0)
                )
                live_row = quoted.get("row") if isinstance(quoted.get("row"), dict) else {}
                real = live_row.get("depth_weighted_spread_pct")
                if quoted.get("status") != "ok" or real is None:
                    totals["unverifiable"] += 1
                    print(
                        f"   {group.get('token'):<12} {ours:>9.3f} {'n/a':>9} {'-':>8}  "
                        f"{route.get('long_venue')} -> {route.get('short_venue')}"
                    )
                    continue
                real = float(real)
                verdict = (
                    "OK"
                    if abs(ours - real) <= max(0.5, abs(real) * args.tolerance)
                    else "MISMATCH"
                )
                totals["ok" if verdict == "OK" else "mismatch"] += 1
                print(
                    f"   {group.get('token'):<12} {ours:>9.3f} {real:>9.3f} "
                    f"{ours - real:>+8.3f}  {route.get('long_venue')} -> "
                    f"{route.get('short_venue')}  {verdict}"
                )
    finally:
        refresher.close()

    print(
        f"\nmatched {totals['ok']}, mismatched {totals['mismatch']}, "
        f"unverifiable {totals['unverifiable']}"
    )
    return 1 if totals["mismatch"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
