"""Public-only token enrichment for SpreadBoard token pages."""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import statistics
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any

from spreadboard import exchange_links

PUBLIC_EXCHANGE_IDS = (
    "binance",
    "bybit",
    "okx",
    "gateio",
    "bitget",
    "mexc",
    "bingx",
    "kucoinfutures",
    "kucoin",
    "htx",
    "phemex",
)
CACHE_TTL_SECONDS = 60.0
ROUTE_PUBLIC_TIMEOUT_MS = 2_000
ROUTE_LEG_DETAIL_TIMEOUT_SECONDS = 7.5
ROUTE_PUBLIC_ENRICHMENT_TIMEOUT_SECONDS = 6.0
TOKEN_EXCHANGE_TIMEOUT_MS = 1_500
TOKEN_EXCHANGE_SCAN_TIMEOUT_SECONDS = 4.0
DEXSCREENER_TIMEOUT_SECONDS = 2.0
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_ROUTE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()

VENUE_EXCHANGE_IDS = {
    "Binance": "binance",
    "Bybit": "bybit",
    "BybitFi": "bybit",
    "OKX": "okx",
    "Okx": "okx",
    "Gate": "gateio",
    "GateFi": "gateio",
    "Bitget": "bitget",
    "MEXC": "mexc",
    "Mexc": "mexc",
    "BingX": "bingx",
    "Bingx": "bingx",
    "KuCoin": "kucoin",
    "Kucoin": "kucoin",
    "KuCoin Futures": "kucoinfutures",
    "Kucoin Futures": "kucoinfutures",
    "Coinbase International": "coinbaseinternational",
    "Upbit": "upbit",
    "Kraken": "kraken",
    "Kraken Futures": "krakenfutures",
    "Coinbase": "coinbase",
    "Hyperliquid": "hyperliquid",
    "Aster": "aster",
    "HTX": "htx",
    "Phemex": "phemex",
    "CoinEx": "coinex",
    "WhiteBIT": "whitebit",
    "BitMart": "bitmart",
    "XT": "xt",
}


def get_token_data(symbol: str, *, force: bool = False) -> dict[str, Any]:
    sym = symbol.strip().upper()
    now = time.time()
    if not force:
        with _CACHE_LOCK:
            cached = _CACHE.get(sym)
            if cached and now - cached[0] < CACHE_TTL_SECONDS:
                return cached[1]
    data = _build_token_data(sym)
    with _CACHE_LOCK:
        _CACHE[sym] = (time.time(), data)
    return data


def get_route_detail(
    board_row: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build a public-only detail payload for one route row."""

    route_key = str(board_row.get("route_key") or "")
    now = time.time()
    if not force:
        with _CACHE_LOCK:
            cached = _ROUTE_CACHE.get(route_key)
            if cached and now - cached[0] < CACHE_TTL_SECONDS:
                return cached[1]
    data = _build_route_detail(board_row, config=config or {})
    with _CACHE_LOCK:
        _ROUTE_CACHE[route_key] = (time.time(), data)
    return data


def get_route_snapshot_detail(board_row: dict[str, Any]) -> dict[str, Any]:
    """Build the chart shell from already-canonical data without public I/O.

    A selected chart used to wait for OHLCV, funding-history, market-stat and
    DEX enrichment before the HTML could be sent.  Those are useful on the
    pair research page, but they are not prerequisites for drawing the chart:
    the exact sampler updates prices and current funding immediately after the
    shell arrives.  Reusing the canonical row here keeps the initial values on
    the same venue/symbol while making a cold chart request deterministic.
    """

    notes = board_row.get("notes") if isinstance(board_row.get("notes"), dict) else {}
    funding_notes = (
        notes.get("funding") if isinstance(notes.get("funding"), dict) else {}
    )
    legs: dict[str, dict[str, Any]] = {}
    for side in ("long", "short"):
        funding = (
            funding_notes.get(side)
            if isinstance(funding_notes.get(side), dict)
            else {}
        )
        legs[side] = _leg_detail_from_board(
            board_row,
            side=side,
            funding=funding,
            reason="public_enrichment_deferred",
        )
    long_leg = legs["long"]
    short_leg = legs["short"]
    long_24h = _leg_funding_24h(long_leg)
    short_24h = _leg_funding_24h(short_leg)
    net_24h = (
        short_24h - long_24h
        if long_24h is not None and short_24h is not None
        else board_row.get("funding_24h_pct")
        or board_row.get("funding_daily_pct")
    )
    return {
        "symbol": str(board_row.get("symbol") or "").upper(),
        "generated_at": int(time.time()),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "trade_authorized": False,
        "mode": "read_only_snapshot_then_live",
        "board_row": board_row,
        "legs": legs,
        "route_volatility_24h": {
            "status": "deferred",
            "reason": "public_enrichment_deferred",
        },
        "funding": {
            "spread_pct": board_row.get("funding_spread_pct"),
            "net_24h_pct": net_24h,
            "long_24h_pct": long_24h,
            "short_24h_pct": short_24h,
            "history_complete": False,
            "note": (
                "Current funding comes from the exact venue/symbol sampler; "
                "settled history loads only when the full pair detail is requested."
            ),
        },
        "okx_dex_quote": {
            "status": "deferred",
            "reason": "exact_chart_sample_runs_in_background",
        },
        "token_overview": {
            "status": "lazy",
            "url": f"/token/{str(board_row.get('symbol') or '').upper()}",
            "note": "Token-wide public exchange scan loads separately.",
        },
        "route_health": route_health(board_row),
    }


def _build_route_detail(board_row: dict[str, Any], *, config: dict[str, Any]) -> dict[str, Any]:
    symbol = str(board_row.get("symbol") or "").upper()
    legs = _route_leg_details(board_row)
    long_leg = legs["long"]
    short_leg = legs["short"]
    route_volatility = route_spread_volatility_24h(long_leg.get("ohlcv"), short_leg.get("ohlcv"))
    long_24h = _leg_funding_24h(long_leg)
    short_24h = _leg_funding_24h(short_leg)
    net_24h = (
        short_24h - long_24h
        if long_24h is not None and short_24h is not None
        else board_row.get("funding_24h_pct") or board_row.get("funding_daily_pct")
    )
    funding = {
        "spread_pct": board_row.get("funding_spread_pct"),
        "net_24h_pct": net_24h,
        "long_24h_pct": long_24h,
        "short_24h_pct": short_24h,
        "history_complete": (
            long_leg.get("market_type") != "Futures"
            or long_leg.get("funding_history_status") == "ok"
        )
        and (
            short_leg.get("market_type") != "Futures"
            or short_leg.get("funding_history_status") == "ok"
        ),
        "note": "24h funding is the sum of settled public funding events; current-rate projection is used only when history is unavailable.",
    }
    return {
        "symbol": symbol,
        "generated_at": int(time.time()),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "trade_authorized": False,
        "mode": "read_only_no_trade",
        "board_row": board_row,
        "legs": {"long": long_leg, "short": short_leg},
        "route_volatility_24h": route_volatility,
        "funding": funding,
        "okx_dex_quote": okx_dex_quote_summary(board_row, config=config),
        "token_overview": {
            "status": "lazy",
            "url": f"/token/{symbol}",
            "note": "Token-wide public exchange scan lives on the token page.",
        },
        "route_health": route_health(board_row),
    }


def _route_leg_details(board_row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    legs: dict[str, dict[str, Any]] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    future_map = {
        pool.submit(_leg_detail, board_row, side=side): side for side in ("long", "short")
    }
    try:
        try:
            for future in concurrent.futures.as_completed(
                future_map,
                timeout=ROUTE_LEG_DETAIL_TIMEOUT_SECONDS,
            ):
                side = future_map[future]
                try:
                    legs[side] = future.result()
                except Exception as exc:  # noqa: BLE001
                    legs[side] = _leg_detail_from_board(
                        board_row,
                        side=side,
                        reason=f"{type(exc).__name__}:{str(exc)[:120]}",
                    )
        except concurrent.futures.TimeoutError:
            pass
    finally:
        for future in future_map:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    for side in ("long", "short"):
        legs.setdefault(
            side,
            _leg_detail_from_board(
                board_row,
                side=side,
                reason="public_leg_enrichment_timeout",
            ),
        )
    return legs


def _leg_detail(board_row: dict[str, Any], *, side: str) -> dict[str, Any]:
    venue = board_row.get(f"{side}_venue")
    market_type = board_row.get(f"{side}_market_type")
    symbol = str(board_row.get("symbol") or "").upper()
    exchange_id = venue_exchange_id(str(venue or ""), str(market_type or ""))
    market_symbol = board_row.get(f"{side}_market_symbol") or market_symbol_for(
        symbol, str(market_type or ""), exchange_id
    )
    if _env_bool("SPREADBOARD_LIGHTWEIGHT_MODE"):
        funding = fetch_funding_24h(exchange_id, market_symbol) if market_type == "Futures" else {}
        return _leg_detail_from_board(
            board_row,
            side=side,
            exchange_id=exchange_id,
            market_symbol=market_symbol,
            volatility={
                "status": "unavailable",
                "reason": "hosted_lightweight_mode",
            },
            funding=funding,
            market_stats={},
        )
    volatility, funding, market_stats = _leg_public_enrichment(
        exchange_id,
        market_symbol,
        str(market_type or ""),
    )
    return _leg_detail_from_board(
        board_row,
        side=side,
        exchange_id=exchange_id,
        market_symbol=market_symbol,
        volatility=volatility,
        funding=funding,
        market_stats=market_stats,
    )


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _leg_multiplier(board_row: dict[str, Any], side: str) -> float:
    """How many units of this leg make one unit of the comparison.

    Carried on a custom chart as `notes.relative_value`, so a pair like
    SKHY/SKHX -- the same asset at a fixed ten-to-one -- can be compared at all.
    """
    notes = board_row.get("notes")
    relative = (notes or {}).get("relative_value") if isinstance(notes, dict) else None
    if not isinstance(relative, dict):
        return 1.0
    value = _float_or_none(relative.get(f"{side}_multiplier"))
    return value if value and value > 0 else 1.0


def book_quote(
    venue: str | None, market_type: str | None, symbol: str | None
) -> dict[str, Any]:
    """Best bid and ask for a leg, from the books the sweep already writes.

    A leg's price came from the board row, so a custom chart -- a pair that by
    definition is not on the board -- had none, and every field rendered blank.
    The ccxt fallback cannot cover it either: loading a venue's markets cold
    costs ~32s against a 6s enrichment budget, so it always timed out. The
    operator's own SKHY/SKHX position charted as an empty page.

    This is a dictionary lookup against quotes that are already there.
    """
    if not venue or not market_type or not symbol:
        return {}
    try:
        from spreadboard import api_spreads, live_book_cache

        book = live_book_cache.LiveBookStore().get(
            str(venue),
            str(market_type),
            str(symbol),
            # The store defaults to a five-second window, which suits the
            # websocket legs. The bulk sweep refreshes a venue every ~230s, so
            # at five seconds every swept book reads as absent -- which is why
            # this returned nothing for a pair that was sitting in the table.
            max_age_seconds=api_spreads.LIVE_BOOK_MAX_AGE_SECONDS,
        )
    except Exception:  # noqa: BLE001 - a missing book is not an error.
        return {}
    if book is None:
        return {}
    # A CachedBook, not a dict.
    bids = list(getattr(book, "bids", None) or [])
    asks = list(getattr(book, "asks", None) or [])
    bid = _float_or_none(bids[0][0]) if bids and bids[0] else None
    ask = _float_or_none(asks[0][0]) if asks and asks[0] else None
    if bid is None and ask is None:
        return {}
    mid = (bid + ask) / 2 if bid is not None and ask is not None else (bid or ask)
    return {
        "bid": bid,
        "ask": ask,
        "price": mid,
        "quote_ts_us": getattr(book, "quote_ts_us", None),
        "source": "live_book",
    }


def _leg_public_enrichment(
    exchange_id: str | None,
    market_symbol: str | None,
    market_type: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    volatility: dict[str, Any] = {
        "status": "unavailable",
        "reason": "market_symbol_unresolved",
    }
    funding: dict[str, Any] = {}
    market_stats: dict[str, Any] = {}
    tasks: dict[str, tuple[Any, tuple[Any, ...], dict[str, Any]]] = {
        "volatility": (
            fetch_ohlcv_stats,
            (exchange_id, market_symbol),
            {"status": "unavailable", "reason": "ohlcv_enrichment_timeout"},
        ),
        "market_stats": (
            fetch_market_stats,
            (exchange_id, market_symbol, market_type),
            {"status": "unavailable", "reason": "market_stats_enrichment_timeout"},
        ),
    }
    if market_type == "Futures":
        tasks["funding"] = (
            fetch_funding_24h,
            (exchange_id, market_symbol),
            {"status": "unavailable", "reason": "funding_history_enrichment_timeout"},
        )
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks))
    future_map = {
        pool.submit(fn, *args): (name, fallback) for name, (fn, args, fallback) in tasks.items()
    }
    completed: dict[str, dict[str, Any]] = {}
    try:
        try:
            for future in concurrent.futures.as_completed(
                future_map,
                timeout=ROUTE_PUBLIC_ENRICHMENT_TIMEOUT_SECONDS,
            ):
                name, fallback = future_map[future]
                try:
                    value = future.result()
                except Exception as exc:  # noqa: BLE001
                    value = {
                        "status": "unavailable",
                        "reason": f"{type(exc).__name__}:{str(exc)[:120]}",
                    }
                completed[name] = value if isinstance(value, dict) else fallback
        except concurrent.futures.TimeoutError:
            pass
    finally:
        for future in future_map:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    volatility = completed.get("volatility") or tasks["volatility"][2]
    if market_type == "Futures":
        funding = completed.get("funding") or tasks["funding"][2]
    market_stats = completed.get("market_stats") or tasks["market_stats"][2]
    return volatility, funding, market_stats


def _leg_detail_from_board(
    board_row: dict[str, Any],
    *,
    side: str,
    exchange_id: str | None = None,
    market_symbol: str | None = None,
    volatility: dict[str, Any] | None = None,
    funding: dict[str, Any] | None = None,
    market_stats: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    venue = board_row.get(f"{side}_venue")
    market_type = board_row.get(f"{side}_market_type")
    symbol = str(board_row.get("symbol") or "").upper()
    price = board_row.get(f"{side}_price")
    exchange_id = (
        exchange_id
        if exchange_id is not None
        else venue_exchange_id(str(venue or ""), str(market_type or ""))
    )
    market_symbol = (
        market_symbol
        if market_symbol is not None
        else board_row.get(f"{side}_market_symbol")
        or market_symbol_for(symbol, str(market_type or ""), exchange_id)
    )
    # A custom pair is not on the board, so it has no board price. The sweep
    # already holds a quote for it; use that before falling back to a ccxt call
    # that cannot finish inside the enrichment budget.
    book = book_quote(venue, market_type, market_symbol) if price is None else {}
    if price is None:
        price = book.get("price")
    # Two legs of the same asset at a fixed ratio -- SKHY against SKHX, where
    # one is worth ten of the other -- only produce a meaningful spread once
    # both are in the same unit. The ratio was carried as a display label only,
    # so the chart read +7572% where the real dislocation is about 23%. Scaling
    # here puts every number downstream -- spread, samples, history -- in
    # normalized units.
    multiplier = _leg_multiplier(board_row, side)
    if multiplier != 1.0:
        price = price * multiplier if price is not None else None
        book = {
            key: (value * multiplier if key in {"bid", "ask", "price"} and value else value)
            for key, value in book.items()
        }
    volatility = volatility or {
        "status": "unavailable",
        "reason": reason or "not_enough_data",
    }
    funding = funding or {}
    market_stats = market_stats or {}
    board_interval = board_row.get(f"{side}_funding_interval_hours")
    funding_interval = funding.get("funding_interval_hours")
    if funding_interval is None:
        funding_interval = board_interval
    funding_24h = funding.get("funding_24h_pct")
    projected_24h = funding.get("projected_24h_pct")
    if funding_24h is None and projected_24h is None:
        projected_24h = _project_funding_24h(
            board_row.get(f"{side}_funding_pct"),
            funding_interval,
        )
    return {
        "side": side,
        "venue": venue,
        "market_type": market_type,
        "market_symbol": market_symbol,
        "exchange_url": (
            board_row.get(f"{side}_exchange_url")
            or exchange_links.exchange_market_url(
                venue=str(venue or ""),
                market_type=str(market_type or ""),
                market_symbol=market_symbol,
                token=symbol,
            )
        ),
        "price": price,
        "mark": board_row.get(f"{side}_mark"),
        "depth_usd": board_row.get(f"{side}_depth_usd"),
        "funding_pct": board_row.get(f"{side}_funding_pct"),
        "current_funding_pct": (
            funding.get("current_funding_pct")
            if funding.get("current_funding_pct") is not None
            else board_row.get(f"{side}_funding_pct")
        ),
        "funding_24h_pct": funding_24h,
        "projected_funding_24h_pct": projected_24h,
        "funding_interval_hours": funding_interval,
        "funding_interval_assumed": (
            funding.get("funding_interval_assumed")
            if funding.get("funding_interval_assumed") is not None
            else board_row.get(f"{side}_funding_interval_assumed", False)
        ),
        "next_funding_ts_us": (
            funding.get("next_funding_ts_us") or board_row.get(f"{side}_next_funding_ts_us")
        ),
        "funding_history": funding.get("history") or [],
        "funding_history_status": funding.get("status", "not_applicable"),
        "funding_history_reason": funding.get("reason"),
        "funding_samples": funding.get("samples"),
        "volume_24h_usd": (
            market_stats.get("volume_24h_usd")
            if market_stats.get("volume_24h_usd") is not None
            else board_row.get(f"{side}_volume_24h_usd")
        ),
        "bid": (
            market_stats.get("bid")
            if market_stats.get("bid") is not None
            else board_row.get(f"{side}_bid")
            if board_row.get(f"{side}_bid") is not None
            else book.get("bid")
        ),
        "ask": (
            market_stats.get("ask")
            if market_stats.get("ask") is not None
            else board_row.get(f"{side}_ask")
            if board_row.get(f"{side}_ask") is not None
            else book.get("ask")
        ),
        "deposit_enabled": board_row.get(f"{side}_deposit_enabled"),
        "withdraw_enabled": board_row.get(f"{side}_withdraw_enabled"),
        "exchange_id": exchange_id,
        "volatility_24h": volatility,
        "ohlcv": volatility.get("_ohlcv") if isinstance(volatility, dict) else None,
    }


def _leg_funding_24h(leg: dict[str, Any]) -> float | None:
    if leg.get("market_type") != "Futures":
        return 0.0
    settled = _float_or_none(leg.get("funding_24h_pct"))
    if settled is not None:
        return settled
    return _float_or_none(leg.get("projected_funding_24h_pct"))


def _project_funding_24h(rate_pct: Any, interval_hours: Any) -> float | None:
    rate = _float_or_none(rate_pct)
    interval = _float_or_none(interval_hours)
    if rate is None or interval is None or interval <= 0:
        return None
    return rate * (24.0 / interval)


def route_health(board_row: dict[str, Any]) -> dict[str, Any]:
    blockers = list(board_row.get("blockers") or [])
    if "DEX" in {board_row.get("long_market_type"), board_row.get("short_market_type")}:
        blockers.append("dex_rows_are_watch_only_without_exact_chain_contract")
    verdict = board_row.get("strategy_verdict") or "watch_only"
    return {
        "verdict": "watch_only" if verdict in {None, "", "ok"} else verdict,
        "next_action": board_row.get("next_action"),
        "blockers": sorted(set(str(item) for item in blockers if item)),
    }


def venue_exchange_id(venue: str, market_type: str) -> str | None:
    if venue == "Kucoin" and market_type == "Futures":
        return "kucoinfutures"
    if venue == "KuCoin" and market_type == "Futures":
        return "kucoinfutures"
    return VENUE_EXCHANGE_IDS.get(venue)


def market_symbol_for(symbol: str, market_type: str, exchange_id: str | None) -> str | None:
    if not exchange_id or market_type == "Dex":
        return None
    if market_type == "Futures":
        return f"{symbol}/USDT:USDT"
    return f"{symbol}/USDT"


def fetch_ohlcv_stats(exchange_id: str | None, symbol: str | None) -> dict[str, Any]:
    if not exchange_id or not symbol:
        return {"status": "unavailable", "reason": "market_symbol_unresolved"}
    cache_key = f"ohlcv:{exchange_id}:{symbol}"
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
    data = _fetch_ohlcv_stats_uncached(exchange_id, symbol)
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.time(), data)
    return data


def _fetch_ohlcv_stats_uncached(exchange_id: str, symbol: str) -> dict[str, Any]:
    native = _fetch_native_ohlcv_stats(exchange_id, symbol)
    if native is not None:
        return native
    try:
        import ccxt

        exchange_class = _ccxt_exchange_class(ccxt, exchange_id)
        exchange = exchange_class({"enableRateLimit": True})
        exchange.timeout = ROUTE_PUBLIC_TIMEOUT_MS
        exchange.load_markets()
        if symbol not in (exchange.markets or {}):
            return {"status": "unavailable", "reason": f"market_not_found:{exchange_id}:{symbol}"}
        if not getattr(exchange, "has", {}).get("fetchOHLCV"):
            return {"status": "unavailable", "reason": f"ohlcv_not_supported:{exchange_id}"}
        since = int((time.time() - 27 * 3600) * 1000)
        candles = exchange.fetch_ohlcv(symbol, timeframe="1h", since=since, limit=30)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"{type(exc).__name__}:{str(exc)[:160]}"}
    stats = volatility_from_ohlcv(candles)
    stats["_ohlcv"] = candles
    return stats


def _fetch_native_ohlcv_stats(
    exchange_id: str,
    symbol: str,
) -> dict[str, Any] | None:
    if exchange_id != "gateio":
        return None
    token = symbol.split("/", 1)[0].upper()
    is_futures = ":" in symbol
    if is_futures:
        path = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks?" + urllib.parse.urlencode(
                {"contract": f"{token}_USDT", "interval": "1h", "limit": 30}
            )
    else:
        path = "https://api.gateio.ws/api/v4/spot/candlesticks?" + urllib.parse.urlencode(
                {"currency_pair": f"{token}_USDT", "interval": "1h", "limit": 30}
            )
    try:
        request = urllib.request.Request(path, headers={"User-Agent": "SpreadBoard/1"})
        with urllib.request.urlopen(
            request,
            timeout=max(1.0, ROUTE_PUBLIC_TIMEOUT_MS / 1000),
        ) as response:
            payload = json.load(response)
        candles = _gate_candles_to_ohlcv(payload, futures=is_futures)
        if not candles:
            return {"status": "unavailable", "reason": "gate_ohlcv_empty"}
        stats = volatility_from_ohlcv(candles)
        stats["_ohlcv"] = candles
        return stats
    except Exception:  # noqa: BLE001 - fall back to the generic public adapter.
        return None


def _gate_candles_to_ohlcv(
    payload: Any,
    *,
    futures: bool,
) -> list[list[float]]:
    candles: list[list[float]] = []
    for item in payload if isinstance(payload, list) else []:
        try:
            if futures and isinstance(item, dict):
                candles.append(
                    [
                        float(item["t"]) * 1000,
                        float(item["o"]),
                        float(item["h"]),
                        float(item["l"]),
                        float(item["c"]),
                        float(item["v"]),
                    ]
                )
            elif not futures and isinstance(item, list) and len(item) >= 7:
                candles.append(
                    [
                        float(item[0]) * 1000,
                        float(item[5]),
                        float(item[3]),
                        float(item[4]),
                        float(item[2]),
                        float(item[6]),
                    ]
                )
        except (KeyError, TypeError, ValueError):
            continue
    return candles


def volatility_from_ohlcv(candles: list[Any]) -> dict[str, Any]:
    clean = [
        item
        for item in candles or []
        if isinstance(item, (list, tuple)) and len(item) >= 5 and _float_or_none(item[4])
    ]
    clean = clean[-25:]
    closes = [_float_or_none(item[4]) for item in clean]
    highs = [_float_or_none(item[2]) for item in clean]
    lows = [_float_or_none(item[3]) for item in clean]
    closes = [value for value in closes if value and value > 0]
    highs = [value for value in highs if value and value > 0]
    lows = [value for value in lows if value and value > 0]
    if len(closes) < 3:
        return {
            "status": "unavailable",
            "reason": "not_enough_ohlcv_candles",
            "samples": len(closes),
        }
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    realized = math.sqrt(sum(value * value for value in returns)) * 100.0
    high_low_range = ((max(highs) / min(lows)) - 1.0) * 100.0 if highs and lows else None
    return {
        "status": "ok",
        "realized_volatility_pct": realized,
        "high_low_range_pct": high_low_range,
        "samples": len(closes),
    }


def route_spread_volatility_24h(long_ohlcv: Any, short_ohlcv: Any) -> dict[str, Any]:
    if not isinstance(long_ohlcv, list) or not isinstance(short_ohlcv, list):
        return {"status": "unavailable", "reason": "leg_ohlcv_missing"}
    long_by_ts = {
        _int_or_none(item[0]): _float_or_none(item[4])
        for item in long_ohlcv
        if isinstance(item, list)
    }
    short_by_ts = {
        _int_or_none(item[0]): _float_or_none(item[4])
        for item in short_ohlcv
        if isinstance(item, list)
    }
    spreads = []
    for ts in sorted(set(long_by_ts) & set(short_by_ts))[-25:]:
        long_close = long_by_ts.get(ts)
        short_close = short_by_ts.get(ts)
        if long_close and short_close and long_close > 0:
            spreads.append((short_close / long_close - 1.0) * 100.0)
    if len(spreads) < 3:
        return {
            "status": "unavailable",
            "reason": "not_enough_aligned_spread_candles",
            "samples": len(spreads),
        }
    returns = [spreads[i] - spreads[i - 1] for i in range(1, len(spreads))]
    return {
        "status": "ok",
        "spread_volatility_pct_points": statistics.pstdev(returns),
        "spread_range_pct_points": max(spreads) - min(spreads),
        "latest_spread_pct": spreads[-1],
        "samples": len(spreads),
    }


def fetch_funding_24h(exchange_id: str | None, symbol: str | None) -> dict[str, Any]:
    if not exchange_id or not symbol:
        return {"status": "unavailable", "reason": "market_symbol_unresolved"}
    cache_key = f"funding24:{exchange_id}:{symbol}"
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
    data = _fetch_funding_24h_uncached(exchange_id, symbol)
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.time(), data)
    return data


def enrich_snapshot_funding_24h(
    snapshot: dict[str, Any],
    *,
    max_workers: int = 12,
    route_keys: set[str] | None = None,
) -> dict[str, int]:
    """Attach settled 24h funding to selected unique futures legs in a snapshot."""

    rows = [
        row
        for bucket in ("api_discovered_rows", "dex_discovered_rows")
        for row in snapshot.get(bucket) or []
        if isinstance(row, dict) and (route_keys is None or _snapshot_route_key(row) in route_keys)
    ]
    leg_keys: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows:
        token = str(row.get("token") or "").upper()
        notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
        route_inputs = (
            notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
        )
        for side in ("long", "short"):
            if row.get(f"{side}_market_type") != "Futures":
                continue
            venue = str(row.get(f"{side}_venue") or "")
            exchange_id = venue_exchange_id(venue, "Futures")
            leg_input = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
            market_symbol = str(leg_input.get("symbol") or "") or market_symbol_for(
                token,
                "Futures",
                exchange_id,
            )
            if exchange_id and market_symbol:
                leg_keys[(exchange_id, market_symbol)] = (exchange_id, market_symbol)

    results: dict[tuple[str, str], dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(max_workers, len(leg_keys) or 1))
    ) as pool:
        futures = {
            pool.submit(fetch_funding_24h, exchange_id, market_symbol): key
            for key, (exchange_id, market_symbol) in leg_keys.items()
        }
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                value = future.result()
            except Exception as exc:  # noqa: BLE001 - one venue must not stop the cycle.
                value = {"status": "unavailable", "reason": type(exc).__name__}
            results[key] = value if isinstance(value, dict) else {"status": "unavailable"}

    settled_routes = 0
    projected_routes = 0
    for row in rows:
        has_futures_leg = any(
            row.get(f"{side}_market_type") == "Futures" for side in ("long", "short")
        )
        token = str(row.get("token") or "").upper()
        notes = row.setdefault("notes", {})
        if not isinstance(notes, dict):
            notes = {}
            row["notes"] = notes
        route_inputs = (
            notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
        )
        funding = notes.setdefault("funding", {})
        if not isinstance(funding, dict):
            funding = {}
            notes["funding"] = funding
        settled: dict[str, float | None] = {}
        projected: dict[str, float | None] = {}
        for side in ("long", "short"):
            market_type = row.get(f"{side}_market_type")
            if market_type != "Futures":
                settled[side] = 0.0
                projected[side] = 0.0
                continue
            venue = str(row.get(f"{side}_venue") or "")
            exchange_id = venue_exchange_id(venue, "Futures")
            leg_input = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
            market_symbol = str(leg_input.get("symbol") or "") or market_symbol_for(
                token,
                "Futures",
                exchange_id,
            )
            result = results.get((str(exchange_id), str(market_symbol)), {})
            leg_funding = funding.setdefault(side, {})
            if not isinstance(leg_funding, dict):
                leg_funding = {}
                funding[side] = leg_funding
            live_current = _float_or_none(leg_funding.get("current_funding_pct"))
            if live_current is None:
                live_current = _float_or_none(leg_funding.get("rate_pct"))
            for key in (
                "status",
                "reason",
                "funding_24h_pct",
                "funding_interval_hours",
                "funding_interval_assumed",
                "next_funding_ts_us",
                "samples",
            ):
                if result.get(key) is not None:
                    leg_funding[key] = result[key]
            if live_current is None and result.get("current_funding_pct") is not None:
                live_current = _float_or_none(result.get("current_funding_pct"))
            if live_current is not None:
                leg_funding["current_funding_pct"] = live_current
            interval = _float_or_none(result.get("funding_interval_hours"))
            if interval is None:
                interval = _float_or_none(leg_funding.get("interval_hours"))
            live_projection = _project_funding_24h(live_current, interval)
            if live_projection is None:
                live_projection = _float_or_none(result.get("projected_24h_pct"))
            if live_projection is not None:
                leg_funding["projected_24h_pct"] = live_projection
            settled[side] = _float_or_none(result.get("funding_24h_pct"))
            projected[side] = live_projection

        if has_futures_leg and settled.get("long") is not None and settled.get("short") is not None:
            row["funding_24h_pct"] = settled["short"] - settled["long"]
            row["funding_24h_source"] = "settled_public_events"
            settled_routes += 1
        else:
            row.pop("funding_24h_pct", None)
            row.pop("funding_24h_source", None)
        if (
            has_futures_leg
            and projected.get("long") is not None
            and projected.get("short") is not None
        ):
            row["funding_projected_24h_pct"] = projected["short"] - projected["long"]
            projected_routes += 1
        else:
            row.pop("funding_projected_24h_pct", None)
    return {
        "selected_routes": len(rows),
        "unique_futures_legs": len(leg_keys),
        "settled_routes": settled_routes,
        "projected_routes": projected_routes,
    }


def _snapshot_route_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key) or "")
        for key in (
            "token",
            "long_venue",
            "long_market_type",
            "short_venue",
            "short_market_type",
        )
    )


def _fetch_funding_24h_uncached(exchange_id: str, symbol: str) -> dict[str, Any]:
    native = _fetch_native_funding_24h(exchange_id, symbol)
    if native is not None:
        return native
    if os.environ.get("SPREADBOARD_NATIVE_FUNDING_ONLY", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return {
            "status": "unavailable",
            "reason": f"native_funding_history_not_supported:{exchange_id}",
        }
    try:
        import ccxt

        exchange_class = _ccxt_exchange_class(ccxt, exchange_id)
        exchange = exchange_class({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        exchange.timeout = ROUTE_PUBLIC_TIMEOUT_MS
        exchange.load_markets()
        if symbol not in (exchange.markets or {}):
            return {"status": "unavailable", "reason": f"market_not_found:{exchange_id}:{symbol}"}
        current_payload = _safe_fetch_funding_rate(exchange, symbol)
        current_rate = _funding_rate_pct(current_payload)
        current_interval = _funding_interval_from_payload(current_payload)
        next_funding_ts_us = _next_funding_ts_us(current_payload)
        if not getattr(exchange, "has", {}).get("fetchFundingRateHistory"):
            return {
                "status": "current_only",
                "reason": f"funding_history_not_supported:{exchange_id}",
                "current_funding_pct": current_rate,
                "funding_interval_hours": current_interval,
                "funding_interval_assumed": current_interval is None,
                "next_funding_ts_us": next_funding_ts_us,
                "projected_24h_pct": _project_funding_24h(current_rate, current_interval),
                "history": [],
            }
        since = int((time.time() - 24 * 3600) * 1000)
        rows = exchange.fetch_funding_rate_history(symbol, since=since, limit=100)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"{type(exc).__name__}:{str(exc)[:160]}"}
    history = _normalize_funding_history(rows)
    inferred_interval = _infer_funding_interval_hours(history)
    interval = current_interval or inferred_interval
    if not history:
        return {
            "status": "current_only",
            "reason": "no_funding_history_rows",
            "current_funding_pct": current_rate,
            "funding_interval_hours": interval,
            "funding_interval_assumed": interval is None,
            "next_funding_ts_us": next_funding_ts_us,
            "projected_24h_pct": _project_funding_24h(current_rate, interval),
            "history": [],
            "samples": 0,
        }
    return {
        "status": "ok",
        "funding_24h_pct": sum(item["rate_pct"] for item in history),
        "current_funding_pct": current_rate,
        "funding_interval_hours": interval,
        "funding_interval_assumed": interval is None,
        "next_funding_ts_us": next_funding_ts_us,
        "projected_24h_pct": _project_funding_24h(current_rate, interval),
        "history": history,
        "samples": len(history),
    }


def _fetch_native_funding_24h(exchange_id: str, symbol: str) -> dict[str, Any] | None:
    """Use lightweight public endpoints for venues whose CCXT bootstrap is slow."""

    base = str(symbol).split("/", 1)[0].upper()
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - 24 * 3600 * 1000
    if exchange_id in {"binance", "aster"}:
        host = (
            "https://fapi.binance.com/fapi/v1"
            if exchange_id == "binance"
            else "https://fapi.asterdex.com/fapi/v3"
        )
        url = (
            host
            + "/fundingRate?"
            + urllib.parse.urlencode(
            {"symbol": f"{base}USDT", "startTime": since_ms, "limit": "100"}
        )
        )
        data = _public_json(url)
        if not isinstance(data, list):
            return None
        history = _normalize_native_funding_rows(
            data,
            timestamp_key="fundingTime",
            rate_key="fundingRate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "bingx":
        url = (
            "https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate?"
            + urllib.parse.urlencode(
                {"symbol": f"{base}-USDT", "startTime": since_ms, "limit": "100"}
            )
        )
        data = _public_json(url)
        if not isinstance(data, dict):
            return None
        history = _normalize_native_funding_rows(
            data.get("data") or [],
            timestamp_key="fundingTime",
            rate_key="fundingRate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "bitget":
        url = (
            "https://api.bitget.com/api/v2/mix/market/history-fund-rate?"
            + urllib.parse.urlencode(
                {
                    "symbol": f"{base}USDT",
                    "productType": "USDT-FUTURES",
                    "pageSize": "100",
                }
            )
        )
        data = _public_json(url)
        if not isinstance(data, dict):
            return None
        history = _normalize_native_funding_rows(
            data.get("data") or [],
            timestamp_key="fundingTime",
            rate_key="fundingRate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "gateio":
        url = "https://api.gateio.ws/api/v4/futures/usdt/funding_rate?" + urllib.parse.urlencode(
                {"contract": f"{base}_USDT", "from": since_ms // 1000, "limit": "100"}
            )
        data = _public_json(url)
        if not isinstance(data, list):
            return None
        history = _normalize_native_funding_rows(
            data,
            timestamp_key="t",
            rate_key="r",
            since_ms=since_ms,
            timestamp_multiplier=1000,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "mexc":
        url = (
            "https://contract.mexc.com/api/v1/contract/funding_rate/history?"
            + urllib.parse.urlencode(
                {"symbol": f"{base}_USDT", "page_num": "1", "page_size": "100"}
            )
        )
        data = _public_json(url)
        if not isinstance(data, dict):
            return None
        history = _normalize_native_funding_rows(
            ((data.get("data") or {}).get("resultList") or []),
            timestamp_key="settleTime",
            rate_key="fundingRate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "kucoinfutures":
        url = (
            "https://api-futures.kucoin.com/api/v1/contract/funding-rates?"
            + urllib.parse.urlencode(
                {
                    "symbol": f"{base}USDTM",
                    "from": since_ms,
                    "to": now_ms,
                }
            )
        )
        data = _public_json(url)
        if not isinstance(data, dict):
            return None
        history = _normalize_native_funding_rows(
            data.get("data") or [],
            timestamp_key="timepoint",
            rate_key="fundingRate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "hyperliquid":
        data = _public_json(
            "https://api.hyperliquid.xyz/info",
            payload={"type": "fundingHistory", "coin": base, "startTime": since_ms},
        )
        if not isinstance(data, list):
            return None
        history = _normalize_native_funding_rows(
            data,
            timestamp_key="time",
            rate_key="fundingRate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "okx":
        url = "https://www.okx.com/api/v5/public/funding-rate-history?" + urllib.parse.urlencode(
            {"instId": f"{base}-USDT-SWAP", "limit": "100"}
        )
        data = _public_json(url)
        if data is None:
            return None
        if str(data.get("code")) != "0":
            return {"status": "unavailable", "reason": "okx_funding_history_error"}
        raw_rows = data.get("data") or []
        history = _normalize_native_funding_rows(
            raw_rows,
            timestamp_key="fundingTime",
            rate_key="realizedRate",
            since_ms=now_ms - 24 * 3600 * 1000,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "bybit":
        url = "https://api.bybit.com/v5/market/funding/history?" + urllib.parse.urlencode(
                {"category": "linear", "symbol": f"{base}USDT", "limit": "100"}
            )
        data = _public_json(url)
        if data is None:
            return None
        if int(data.get("retCode") or 0) != 0:
            return {"status": "unavailable", "reason": "bybit_funding_history_error"}
        raw_rows = (data.get("result") or {}).get("list") or []
        history = _normalize_native_funding_rows(
            raw_rows,
            timestamp_key="fundingRateTimestamp",
            rate_key="fundingRate",
            since_ms=now_ms - 24 * 3600 * 1000,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "htx":
        url = (
            "https://api.hbdm.com/linear-swap-api/v1/swap_historical_funding_rate?"
            + urllib.parse.urlencode({"contract_code": f"{base}-USDT", "page_size": "100"})
        )
        data = _public_json(url)
        raw_rows = ((data or {}).get("data") or {}).get("data") or []
        history = _normalize_native_funding_rows(
            raw_rows,
            timestamp_key="funding_time",
            rate_key="funding_rate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "phemex":
        url = (
            "https://api.phemex.com/api-data/public/data/funding-rate-history?"
            + urllib.parse.urlencode(
                {
                    "symbol": f".{base}FR8H",
                    "start": since_ms,
                    "end": now_ms,
                    "limit": "100",
                }
            )
        )
        data = _public_json(url)
        raw_rows = ((data or {}).get("data") or {}).get("rows") or []
        history = _normalize_native_funding_rows(
            raw_rows,
            timestamp_key="fundingTime",
            rate_key="fundingRate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "coinex":
        url = "https://api.coinex.com/v2/futures/funding-rate-history?" + urllib.parse.urlencode(
                {
                    "market": f"{base}USDT",
                    "start_time": since_ms,
                    "end_time": now_ms,
                    "limit": "100",
                }
            )
        data = _public_json(url)
        raw_rows = (data or {}).get("data") or []
        history = _normalize_native_funding_rows(
            raw_rows,
            timestamp_key="funding_time",
            rate_key="actual_funding_rate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "whitebit":
        url = (
            f"https://whitebit.com/api/v4/public/funding-history/{base}_PERP?"
            + urllib.parse.urlencode(
                {
                    "startDate": since_ms // 1000,
                    "endDate": now_ms // 1000,
                    "limit": "100",
                }
            )
        )
        data = _public_json(url)
        history = _normalize_native_funding_rows(
            data if isinstance(data, list) else [],
            timestamp_key="fundingTime",
            rate_key="fundingRate",
            since_ms=since_ms,
            timestamp_multiplier=1000,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "bitmart":
        url = (
            "https://api-cloud-v2.bitmart.com/contract/public/funding-rate-history?"
            + urllib.parse.urlencode({"symbol": f"{base}USDT"})
        )
        data = _public_json(url)
        history = _normalize_native_funding_rows(
            (((data or {}).get("data") or {}).get("list") or []),
            timestamp_key="funding_time",
            rate_key="funding_rate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "xt":
        url = (
            "https://fapi.xt.com/future/market/v1/public/q/funding-rate-record?"
            + urllib.parse.urlencode({"symbol": f"{base.lower()}_usdt", "limit": "100"})
        )
        data = _public_json(url)
        history = _normalize_native_funding_rows(
            (((data or {}).get("result") or {}).get("items") or []),
            timestamp_key="createdTime",
            rate_key="fundingRate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "coinbaseinternational":
        url = (
            "https://api.international.coinbase.com/api/v1/instruments/"
            f"{base}-PERP/funding?" + urllib.parse.urlencode({"result_limit": "100"})
        )
        data = _public_json(url)
        rows = []
        for item in (data or {}).get("results") or []:
            if not isinstance(item, dict):
                continue
            timestamp_ms = _iso_timestamp_ms(item.get("event_time"))
            if timestamp_ms is None:
                continue
            rows.append({**item, "timestamp_ms": timestamp_ms})
        history = _normalize_native_funding_rows(
            rows,
            timestamp_key="timestamp_ms",
            rate_key="funding_rate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    if exchange_id == "krakenfutures":
        # Kraken settles funding HOURLY and caps the rate at +/-0.5%/h, so the
        # single current print annualises to absurdity: AGLD extrapolated to
        # 3570% APR while the 24 rates it actually paid summed to 5.66%/day.
        # Without this branch every Kraken leg fell through to that one print,
        # because production runs with SPREADBOARD_NATIVE_FUNDING_ONLY=1.
        # Kraken lists PF_XBTUSD but PF_DOGEUSD, so BTC is the only alias needed.
        kraken_base = "XBT" if base == "BTC" else base
        url = (
            "https://futures.kraken.com/derivatives/api/v4/historicalfundingrates?"
            + urllib.parse.urlencode({"symbol": f"PF_{kraken_base}USD"})
        )
        data = _public_json(url)
        rows = []
        for item in (data or {}).get("rates") or []:
            if not isinstance(item, dict):
                continue
            timestamp_ms = _iso_timestamp_ms(item.get("timestamp"))
            if timestamp_ms is None:
                continue
            rows.append({**item, "timestamp_ms": timestamp_ms})
        history = _normalize_native_funding_rows(
            rows,
            timestamp_key="timestamp_ms",
            # relativeFundingRate is the fraction of mark price; the absolute
            # `fundingRate` is quote currency per contract and is not a percent.
            rate_key="relativeFundingRate",
            since_ms=since_ms,
        )
        return _native_funding_result(history, exchange_id=exchange_id)
    return None


def _public_json(url: str, *, payload: dict[str, Any] | None = None) -> Any:
    body = None
    headers = {"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(2.0, ROUTE_PUBLIC_TIMEOUT_MS / 1000.0),
        ) as response:
            value = json.load(response)
    except Exception:  # noqa: BLE001 - CCXT remains the fallback.
        return None
    return value


def _normalize_native_funding_rows(
    rows: list[dict[str, Any]],
    *,
    timestamp_key: str,
    rate_key: str,
    since_ms: int,
    timestamp_multiplier: int = 1,
) -> list[dict[str, Any]]:
    history = []
    for row in rows:
        timestamp_ms = _int_or_none(row.get(timestamp_key))
        rate = _float_or_none(row.get(rate_key))
        if timestamp_ms is not None:
            timestamp_ms *= timestamp_multiplier
        if timestamp_ms is None or rate is None or timestamp_ms < since_ms:
            continue
        history.append({"timestamp_ms": timestamp_ms, "rate_pct": rate * 100.0})
    history.sort(key=lambda item: item["timestamp_ms"])
    cumulative = 0.0
    for item in history:
        cumulative += item["rate_pct"]
        item["cumulative_pct"] = cumulative
    return history


def _iso_timestamp_ms(value: Any) -> int | None:
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return int(parsed.timestamp() * 1000)


def _native_funding_result(
    history: list[dict[str, Any]],
    *,
    exchange_id: str,
) -> dict[str, Any]:
    if not history:
        return {
            "status": "current_only",
            "reason": f"no_recent_funding_history_rows:{exchange_id}",
            "history": [],
            "samples": 0,
        }
    interval = _infer_funding_interval_hours(history)
    latest = history[-1]
    next_funding_ts_us = None
    if interval:
        next_funding_ts_us = int((latest["timestamp_ms"] + interval * 3600 * 1000) * 1000)
    return {
        "status": "ok",
        "funding_24h_pct": sum(item["rate_pct"] for item in history),
        "funding_interval_hours": interval,
        "funding_interval_assumed": interval is None,
        "next_funding_ts_us": next_funding_ts_us,
        "history": history,
        "samples": len(history),
    }


def fetch_market_stats(
    exchange_id: str | None,
    symbol: str | None,
    market_type: str | None = None,
) -> dict[str, Any]:
    if not exchange_id or not symbol:
        return {"status": "unavailable", "reason": "market_symbol_unresolved"}
    cache_key = f"marketstats:{exchange_id}:{symbol}"
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
    try:
        import ccxt

        exchange_class = _ccxt_exchange_class(ccxt, exchange_id)
        options = {"defaultType": "swap"} if market_type == "Futures" else {}
        exchange = exchange_class({"enableRateLimit": True, "options": options})
        exchange.timeout = ROUTE_PUBLIC_TIMEOUT_MS
        exchange.load_markets()
        if symbol not in (exchange.markets or {}):
            data = {"status": "unavailable", "reason": f"market_not_found:{exchange_id}:{symbol}"}
        else:
            ticker = _safe_fetch_ticker(exchange, symbol)
            last = _last_price(ticker)
            data = {
                "status": "ok" if ticker else "unavailable",
                "reason": None if ticker else "ticker_unavailable",
                "bid": _float_or_none(ticker.get("bid")),
                "ask": _float_or_none(ticker.get("ask")),
                "last": last,
                "volume_24h_usd": _quote_volume(ticker, last) or None,
            }
    except Exception as exc:  # noqa: BLE001
        data = {"status": "unavailable", "reason": f"{type(exc).__name__}:{str(exc)[:160]}"}
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.time(), data)
    return data


def _normalize_funding_history(rows: Any) -> list[dict[str, Any]]:
    clean: list[tuple[int, float]] = []
    cutoff_ms = int((time.time() - 24 * 3600) * 1000)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        timestamp_ms = _int_or_none(row.get("timestamp") or row.get("fundingTimestamp"))
        rate = _float_or_none(row.get("fundingRate"))
        if timestamp_ms is None or rate is None or timestamp_ms < cutoff_ms:
            continue
        clean.append((timestamp_ms, rate * 100.0))
    clean.sort(key=lambda item: item[0])
    cumulative = 0.0
    history: list[dict[str, Any]] = []
    for timestamp_ms, rate_pct in clean:
        cumulative += rate_pct
        history.append(
            {
                "timestamp_ms": timestamp_ms,
                "rate_pct": rate_pct,
                "cumulative_pct": cumulative,
            }
        )
    return history


def _infer_funding_interval_hours(history: list[dict[str, Any]]) -> float | None:
    timestamps = [
        _int_or_none(item.get("timestamp_ms"))
        for item in history
        if _int_or_none(item.get("timestamp_ms")) is not None
    ]
    deltas = [
        (current - previous) / 3_600_000.0
        for previous, current in zip(timestamps, timestamps[1:])
        if current > previous
    ]
    if not deltas:
        return None
    interval = statistics.median(deltas)
    if interval <= 0:
        return None
    nearest_hour = round(interval)
    if nearest_hour > 0 and abs(interval - nearest_hour) <= 0.02:
        return float(nearest_hour)
    return round(interval, 4)


def _safe_fetch_funding_rate(exchange: Any, symbol: str) -> dict[str, Any]:
    try:
        payload = exchange.fetch_funding_rate(symbol)
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _funding_rate_pct(payload: dict[str, Any]) -> float | None:
    rate = _float_or_none(payload.get("fundingRate") or payload.get("predictedFundingRate"))
    return rate * 100.0 if rate is not None else None


def _funding_interval_from_payload(payload: dict[str, Any]) -> float | None:
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    raw = (
        payload.get("interval")
        or payload.get("fundingInterval")
        or payload.get("fundingIntervalHours")
        or info.get("fundingInterval")
        or info.get("fundingIntervalHour")
        or info.get("fundingIntervalHours")
    )
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 3_600_000.0 if value > 86_400 else value
    text = str(raw).strip().casefold()
    try:
        if text.endswith("h"):
            return float(text[:-1])
        if text.endswith("m"):
            return float(text[:-1]) / 60.0
        return float(text)
    except ValueError:
        return None


def _next_funding_ts_us(payload: dict[str, Any]) -> int | None:
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    timestamp = _int_or_none(
        payload.get("nextFundingTimestamp")
        or payload.get("fundingTimestamp")
        or info.get("nextFundingTime")
    )
    if timestamp is None:
        return None
    if timestamp < 10**15:
        timestamp *= 1000
    return timestamp


def okx_dex_quote_summary(
    board_row: dict[str, Any], *, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = config or {}
    venue_text = " ".join(
        str(board_row.get(key) or "") for key in ("long_venue", "short_venue")
    ).casefold()
    if (
        "Dex" not in {board_row.get("long_market_type"), board_row.get("short_market_type")}
        and "dex" not in venue_text
    ):
        return {"provider": "OKX DEX", "status": "not_applicable"}
    if config.get("okx_dex_quotes_enabled") is False:
        return {
            "provider": "OKX DEX",
            "status": "disabled",
            "blockers": ["okx_dex_quotes_disabled"],
        }
    chain = str(board_row.get("dex_chain") or config.get("default_dex_chain") or "").strip()
    token_address = str(
        board_row.get("dex_contract") or board_row.get("token_address") or ""
    ).strip()
    if not chain or not token_address:
        return {
            "provider": "OKX DEX",
            "status": "blocked",
            "blockers": ["exact_chain_contract_required"],
            "note": "DEX rows need exact chain and token contract before OKX DEX quotes are meaningful.",
        }
    try:
        notional = Decimal(str(config.get("okx_dex_quote_notional_usd") or "30"))
    except (InvalidOperation, ValueError):
        notional = Decimal("30")
    try:
        from spreadarb.dex import okx_quotes as okx_dex

        if (
            board_row.get("long_market_type") == "Dex"
            or "dex" in str(board_row.get("long_venue") or "").casefold()
        ):
            raw = okx_dex.quote_usdc_to_token(
                chain=chain,
                token_address=token_address,
                notional_usd=notional,
            )
        else:
            price = _float_or_none(board_row.get("short_price") or board_row.get("long_price"))
            if not price or price <= 0:
                return {
                    "provider": "OKX DEX",
                    "status": "blocked",
                    "blockers": ["token_quantity_required"],
                }
            raw = okx_dex.quote_token_to_usdc(
                chain=chain,
                token_address=token_address,
                token_quantity=notional / Decimal(str(price)),
            )
    except Exception as exc:  # noqa: BLE001
        return {
            "provider": "OKX DEX",
            "status": "blocked",
            "blockers": [f"{type(exc).__name__}:{str(exc)[:160]}"],
        }
    return sanitize_okx_quote(raw)


def sanitize_okx_quote(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "chain_index",
        "from_token",
        "from_token_symbol",
        "to_token",
        "to_token_symbol",
        "notional_usd",
        "out_qty",
        "dex_buy_price_usd",
        "dex_sell_price_usd",
        "trade_fee_usd",
        "estimate_gas_fee",
        "router",
        "blockers",
        "raw_code",
    }
    output = {key: raw.get(key) for key in allowed if key in raw}
    output["provider"] = "OKX DEX"
    output.setdefault("status", raw.get("status") or "blocked")
    return output


def _build_token_data(symbol: str) -> dict[str, Any]:
    exchange_rows = fetch_exchange_rows(symbol)
    cex_prices = sorted(
        price
        for row in exchange_rows
        for price in (row.get("perp_price"), row.get("spot_price"))
        if isinstance(price, (int, float)) and price > 0
    )
    cex_median = statistics.median(cex_prices) if cex_prices else None
    dex = fetch_dexscreener(symbol, cex_median)
    spreads = best_spreads(exchange_rows, dex)
    return {
        "symbol": symbol,
        "generated_at": int(time.time()),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "exchange_rows": exchange_rows,
        "dex": dex,
        "best_spreads": spreads,
        "convergence_hint": convergence_hint(spreads),
    }


def fetch_exchange_rows(symbol: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(PUBLIC_EXCHANGE_IDS))
    future_map = {
        pool.submit(_fetch_exchange_row, exchange_id, symbol): exchange_id
        for exchange_id in PUBLIC_EXCHANGE_IDS
    }
    try:
        try:
            for future in concurrent.futures.as_completed(
                future_map, timeout=TOKEN_EXCHANGE_SCAN_TIMEOUT_SECONDS
            ):
                row = future.result()
                if row:
                    rows.append(row)
        except concurrent.futures.TimeoutError:
            pass
    finally:
        for future in future_map:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    return _merge_rows(rows)


def _fetch_exchange_row(exchange_id: str, symbol: str) -> dict[str, Any] | None:
    try:
        import ccxt

        exchange_class = _ccxt_exchange_class(ccxt, exchange_id)
        exchange = exchange_class(
            {
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            }
        )
        exchange.timeout = TOKEN_EXCHANGE_TIMEOUT_MS
        exchange.load_markets()
        perp_symbol = f"{symbol}/USDT:USDT"
        spot_symbol = f"{symbol}/USDT"
        markets = exchange.markets or {}
        has_perp = perp_symbol in markets
        has_spot = spot_symbol in markets and bool(markets[spot_symbol].get("spot", True))
        if not has_perp and not has_spot:
            return None

        perp_price = None
        spot_price = None
        funding_rate_pct = None
        volume_usd = 0.0
        if has_perp:
            ticker = _safe_fetch_ticker(exchange, perp_symbol)
            perp_price = _last_price(ticker)
            volume_usd = max(volume_usd, _quote_volume(ticker, perp_price))
            funding_rate_pct = _funding_pct(exchange, perp_symbol)
        if has_spot:
            ticker = _safe_fetch_ticker(exchange, spot_symbol)
            spot_price = _last_price(ticker)
            volume_usd = max(volume_usd, _quote_volume(ticker, spot_price))

        deposit, withdraw = _deposit_withdraw_status(exchange, symbol)
        if perp_price is None and spot_price is None:
            return None
        return {
            "exchange_id": exchange_id,
            "venue": _display_venue(exchange_id),
            "perp_symbol": perp_symbol if has_perp else None,
            "spot_symbol": spot_symbol if has_spot else None,
            "perp_price": perp_price,
            "spot_price": spot_price,
            "funding_rate_pct": funding_rate_pct,
            "volume_usd": volume_usd or None,
            "deposit": deposit,
            "withdraw": withdraw,
        }
    except Exception:  # noqa: BLE001
        return None


def fetch_dexscreener(symbol: str, cex_median_price: float | None) -> dict[str, Any] | None:
    if not cex_median_price or cex_median_price <= 0:
        return None
    query = urllib.parse.urlencode({"q": symbol})
    request = urllib.request.Request(
        f"https://api.dexscreener.com/latest/dex/search?{query}",
        headers={"User-Agent": "SpreadBoard/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=DEXSCREENER_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    pairs = []
    for pair in payload.get("pairs") or []:
        base = pair.get("baseToken") or {}
        if str(base.get("symbol") or "").upper() != symbol:
            continue
        if pair.get("chainId") not in {"bsc", "solana"}:
            continue
        price = _float_or_none(pair.get("priceUsd"))
        if price is None or abs(price - cex_median_price) > cex_median_price * 0.4:
            continue
        pairs.append(pair)
    pairs.sort(
        key=lambda item: _float_or_none((item.get("liquidity") or {}).get("usd")) or 0.0,
        reverse=True,
    )
    if not pairs:
        return None
    pair = pairs[0]
    return {
        "venue": "DEX",
        "chain_id": pair.get("chainId"),
        "dex_id": pair.get("dexId"),
        "price_usd": _float_or_none(pair.get("priceUsd")),
        "liquidity_usd": _float_or_none((pair.get("liquidity") or {}).get("usd")),
        "volume_24h_usd": _float_or_none((pair.get("volume") or {}).get("h24")),
        "url": pair.get("url"),
    }


# A quote this far from what everyone else is quoting is a broken feed. The bound
# is generous on purpose: the operator has captured a 150% spread for real money,
# so the guard labels rather than removes.
PRICE_CONSENSUS_TOLERANCE = 0.25
PRICE_CONSENSUS_MIN_QUOTES = 4


def best_spreads(rows: list[dict[str, Any]], dex: dict[str, Any] | None) -> list[dict[str, Any]]:
    quotes: list[dict[str, Any]] = []
    for row in rows:
        if row.get("spot_price"):
            quotes.append(
                {
                    "venue": row["venue"],
                    "leg": "spot",
                    "price": row["spot_price"],
                    "deposit": row.get("deposit"),
                    "withdraw": row.get("withdraw"),
                }
            )
        if row.get("perp_price"):
            quotes.append(
                {
                    "venue": row["venue"],
                    "leg": "perp",
                    "price": row["perp_price"],
                    "deposit": None,
                    "withdraw": None,
                }
            )
    if dex and dex.get("price_usd"):
        quotes.append(
            {
                "venue": "DEX",
                "leg": "dex",
                "price": dex["price_usd"],
                "deposit": True,
                "withdraw": True,
            }
        )

    # One bad quote used to become a headline opportunity: BTC was reported at a
    # 26% spread, BingX perp 63,298 against a DEX leg reading 79,828. With enough
    # venues quoting, a price far from all of them is a broken feed, not an edge.
    # It is FLAGGED rather than dropped -- large spreads on this board have been
    # real before, and hiding them is the worse failure.
    reference = statistics.median([quote["price"] for quote in quotes]) if quotes else None

    def _disputed(quote: dict[str, Any]) -> bool:
        if reference is None or len(quotes) < PRICE_CONSENSUS_MIN_QUOTES or reference <= 0:
            return False
        return abs(quote["price"] / reference - 1.0) > PRICE_CONSENSUS_TOLERANCE

    pairs = []
    for buy in quotes:
        for sell in quotes:
            if buy is sell or sell["price"] <= buy["price"]:
                continue
            if buy["venue"] == sell["venue"] and buy["leg"] == sell["leg"]:
                continue
            spread_pct = (sell["price"] / buy["price"] - 1.0) * 100.0
            if spread_pct < 0.3:
                continue
            transfer = _transfer_note(buy, sell)
            pairs.append(
                {
                    "buy_venue": buy["venue"],
                    "buy_leg": buy["leg"],
                    "buy_price": buy["price"],
                    "sell_venue": sell["venue"],
                    "sell_leg": sell["leg"],
                    "sell_price": sell["price"],
                    "spread_pct": spread_pct,
                    "transfer_note": transfer["label"],
                    "withdraw_open": transfer["withdraw_open"],
                    "deposit_open": transfer["deposit_open"],
                    "needs_transfer": transfer["needs_transfer"],
                    "price_disputed": _disputed(buy) or _disputed(sell),
                }
            )
    pairs.sort(key=lambda item: item["spread_pct"], reverse=True)
    shown = []
    seen_sell_legs: set[tuple[str, str]] = set()
    for pair in pairs:
        key = (str(pair["sell_venue"]), str(pair["sell_leg"]))
        if key in seen_sell_legs:
            continue
        seen_sell_legs.add(key)
        shown.append(pair)
        if len(shown) >= 8:
            break
    return shown


def convergence_hint(spreads: list[dict[str, Any]]) -> str | None:
    spot_to_spot = [
        spread
        for spread in spreads
        if spread.get("buy_leg") == "spot" and spread.get("sell_leg") == "spot"
    ]
    if not spot_to_spot:
        return None
    best = max(spot_to_spot, key=lambda item: item["spread_pct"])
    if best.get("withdraw_open") is False:
        return (
            f"The best spot-to-spot gap buys on {best['buy_venue']}, but withdrawals look closed. "
            "That can trap cheap coins on the buy exchange, so the price gap may stay open."
        )
    if best.get("deposit_open") is False:
        return (
            f"The best spot-to-spot gap sells on {best['sell_venue']}, but deposits look closed. "
            "Traders cannot easily move coins there to sell, so convergence can be slow."
        )
    return None


def _safe_fetch_ticker(exchange: Any, symbol: str) -> dict[str, Any]:
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker if isinstance(ticker, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _funding_pct(exchange: Any, symbol: str) -> float | None:
    try:
        data = exchange.fetch_funding_rate(symbol)
        rate = _float_or_none((data or {}).get("fundingRate"))
        return rate * 100.0 if rate is not None else None
    except Exception:  # noqa: BLE001
        return None


def _deposit_withdraw_status(exchange: Any, symbol: str) -> tuple[bool | None, bool | None]:
    try:
        if getattr(exchange, "has", {}).get("fetchCurrencies"):
            exchange.fetch_currencies()
    except Exception:  # noqa: BLE001
        pass
    try:
        currency = (exchange.currencies or {}).get(symbol) or {}
    except Exception:  # noqa: BLE001
        currency = {}
    return _bool_or_none(currency.get("deposit")), _bool_or_none(currency.get("withdraw"))


def _merge_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        venue = _merge_venue_name(str(row.get("venue") or ""))
        existing = merged.setdefault(
            venue,
            {
                "venue": venue,
                "exchange_id": row.get("exchange_id"),
                "perp_symbol": None,
                "spot_symbol": None,
                "perp_price": None,
                "spot_price": None,
                "funding_rate_pct": None,
                "volume_usd": None,
                "deposit": None,
                "withdraw": None,
            },
        )
        for key in (
            "perp_symbol",
            "spot_symbol",
            "perp_price",
            "spot_price",
            "funding_rate_pct",
            "deposit",
            "withdraw",
        ):
            if existing.get(key) is None and row.get(key) is not None:
                existing[key] = row[key]
        existing["volume_usd"] = (
            max(
            _float_or_none(existing.get("volume_usd")) or 0.0,
            _float_or_none(row.get("volume_usd")) or 0.0,
            )
            or None
        )
    result = list(merged.values())
    result.sort(
        key=lambda item: (
            item.get("funding_rate_pct") is None,
            -float(item.get("funding_rate_pct") or -9999),
            item["venue"],
        )
    )
    return result


def _transfer_note(buy: dict[str, Any], sell: dict[str, Any]) -> dict[str, Any]:
    needs_transfer = sell["leg"] in {"spot", "dex"} and buy["leg"] != "perp"
    if not needs_transfer:
        return {
            "label": "no token transfer needed",
            "withdraw_open": None,
            "deposit_open": None,
            "needs_transfer": False,
        }
    withdraw_open = True if buy["leg"] == "dex" else buy.get("withdraw")
    deposit_open = True if sell["leg"] == "dex" else sell.get("deposit")
    return {
        "label": f"W{mark_status(withdraw_open)}→D{mark_status(deposit_open)}",
        "withdraw_open": withdraw_open,
        "deposit_open": deposit_open,
        "needs_transfer": True,
    }


def mark_status(value: bool | None) -> str:
    if value is True:
        return "✅"
    if value is False:
        return "⛔"
    return "?"


def _display_venue(exchange_id: str) -> str:
    return {
        "binance": "Binance",
        "bybit": "Bybit",
        "okx": "OKX",
        "gateio": "Gate",
        "bitget": "Bitget",
        "mexc": "MEXC",
        "bingx": "BingX",
        "kucoinfutures": "KuCoin Futures",
        "kucoin": "KuCoin",
        "htx": "HTX",
        "phemex": "Phemex",
    }.get(exchange_id, exchange_id.capitalize())


def _ccxt_exchange_class(ccxt_module: Any, exchange_id: str) -> Any:
    for candidate in ("gateio", "gate") if exchange_id == "gateio" else (exchange_id,):
        exchange_class = getattr(ccxt_module, candidate, None)
        if exchange_class is not None:
            return exchange_class
    raise AttributeError(f"CCXT exchange adapter unavailable: {exchange_id}")


def _merge_venue_name(name: str) -> str:
    return "KuCoin" if name == "KuCoin Futures" else name


def _last_price(ticker: dict[str, Any]) -> float | None:
    return _float_or_none(ticker.get("last") or ticker.get("close"))


def _quote_volume(ticker: dict[str, Any], price: float | None) -> float:
    quote_volume = _float_or_none(ticker.get("quoteVolume"))
    if quote_volume is not None:
        return quote_volume
    base_volume = _float_or_none(ticker.get("baseVolume"))
    if base_volume is not None and price is not None:
        return base_volume * price
    return 0.0


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
