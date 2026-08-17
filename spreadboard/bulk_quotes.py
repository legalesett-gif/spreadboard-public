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

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

from spreadboard import fair_price, live_book_cache, ourbit_quotes
from spreadboard.fast_quotes import VENUE_IDS

LOGGER = logging.getLogger("spreadboard.bulk_quotes")

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))

#: Venues whose bulk ticker carries no bid/ask. Coinbase returns 528 symbols
#: with neither, so calling it every cycle buys nothing.
SKIP_VENUES: set[str] = {"Coinbase"}

#: CCXT renamed some adapters; VENUE_IDS still carries the older ids.
_ALIASES = {"gateio": ("gate", "gateio"), "coinbaseexchange": ("coinbaseexchange", "coinbase")}

#: One loaded client per venue -- not one per venue and market type.
#:
#: `load_markets()` returns the same set whichever `defaultType` the client was
#: built with: Binance answers with 4,552 markets (3,670 spot, 848 swap) either
#: way, Gate with 6,297, Bitget with 2,017. So a second client per venue held a
#: byte-for-byte duplicate of the first venue's market metadata. Binance's pair
#: alone measured 303MB, and across 21 venues the duplication was gigabytes --
#: enough to OOM-kill the service inside its 6GB cgroup roughly hourly, taking
#: the scan and the site with it. `defaultType` is read at call time, so one
#: client can serve both sweeps.
_CLIENTS: dict[str, Any] = {}

_DEFAULT_TYPES = {"Futures": "swap", "Spot": "spot"}

# Venues have independent adapters and rate limits. Four simultaneous venue
# calls keeps memory bounded while bringing a complete pass inside the
# current-spread boundary. The previous sequential pass took about 127 seconds,
# so a 90-second current-only board could never hold every cross-venue pair at
# once even when every provider answered successfully.
BULK_QUOTE_WORKERS = max(
    1, min(8, int(os.environ.get("SPREADBOARD_BULK_QUOTE_WORKERS", "6")))
)


def _client(venue: str, market_type: str) -> Any:
    """The venue's client, pointed at the market type this sweep wants."""
    client = _CLIENTS.get(venue) if venue in _CLIENTS else None
    if venue not in _CLIENTS:
        import ccxt

        exchange_id = VENUE_IDS.get(venue)
        for candidate in _ALIASES.get(exchange_id or "", (exchange_id,)):
            klass = getattr(ccxt, candidate, None) if candidate else None
            if klass is None:
                continue
            try:
                client = klass({"enableRateLimit": True, "timeout": 25000})
                client.load_markets()
                break
            except Exception:  # noqa: BLE001 - an unreachable venue is not fatal.
                client = None
        _CLIENTS[venue] = client
    if client is None:
        return None
    try:
        client.options["defaultType"] = _DEFAULT_TYPES.get(market_type, "spot")
    except Exception:  # noqa: BLE001 - a client without options still fetches.
        pass
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
    fair_price_rows: list[dict[str, Any]] | None = None,
) -> int:
    """Write every priced symbol this venue lists into the live book store.

    Also collects how far each contract trades from the venue's own fair price,
    when `fair_price_rows` is given. The tickers are already in hand, so that
    signal costs nothing beyond reading two more fields.
    """
    if venue in SKIP_VENUES:
        return 0
    pending_books: list[dict[str, Any]] = []
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
            market = markets.get(symbol) or {}
            try:
                contract_size = (
                    float(market.get("contractSize") or 1.0)
                    if market_type == "Futures"
                    else 1.0
                )
            except (TypeError, ValueError):
                contract_size = 1.0
            if contract_size <= 0:
                contract_size = 1.0
            if fair_price_rows is not None and market_type == "Futures":
                row = fair_price.deviation(venue, str(symbol), ticker)
                if row is not None:
                    fair_price_rows.append(row)
            timestamp_ms = ticker.get("timestamp")
            try:
                pending_books.append({
                    "venue": venue,
                    "market_type": market_type,
                    "symbol": str(symbol),
                    "bids": [[
                        float(bid),
                        float(ticker.get("bidVolume") or 0.0) * contract_size,
                    ]],
                    "asks": [[
                        float(ask),
                        float(ticker.get("askVolume") or 0.0) * contract_size,
                    ]],
                    "quote_ts_us": (
                        int(float(timestamp_ms) * 1000)
                        if timestamp_ms
                        else now_us
                    ),
                    "source": "bulk_ticker",
                })
            except (TypeError, ValueError):
                continue
    put_many = getattr(store, "put_many", None)
    if callable(put_many):
        return int(put_many(pending_books) or 0)
    for book in pending_books:
        store.put(
            book["venue"],
            book["market_type"],
            book["symbol"],
            bids=book["bids"],
            asks=book["asks"],
            quote_ts_us=book["quote_ts_us"],
            source=book["source"],
        )
    return len(pending_books)


#: Where the last sweep stopped. Without this every pass starts at the same
#: venue, so when the budget runs out it is always the same later venues that
#: go unswept -- observed as 18 venues one pass, then 10, then 1, with coverage
#: swinging between 42% and 76%. The funding sweep has always rotated for this
#: reason; prices need it for the same reason.
_CURSOR = {"index": 0}

#: The cursor outlives the process that advances it.
#:
#: The sweep runs as a worker that exits after each pass, so an in-memory cursor
#: would reset to zero every time and the rotation would stop rotating -- the
#: same starvation it was added to fix, just with a new cause.
CURSOR_PATH = RUNTIME_DIR / "bulk_quote_cursor.json"


def _load_cursor(count: int) -> int:
    try:
        stored = json.loads(Path(CURSOR_PATH).read_text(encoding="utf-8"))
        return int(stored.get("index", 0)) % max(1, count)
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return _CURSOR["index"] % max(1, count)


def _store_cursor(index: int) -> None:
    _CURSOR["index"] = index
    try:
        path = Path(CURSOR_PATH)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"index": index}), encoding="utf-8")
        temporary.replace(path)
    except OSError:  # noqa: S110 - a cursor that cannot be saved is not fatal.
        pass


def sweep(
    venues: list[str] | None = None,
    *,
    store: live_book_cache.LiveBookStore | None = None,
    budget_seconds: float = 180.0,
    workers: int | None = None,
) -> dict[str, Any]:
    """One bounded-concurrent pass, resuming where a prior pass timed out."""
    target = store or live_book_cache.LiveBookStore()
    deadline = time.monotonic() + budget_seconds
    started = time.monotonic()
    written = 0
    covered = 0
    fair_price_rows: list[dict[str, Any]] = []
    # Ourbit has no CCXT adapter, so it is absent from VENUE_IDS and would
    # never be priced by the rotation below. It is swept natively first, on its
    # own budget, so a slow rotation cannot starve the venue whose absence was
    # costing us whole routes.
    if venues is None:
        try:
            written += ourbit_quotes.sweep(
                store=target,
                depth_priority=_ourbit_depth_priority(),
                # Protection is DISABLED: skipping ticker refreshes starved the
                # protected symbols, because depth reaches 25 of 711 contracts a
                # pass and cannot refresh them faster than they go stale. A
                # one-level book that is current beats a fifty-level book that
                # is not, so the ticker keeps every symbol alive until depth
                # coverage is dense enough to carry them on its own.
                protected_symbols=None,
            )
            covered += 1
        except Exception:
            LOGGER.warning("ourbit native sweep failed", exc_info=True)
    ordered = venues if venues is not None else sorted(VENUE_IDS)
    start = _load_cursor(len(ordered))
    rotation = ordered[start:] + ordered[:start]
    position = start
    concurrency = max(1, min(int(workers or BULK_QUOTE_WORKERS), len(rotation) or 1))
    next_index = 0
    in_flight: dict[Future[int], tuple[str, int]] = {}
    with ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="bulk-quote-venue",
    ) as executor:
        while next_index < len(rotation) or in_flight:
            while (
                next_index < len(rotation)
                and len(in_flight) < concurrency
                and time.monotonic() < deadline
            ):
                venue = rotation[next_index]
                ordered_index = ordered.index(venue)
                future = executor.submit(
                    sweep_venue,
                    venue,
                    store=target,
                    fair_price_rows=fair_price_rows,
                )
                in_flight[future] = (venue, ordered_index)
                next_index += 1
                # The cursor follows attempts, not successes: a dead venue must
                # not starve every venue behind it on the next worker process.
                position = (ordered_index + 1) % max(1, len(ordered))
            if not in_flight:
                break
            done, _pending = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
            for future in done:
                in_flight.pop(future, None)
                try:
                    count = int(future.result() or 0)
                except Exception:  # noqa: BLE001 - one venue is isolated.
                    count = 0
                if count:
                    covered += 1
                    written += count
    _store_cursor(position)
    deviations = fair_price.write(fair_price_rows)
    return {
        "status": "ok",
        "venues": covered,
        "quotes": written,
        "fair_price_deviations": deviations,
        "seconds": round(time.monotonic() - started, 1),
        "updated_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
    }


# The pass itself is non-overlapping: the loop sleeps only after its worker
# exits. A 15-second hard minimum silently turned an 85-second production pass
# into a 100-second cadence despite a 90-second truth boundary. Keep a small
# configurable breath without contradicting that boundary.
INTERVAL_SECONDS = max(1.0, float(os.environ.get("SPREADBOARD_BULK_QUOTE_SECONDS", "5")))

#: Current funding per leg, written where the board can overlay it. The funding
#: sweep runs inside the quote worker, which is a fresh process every cycle, so
#: it pays load_markets for each venue and covers about three of eighteen per
#: pass: 554 of 5,382 futures legs carried no rate at all and 424 more disagreed
#: with the venue. Here the clients are already loaded, so a full pass is cheap.
FUNDING_CACHE_PATH = RUNTIME_DIR / "live_funding.json"
FUNDING_MAX_AGE_SECONDS = max(
    300.0, float(os.environ.get("SPREADBOARD_FUNDING_MAX_AGE_SECONDS", "1800"))
)


def sweep_funding(
    venues: list[str] | None = None,
    *,
    cache_path: Path | str = FUNDING_CACHE_PATH,
    budget_seconds: float = 180.0,
    merge_existing: bool = False,
) -> dict[str, Any]:
    """One bulk funding call per venue, for every leg the venue lists.

    Goes through FastQuoteRefresher._bulk_funding_rates rather than calling
    CCXT directly: eight of the eighteen futures venues publish no bulk funding
    through CCXT and are served by native endpoints instead. Calling CCXT alone
    left Ourbit (845 legs) and XT (688) without a rate at all, along with Mexc,
    HTX, BitMart and both Kraken and Kucoin futures.
    """
    from spreadboard.fast_quotes import FastQuoteRefresher

    deadline = time.monotonic() + budget_seconds
    started = time.monotonic()
    rates: dict[str, dict[str, Any]] = {}
    leg_updated_at: dict[str, float] = {}
    path = Path(cache_path)
    if merge_existing:
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            rates.update(previous.get("legs") or {})
            fallback_stamp = _iso_timestamp(previous.get("updated_at")) or time.time()
            leg_updated_at.update(
                {
                    str(key): float(value)
                    for key, value in (previous.get("leg_updated_at") or {}).items()
                    if _float(value) is not None
                }
            )
            for key in rates:
                leg_updated_at.setdefault(str(key), fallback_stamp)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    covered = 0
    refresher = FastQuoteRefresher()
    try:
        # Ourbit has no CCXT adapter and so is absent from VENUE_IDS, which is
        # what this iterated: 844 of its legs never had a rate asked for. It is
        # served by a native endpoint in NATIVE_FUNDING_SOURCES.
        from spreadboard.fast_quotes import NATIVE_FUNDING_SOURCES

        all_venues = sorted(set(VENUE_IDS) | set(NATIVE_FUNDING_SOURCES))
        for venue in venues if venues is not None else all_venues:
            if time.monotonic() >= deadline:
                break
            try:
                venue_rates = refresher._bulk_funding_rates(venue) or {}
            except Exception:  # noqa: BLE001 - one venue must not stop the sweep.
                continue
            if not venue_rates:
                continue
            covered += 1
            for symbol, fields in venue_rates.items():
                entry: dict[str, Any] = {}
                if fields.get("current_funding_pct") is not None:
                    entry["rate_pct"] = fields["current_funding_pct"]
                if fields.get("funding_interval_hours") is not None:
                    entry["interval_hours"] = fields["funding_interval_hours"]
                if fields.get("next_funding_ts_us") is not None:
                    entry["next_funding_ts_us"] = fields["next_funding_ts_us"]
                if entry:
                    key = f"{venue}|{symbol}"
                    rates[key] = entry
                    leg_updated_at[key] = time.time()
    finally:
        try:
            refresher.close()
        except Exception:  # noqa: BLE001
            pass

    cutoff = time.time() - FUNDING_MAX_AGE_SECONDS
    rates = {
        key: entry
        for key, entry in rates.items()
        if float(leg_updated_at.get(key) or 0.0) >= cutoff
    }
    leg_updated_at = {key: leg_updated_at[key] for key in rates}
    payload_out = {
        "schema": "spreadboard.live_funding.v1",
        "updated_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
        "legs": rates,
        "leg_updated_at": leg_updated_at,
    }
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
        legs = payload.get("legs") or {}
        timestamps = payload.get("leg_updated_at") or {}
        fallback_stamp = _iso_timestamp(payload.get("updated_at"))
        cutoff = time.time() - FUNDING_MAX_AGE_SECONDS
        _FUNDING_CACHE["legs"] = {
            str(key): value
            for key, value in legs.items()
            if float(_float(timestamps.get(key)) or fallback_stamp or 0.0) >= cutoff
        }
        _FUNDING_CACHE["stamp"] = stamp
    return _FUNDING_CACHE["legs"]


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_timestamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ourbit_depth_priority() -> list[str]:
    """Ourbit contracts the board is actually showing, most prominent first.

    Depth is the scarce resource here -- one request per contract on a box with
    no CPU to spare -- so it should land on the tokens a subscriber can see
    rather than on whatever happens to sort first alphabetically.
    """
    try:
        from spreadboard import api_spreads

        # The whole board, not its first page. Sourcing priority from the top
        # 250 meant a token ranked below that could never receive depth, and
        # without depth its spread cannot form -- so it could never rank higher
        # either. UNITREE sat in exactly that loop.
        data = api_spreads.load_spreads(limit=None)
    except Exception:  # noqa: BLE001 - priority is an optimisation, never a requirement
        return []
    tokens: list[str] = []
    for group in data.get("groups") or []:
        token = str(group.get("token") or "").strip().upper()
        if token and token not in tokens:
            tokens.append(token)
    return [f"{token}_USDT" for token in tokens]
