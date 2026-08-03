#!/usr/bin/env python3
"""Cross-check displayed spreads against live order books.

A spread is only real if you could buy the long leg and sell the short leg at
the prices shown. This re-quotes both legs from the venue right now and prints
what the board claims against what the books say.

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

import ccxt  # noqa: E402

from spreadboard.fast_quotes import VENUE_IDS  # noqa: E402
from spreadboard.server import api_market_spreads, _query_lists_with  # noqa: E402

#: CCXT renamed some adapters. VENUE_IDS still carries the older ids, and the
#: websocket worker has always mapped them; without the same map here every
#: Gate route reported as unverifiable for no reason but the name.
CCXT_ALIASES = {
    "gateio": ("gate", "gateio"),
    "gate": ("gate", "gateio"),
    "coinbaseexchange": ("coinbaseexchange", "coinbase"),
    "huobi": ("htx", "huobi"),
}


def _exchange(exchange_id: str):
    """A loaded CCXT client for a venue id, or None if it has no adapter."""
    import ccxt

    for candidate in CCXT_ALIASES.get(exchange_id, (exchange_id,)):
        klass = getattr(ccxt, candidate, None)
        if klass is None:
            continue
        try:
            client = klass({"enableRateLimit": True, "timeout": 20000})
            client.load_markets()
            return client
        except Exception:  # noqa: BLE001 - try the next name, then give up.
            continue
    return None


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
    clients: dict[str, object] = {}

    def book(venue: str, symbol: str) -> tuple[float | None, float | None]:
        """Best bid and ask, straight from the venue."""
        exchange_id = VENUE_IDS.get(venue)
        if not exchange_id or not symbol:
            return None, None
        if exchange_id not in clients:
            clients[exchange_id] = _exchange(exchange_id)
        client = clients[exchange_id]
        if client is None or symbol not in getattr(client, "symbols", []):
            return None, None
        try:
            ticker = client.fetch_ticker(symbol)
            return ticker.get("bid"), ticker.get("ask")
        except Exception:  # noqa: BLE001
            return None, None

    totals = {"ok": 0, "mismatch": 0, "unverifiable": 0}
    for lane_name, lane in LANES:
        query = _query_lists_with({}, sort="edge", direction="desc", limit="25", **lane)
        groups = (api_market_spreads(board_path, query).get("groups") or [])[: args.top]
        print(f"\n== {lane_name} ==")
        print(f"   {'TOKEN':<12} {'OURS':>8} {'REAL':>8} {'DELTA':>8}  ROUTE")
        for group in groups:
            route = group.get("best_route") or {}
            _, buy_ask = book(str(route.get("long_venue")), str(route.get("long_market_symbol")))
            sell_bid, _ = book(str(route.get("short_venue")), str(route.get("short_market_symbol")))
            ours = float(group.get("best_edge_pct") or 0.0)
            if not buy_ask or not sell_bid:
                totals["unverifiable"] += 1
                print(
                    f"   {group.get('token'):<12} {ours:>8.3f} {'n/a':>8} {'-':>8}  "
                    f"{route.get('long_venue')} -> {route.get('short_venue')}"
                )
                continue
            real = (sell_bid / buy_ask - 1.0) * 100.0
            verdict = "OK" if abs(ours - real) <= max(0.5, abs(real) * args.tolerance) else "MISMATCH"
            totals["ok" if verdict == "OK" else "mismatch"] += 1
            print(
                f"   {group.get('token'):<12} {ours:>8.3f} {real:>8.3f} {ours - real:>+8.3f}  "
                f"{route.get('long_venue')} -> {route.get('short_venue')}  {verdict}"
            )

    print(
        f"\nmatched {totals['ok']}, mismatched {totals['mismatch']}, "
        f"unverifiable {totals['unverifiable']}"
    )
    return 1 if totals["mismatch"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
