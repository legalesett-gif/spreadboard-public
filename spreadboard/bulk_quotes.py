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
import json
import os
from pathlib import Path
import time
from typing import Any

from spreadboard import live_book_cache
from spreadboard.fast_quotes import VENUE_IDS

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))

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


#: Where the last sweep stopped. Without this every pass starts at the same
#: venue, so when the budget runs out it is always the same later venues that
#: go unswept -- observed as 18 venues one pass, then 10, then 1, with coverage
#: swinging between 42% and 76%. The funding sweep has always rotated for this
#: reason; prices need it for the same reason.
_CURSOR = {"index": 0}


def sweep(
    venues: list[str] | None = None,
    *,
    store: live_book_cache.LiveBookStore | None = None,
    budget_seconds: float = 180.0,
) -> dict[str, Any]:
    """One pass over the venues, resuming where the last pass ran out of time."""
    target = store or live_book_cache.LiveBookStore()
    deadline = time.monotonic() + budget_seconds
    started = time.monotonic()
    written = 0
    covered = 0
    ordered = venues if venues is not None else sorted(VENUE_IDS)
    start = _CURSOR["index"] % max(1, len(ordered))
    rotation = ordered[start:] + ordered[:start]
    position = start
    for venue in rotation:
        if time.monotonic() >= deadline:
            break
        position = (ordered.index(venue) + 1) % max(1, len(ordered))
        count = sweep_venue(venue, store=target)
        if count:
            covered += 1
            written += count
    _CURSOR["index"] = position
    return {
        "status": "ok",
        "venues": covered,
        "quotes": written,
        "seconds": round(time.monotonic() - started, 1),
        "updated_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
    }


INTERVAL_SECONDS = max(15.0, float(os.environ.get("SPREADBOARD_BULK_QUOTE_SECONDS", "45")))

#: Current funding per leg, written where the board can overlay it. The funding
#: sweep runs inside the quote worker, which is a fresh process every cycle, so
#: it pays load_markets for each venue and covers about three of eighteen per
#: pass: 554 of 5,382 futures legs carried no rate at all and 424 more disagreed
#: with the venue. Here the clients are already loaded, so a full pass is cheap.
FUNDING_CACHE_PATH = RUNTIME_DIR / "live_funding.json"


def sweep_funding(
    venues: list[str] | None = None,
    *,
    cache_path: Path | str = FUNDING_CACHE_PATH,
    budget_seconds: float = 120.0,
) -> dict[str, Any]:
    """One bulk funding call per venue, for every leg the venue lists."""
    deadline = time.monotonic() + budget_seconds
    started = time.monotonic()
    rates: dict[str, dict[str, Any]] = {}
    covered = 0
    for venue in venues if venues is not None else sorted(VENUE_IDS):
        if time.monotonic() >= deadline:
            break
        client = _client(venue, "Futures")
        if client is None or not getattr(client, "has", {}).get("fetchFundingRates"):
            continue
        try:
            payload = client.fetch_funding_rates()
        except Exception:  # noqa: BLE001 - one venue must not stop the sweep.
            continue
        items = payload.values() if isinstance(payload, dict) else payload
        wrote = 0
        for item in items or []:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            rate = item.get("fundingRate")
            if rate is None:
                continue
            interval = item.get("interval")
            if isinstance(interval, str) and interval.casefold().endswith("h"):
                interval = interval[:-1]
            try:
                entry: dict[str, Any] = {"rate_pct": float(rate) * 100.0}
            except (TypeError, ValueError):
                continue
            try:
                if interval is not None:
                    entry["interval_hours"] = float(interval)
            except (TypeError, ValueError):
                pass
            next_ms = item.get("fundingTimestamp") or item.get("nextFundingTimestamp")
            try:
                if next_ms:
                    entry["next_funding_ts_us"] = int(float(next_ms) * 1000)
            except (TypeError, ValueError):
                pass
            rates[f"{venue}|{item['symbol']}"] = entry
            wrote += 1
        if wrote:
            covered += 1
    payload_out = {
        "schema": "spreadboard.live_funding.v1",
        "updated_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
        "legs": rates,
    }
    path = Path(cache_path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload_out, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
    return {
        "status": "ok",
        "venues": covered,
        "legs": len(rates),
        "seconds": round(time.monotonic() - started, 1),
    }


_FUNDING_CACHE: dict[str, Any] = {"stamp": None, "legs": {}}


def load_funding(*, cache_path: Path | str = FUNDING_CACHE_PATH) -> dict[str, dict[str, Any]]:
    """The cached rates, re-read only when the file changes."""
    path = Path(cache_path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _FUNDING_CACHE["stamp"] != stamp:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        _FUNDING_CACHE["legs"] = payload.get("legs") or {}
        _FUNDING_CACHE["stamp"] = stamp
    return _FUNDING_CACHE["legs"]
