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
import time
import urllib.request
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger("spreadboard.ourbit")

VENUE = "Ourbit"
SPOT_TICKER_URL = "https://api.ourbit.com/api/v3/ticker/bookTicker"
FUTURES_TICKER_URL = "https://futures.ourbit.com/api/v1/contract/ticker"
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
) -> int:
    """Write every priced Ourbit symbol into the live book store.

    Spot and futures are independent calls, and one being down must not cost
    the other: a venue half-present is far better than a venue absent.
    """
    stamp = now_us if now_us is not None else int(time.time() * 1_000_000)
    books: list[dict[str, Any]] = []
    for fetch, build in ((fetch_spot, spot_books), (fetch_futures, futures_books)):
        try:
            books.extend(build(fetch(), now_us=stamp))
        except Exception:
            # One endpoint must not stop the sweep, but a venue that quietly
            # stops answering should be visible rather than merely absent.
            LOGGER.warning("ourbit endpoint failed", exc_info=True)
            continue
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
