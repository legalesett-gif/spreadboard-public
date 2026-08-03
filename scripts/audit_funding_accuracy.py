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

#: Venues settling more often than the 8h standard. One instantaneous print
#: times 24 is a forecast, not a measurement, and on these it is a bad one:
#: AGLD's print once extrapolated to 9.78%/day against the 5.66%/day the 24
#: rates it actually paid summed to. The board uses the settled sum, so a
#: disagreement here is this script being naive, not the board being wrong.
SUB_8H_VENUES = {"Kraken Futures", "Hyperliquid"}

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
    hours = fields.get("funding_interval_hours") or 8.0
    try:
        return float(rate) * (24.0 / float(hours))
    except (TypeError, ValueError, ZeroDivisionError):
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

    totals = {"ok": 0, "mismatch": 0, "forecast": 0, "unverifiable": 0}
    try:
        for lane_name, lane in LANES:
            query = _query_lists_with(
                {}, funding_only="1", sort="funding", direction="desc", limit="25", **lane
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
                ours = float(group.get("best_funding_24h_pct") or 0.0)
                if long_leg is None or short_leg is None:
                    totals["unverifiable"] += 1
                    print(
                        f"   {group.get('token'):<12} {ours:>9.4f} {'n/a':>9} {'-':>9}  "
                        f"{route.get('long_venue')} -> {route.get('short_venue')}"
                    )
                    continue
                real = short_leg - long_leg
                # The paired carry is the short leg's receipt less the long
                # leg's payment; a route is fine if it is within drift of that.
                # A row whose carry came from the rates that actually settled
                # over 24h is a measurement. This script can only build a
                # forecast from the current print, so the two are not the same
                # quantity and a gap between them is not an error.
                measured = route.get("funding_24h_source") == "settled_public_events"
                hourly = {route.get("long_venue"), route.get("short_venue")} & SUB_8H_VENUES
                if abs(ours - real) <= max(0.15, abs(real) * args.tolerance):
                    verdict = "OK"
                    totals["ok"] += 1
                elif measured or hourly:
                    verdict = "MEASURED" if measured else "FORECAST"
                    totals["forecast"] += 1
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
        f"measured-vs-forecast {totals['forecast']}, "
        f"unverifiable {totals['unverifiable']}"
    )
    if verifiable:
        print(f"agreement on comparable rows: {100.0 * totals['ok'] / verifiable:.0f}%")
    return 1 if totals["mismatch"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
