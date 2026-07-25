"""Fast public order-book refreshes for routes already leading the board."""

from __future__ import annotations

from datetime import datetime, timezone
import gc
import json
from pathlib import Path
import time
from typing import Any

import ccxt

from spreadarb.api_discovery.models import spread_pct
from spreadarb.api_discovery.orderbook import depth_weighted_price

VENUE_IDS = {
    "Aster": "aster",
    "Binance": "binance",
    "Bingx": "bingx",
    "Bitget": "bitget",
    "Bybit": "bybit",
    "Coinbase": "coinbaseexchange",
    "Gate": "gateio",
    "Hyperliquid": "hyperliquid",
    "Kraken": "kraken",
    "Kraken Futures": "krakenfutures",
    "Kucoin": "kucoin",
    "Kucoin Futures": "kucoinfutures",
    "Mexc": "mexc",
    "OKX": "okx",
}


class FastQuoteRefresher:
    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], Any] = {}

    def refresh(
        self, snapshot_path: Path, *, route_limit: int = 30, target_notional_usd: float = 50.0
    ) -> dict[str, Any]:
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"status": "unavailable", "updated": 0, "error": type(exc).__name__}
        rows = [
            row
            for bucket in ("api_discovered_rows", "dex_discovered_rows")
            for row in payload.get(bucket) or []
            if isinstance(row, dict)
            and not any(str(item).startswith("mirage_guard:") for item in row.get("blockers") or [])
        ]
        selected = sorted(
            rows,
            key=lambda row: _number(row.get("depth_weighted_spread_pct"), -999999.0),
            reverse=True,
        )[: max(0, route_limit)]
        leg_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        updated = failed = 0
        for row in selected:
            blockers = [
                str(item)
                for item in row.get("blockers") or []
                if not str(item).startswith("mirage_guard:fast_")
            ]
            long_quote = self._leg_quote(
                row, "long", target_notional_usd=target_notional_usd, cache=leg_cache
            )
            short_quote = self._leg_quote(
                row, "short", target_notional_usd=target_notional_usd, cache=leg_cache
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
            notes = row.setdefault("notes", {})
            route_inputs = notes.setdefault("route_inputs", {})
            route_inputs["long"] = {**(route_inputs.get("long") or {}), **long_quote}
            route_inputs["short"] = {**(route_inputs.get("short") or {}), **short_quote}
            row["executable_spread_pct"] = f"{executable:.8f}".rstrip("0").rstrip(".")
            row["depth_weighted_spread_pct"] = f"{depth:.8f}".rstrip("0").rstrip(".")
            row["quote_ts_us"] = min(long_quote["quote_ts_us"], short_quote["quote_ts_us"])
            row["blockers"] = list(dict.fromkeys(blockers))
            updated += 1
        payload["fast_quote_refresh"] = {
            "status": "ok" if updated else "unavailable",
            "updated_at": _utc_now_iso(),
            "updated_routes": updated,
            "failed_routes": failed,
            "selected_routes": len(selected),
            "target_notional_usd": target_notional_usd,
        }
        _atomic_write(snapshot_path, payload)
        return payload["fast_quote_refresh"]

    def close(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        self._clients.clear()
        gc.collect()

    def _leg_quote(
        self,
        row: dict[str, Any],
        side: str,
        *,
        target_notional_usd: float,
        cache: dict[tuple[str, str, str], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        venue = str(row.get(f"{side}_venue") or "")
        market_type = str(row.get(f"{side}_market_type") or "")
        notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
        route_inputs = (
            notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
        )
        leg = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
        symbol = str(leg.get("symbol") or "")
        key = (venue, market_type, symbol)
        if not venue or not symbol or venue not in VENUE_IDS:
            return None
        if key in cache:
            return cache[key]
        try:
            client = self._client(venue, market_type)
            market = client.market(symbol)
            book = client.fetch_order_book(symbol, limit=20)
            contract_size = (
                _number(market.get("contractSize"), 1.0) if market_type == "Futures" else 1.0
            )
            bids = _levels(book.get("bids"))
            asks = _levels(book.get("asks"))
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
                "quote_ts_us": int(time.time() * 1_000_000),
            }
        except Exception:
            value = None
            self._discard_client(venue, market_type)
        cache[key] = value
        return value

    def _client(self, venue: str, market_type: str) -> Any:
        key = (venue, market_type)
        client = self._clients.get(key)
        if client is not None:
            return client
        klass = getattr(ccxt, VENUE_IDS[venue])
        client = klass(
            {
                "enableRateLimit": True,
                "timeout": 8_000,
                "options": {"defaultType": "spot" if market_type == "Spot" else "swap"},
            }
        )
        client.load_markets()
        self._clients[key] = client
        return client

    def _discard_client(self, venue: str, market_type: str) -> None:
        client = self._clients.pop((venue, market_type), None)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


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
