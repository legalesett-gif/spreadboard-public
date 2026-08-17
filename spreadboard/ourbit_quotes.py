"""Ourbit prices, fetched natively because CCXT has no adapter for it.

Ourbit already supplied funding rates through NATIVE_FUNDING_SOURCES but never
prices: the bulk sweep iterates VENUE_IDS, which is CCXT's world, so 845 Ourbit
legs were carried for funding and priced by nobody. The visible cost was routes
that could not exist at all -- the reference product headlines UNITREE at 4.07%
on Mexc->Ourbit while our board shows 0.000%, because a widest-spread selection
can only choose among venues we actually carry.

Symbols are the whole risk here. Spot returns "BTCUSDT" with no separator,
futures returns "BTC_USDT", and the board keys spot as "BTC/USDT" and a linear
perpetual as "BTC/USDT:USDT". A wrong split does not fail loudly: the leg simply
never matches anything, which is indistinguishable from the venue having no
routes. So an unrecognised quote currency is refused rather than guessed.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger("spreadboard.ourbit")

VENUE = "Ourbit"
SPOT_TICKER_URL = "https://api.ourbit.com/api/v3/ticker/bookTicker"
FUTURES_TICKER_URL = "https://futures.ourbit.com/api/v1/contract/ticker"
FUTURES_DETAIL_URL = "https://futures.ourbit.com/api/v1/contract/detail"
FUTURES_DEPTH_URL = "https://futures.ourbit.com/api/v1/contract/depth/{symbol}"
#: One request per symbol, so this is bounded and rotated rather than a sweep
#: over all 711 contracts. The box is CPU-tight; a flood helps nobody.
DEPTH_SYMBOLS_PER_SWEEP = max(0, int(os.environ.get("SPREADBOARD_OURBIT_DEPTH_SYMBOLS", "40")))
REQUEST_TIMEOUT_SECONDS = 20.0

#: Longest first, so USDT is not matched inside a longer suffix.
QUOTE_CURRENCIES = ("USDT", "USDC", "USD", "BTC", "ETH")


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def spot_symbol(raw: str) -> str | None:
    """"BTCUSDT" -> "BTC/USDT". None when the quote currency is unknown."""
    text = str(raw or "").strip().upper()
    for quote in QUOTE_CURRENCIES:
        if text.endswith(quote) and len(text) > len(quote):
            return f"{text[: -len(quote)]}/{quote}"
    return None


def futures_symbol(raw: str) -> str | None:
    """"BTC_USDT" -> "BTC/USDT:USDT", the board's linear perpetual key."""
    text = str(raw or "").strip().upper()
    if "_" not in text:
        return None
    base, _, quote = text.partition("_")
    if not base or quote not in QUOTE_CURRENCIES:
        return None
    return f"{base}/{quote}:{quote}"


def _book(symbol: str, market_type: str, bid: float, bid_qty: float,
          ask: float, ask_qty: float, now_us: int) -> dict[str, Any]:
    return {
        "venue": VENUE,
        "market_type": market_type,
        "symbol": symbol,
        "bids": [[bid, bid_qty]],
        "asks": [[ask, ask_qty]],
        "quote_ts_us": now_us,
        # One level from a ticker is not an order book, and must never be
        # mistaken for the 50-level L2 the websocket worker collects.
        "source": "bulk_ticker",
    }


def spot_books(payload: Any, *, now_us: int) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    books = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        symbol = spot_symbol(entry.get("symbol"))
        bid = _float(entry.get("bidPrice"))
        ask = _float(entry.get("askPrice"))
        if symbol is None or bid is None or ask is None:
            continue
        books.append(_book(
            symbol, "Spot", bid, float(_float(entry.get("bidQty")) or 0.0),
            ask, float(_float(entry.get("askQty")) or 0.0), now_us,
        ))
    return books


def futures_books(payload: Any, *, now_us: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("success") is False:
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    books = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        symbol = futures_symbol(entry.get("symbol"))
        bid = _float(entry.get("bid1"))
        ask = _float(entry.get("ask1"))
        if symbol is None or bid is None or ask is None:
            continue
        # The contract ticker publishes no top-of-book size, so the size is
        # left at zero rather than invented. Depth verification then declines
        # this leg instead of vouching for liquidity nobody measured.
        books.append(_book(symbol, "Futures", bid, 0.0, ask, 0.0, now_us))
    return books


def _http_json(url: str) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"}
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_spot() -> Any:
    return _http_json(SPOT_TICKER_URL)


def fetch_futures() -> Any:
    return _http_json(FUTURES_TICKER_URL)


def sweep(
    *,
    store: Any,
    fetch_spot: Callable[[], Any] = fetch_spot,
    fetch_futures: Callable[[], Any] = fetch_futures,
    now_us: int | None = None,
    with_depth: bool = True,
    depth_priority: list[str] | None = None,
    protected_symbols: set[str] | None = None,
) -> int:
    """Write every priced Ourbit symbol into the live book store.

    Spot and futures are independent calls, and one being down must not cost
    the other: a venue half-present is far better than a venue absent.
    """
    stamp = now_us if now_us is not None else int(time.time() * 1_000_000)
    books: list[dict[str, Any]] = []
    # live_books is keyed venue|market_type|symbol, so a ticker book and a depth
    # book for the same contract are the SAME row. Depth reaches only a slice
    # of 711 contracts per pass, so an unguarded ticker pass flattened every
    # book outside that slice straight back to one level.
    guarded = {str(s) for s in (protected_symbols or set())}
    for fetch, build in ((fetch_spot, spot_books), (fetch_futures, futures_books)):
        try:
            produced = build(fetch(), now_us=stamp)
            books.extend(
                book for book in produced
                if not (book["market_type"] == "Futures" and book["symbol"] in guarded)
            )
        except Exception:
            # One endpoint must not stop the sweep, but a venue that quietly
            # stops answering should be visible rather than merely absent.
            LOGGER.warning("ourbit endpoint failed", exc_info=True)
            continue
    # Real L2 for a rotating slice of contracts. The ticker cannot size a
    # trade at all, so without this every Ourbit futures leg stays depth
    # unverified and its spread never forms.
    if with_depth and DEPTH_SYMBOLS_PER_SWEEP:
        try:
            sizes = contract_sizes(fetch_detail())
            symbols = depth_order(
                sorted(sizes),
                priority=list(depth_priority or []),
                count=DEPTH_SYMBOLS_PER_SWEEP,
            )
            books.extend(depth_books(symbols, sizes=sizes, now_us=stamp))
        except Exception:
            LOGGER.warning("ourbit depth sweep failed", exc_info=True)
    if not books:
        return 0
    put_many = getattr(store, "put_many", None)
    if callable(put_many):
        return int(put_many(books) or 0)
    for book in books:
        store.put(
            book["venue"], book["market_type"], book["symbol"],
            bids=book["bids"], asks=book["asks"],
            quote_ts_us=book["quote_ts_us"], source=book["source"],
        )
    return len(books)


# ---------------------------------------------------------------------------
# Real L2 depth
# ---------------------------------------------------------------------------


def contract_sizes(payload: Any) -> dict[str, float]:
    """symbol -> contractSize, in one call for all 711 contracts.

    Depth is quoted in contracts. 330 contracts of a 0.01 contract is 3.3
    tokens, not 330: assuming 1.0 would overstate liquidity a hundredfold and
    every size-gated decision downstream would inherit that.
    """
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("data")
    if not isinstance(rows, list):
        return {}
    sizes = {}
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        size = _float(entry.get("contractSize"))
        symbol = str(entry.get("symbol") or "")
        if symbol and size is not None:
            sizes[symbol] = size
    return sizes


def _levels(raw: Any, contract_size: float) -> list[list[float]]:
    levels = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        price = _float(entry[0])
        contracts = _float(entry[1])
        if price is None or contracts is None:
            continue
        levels.append([price, contracts * contract_size])
    return levels


def depth_book(
    raw_symbol: str, payload: Any, contract_size: float | None, *, now_us: int
) -> dict[str, Any] | None:
    """One venue-native L2 book, the only Ourbit source that can size a trade."""
    if contract_size is None or contract_size <= 0:
        return None
    if not isinstance(payload, dict) or payload.get("success") is False:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    symbol = futures_symbol(raw_symbol)
    if symbol is None:
        return None
    bids = _levels(data.get("bids"), contract_size)
    asks = _levels(data.get("asks"), contract_size)
    if not bids or not asks:
        return None
    return {
        "venue": VENUE,
        "market_type": "Futures",
        "symbol": symbol,
        "bids": bids,
        "asks": asks,
        "quote_ts_us": now_us,
        # Genuine multi-level depth, unlike the ticker-derived single level.
        "source": "public_rest_l2",
    }


def fetch_detail() -> Any:
    return _http_json(FUTURES_DETAIL_URL)


def fetch_depth(raw_symbol: str) -> Any:
    return _http_json(FUTURES_DEPTH_URL.format(symbol=raw_symbol))


def depth_books(
    raw_symbols: list[str],
    *,
    sizes: dict[str, float],
    fetch_depth: Callable[[str], Any] = fetch_depth,
    limit: int = DEPTH_SYMBOLS_PER_SWEEP,
    now_us: int | None = None,
) -> list[dict[str, Any]]:
    """Depth for a bounded slice of symbols; one dead call cannot stop the rest."""
    stamp = now_us if now_us is not None else int(time.time() * 1_000_000)
    books = []
    for raw in list(raw_symbols)[: max(0, int(limit))]:
        try:
            book = depth_book(raw, fetch_depth(raw), sizes.get(raw), now_us=stamp)
        except Exception:
            LOGGER.warning("ourbit depth failed for %s", raw, exc_info=True)
            continue
        if book is not None:
            books.append(book)
    return books


#: Where the last depth slice stopped, so successive sweeps cover different
#: contracts instead of refreshing the same forty for ever.
_DEPTH_CURSOR = {"index": 0}

#: Independent cursor for the priority list, so a long board rotates through
#: it rather than re-fetching its first page for ever.
_PRIORITY_CURSOR = {"index": 0}


def depth_order(
    symbols: list[str], *, priority: list[str] | None = None, count: int
) -> list[str]:
    """Which contracts to fetch depth for this sweep, most useful first.

    Alphabetical rotation alone left tokens waiting most of a cycle for the
    depth they need to form a spread at all -- UNITREE sat at 0.000% for want
    of a size. Symbols the board is actually showing come first; the rotation
    then fills the remainder so nothing starves.
    """
    listed = set(symbols)
    ordered: list[str] = []
    # The priority list rotates too. Always taking its first N starved every
    # symbol past position N: UNITREE was on the board, was in the list, and
    # was still never fetched, so its spread could never form.
    wanted = [raw for raw in (priority or []) if raw in listed]
    if wanted:
        start = _PRIORITY_CURSOR["index"] % len(wanted)
        _PRIORITY_CURSOR["index"] = (start + count) % len(wanted)
        for raw in (wanted + wanted)[start : start + count]:
            if raw not in ordered:
                ordered.append(raw)
        if len(ordered) >= count:
            return ordered[:count]
    for raw in _depth_rotation(symbols, count):
        if raw not in ordered:
            ordered.append(raw)
        if len(ordered) >= count:
            break
    return ordered[:count]


def _depth_rotation(symbols: list[str], count: int) -> list[str]:
    if not symbols or count <= 0:
        return []
    start = _DEPTH_CURSOR["index"] % len(symbols)
    _DEPTH_CURSOR["index"] = (start + count) % len(symbols)
    doubled = symbols + symbols
    return doubled[start : start + count]


def symbols_with_live_depth(store: Any, *, max_age_seconds: float = 900.0) -> set[str]:
    """Ourbit futures symbols already holding a recent venue-native L2 book.

    Read straight from the store so protection survives across sweeps, not just
    within one: the clobbering that made depth look uncollectable happened
    between passes, never inside them.
    """
    cutoff = int((time.time() - max_age_seconds) * 1_000_000)
    try:
        rows = store._conn.execute(
            "SELECT cache_key FROM live_books WHERE venue = ? AND source = ? "
            "AND quote_ts_us > ?",
            (VENUE, "public_rest_l2", cutoff),
        )
        return {str(row[0]).split("|")[-1] for row in rows}
    except Exception:  # noqa: BLE001 - protection is best effort, never fatal
        LOGGER.warning("could not read existing ourbit depth", exc_info=True)
        return set()
