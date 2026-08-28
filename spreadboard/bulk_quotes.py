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
import math
import os
from pathlib import Path
import time
from typing import Any
from urllib.request import Request, urlopen

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

# Venues have independent adapters and rate limits. The complete production
# pass grew to 91–98 seconds at eight and twelve workers, beyond the unchanged
# 90-second current-spread boundary. A measured one-worker-per-venue pass takes
# about 38 seconds because the independent provider waits no longer queue in
# waves. It remains one bounded process with the same client set and SQLite
# writer; no quote receives a longer lifetime.
BULK_QUOTE_WORKERS = max(
    1, min(24, int(os.environ.get("SPREADBOARD_BULK_QUOTE_WORKERS", "12")))
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


def _public_json(url: str) -> Any:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"},
    )
    with urlopen(request, timeout=25.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _public_post_json(url: str, payload: dict[str, Any]) -> Any:
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "SpreadBoard/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=25.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _native_aster_books(*, fetcher: Any = None) -> list[dict[str, Any]]:
    """Return Aster's complete USDT spot and perpetual top books.

    CCXT's bulk adapter has repeatedly omitted Aster's newer contracts even
    while the venue's own all-symbol endpoint listed and priced them.  That
    made a DEX with hundreds of markets look like a seven-token shortlist.
    Aster exposes Binance-compatible all-symbol book tickers, so one public
    request per instrument family covers the whole venue without per-market
    polling or private credentials.
    """

    get_json = fetcher or _public_json
    output: list[dict[str, Any]] = []
    endpoints = (
        ("Futures", "https://fapi.asterdex.com/fapi/v1/ticker/bookTicker"),
        ("Spot", "https://sapi.asterdex.com/api/v1/ticker/bookTicker"),
    )
    for market_type, endpoint in endpoints:
        try:
            payload = get_json(endpoint)
        except Exception as exc:  # noqa: BLE001 - keep the other public family healthy.
            LOGGER.warning("Aster native %s bulk failed: %s", market_type, type(exc).__name__)
            continue
        now_us = int(time.time() * 1_000_000)
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            compact = str(item.get("symbol") or "").upper()
            # SpreadBoard's product quote is USDT. Do not guess how an
            # unfamiliar suffix maps into a unified exchange symbol.
            if not compact.endswith("USDT") or len(compact) <= 4:
                continue
            base = compact[:-4]
            symbol = (
                f"{base}/USDT:USDT" if market_type == "Futures" else f"{base}/USDT"
            )
            try:
                bid = float(item.get("bidPrice") or 0.0)
                ask = float(item.get("askPrice") or 0.0)
                bid_amount = float(item.get("bidQty") or 0.0)
                ask_amount = float(item.get("askQty") or 0.0)
                timestamp_ms = float(item.get("time") or 0.0)
            except (TypeError, ValueError):
                continue
            if bid <= 0 or ask <= 0 or bid_amount <= 0 or ask_amount <= 0:
                continue
            output.append(
                {
                    "venue": "Aster",
                    "market_type": market_type,
                    "symbol": symbol,
                    "bids": [[bid, bid_amount]],
                    "asks": [[ask, ask_amount]],
                    "quote_ts_us": int(timestamp_ms * 1000) if timestamp_ms > 0 else now_us,
                    "source": "native_bulk_ticker",
                }
            )
    return output


_NATIVE_COMPLETE_BULK_VENUES = {"Binance", "Kucoin Futures"}
_NATIVE_PARTIAL_BULK_VENUES = {"Phemex", "WhiteBIT"}
_NATIVE_FUTURES_ONLY_BULK_VENUES = {"Hyperliquid"}
_NATIVE_LINEAR_QUOTES = ("USDT", "USDC", "USD")


def _native_bulk_books(
    venue: str,
    *,
    fetcher: Any = None,
    poster: Any = None,
) -> list[dict[str, Any]]:
    """Fast first-party BBO snapshots for adapters CCXT leaves one-sided.

    Production's generic sweep had no Binance futures or KuCoin Futures books
    at all, and its Phemex/WhiteBIT futures coverage depended on whichever side
    of a 90+ second CCXT rotation happened to finish last. These public venue
    endpoints return the complete family in one request. A provider that does
    not publish size remains a useful current top-book lead with amount zero;
    downstream depth verification deliberately fails closed for that row.
    """

    get_json = fetcher or _public_json
    post_json = poster or _public_post_json
    now_us = int(time.time() * 1_000_000)
    output: list[dict[str, Any]] = []

    def pair(compact: Any, *, perpetual: bool) -> tuple[str, str] | None:
        value = str(compact or "").upper().replace("-", "").replace("_", "")
        if perpetual and value.endswith("PERP"):
            value = value[:-4] + "USDT"
        for quote in _NATIVE_LINEAR_QUOTES:
            if value.endswith(quote) and len(value) > len(quote):
                return value[: -len(quote)], quote
        return None

    def append(
        *,
        market_type: str,
        base: Any,
        quote: Any,
        bid: Any,
        ask: Any,
        bid_size: Any = 0.0,
        ask_size: Any = 0.0,
        timestamp_us: Any = None,
    ) -> None:
        normalized_base = {"XBT": "BTC", "XDG": "DOGE"}.get(
            str(base or "").upper(), str(base or "").upper()
        )
        normalized_quote = str(quote or "").upper()
        try:
            bid_value = float(bid or 0.0)
            ask_value = float(ask or 0.0)
            bid_amount = max(0.0, float(bid_size or 0.0))
            ask_amount = max(0.0, float(ask_size or 0.0))
            quote_ts_us = int(timestamp_us or now_us)
        except (TypeError, ValueError):
            return
        if (
            not normalized_base
            or normalized_quote not in _NATIVE_LINEAR_QUOTES
            or bid_value <= 0
            or ask_value <= 0
            or bid_value > ask_value
        ):
            return
        symbol = f"{normalized_base}/{normalized_quote}"
        if market_type == "Futures":
            symbol += f":{normalized_quote}"
        output.append(
            {
                "venue": venue,
                "market_type": market_type,
                "symbol": symbol,
                "bids": [[bid_value, bid_amount]],
                "asks": [[ask_value, ask_amount]],
                "quote_ts_us": quote_ts_us,
                "source": "native_bulk_ticker",
            }
        )

    if venue == "Binance":
        endpoints = (
            ("Spot", "https://api.binance.com/api/v3/ticker/bookTicker"),
            ("Futures", "https://fapi.binance.com/fapi/v1/ticker/bookTicker"),
        )
        for market_type, endpoint in endpoints:
            payload = get_json(endpoint)
            for item in payload if isinstance(payload, list) else []:
                if not isinstance(item, dict):
                    continue
                identity = pair(item.get("symbol"), perpetual=market_type == "Futures")
                if identity is None:
                    continue
                timestamp_ms = _float(item.get("time"))
                append(
                    market_type=market_type,
                    base=identity[0],
                    quote=identity[1],
                    bid=item.get("bidPrice"),
                    ask=item.get("askPrice"),
                    bid_size=item.get("bidQty"),
                    ask_size=item.get("askQty"),
                    timestamp_us=(int(timestamp_ms * 1000) if timestamp_ms else now_us),
                )
        return output

    if venue == "Kucoin Futures":
        tickers = get_json("https://api-futures.kucoin.com/api/v1/allTickers")
        contracts = get_json("https://api-futures.kucoin.com/api/v1/contracts/active")
        contract_rows = contracts.get("data") if isinstance(contracts, dict) else []
        if isinstance(contract_rows, dict):
            contract_rows = [contract_rows]
        contract_by_id = {
            str(item.get("symbol") or "").upper(): item
            for item in contract_rows or []
            if isinstance(item, dict)
        }
        ticker_rows = tickers.get("data") if isinstance(tickers, dict) else []
        for item in ticker_rows or []:
            if not isinstance(item, dict):
                continue
            market_id = str(item.get("symbol") or "").upper()
            contract = contract_by_id.get(market_id) or {}
            if contract.get("isInverse") is True:
                continue
            if str(contract.get("status") or "Open").casefold() not in {
                "open",
                "trading",
            }:
                continue
            base = contract.get("baseCurrency")
            quote = contract.get("quoteCurrency")
            if not base or not quote:
                compact = market_id.removesuffix("M")
                identity = pair(compact, perpetual=True)
                if identity is None:
                    continue
                base, quote = identity
            multiplier = abs(_float(contract.get("multiplier")) or 1.0)
            timestamp_ns = _float(item.get("ts"))
            append(
                market_type="Futures",
                base=base,
                quote=quote,
                bid=item.get("bestBidPrice"),
                ask=item.get("bestAskPrice"),
                bid_size=(_float(item.get("bestBidSize")) or 0.0) * multiplier,
                ask_size=(_float(item.get("bestAskSize")) or 0.0) * multiplier,
                timestamp_us=(int(timestamp_ns / 1000) if timestamp_ns else now_us),
            )
        return output

    if venue == "Phemex":
        payload = get_json("https://api.phemex.com/md/v3/ticker/24hr/all")
        rows = payload.get("result") if isinstance(payload, dict) else []
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            identity = pair(item.get("symbol"), perpetual=True)
            if identity is None:
                continue
            timestamp_ns = _float(item.get("timestamp"))
            append(
                market_type="Futures",
                base=identity[0],
                quote=identity[1],
                bid=item.get("bidRp"),
                ask=item.get("askRp"),
                timestamp_us=(int(timestamp_ns / 1000) if timestamp_ns else now_us),
            )
        return output

    if venue == "WhiteBIT":
        payload = get_json("https://whitebit.com/api/v4/public/futures")
        rows = payload.get("result") if isinstance(payload, dict) else payload
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("product_type") or "Perpetual").casefold() != "perpetual":
                continue
            append(
                market_type="Futures",
                base=item.get("stock_currency")
                or str(item.get("ticker_id") or "").removesuffix("_PERP"),
                quote=item.get("money_currency") or "USDT",
                bid=item.get("bid"),
                ask=item.get("ask"),
            )
        return output

    if venue == "Hyperliquid":
        endpoint = "https://api.hyperliquid.xyz/info"
        dex_payload = post_json(endpoint, {"type": "perpDexs"})
        dex_names = [""]
        for item in dex_payload if isinstance(dex_payload, list) else []:
            name = item.get("name") if isinstance(item, dict) else item
            normalized = str(name or "").strip()
            if normalized and normalized not in dex_names:
                dex_names.append(normalized)
        for dex_name in dex_names:
            request_payload: dict[str, Any] = {"type": "metaAndAssetCtxs"}
            if dex_name:
                request_payload["dex"] = dex_name
            payload = post_json(endpoint, request_payload)
            if not isinstance(payload, list) or len(payload) < 2:
                continue
            metadata = payload[0] if isinstance(payload[0], dict) else {}
            universe = metadata.get("universe") or []
            contexts = payload[1] if isinstance(payload[1], list) else []
            for market, context in zip(universe, contexts, strict=False):
                if not isinstance(market, dict) or not isinstance(context, dict):
                    continue
                if market.get("isDelisted") is True:
                    continue
                coin = str(market.get("name") or "").strip()
                impact_prices = context.get("impactPxs")
                if not coin or not isinstance(impact_prices, list) or len(impact_prices) < 2:
                    continue
                if ":" in coin:
                    namespace, ticker = coin.split(":", 1)
                    base = f"{namespace.upper()}-{ticker.upper()}"
                else:
                    base = coin.upper()
                append(
                    market_type="Futures",
                    base=base,
                    quote="USDC",
                    bid=impact_prices[0],
                    ask=impact_prices[1],
                )
        return output

    return output


def _write_books(store: live_book_cache.LiveBookStore, books: list[dict[str, Any]]) -> int:
    put_many = getattr(store, "put_many", None)
    if callable(put_many):
        return int(put_many(books) or 0)
    for book in books:
        store.put(
            book["venue"],
            book["market_type"],
            book["symbol"],
            bids=book["bids"],
            asks=book["asks"],
            quote_ts_us=book["quote_ts_us"],
            source=book["source"],
        )
    return len(books)


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
    if venue == "Aster" and client_factory is None:
        # The native response is complete and materially broader than the CCXT
        # adapter. Fall back to CCXT only if both public families fail.
        native_books = _native_aster_books()
        if native_books:
            return _write_books(store, native_books)
    native_written = 0
    if (
        client_factory is None
        and venue
        in (
            _NATIVE_COMPLETE_BULK_VENUES
            | _NATIVE_PARTIAL_BULK_VENUES
            | _NATIVE_FUTURES_ONLY_BULK_VENUES
        )
    ):
        try:
            native_books = _native_bulk_books(venue)
        except Exception:  # The generic adapter remains a fallback.
            LOGGER.warning("%s native bulk sweep failed", venue, exc_info=True)
            native_books = []
        if native_books:
            native_written = _write_books(store, native_books)
            if venue in (
                _NATIVE_COMPLETE_BULK_VENUES | _NATIVE_FUTURES_ONLY_BULK_VENUES
            ):
                # Hyperliquid publishes a complete bulk perpetual BBO through
                # impactPxs. Its spot metadata has only mark/mid prices, not a
                # book, so do not fabricate spot spreads or let the slow CCXT
                # fallback hold every venue past the live-truth budget. Exact
                # spot readers remain available for individually demanded legs.
                return native_written
    pending_books: list[dict[str, Any]] = []
    market_types = (
        ("Spot",)
        if native_written and venue in _NATIVE_PARTIAL_BULK_VENUES
        else ("Spot", "Futures")
    )
    for market_type in market_types:
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
    return native_written + _write_books(store, pending_books)


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
    # Aster's complete native response takes well under a second. Refresh it
    # before the slower cross-venue pass for immediate startup coverage, then
    # again at publication so its books do not spend the entire 90-second
    # client freshness window ageing behind the other venues.
    aster_opening_count = 0
    if venues is None:
        try:
            aster_opening_count = sweep_venue("Aster", store=target)
        except Exception:
            LOGGER.warning("Aster opening bulk sweep failed", exc_info=True)
    # Ourbit has no CCXT adapter, so it is absent from VENUE_IDS and would
    # never be priced by the rotation below. It is swept natively first, on its
    # own budget, so a slow rotation cannot starve the venue whose absence was
    # costing us whole routes.
    if venues is None:
        try:
            ourbit_count = ourbit_quotes.sweep(
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
            written += ourbit_count
            covered += int(ourbit_count > 0)
        except Exception:
            LOGGER.warning("ourbit native sweep failed", exc_info=True)
    ordered = (
        venues
        if venues is not None
        else sorted(venue for venue in VENUE_IDS if venue != "Aster")
    )
    start = _load_cursor(len(ordered))
    rotation = ordered[start:] + ordered[:start]
    position = start
    concurrency = max(1, min(int(workers or BULK_QUOTE_WORKERS), len(rotation) or 1))
    next_index = 0
    in_flight: dict[Future[int], tuple[str, int]] = {}
    timed_out = False
    executor = ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="bulk-quote-venue",
    )
    try:
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            done, _pending = wait(
                tuple(in_flight),
                timeout=remaining,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                timed_out = True
                break
            for future in done:
                in_flight.pop(future, None)
                try:
                    count = int(future.result() or 0)
                except Exception:  # noqa: BLE001 - one venue is isolated.
                    count = 0
                if count:
                    covered += 1
                    written += count
    finally:
        # The disposable production worker calls os._exit after publishing the
        # summary, so a provider which exceeded the truth budget must not make
        # ThreadPoolExecutor.__exit__ wait for it anyway. Unit/in-process callers
        # can release their test/provider thread normally; pending work is never
        # counted as published evidence.
        executor.shutdown(wait=not timed_out, cancel_futures=timed_out)
    _store_cursor(position)
    if venues is None and timed_out and aster_opening_count:
        covered += 1
        written += aster_opening_count
    if venues is None and not timed_out:
        try:
            aster_closing_count = sweep_venue("Aster", store=target)
        except Exception:
            LOGGER.warning("Aster closing bulk sweep failed", exc_info=True)
            aster_closing_count = 0
        aster_count = aster_closing_count or aster_opening_count
        if aster_count:
            covered += 1
            written += aster_count
    deviations = fair_price.write(fair_price_rows) if not timed_out else 0
    return {
        "status": "ok",
        "venues": covered,
        "quotes": written,
        "fair_price_deviations": deviations,
        "timed_out": timed_out,
        "pending_venues": sorted(
            venue for venue, _ordered_index in in_flight.values()
        ),
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
    300.0, float(os.environ.get("SPREADBOARD_FUNDING_MAX_AGE_SECONDS", "600"))
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
                entry = _funding_entry(fields)
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


_FUNDING_CACHE: dict[str, Any] = {"stamp": None, "legs": {}, "health": {}}


def _funding_entry(fields: dict[str, Any]) -> dict[str, Any]:
    """One venue's funding print as the cache stores it.

    The interval's provenance travels with it. Dropped here, a schedule the
    venue published is indistinguishable from the 8h default by the time it
    reaches a row, and the badge that warns a reader the carry is a guess
    cannot be set correctly either way.
    """

    entry: dict[str, Any] = {}
    if fields.get("current_funding_pct") is not None:
        entry["rate_pct"] = fields["current_funding_pct"]
    if fields.get("funding_interval_hours") is not None:
        entry["interval_hours"] = fields["funding_interval_hours"]
        if fields.get("funding_interval_assumed") is not None:
            entry["interval_assumed"] = bool(fields["funding_interval_assumed"])
    if fields.get("next_funding_ts_us") is not None:
        entry["next_funding_ts_us"] = fields["next_funding_ts_us"]
    return entry


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
        now = time.time()
        accepted: dict[str, dict[str, Any]] = {}
        ages: list[float] = []
        for key, value in legs.items():
            observed_at = float(_float(timestamps.get(key)) or fallback_stamp or 0.0)
            if observed_at < cutoff:
                continue
            age_seconds = max(0.0, now - observed_at)
            accepted[str(key)] = {
                **(value if isinstance(value, dict) else {}),
                "observed_at": observed_at,
                "age_seconds": age_seconds,
            }
            ages.append(age_seconds)
        ages.sort()
        p95_index = min(len(ages) - 1, max(0, math.ceil(len(ages) * 0.95) - 1)) if ages else 0
        _FUNDING_CACHE["legs"] = accepted
        _FUNDING_CACHE["health"] = {
            "status": "fresh" if accepted else "stale_or_empty",
            "leg_count": len(accepted),
            "updated_at": payload.get("updated_at"),
            "max_age_seconds": round(ages[-1], 1) if ages else None,
            "p95_age_seconds": round(ages[p95_index], 1) if ages else None,
            "ttl_seconds": FUNDING_MAX_AGE_SECONDS,
        }
        _FUNDING_CACHE["stamp"] = stamp
    return _FUNDING_CACHE["legs"]


def funding_health(*, cache_path: Path | str = FUNDING_CACHE_PATH) -> dict[str, Any]:
    """Freshness of the exact per-leg current-funding cache."""
    load_funding(cache_path=cache_path)
    return dict(_FUNDING_CACHE.get("health") or {})


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
