#!/usr/bin/env python3
"""Cross-check displayed spreads against live order books.

A spread is only real if you could buy the long leg and sell the short leg at
the prices shown. This re-quotes both legs from the venue right now at the same
matched notional the board ranks and compares like with like. The default is
imported from the canonical product constant so raising the product probe
cannot silently leave this checker comparing a different trade size.

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

from spreadboard import api_spreads
from spreadboard.fast_quotes import FastQuoteRefresher  # noqa: E402
from spreadboard.server import api_market_spreads, _query_lists_with  # noqa: E402


LANES = (
    ("FUTURES", {"kind": "FUTURES"}),
    ("FUTURES-SPOT", {"kind": "FUTURES-SPOT-PAIR"}),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--tolerance", type=float, default=0.25)
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=30.0,
        help=(
            "only compare displayed quotes this recent; older rows are valid "
            "radar context but cannot be compared fairly with an exact-now re-quote"
        ),
    )
    parser.add_argument(
        "--notional",
        type=float,
        default=api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD,
        help="matched quote size; defaults to the canonical product probe",
    )
    args = parser.parse_args()
    if args.notional <= 0:
        parser.error("--notional must be positive")
    if args.max_age_seconds <= 0:
        parser.error("--max-age-seconds must be positive")

    board_path = Path(
        os.environ.get("SPREADBOARD_BOARD_PATH", str(ROOT / "runtime" / "board_latest.json"))
    )
    refresher = FastQuoteRefresher()
    totals = {
        "ok": 0,
        "mismatch": 0,
        "unverifiable": 0,
        "stale_for_comparison": 0,
        "unranked": 0,
    }
    try:
        for lane_name, lane in LANES:
            query = _query_lists_with({}, sort="edge", direction="desc", limit="25", **lane)
            groups = api_market_spreads(board_path, query).get("groups") or []
            print(f"\n== {lane_name} ==")
            probe = f"${args.notional:,.0f}"
            print(
                f"   {'TOKEN':<12} {('OURS ' + probe):>12} "
                f"{('LIVE ' + probe):>12} {'DELTA':>8} {'AGE':>6}  ROUTE"
            )
            compared_in_lane = 0
            for group in groups:
                edge = group.get("best_edge_pct")
                if edge is None:
                    totals["unranked"] += 1
                    continue
                route = group.get("best_route") or {}
                age_min = api_spreads.quote_age_min(route)
                age_seconds = age_min * 60.0 if age_min is not None else None
                if age_seconds is None or age_seconds > args.max_age_seconds:
                    totals["stale_for_comparison"] += 1
                    continue
                ours = float(edge)
                # A stored row can be marked depth-unverified because its broad
                # discovery pass used a ticker.  The point of this audit is to
                # try the exact route again now, so that flag must not suppress
                # a fresh order-book quote.
                quoted = refresher.quote_route(route, target_notional_usd=args.notional)
                live_row = quoted.get("row") if isinstance(quoted.get("row"), dict) else {}
                real = live_row.get("depth_weighted_spread_pct")
                if quoted.get("status") != "ok" or real is None:
                    totals["unverifiable"] += 1
                    print(
                        f"   {group.get('token'):<12} {ours:>9.3f} {'n/a':>9} {'-':>8}  "
                        f"{age_seconds:>5.1f}s  "
                        f"{route.get('long_venue')} -> {route.get('short_venue')} "
                        f"({quoted.get('error') or 'unavailable'})"
                    )
                    compared_in_lane += 1
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
                    f"{ours - real:>+8.3f} {age_seconds:>5.1f}s  "
                    f"{route.get('long_venue')} -> "
                    f"{route.get('short_venue')}  {verdict}"
                )
                compared_in_lane += 1
                if compared_in_lane >= args.top:
                    break
    finally:
        refresher.close()

    print(
        f"\nmatched {totals['ok']}, mismatched {totals['mismatch']}, "
        f"unverifiable {totals['unverifiable']}, too old for exact-now "
        f"comparison {totals['stale_for_comparison']}, unranked {totals['unranked']}"
    )
    compared = totals["ok"] + totals["mismatch"] + totals["unverifiable"]
    return 1 if totals["mismatch"] or compared == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
