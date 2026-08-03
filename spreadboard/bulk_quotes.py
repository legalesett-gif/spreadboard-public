"""Re-price the whole board from one bulk call per venue.

Prices reached the board two ways, and neither covered it. Websockets stream a
few hundred legs, which is a fraction of the eight thousand the board carries,
and the discovery scan re-quotes the rest on a twenty-five minute cycle -- so a
route outside the streaming set could be twenty minutes stale, and a token that
turned positive in between simply did not appear until the next scan.

`fetch_tickers()` returns every symbol a venue lists in a single request:
Binance answers with 1,371 priced symbols in 0.6s, Bybit 789 in 0.2s, Bitget
1,207 in 0.3s. Twenty-one venues cover the universe in about fifteen seconds,
which means the whole board can be re-priced every half minute rather than
every twenty-five.

This is the same trick the funding sweep already uses -- one bulk call per venue
instead of one call per symbol -- applied to prices.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import time
from typing import Any

from spreadboard import live_book_cache
from spreadboard.fast_quotes import VENUE_IDS

#: Venues whose bulk ticker carries no bid/ask. Coinbase returns 528 symbols
#: with neither, so calling it every cycle buys nothing.
SKIP_VENUES: set[str] = {"Coinbase"}

#: CCXT renamed some adapters; VENUE_IDS still carries the older ids.
_ALIASES = {"gateio": ("gate", "gateio"), "coinbaseexchange": ("coinbaseexchange", "coinbase")}

_CLIENTS: dict[tuple[str, str], Any] = {}


def _client(venue: str, market_type: str) -> Any:
    """A loaded client per venue and market type, reused across cycles."""
    key = (venue, market_type)
    if key in _CLIENTS:
        return _CLIENTS[key]
    import ccxt

    exchange_id = VENUE_IDS.get(venue)
    client = None
    for candidate in _ALIASES.get(exchange_id or "", (exchange_id,)):
        klass = getattr(ccxt, candidate, None) if candidate else None
        if klass is None:
            continue
        try:
            options = {"enableRateLimit": True, "timeout": 25000}
            if market_type == "Futures":
                options["options"] = {"defaultType": "swap"}
            client = klass(options)
            client.load_markets()
            break
        except Exception:  # noqa: BLE001 - an unreachable venue is not fatal.
            client = None
    _CLIENTS[key] = client
    return client


def _market_type_of(market: dict[str, Any]) -> str | None:
    """Spot or Futures, as the board names them."""
    if market.get("swap") and market.get("inverse") is not True:
        return "Futures"
    if market.get("spot"):
        return "Spot"
    return None


def sweep_venue(
    venue: str,
    *,
    store: live_book_cache.LiveBookStore,
    client_factory: Any = None,
) -> int:
    """Write every priced symbol this venue lists into the live book store."""
    if venue in SKIP_VENUES:
        return 0
    written = 0
    for market_type in ("Spot", "Futures"):
        try:
            client = (client_factory or _client)(venue, market_type)
            if client is None or not getattr(client, "has", {}).get("fetchTickers"):
                continue
            tickers = client.fetch_tickers()
        except Exception:  # noqa: BLE001 - one venue must not stop the sweep.
            continue
        markets = getattr(client, "markets", {}) or {}
        now_us = int(time.time() * 1_000_000)
        for symbol, ticker in (tickers or {}).items():
            bid = ticker.get("bid")
            ask = ticker.get("ask")
            if not bid or not ask or bid <= 0 or ask <= 0:
                continue
            if _market_type_of(markets.get(symbol) or {}) != market_type:
                continue
            timestamp_ms = ticker.get("timestamp")
            try:
                store.put(
                    venue,
                    market_type,
                    str(symbol),
                    bids=[[float(bid), float(ticker.get("bidVolume") or 0.0)]],
                    asks=[[float(ask), float(ticker.get("askVolume") or 0.0)]],
                    quote_ts_us=(
                        int(float(timestamp_ms) * 1000)
                        if timestamp_ms
                        else now_us
                    ),
                )
                written += 1
            except (TypeError, ValueError):
                continue
    return written


def sweep(
    venues: list[str] | None = None,
    *,
    store: live_book_cache.LiveBookStore | None = None,
    budget_seconds: float = 120.0,
) -> dict[str, Any]:
    """One pass over every venue, bounded so it cannot run into the next one."""
    target = store or live_book_cache.LiveBookStore()
    deadline = time.monotonic() + budget_seconds
    started = time.monotonic()
    written = 0
    covered = 0
    for venue in venues if venues is not None else sorted(VENUE_IDS):
        if time.monotonic() >= deadline:
            break
        count = sweep_venue(venue, store=target)
        if count:
            covered += 1
            written += count
    return {
        "status": "ok",
        "venues": covered,
        "quotes": written,
        "seconds": round(time.monotonic() - started, 1),
        "updated_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
    }


INTERVAL_SECONDS = max(15.0, float(os.environ.get("SPREADBOARD_BULK_QUOTE_SECONDS", "45")))
