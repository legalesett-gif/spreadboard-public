"""Fast public order-book refreshes for routes already leading the board."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import gc
import json
from pathlib import Path
from threading import Lock
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from spreadboard import live_book_cache
from spreadarb.api_discovery.models import spread_pct
from spreadarb.api_discovery.orderbook import depth_weighted_price

VENUE_IDS = {
    "Aster": "aster",
    "Binance": "binance",
    "Bingx": "bingx",
    "Bitget": "bitget",
    "BitMart": "bitmart",
    "Bybit": "bybit",
    "Coinbase": "coinbaseexchange",
    "Coinbase International": "coinbaseinternational",
    "CoinEx": "coinex",
    "Gate": "gateio",
    "HTX": "htx",
    "Hyperliquid": "hyperliquid",
    "Kraken": "kraken",
    "Kraken Futures": "krakenfutures",
    "Kucoin": "kucoin",
    "Kucoin Futures": "kucoinfutures",
    "Mexc": "mexc",
    "OKX": "okx",
    "Phemex": "phemex",
    "WhiteBIT": "whitebit",
    "XT": "xt",
}

NATIVE_FUTURES_VENUES = {
    "Aster",
    "Binance",
    "Bingx",
    "Bitget",
    "BitMart",
    "Bybit",
    "CoinEx",
    "Coinbase International",
    "Gate",
    "HTX",
    "Hyperliquid",
    "Kraken Futures",
    "Kucoin Futures",
    "Mexc",
    "OKX",
    "Phemex",
    "WhiteBIT",
    "XT",
}
NATIVE_SPOT_VENUES = {
    "Binance",
    "Bingx",
    "Bitget",
    "BitMart",
    "Bybit",
    "CoinEx",
    "Coinbase",
    "Gate",
    "HTX",
    "Kraken",
    "Kucoin",
    "Mexc",
    "OKX",
    "WhiteBIT",
    "XT",
}
FAST_QUOTE_LANES = ("FUTURES", "FUTURES-SPOT", "SPOT", "DEX-FUTURES")


class FastQuoteRefresher:
    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], Any] = {}
        self._client_lock = Lock()
        self._client_request_locks: dict[tuple[str, str], Lock] = {}

    def refresh(
        self, snapshot_path: Path, *, route_limit: int = 100, target_notional_usd: float = 50.0
    ) -> dict[str, Any]:
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"status": "unavailable", "updated": 0, "error": type(exc).__name__}
        rows_by_lane: dict[str, list[dict[str, Any]]] = {lane: [] for lane in FAST_QUOTE_LANES}
        for bucket in ("api_discovered_rows", "dex_discovered_rows"):
            for row in payload.get(bucket) or []:
                if not isinstance(row, dict) or _has_permanent_mirage_guard(row):
                    continue
                lane = _fast_quote_lane(row)
                if lane is None:
                    continue
                spread = _number(row.get("depth_weighted_spread_pct"), -999999.0)
                if 0.0 <= spread <= 90.0 or row.get("fast_quote_verified_at"):
                    rows_by_lane[lane].append(row)
        base_quota, extra = divmod(max(0, route_limit), len(FAST_QUOTE_LANES))
        selected: list[dict[str, Any]] = []
        for index, lane in enumerate(FAST_QUOTE_LANES):
            lane_limit = base_quota + (1 if index < extra else 0)
            selected.extend(
                _expanded_token_rows(
                    rows_by_lane[lane],
                    token_limit=min(50, lane_limit),
                    route_limit=lane_limit,
                )
            )
        for lane_rows in rows_by_lane.values():
            for row in lane_rows:
                blockers = [
                    str(item)
                    for item in row.get("blockers") or []
                    if not str(item).startswith("mirage_guard:fast_")
                ]
                row["blockers"] = list(dict.fromkeys(blockers))
        leg_jobs: dict[tuple[str, str, str], tuple[dict[str, Any], str]] = {}
        for row in selected:
            for side in ("long", "short"):
                key = _route_leg_key(row, side)
                if key is not None:
                    leg_jobs.setdefault(key, (row, side))
        jobs_by_venue: dict[
            tuple[str, str],
            list[tuple[tuple[str, str, str], dict[str, Any], str]],
        ] = {}
        for key, (row, side) in leg_jobs.items():
            jobs_by_venue.setdefault((key[0], key[1]), []).append((key, row, side))
        leg_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(2, len(jobs_by_venue)))) as pool:
            futures = {
                venue_key: pool.submit(
                    self._quote_venue_jobs,
                    venue_key,
                    jobs,
                    target_notional_usd=target_notional_usd,
                )
                for venue_key, jobs in jobs_by_venue.items()
            }
            for future in futures.values():
                leg_cache.update(future.result())
        updated = failed = 0
        for row in selected:
            blockers = [
                str(item)
                for item in row.get("blockers") or []
                if not str(item).startswith("mirage_guard:fast_")
            ]
            long_quote = self._leg_quote(
                row,
                "long",
                target_notional_usd=target_notional_usd,
                cache=leg_cache,
                include_funding=True,
            )
            short_quote = self._leg_quote(
                row,
                "short",
                target_notional_usd=target_notional_usd,
                cache=leg_cache,
                include_funding=True,
            )
            if long_quote is None or short_quote is None:
                blockers.append("mirage_guard:fast_requote_unavailable")
                row["blockers"] = list(dict.fromkeys(blockers))
                failed += 1
                continue
            executable = spread_pct(long_quote["ask"], short_quote["bid"])
            depth = spread_pct(long_quote["ask_vwap"], short_quote["bid_vwap"])
            if executable is None or depth is None:
                blockers.append("mirage_guard:fast_target_depth_unavailable")
                row["blockers"] = list(dict.fromkeys(blockers))
                failed += 1
                continue
            if _is_dex_route(row) and max(executable, depth) > 90.0:
                blockers.append("mirage_guard:fast_spread_out_of_bounds")
                row["blockers"] = list(dict.fromkeys(blockers))
                failed += 1
                continue
            notes = row.setdefault("notes", {})
            route_inputs = notes.setdefault("route_inputs", {})
            route_inputs["long"] = {**(route_inputs.get("long") or {}), **long_quote}
            route_inputs["short"] = {**(route_inputs.get("short") or {}), **short_quote}
            row["executable_spread_pct"] = f"{executable:.8f}".rstrip("0").rstrip(".")
            row["depth_weighted_spread_pct"] = f"{depth:.8f}".rstrip("0").rstrip(".")
            row["quote_ts_us"] = min(long_quote["quote_ts_us"], short_quote["quote_ts_us"])
            row["fast_quote_verified_at"] = _utc_now_iso()
            row["blockers"] = list(dict.fromkeys(blockers))
            updated += 1
        refreshed_at = _utc_now_iso()
        payload["fast_quote_refresh"] = {
            "status": "ok" if updated else "unavailable",
            "updated_at": refreshed_at,
            "updated_routes": updated,
            "failed_routes": failed,
            "selected_routes": len(selected),
            "target_notional_usd": target_notional_usd,
        }
        _atomic_write(snapshot_path, payload)
        return payload["fast_quote_refresh"]

    def _quote_venue_jobs(
        self,
        venue_key: tuple[str, str],
        jobs: list[tuple[tuple[str, str, str], dict[str, Any], str]],
        *,
        target_notional_usd: float,
    ) -> dict[tuple[str, str, str], dict[str, Any] | None]:
        cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        try:
            for key, row, side in jobs:
                cache[key] = self._leg_quote(
                    row,
                    side,
                    target_notional_usd=target_notional_usd,
                    cache=cache,
                    include_funding=True,
                )
        finally:
            self._discard_client(*venue_key)
        return cache

    def quote_route(
        self,
        row: dict[str, Any],
        *,
        target_notional_usd: float = 50.0,
    ) -> dict[str, Any]:
        """Reprice one exact route without changing the broad-board snapshot."""

        quoted = json.loads(json.dumps(row))
        with ThreadPoolExecutor(max_workers=2) as pool:
            long_future = pool.submit(
                self._leg_quote,
                quoted,
                "long",
                target_notional_usd=target_notional_usd,
                cache={},
                include_funding=True,
            )
            short_future = pool.submit(
                self._leg_quote,
                quoted,
                "short",
                target_notional_usd=target_notional_usd,
                cache={},
                include_funding=True,
            )
            long_quote = long_future.result()
            short_quote = short_future.result()
        if long_quote is None or short_quote is None:
            return {
                "status": "unavailable",
                "error": "exact_route_order_book_unavailable",
            }
        executable = spread_pct(long_quote["ask"], short_quote["bid"])
        depth = spread_pct(long_quote["ask_vwap"], short_quote["bid_vwap"])
        if executable is None or depth is None:
            return {
                "status": "unavailable",
                "error": "exact_route_target_depth_unavailable",
            }
        if _is_dex_route(quoted) and max(executable, depth) > 90.0:
            return {
                "status": "unavailable",
                "error": "exact_route_spread_out_of_bounds",
            }
        notes = quoted.setdefault("notes", {})
        route_inputs = notes.setdefault("route_inputs", {})
        route_inputs["long"] = {**(route_inputs.get("long") or {}), **long_quote}
        route_inputs["short"] = {**(route_inputs.get("short") or {}), **short_quote}
        quoted["executable_spread_pct"] = executable
        quoted["depth_weighted_spread_pct"] = depth
        quoted["quote_ts_us"] = min(long_quote["quote_ts_us"], short_quote["quote_ts_us"])
        quoted["target_notional_usd"] = target_notional_usd
        return {
            "status": "ok",
            "sample_source": "live_chart_exact_route",
            "target_notional_usd": target_notional_usd,
            "row": quoted,
        }

    def close(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        self._clients.clear()
        self._client_request_locks.clear()
        gc.collect()

    def _leg_quote(
        self,
        row: dict[str, Any],
        side: str,
        *,
        target_notional_usd: float,
        cache: dict[tuple[str, str, str], dict[str, Any] | None],
        include_funding: bool,
    ) -> dict[str, Any] | None:
        venue = str(row.get(f"{side}_venue") or "")
        market_type = str(row.get(f"{side}_market_type") or "")
        notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
        route_inputs = (
            notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
        )
        leg = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
        symbol = str(
            leg.get("symbol") or row.get(f"{side}_market_symbol") or row.get(f"{side}_symbol") or ""
        )
        if "okx dex" in venue.casefold():
            chain, contract = _dex_chain_contract(row)
            key = (venue, market_type, f"{chain}:{contract}")
        else:
            key = (venue, market_type, symbol)
        if key in cache:
            return cache[key]
        if "okx dex" in venue.casefold():
            value = _okx_dex_leg_quote(
                row,
                side,
                target_notional_usd=target_notional_usd,
            )
            cache[key] = value
            return value
        if not venue or not symbol or venue not in VENUE_IDS:
            return None
        try:
            live_book = live_book_cache.load_live_book(
                venue,
                market_type,
                symbol,
                max_age_seconds=5.0,
            )
            native_book = (
                (live_book.bids, live_book.asks)
                if live_book is not None
                else _native_order_book(venue, market_type, symbol)
            )
            if native_book is None:
                client = self._client(venue, market_type)
                with self._client_request_lock(venue, market_type):
                    market = client.market(symbol)
                    book = client.fetch_order_book(symbol, limit=20)
                    funding = (
                        _ccxt_current_funding(client, symbol, venue=venue)
                        if include_funding and market_type == "Futures"
                        else {}
                    )
                    if (
                        include_funding
                        and market_type == "Futures"
                        and (
                            funding.get("current_funding_pct") is None
                            or funding.get("funding_interval_hours") is None
                        )
                    ):
                        funding = {
                            **funding,
                            **{
                                key: value
                                for key, value in _native_current_funding(venue, symbol).items()
                                if value is not None
                            },
                        }
                    contract_size = (
                        _number(market.get("contractSize"), 1.0)
                        if market_type == "Futures"
                        else 1.0
                    )
                bids = _levels(book.get("bids"))
                asks = _levels(book.get("asks"))
            else:
                bids, asks = native_book
                funding = (
                    _native_current_funding(venue, symbol)
                    if include_funding and market_type == "Futures"
                    else {}
                )
                contract_size = _number(
                    leg.get("contract_size") or row.get(f"{side}_contract_size"),
                    1.0,
                )
            bid_vwap = depth_weighted_price(bids, target_notional_usd, contract_size=contract_size)
            ask_vwap = depth_weighted_price(asks, target_notional_usd, contract_size=contract_size)
            if not bids or not asks or bid_vwap is None or ask_vwap is None:
                cache[key] = None
                return None
            value = {
                "symbol": symbol,
                "bid": bids[0][0],
                "ask": asks[0][0],
                "bid_vwap": bid_vwap,
                "ask_vwap": ask_vwap,
                "contract_size": contract_size,
                "quote_ts_us": (
                    live_book.quote_ts_us if live_book is not None else int(time.time() * 1_000_000)
                ),
                "quote_source": (live_book.source if live_book is not None else "public_rest"),
                **funding,
            }
        except Exception:
            value = None
        cache[key] = value
        return value

    def _client(self, venue: str, market_type: str) -> Any:
        import ccxt

        key = (venue, market_type)
        with self._client_lock:
            client = self._clients.get(key)
            if client is not None:
                return client
            exchange_id = VENUE_IDS[venue]
            aliases = {
                "gateio": ("gateio", "gate"),
                "gate": ("gate", "gateio"),
            }
            klass = next(
                (
                    getattr(ccxt, candidate)
                    for candidate in aliases.get(exchange_id, (exchange_id,))
                    if hasattr(ccxt, candidate)
                ),
                None,
            )
            if klass is None:
                raise AttributeError(f"CCXT exchange adapter unavailable: {exchange_id}")
            client = klass(
                {
                    "enableRateLimit": True,
                    "timeout": 8_000,
                    "options": {"defaultType": "spot" if market_type == "Spot" else "swap"},
                }
            )
            client.load_markets()
            self._clients[key] = client
            self._client_request_locks.setdefault(key, Lock())
            return client

    def _client_request_lock(self, venue: str, market_type: str) -> Lock:
        key = (venue, market_type)
        with self._client_lock:
            return self._client_request_locks.setdefault(key, Lock())

    def _discard_client(self, venue: str, market_type: str) -> None:
        key = (venue, market_type)
        client = self._clients.pop(key, None)
        self._client_request_locks.pop(key, None)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        gc.collect()


def _levels(value: Any) -> list[list[float]]:
    output = []
    for item in value or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        price = _number(item[0], 0.0)
        amount = _number(item[1], 0.0)
        if price > 0 and amount > 0:
            output.append([price, amount])
    return output


def _route_leg_key(
    row: dict[str, Any],
    side: str,
) -> tuple[str, str, str] | None:
    venue = str(row.get(f"{side}_venue") or "")
    market_type = str(row.get(f"{side}_market_type") or "")
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    route_inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    leg = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
    symbol = str(
        leg.get("symbol") or row.get(f"{side}_market_symbol") or row.get(f"{side}_symbol") or ""
    )
    if "okx dex" in venue.casefold():
        chain, contract = _dex_chain_contract(row)
        symbol = f"{chain}:{contract}" if chain and contract else ""
    return (venue, market_type, symbol) if venue and market_type and symbol else None


def _fast_quote_lane(row: dict[str, Any]) -> str | None:
    long_type = str(row.get("long_market_type") or "")
    short_type = str(row.get("short_market_type") or "")
    long_venue = str(row.get("long_venue") or "")
    short_venue = str(row.get("short_venue") or "")
    venues = (long_venue, short_venue)
    has_okx_dex = any("okx dex" in venue.casefold() for venue in venues)
    cex_supported = all(venue in VENUE_IDS or "okx dex" in venue.casefold() for venue in venues)
    if not cex_supported:
        return None
    if has_okx_dex:
        blockers = {str(item) for item in row.get("blockers") or []}
        identity_unverified = (
            "cex_identity_unverified" in blockers
            or "identity_unverified" in blockers
            or any(item.startswith("identity_collision:") for item in blockers)
        )
        chain, contract = _dex_chain_contract(row)
        if identity_unverified or not chain or not contract:
            return None
        return "DEX-FUTURES" if {long_type, short_type} == {"Spot", "Futures"} else None
    if long_type == short_type == "Futures":
        return "FUTURES"
    if {long_type, short_type} == {"Spot", "Futures"}:
        return "FUTURES-SPOT"
    if long_type == short_type == "Spot":
        return "SPOT"
    return None


def _is_dex_route(row: dict[str, Any]) -> bool:
    return any(
        "okx dex" in str(row.get(f"{side}_venue") or "").casefold() for side in ("long", "short")
    )


def _unique_token_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    ranked = sorted(
        rows,
        key=lambda row: _number(row.get("depth_weighted_spread_pct"), -999999.0),
        reverse=True,
    )
    for row in ranked:
        token = str(row.get("token") or "").upper()
        if not token or token in seen:
            continue
        seen.add(token)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _expanded_token_rows(
    rows: list[dict[str, Any]],
    *,
    token_limit: int,
    route_limit: int,
) -> list[dict[str, Any]]:
    """Select one route per top token first, then its other ranked venue routes."""

    if route_limit <= 0 or token_limit <= 0:
        return []
    ranked = sorted(
        rows,
        key=lambda row: _number(row.get("depth_weighted_spread_pct"), -999999.0),
        reverse=True,
    )
    seeds = _unique_token_rows(ranked, limit=min(token_limit, route_limit))
    selected = list(seeds)
    selected_ids = {id(row) for row in selected}
    selected_tokens = {str(row.get("token") or "").upper() for row in selected}
    for row in ranked:
        if len(selected) >= route_limit:
            break
        token = str(row.get("token") or "").upper()
        if token in selected_tokens and id(row) not in selected_ids:
            selected.append(row)
            selected_ids.add(id(row))
    return selected


def _has_permanent_mirage_guard(row: dict[str, Any]) -> bool:
    return any(
        str(item).startswith("mirage_guard:")
        and not str(item).startswith("mirage_guard:fast_")
        and str(item) != "mirage_guard:spot_sell_inventory_required"
        for item in row.get("blockers") or []
    )


def _native_order_book(
    venue: str,
    market_type: str,
    symbol: str,
) -> tuple[list[list[float]], list[list[float]]] | None:
    if market_type == "Spot":
        return _native_spot_order_book(venue, symbol)
    if market_type != "Futures" or venue not in NATIVE_FUTURES_VENUES:
        return None
    base, quote = _symbol_base_quote(symbol)
    compact = _native_linear_symbol(venue, base, quote)
    if venue == "Aster":
        url = (
            f"https://fapi.asterdex.com/fapi/v1/depth?{urlencode({'symbol': compact, 'limit': 20})}"
        )
    elif venue == "Binance":
        url = (
            f"https://fapi.binance.com/fapi/v1/depth?{urlencode({'symbol': compact, 'limit': 20})}"
        )
    elif venue == "Bingx":
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/depth?" + urlencode(
            {"symbol": f"{base}-{quote}", "limit": 20}
        )
    elif venue == "Bitget":
        url = "https://api.bitget.com/api/v2/mix/market/merge-depth?" + urlencode(
            {
                "symbol": compact,
                "productType": f"{quote}-FUTURES",
                "precision": "scale0",
                "limit": 20,
            }
        )
    elif venue == "Bybit":
        url = "https://api.bybit.com/v5/market/orderbook?" + urlencode(
            {"category": "linear", "symbol": compact, "limit": 20}
        )
    elif venue == "Gate":
        url = "https://api.gateio.ws/api/v4/futures/usdt/order_book?" + urlencode(
            {"contract": f"{base}_USDT", "limit": 20}
        )
    elif venue == "Kraken Futures":
        kraken_base = _kraken_asset_code(base)
        url = "https://futures.kraken.com/derivatives/api/v3/orderbook?" + urlencode(
            {"symbol": f"pf_{kraken_base.lower()}usd"}
        )
    elif venue == "Kucoin Futures":
        url = "https://api-futures.kucoin.com/api/v1/level2/depth20?" + urlencode(
            {"symbol": compact}
        )
    elif venue == "Mexc":
        url = f"https://contract.mexc.com/api/v1/contract/depth/{base}_{quote}"
    elif venue == "HTX":
        url = "https://api.hbdm.com/linear-swap-ex/market/depth?" + urlencode(
            {"contract_code": f"{base}-{quote}", "depth": 20, "type": "step0"}
        )
    elif venue == "CoinEx":
        url = "https://api.coinex.com/v2/futures/depth?" + urlencode(
            {"market": compact, "limit": 20, "interval": "0"}
        )
    elif venue == "Phemex":
        url = "https://api.phemex.com/md/v2/orderbook?" + urlencode({"symbol": compact})
    elif venue == "WhiteBIT":
        url = f"https://whitebit.com/api/v4/public/orderbook/{base}_PERP?" + urlencode(
            {"limit": 20, "level": 2}
        )
    elif venue == "BitMart":
        url = "https://api-cloud-v2.bitmart.com/contract/public/depth?" + urlencode(
            {"symbol": compact}
        )
    elif venue == "XT":
        url = "https://fapi.xt.com/future/market/v1/public/q/depth?" + urlencode(
            {"symbol": f"{base.lower()}_{quote.lower()}", "level": 20}
        )
    elif venue == "Coinbase International":
        url = f"https://api.international.coinbase.com/api/v1/instruments/{base}-PERP/quote"
    elif venue == "Hyperliquid":
        payload = _json_post(
            "https://api.hyperliquid.xyz/info",
            {"type": "l2Book", "coin": base},
        )
        levels = payload.get("levels") if isinstance(payload, dict) else []
        raw_bids = [
            [item.get("px"), item.get("sz")]
            for item in (levels[0] if isinstance(levels, list) and levels else [])
            if isinstance(item, dict)
        ]
        raw_asks = [
            [item.get("px"), item.get("sz")]
            for item in (levels[1] if isinstance(levels, list) and len(levels) > 1 else [])
            if isinstance(item, dict)
        ]
        return _sorted_book(raw_bids, raw_asks)
    else:
        url = "https://www.okx.com/api/v5/market/books?" + urlencode(
            {"instId": f"{base}-{quote}-SWAP", "sz": 20}
        )
    request = Request(url, headers={"User-Agent": "SpreadBoard/1.0"})
    with urlopen(request, timeout=8.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if venue == "Bybit":
        raw_bids = (payload.get("result") or {}).get("b")
        raw_asks = (payload.get("result") or {}).get("a")
    elif venue in {"Bingx", "Bitget"}:
        raw_bids = (payload.get("data") or {}).get("bids")
        raw_asks = (payload.get("data") or {}).get("asks")
    elif venue == "Gate":
        raw_bids = [[item.get("p"), item.get("s")] for item in payload.get("bids") or []]
        raw_asks = [[item.get("p"), item.get("s")] for item in payload.get("asks") or []]
    elif venue == "Kraken Futures":
        raw_bids = (payload.get("orderBook") or {}).get("bids")
        raw_asks = (payload.get("orderBook") or {}).get("asks")
    elif venue == "Kucoin Futures":
        raw_bids = (payload.get("data") or {}).get("bids")
        raw_asks = (payload.get("data") or {}).get("asks")
    elif venue == "Mexc":
        raw_bids = (payload.get("data") or {}).get("bids")
        raw_asks = (payload.get("data") or {}).get("asks")
    elif venue == "HTX":
        raw_bids = (payload.get("tick") or {}).get("bids")
        raw_asks = (payload.get("tick") or {}).get("asks")
    elif venue == "CoinEx":
        depth = (payload.get("data") or {}).get("depth") or {}
        raw_bids = depth.get("bids")
        raw_asks = depth.get("asks")
    elif venue == "Phemex":
        book = (payload.get("result") or {}).get("orderbook_p") or {}
        raw_bids = book.get("bids")
        raw_asks = book.get("asks")
    elif venue in {"WhiteBIT", "BitMart"}:
        data = payload.get("data") if venue == "BitMart" else payload
        raw_bids = (data or {}).get("bids")
        raw_asks = (data or {}).get("asks")
    elif venue == "XT":
        data = payload.get("result") or {}
        raw_bids = data.get("b")
        raw_asks = data.get("a")
    elif venue == "Coinbase International":
        raw_bids = [[payload.get("best_bid_price"), payload.get("best_bid_size")]]
        raw_asks = [[payload.get("best_ask_price"), payload.get("best_ask_size")]]
    elif venue == "OKX":
        books = payload.get("data") or []
        raw_bids = books[0].get("bids") if books else []
        raw_asks = books[0].get("asks") if books else []
    else:
        raw_bids = payload.get("bids")
        raw_asks = payload.get("asks")
    return _sorted_book(raw_bids, raw_asks)


def supports_native_order_book(venue: str, market_type: str) -> bool:
    if market_type == "Spot":
        return venue in NATIVE_SPOT_VENUES
    if market_type == "Futures":
        return venue in NATIVE_FUTURES_VENUES
    return False


def _native_spot_order_book(
    venue: str,
    symbol: str,
) -> tuple[list[list[float]], list[list[float]]] | None:
    if venue not in NATIVE_SPOT_VENUES:
        return None
    base, quote = _symbol_base_quote(symbol)
    compact = f"{base}{quote}"
    dashed = f"{base}-{quote}"
    if venue == "Binance":
        url = "https://api.binance.com/api/v3/depth?" + urlencode({"symbol": compact, "limit": 20})
    elif venue == "Bingx":
        url = "https://open-api.bingx.com/openApi/spot/v1/market/depth?" + urlencode(
            {"symbol": dashed, "limit": 20}
        )
    elif venue == "Bitget":
        url = "https://api.bitget.com/api/v2/spot/market/orderbook?" + urlencode(
            {"symbol": compact, "type": "step0", "limit": 20}
        )
    elif venue == "Bybit":
        url = "https://api.bybit.com/v5/market/orderbook?" + urlencode(
            {"category": "spot", "symbol": compact, "limit": 20}
        )
    elif venue == "Gate":
        url = "https://api.gateio.ws/api/v4/spot/order_book?" + urlencode(
            {"currency_pair": f"{base}_{quote}", "limit": 20}
        )
    elif venue == "Kucoin":
        url = f"https://api.kucoin.com/api/v1/market/orderbook/level2_20?symbol={dashed}"
    elif venue == "Mexc":
        url = "https://api.mexc.com/api/v3/depth?" + urlencode({"symbol": compact, "limit": 20})
    elif venue == "HTX":
        url = "https://api.huobi.pro/market/depth?" + urlencode(
            {"symbol": compact.lower(), "type": "step0", "depth": 20}
        )
    elif venue == "CoinEx":
        url = "https://api.coinex.com/v2/spot/depth?" + urlencode(
            {"market": compact, "limit": 20, "interval": "0"}
        )
    elif venue == "WhiteBIT":
        url = f"https://whitebit.com/api/v4/public/orderbook/{base}_{quote}?" + urlencode(
            {"limit": 20, "level": 2}
        )
    elif venue == "BitMart":
        url = "https://api-cloud.bitmart.com/spot/quotation/v3/books?" + urlencode(
            {"symbol": f"{base}_{quote}", "limit": 20}
        )
    elif venue == "XT":
        url = "https://sapi.xt.com/v4/public/depth?" + urlencode(
            {"symbol": f"{base.lower()}_{quote.lower()}", "limit": 20}
        )
    elif venue == "Coinbase":
        url = f"https://api.exchange.coinbase.com/products/{base}-{quote}/book?" + urlencode(
            {"level": 2}
        )
    elif venue == "Kraken":
        url = "https://api.kraken.com/0/public/PreTrade?" + urlencode(
            {"symbol": f"{base}/{quote}"}
        )
    else:
        url = "https://www.okx.com/api/v5/market/books?" + urlencode({"instId": dashed, "sz": 20})
    payload = _json_url(url)
    if venue == "Bybit":
        raw_bids = (payload.get("result") or {}).get("b")
        raw_asks = (payload.get("result") or {}).get("a")
    elif venue in {"Bingx", "Bitget", "Kucoin"}:
        data = payload.get("data") or {}
        raw_bids = data.get("bids")
        raw_asks = data.get("asks")
    elif venue == "OKX":
        books = payload.get("data") or []
        raw_bids = books[0].get("bids") if books else []
        raw_asks = books[0].get("asks") if books else []
    elif venue == "HTX":
        raw_bids = (payload.get("tick") or {}).get("bids")
        raw_asks = (payload.get("tick") or {}).get("asks")
    elif venue == "CoinEx":
        depth = (payload.get("data") or {}).get("depth") or {}
        raw_bids = depth.get("bids")
        raw_asks = depth.get("asks")
    elif venue in {"BitMart", "XT"}:
        data = payload.get("data") if venue == "BitMart" else payload.get("result")
        raw_bids = (data or {}).get("bids")
        raw_asks = (data or {}).get("asks")
    elif venue == "Kraken":
        data = payload.get("result") or {}
        raw_bids = [[item.get("price"), item.get("qty")] for item in data.get("bids") or []]
        raw_asks = [[item.get("price"), item.get("qty")] for item in data.get("asks") or []]
    else:
        raw_bids = payload.get("bids")
        raw_asks = payload.get("asks")
    return _sorted_book(raw_bids, raw_asks)


def _native_current_funding(venue: str, symbol: str) -> dict[str, Any]:
    base, quote = _symbol_base_quote(symbol)
    compact = _native_linear_symbol(venue, base, quote)
    try:
        if venue in {"Aster", "Binance"}:
            host = "fapi.asterdex.com" if venue == "Aster" else "fapi.binance.com"
            payload = _json_url(
                f"https://{host}/fapi/v1/premiumIndex?" + urlencode({"symbol": compact})
            )
            info_payload = _json_url(
                f"https://{host}/fapi/v1/fundingInfo?" + urlencode({"symbol": compact})
            )
            info_rows = info_payload if isinstance(info_payload, list) else []
            funding_info = next(
                (
                    item
                    for item in info_rows
                    if isinstance(item, dict) and item.get("symbol") == compact
                ),
                {},
            )
            return _funding_fields(
                payload.get("lastFundingRate"),
                interval_hours=funding_info.get("fundingIntervalHours"),
                next_funding_ms=payload.get("nextFundingTime"),
            )
        if venue == "Bybit":
            payload = _json_url(
                "https://api.bybit.com/v5/market/tickers?"
                + urlencode({"category": "linear", "symbol": compact})
            )
            rows = (payload.get("result") or {}).get("list") or []
            item = rows[0] if rows else {}
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=item.get("fundingIntervalHour"),
                next_funding_ms=item.get("nextFundingTime"),
            )
        if venue == "OKX":
            payload = _json_url(
                "https://www.okx.com/api/v5/public/funding-rate?"
                + urlencode({"instId": f"{base}-{quote}-SWAP"})
            )
            rows = payload.get("data") or []
            item = rows[0] if rows else {}
            interval = _interval_hours(item.get("fundingTime"), item.get("nextFundingTime"))
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=interval,
                next_funding_ms=item.get("nextFundingTime"),
            )
        if venue == "Gate":
            payload = _json_url(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{base}_USDT")
            return _funding_fields(
                payload.get("funding_rate"),
                interval_hours=_seconds_to_hours(payload.get("funding_interval")),
                next_funding_seconds=payload.get("funding_next_apply"),
            )
        if venue == "Bitget":
            payload = _json_url(
                "https://api.bitget.com/api/v2/mix/market/current-fund-rate?"
                + urlencode({"symbol": compact, "productType": f"{quote}-FUTURES"})
            )
            rows = payload.get("data") or []
            item = rows[0] if rows else {}
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=item.get("fundingRateInterval"),
                next_funding_ms=item.get("nextUpdate"),
            )
        if venue == "Bingx":
            payload = _json_url(
                "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?"
                + urlencode({"symbol": f"{base}-{quote}"})
            )
            item = payload.get("data") or {}
            return _funding_fields(
                item.get("lastFundingRate"),
                interval_hours=item.get("fundingIntervalHours"),
                next_funding_ms=item.get("nextFundingTime"),
            )
        if venue == "Kucoin Futures":
            payload = _json_url(
                f"https://api-futures.kucoin.com/api/v1/funding-rate/{compact}/current"
            )
            item = payload.get("data") or {}
            return _funding_fields(
                item.get("value"),
                interval_hours=_milliseconds_to_hours(item.get("granularity")),
                next_funding_ms=item.get("fundingTime"),
            )
        if venue == "Mexc":
            payload = _json_url(
                f"https://contract.mexc.com/api/v1/contract/funding_rate/{base}_{quote}"
            )
            item = payload.get("data") or {}
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=item.get("collectCycle"),
                next_funding_ms=item.get("nextSettleTime"),
            )
        if venue == "Hyperliquid":
            payload = _json_post(
                "https://api.hyperliquid.xyz/info",
                {"type": "metaAndAssetCtxs"},
            )
            if not isinstance(payload, list) or len(payload) < 2:
                return {}
            meta = payload[0] if isinstance(payload[0], dict) else {}
            contexts = payload[1] if isinstance(payload[1], list) else []
            universe = meta.get("universe") if isinstance(meta.get("universe"), list) else []
            index = next(
                (
                    index
                    for index, item in enumerate(universe)
                    if isinstance(item, dict)
                    and (
                        str(item.get("name") or "").upper() == base
                        or str(item.get("name") or "").upper().endswith(f":{base}")
                    )
                ),
                None,
            )
            if index is None or index >= len(contexts) or not isinstance(contexts[index], dict):
                return {}
            next_hour_ms = int((time.time() // 3600 + 1) * 3600 * 1000)
            return _funding_fields(
                contexts[index].get("funding"),
                interval_hours=1,
                next_funding_ms=next_hour_ms,
            )
        if venue == "HTX":
            payload = _json_url(
                "https://api.hbdm.com/v5/market/funding_rate?"
                + urlencode({"contract_code": f"{base}-{quote}"})
            )
            rows = payload.get("data") or []
            item = rows[0] if rows else {}
            return _funding_fields(
                item.get("funding_rate"),
                interval_hours=_interval_hours(
                    item.get("funding_time"), item.get("next_funding_time")
                ),
                next_funding_ms=item.get("next_funding_time"),
            )
        if venue == "Phemex":
            payload = _json_url(
                "https://api.phemex.com/md/v2/ticker/24hr?" + urlencode({"symbol": compact})
            )
            item = payload.get("result") or {}
            return _funding_fields(item.get("fundingRateRr"), interval_hours=8)
        if venue == "CoinEx":
            payload = _json_url(
                "https://api.coinex.com/v2/futures/funding-rate?" + urlencode({"market": compact})
            )
            rows = payload.get("data") or []
            item = rows[0] if rows else {}
            return _funding_fields(
                item.get("next_funding_rate") or item.get("latest_funding_rate"),
                interval_hours=_interval_hours(
                    item.get("latest_funding_time"), item.get("next_funding_time")
                ),
                next_funding_ms=item.get("next_funding_time"),
            )
        if venue == "BitMart":
            payload = _json_url(
                "https://api-cloud-v2.bitmart.com/contract/public/funding-rate?"
                + urlencode({"symbol": compact})
            )
            item = payload.get("data") or {}
            return _funding_fields(
                item.get("rate_value") or item.get("expected_rate"),
                interval_hours=8,
                next_funding_ms=item.get("funding_time"),
            )
        if venue == "XT":
            payload = _json_url(
                "https://fapi.xt.com/future/market/v1/public/q/funding-rate?"
                + urlencode({"symbol": f"{base.lower()}_{quote.lower()}"})
            )
            item = payload.get("result") or {}
            return _funding_fields(
                item.get("fundingRate"),
                interval_hours=item.get("collectionInternal"),
                next_funding_ms=item.get("nextCollectionTime"),
            )
        if venue == "WhiteBIT":
            payload = _json_url("https://whitebit.com/api/v4/public/futures")
            rows = payload.get("result") if isinstance(payload, dict) else payload
            item = next(
                (
                    row
                    for row in rows or []
                    if isinstance(row, dict)
                    and str(row.get("ticker_id") or row.get("name") or "").upper()
                    in {f"{base}_PERP", f"{base}_{quote}"}
                ),
                {},
            )
            return _funding_fields(
                item.get("funding_rate"),
                interval_hours=(
                    _optional_number(item.get("funding_interval_minutes")) / 60.0
                    if _optional_number(item.get("funding_interval_minutes"))
                    else _seconds_to_hours(item.get("funding_interval"))
                ),
                next_funding_ms=item.get("next_funding_rate_timestamp"),
                next_funding_seconds=item.get("funding_next_apply"),
            )
        if venue == "Coinbase International":
            payload = _json_url(
                f"https://api.international.coinbase.com/api/v1/instruments/{base}-PERP/quote"
            )
            return _funding_fields(payload.get("predicted_funding"), interval_hours=1)
        if venue == "Kraken Futures":
            kraken_symbol = f"PF_{_kraken_asset_code(base)}USD"
            payload = _json_url("https://futures.kraken.com/derivatives/api/v3/tickers")
            item = next(
                (
                    row
                    for row in payload.get("tickers") or []
                    if isinstance(row, dict)
                    and str(row.get("symbol") or "").upper() == kraken_symbol
                ),
                {},
            )
            funding_velocity = _optional_number(item.get("fundingRate"))
            index_price = _optional_number(item.get("indexPrice"))
            if funding_velocity is None or index_price is None or index_price <= 0:
                return {}
            next_hour_ms = int((time.time() // 3600 + 1) * 3600 * 1000)
            return _funding_fields(
                funding_velocity / index_price,
                interval_hours=1,
                next_funding_ms=next_hour_ms,
            )
    except Exception:
        return {}
    return {}


def _milliseconds_to_hours(value: Any) -> float | None:
    parsed = _optional_number(value)
    return parsed / 3_600_000.0 if parsed is not None and parsed > 0 else None


def _ccxt_current_funding(
    client: Any,
    symbol: str,
    *,
    venue: str | None = None,
) -> dict[str, Any]:
    try:
        if not getattr(client, "has", {}).get("fetchFundingRate"):
            return {}
        payload = client.fetch_funding_rate(symbol) or {}
        interval = payload.get("interval")
        if isinstance(interval, str) and interval.casefold().endswith("h"):
            interval = interval[:-1]
        return _funding_fields(
            payload.get("fundingRate"),
            interval_hours=interval,
            next_funding_ms=payload.get("fundingTimestamp"),
        )
    except Exception:
        return {}


def _funding_fields(
    rate: Any,
    *,
    interval_hours: Any = None,
    next_funding_ms: Any = None,
    next_funding_seconds: Any = None,
) -> dict[str, Any]:
    parsed = _optional_number(rate)
    if parsed is None:
        return {}
    interval = _optional_number(interval_hours)
    next_ms = _optional_number(next_funding_ms)
    if next_ms is None:
        next_seconds = _optional_number(next_funding_seconds)
        next_ms = next_seconds * 1000 if next_seconds is not None else None
    output = {"current_funding_pct": parsed * 100.0}
    if interval is not None:
        output["funding_interval_hours"] = interval
    if next_ms is not None:
        output["next_funding_ts_us"] = int(next_ms * 1000)
    return output


def _json_url(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"})
    with urlopen(request, timeout=6.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, (dict, list)) else {}


def _json_post(url: str, payload: dict[str, Any]) -> Any:
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
    with urlopen(request, timeout=6.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _symbol_base_quote(symbol: str) -> tuple[str, str]:
    base, _, quote = str(symbol).partition("/")
    return base.upper(), (quote.split(":", 1)[0] or "USDT").upper()


def _kraken_asset_code(base: str) -> str:
    return {"BTC": "XBT", "DOGE": "XDG"}.get(base.upper(), base.upper())


def _native_linear_symbol(venue: str, base: str, quote: str) -> str:
    if quote.upper() == "USDC" and venue in {"Bitget", "Bybit"}:
        return f"{base.upper()}PERP"
    if venue == "Kucoin Futures":
        return f"{_kraken_asset_code(base)}{quote.upper()}M"
    return f"{base.upper()}{quote.upper()}"


def _sorted_book(
    raw_bids: Any,
    raw_asks: Any,
) -> tuple[list[list[float]], list[list[float]]]:
    bids = sorted(_levels(raw_bids), key=lambda level: level[0], reverse=True)
    asks = sorted(_levels(raw_asks), key=lambda level: level[0])
    return bids, asks


def _okx_dex_leg_quote(
    row: dict[str, Any],
    side: str,
    *,
    target_notional_usd: float,
) -> dict[str, Any] | None:
    chain, contract = _dex_chain_contract(row)
    if not chain or not contract:
        return None
    try:
        from spreadarb.dex import okx_quotes

        buy = okx_quotes.quote_usdc_to_token(
            chain=chain,
            token_address=contract,
            notional_usd=Decimal(str(target_notional_usd)),
        )
        quantity = Decimal(str(buy.get("out_qty") or "0"))
        decimals = _optional_int(buy.get("to_token_decimals"))
        if buy.get("status") != "ok" or quantity <= 0 or decimals is None:
            return None
        sell = okx_quotes.quote_token_to_usdc(
            chain=chain,
            token_address=contract,
            token_quantity=quantity,
            token_decimals=decimals,
        )
        bid = _optional_number(sell.get("dex_sell_price_usd"))
        ask = _optional_number(buy.get("dex_buy_price_usd"))
        if sell.get("status") != "ok" or bid is None or ask is None:
            return None
        return {
            "symbol": str(row.get("token") or ""),
            "bid": bid,
            "ask": ask,
            "bid_vwap": bid,
            "ask_vwap": ask,
            "contract_size": 1.0,
            "quote_ts_us": int(time.time() * 1_000_000),
            "chain_id": chain,
            "token_address": contract,
            "sample_side": side,
        }
    except Exception:
        return None


def _dex_chain_contract(row: dict[str, Any]) -> tuple[str, str]:
    chain = str(row.get("dex_chain") or "").strip()
    contract = str(row.get("dex_contract") or "").strip()
    if chain and contract:
        return chain, contract
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    identities = notes.get("identity") if isinstance(notes.get("identity"), dict) else {}
    for side in ("long", "short"):
        venue = str(row.get(f"{side}_venue") or "")
        identity = identities.get(side) if isinstance(identities.get(side), dict) else {}
        if "okx dex" not in venue.casefold():
            continue
        nested_chain = str(identity.get("chain_id") or "").strip()
        nested_contract = str(identity.get("token_address") or "").strip()
        if nested_chain and nested_contract:
            return nested_chain, nested_contract
    return "", ""


def _interval_hours(current_ms: Any, next_ms: Any) -> float | None:
    current = _optional_number(current_ms)
    upcoming = _optional_number(next_ms)
    if current is None or upcoming is None or upcoming <= current:
        return None
    return (upcoming - current) / 3_600_000.0


def _seconds_to_hours(value: Any) -> float | None:
    parsed = _optional_number(value)
    return parsed / 3600.0 if parsed is not None else None


def _optional_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".fast.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
