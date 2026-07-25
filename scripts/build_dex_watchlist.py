"""Resolve DEX contract addresses for Futures-DEX watchlist candidates.

Futures-DEX rows only exist for watchlist assets that carry an exact chain and
contract, because ``OkxDexQuoteSource`` refuses to quote anything it cannot
identify. This script resolves those contracts from DexScreener and prints
watchlist entries for review.

Safety rules, mirroring ``spreadboard.live.fetch_dexscreener``:
  * the pair's base symbol must match the requested token exactly;
  * the pair price must sit within ``PRICE_TOLERANCE`` of the CEX reference
    price, so an impostor token with a wildly different price is rejected;
  * among survivors the deepest-liquidity pair wins.

A wrong contract does not fail loudly -- it quotes a different asset and
manufactures a fake spread -- so every resolved address must be eyeballed before
it is committed to the watchlist.

KNOWN LIMITATION: DexScreener symbol search is not an identity oracle. Back to
back runs return different contracts for the same symbol, and it resolves ARB and
LINK to Solana impostors rather than the canonical Arbitrum/Ethereum tokens. Treat
this script as a review aid that produces candidates for a human to confirm, never
as an automatic watchlist writer. The authoritative source is the OKX DEX
supported-token endpoint, which quotes on the same venue the route would execute
on -- prefer that once OKX DEX credentials are configured.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

PRICE_TOLERANCE = 0.10
TIMEOUT_SECONDS = 15

# OKX DEX chain indexes, keyed by DexScreener chain id.
CHAIN_INDEX = {
    "ethereum": 1,
    "bsc": 56,
    "polygon": 137,
    "base": 8453,
    "arbitrum": 42161,
    "optimism": 10,
    "avalanche": 43114,
    "solana": 501,
}


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve(symbol: str, reference_price: float | None) -> dict | None:
    query = urllib.parse.urlencode({"q": symbol})
    request = urllib.request.Request(
        f"https://api.dexscreener.com/latest/dex/search?{query}",
        headers={"User-Agent": "SpreadBoard/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"token": symbol, "error": str(exc)[:120]}

    candidates = []
    for pair in payload.get("pairs") or []:
        base = pair.get("baseToken") or {}
        if str(base.get("symbol") or "").upper() != symbol.upper():
            continue
        if pair.get("chainId") not in CHAIN_INDEX:
            continue
        price = _float(pair.get("priceUsd"))
        if price is None:
            continue
        if reference_price and abs(price - reference_price) > reference_price * PRICE_TOLERANCE:
            continue
        candidates.append((pair, price))

    if not candidates:
        return {"token": symbol, "error": "no_pair_matched"}

    candidates.sort(
        key=lambda item: _float((item[0].get("liquidity") or {}).get("usd")) or 0.0,
        reverse=True,
    )
    alternates = [
        {
            "chain": alt.get("chainId"),
            "contract": (alt.get("baseToken") or {}).get("address"),
            "price_usd": alt_price,
            "liquidity_usd": _float((alt.get("liquidity") or {}).get("usd")),
        }
        for alt, alt_price in candidates[1:4]
    ]
    pair, price = candidates[0]
    base = pair.get("baseToken") or {}
    chain = pair.get("chainId")
    return {
        "alternates": alternates,
        "token": symbol.upper(),
        "chain": chain,
        "chain_index": CHAIN_INDEX[chain],
        "contract": base.get("address"),
        "price_usd": price,
        "reference_price": reference_price,
        "liquidity_usd": _float((pair.get("liquidity") or {}).get("usd")),
        "dex_id": pair.get("dexId"),
        "url": pair.get("url"),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: build_dex_watchlist.py TOKEN[:price] ...", file=sys.stderr)
        return 2
    results = []
    for item in argv[1:]:
        symbol, _, price_text = item.partition(":")
        results.append(resolve(symbol, _float(price_text)))
        time.sleep(0.35)  # DexScreener rate limit courtesy
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
