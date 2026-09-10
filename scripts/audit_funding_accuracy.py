#!/usr/bin/env python3
"""Cross-check the funding board against the exchanges themselves.

The board is only worth what its numbers are worth, and the failures have been
silent every time: an inverse contract priced with linear arithmetic, a venue
whose funding never refreshed. This walks each lane's top routes, fetches both
legs straight from the venue, and prints the difference.

Uses the same native endpoints the refresher does, so it can verify the eight
venues CCXT cannot bulk-fetch -- checking with CCXT alone reproduces the
product's own blind spot and reports them as unverifiable.

    python scripts/audit_funding_accuracy.py --top 10
"""

from __future__ import annotations

import argparse
import math
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
    ("FUTURES-FUTURES", {"kind": "FUTURES"}),
    ("FUTURES-SPOT", {"kind": "FUTURES-SPOT-PAIR"}),
    ("FUTURES-DEX", {"kind": "DEX-FUTURES"}),
)


def _daily_pct(fields: dict | None) -> float | None:
    """A venue's per-interval rate as percent per day."""
    if not fields:
        return None
    rate = fields.get("current_funding_pct")
    if rate is None:
        return None
    hours = fields.get("funding_interval_hours")
    if fields.get("funding_interval_assumed") or hours is None:
        return None
    try:
        rate, hours = float(rate), float(hours)
        if not math.isfinite(rate) or not math.isfinite(hours) or hours <= 0:
            return None
        daily = rate * (24.0 / hours)
        return daily if math.isfinite(daily) else None
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.25,
        help="fraction of the true value still counted as drift rather than a bug",
    )
    args = parser.parse_args()

    board_path = Path(
        os.environ.get("SPREADBOARD_BOARD_PATH", str(ROOT / "runtime" / "board_latest.json"))
    )
    refresher = FastQuoteRefresher()
    venue_rates: dict[str, dict] = {}

    def rates_for(venue: str) -> dict:
        if venue not in venue_rates:
            try:
                venue_rates[venue] = refresher._bulk_funding_rates(venue)
            except Exception:  # noqa: BLE001 - an unreachable venue is not a board bug.
                venue_rates[venue] = {}
        return venue_rates[venue]

    totals = {"ok": 0, "mismatch": 0, "unverifiable": 0}
    try:
        for lane_name, lane in LANES:
            query = _query_lists_with(
                {}, funding_only="1", funding_window="now", sort="funding", direction="desc", limit="25", **lane
            )
            groups = (api_market_spreads(board_path, query).get("groups") or [])[: args.top]
            print(f"\n== {lane_name} ==")
            print(f"   {'TOKEN':<12} {'OURS/24h':>9} {'REAL/24h':>9} {'DELTA':>9}  ROUTE")
            for group in groups:
                route = group.get("best_funding_route") or {}

                def leg(side: str) -> float | None:
                    # Only a perpetual pays or charges funding. A spot or DEX
                    # leg contributes exactly zero -- treating it as missing
                    # made every Futures-Spot and Futures-DEX row unverifiable.
                    if str(route.get(f"{side}_market_type") or "") != "Futures":
                        return 0.0
                    return _daily_pct(
                        rates_for(str(route.get(f"{side}_venue"))).get(
                            str(route.get(f"{side}_market_symbol"))
                        )
                    )

                long_leg, short_leg = leg("long"), leg("short")
                try:
                    ours = float(group.get("best_funding_24h_pct"))
                    if not math.isfinite(ours):
                        ours = None
                except (TypeError, ValueError):
                    ours = None
                if ours is None or long_leg is None or short_leg is None:
                    totals["unverifiable"] += 1
                    shown = f"{ours:>9.4f}" if ours is not None else f"{'n/a':>9}"
                    print(
                        f"   {group.get('token'):<12} {shown} {'n/a':>9} {'-':>9}  "
                        f"{route.get('long_venue')} -> {route.get('short_venue')}"
                    )
                    continue
                real = short_leg - long_leg
                # Now compares two current-rate projections on every venue.
                # An hourly venue or attached settled history cannot excuse a
                # disagreement; settled totals belong to different tabs.
                if abs(ours - real) <= max(0.15, abs(real) * args.tolerance):
                    verdict = "OK"
                    totals["ok"] += 1
                else:
                    verdict = "MISMATCH"
                    totals["mismatch"] += 1
                print(
                    f"   {group.get('token'):<12} {ours:>9.4f} {real:>9.4f} {ours - real:>+9.3f}  "
                    f"{route.get('long_venue')} -> {route.get('short_venue')}  {verdict}"
                )
    finally:
        refresher.close()

    verifiable = totals["ok"] + totals["mismatch"]
    print(
        f"\nmatched {totals['ok']}, mismatched {totals['mismatch']}, "
        f"unverifiable {totals['unverifiable']}"
    )
    if verifiable:
        print(f"agreement on comparable rows: {100.0 * totals['ok'] / verifiable:.0f}%")
    if totals["mismatch"]:
        return 1
    # An unreachable provider or unknown schedule cannot produce a green audit.
    return 2 if totals["unverifiable"] or not verifiable else 0


if __name__ == "__main__":
    raise SystemExit(main())
