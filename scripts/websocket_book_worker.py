#!/usr/bin/env python3
"""Stream exact public books for currently relevant SpreadBoard routes."""

from __future__ import annotations

import asyncio
import gc
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

import ccxt.pro as ccxtpro  # noqa: E402

from spreadboard.fast_quotes import VENUE_IDS  # noqa: E402
from spreadboard.live_book_cache import LiveBookStore  # noqa: E402


RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
SNAPSHOT_PATH = RUNTIME_DIR / "api_discovery_latest.json"
MAX_SUBSCRIPTIONS = max(20, int(os.environ.get("SPREADBOARD_WS_BOOKS", "160")))
RECONCILE_SECONDS = max(3.0, float(os.environ.get("SPREADBOARD_WS_RECONCILE_SECONDS", "10")))
WRITE_INTERVAL_SECONDS = max(0.1, float(os.environ.get("SPREADBOARD_WS_WRITE_SECONDS", "0.25")))

LegKey = tuple[str, str, str]


class BookWorker:
    def __init__(self) -> None:
        self.stop = asyncio.Event()
        self.store = LiveBookStore()
        self.clients: dict[tuple[str, str], Any] = {}
        self.tasks: dict[LegKey, asyncio.Task[None]] = {}
        self._desired: set[LegKey] = set()
        self._desired_stamp: int | None = None

    async def run(self) -> None:
        while not self.stop.is_set():
            desired = self._desired_legs_cached()
            for key in set(self.tasks) - desired:
                self.tasks.pop(key).cancel()
            for key in desired - set(self.tasks):
                self.tasks[key] = asyncio.create_task(self._watch(key))
            self.store.prune(max_age_seconds=3600)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=RECONCILE_SECONDS)
            except TimeoutError:
                pass
        await self.close()

    def _desired_legs_cached(self) -> set[LegKey]:
        """Re-read the subscription list only when the snapshot actually changes.

        This ran every reconcile tick, parsing the whole snapshot each time. Once
        the board grew that meant re-parsing 77MB every ten seconds -- seconds of
        CPU and hundreds of megabytes of allocation per cycle -- and the worker
        spent all its time doing that instead of streaming. The feed went silent
        for thirteen minutes while the file kept being re-read.
        """
        try:
            stamp = SNAPSHOT_PATH.stat().st_mtime_ns
        except OSError:
            return self._desired
        if stamp != self._desired_stamp:
            self._desired = _desired_legs(SNAPSHOT_PATH, limit=MAX_SUBSCRIPTIONS)
            self._desired_stamp = stamp
            gc.collect()
        return self._desired

    async def _watch(self, key: LegKey) -> None:
        venue, market_type, symbol = key
        last_write = 0.0
        delay = 1.0
        while not self.stop.is_set():
            try:
                client = self._client(venue, market_type)
                book = await client.watch_order_book(
                    symbol,
                    limit=_websocket_depth_limit(venue, market_type),
                )
                bids = _levels(book.get("bids"))
                asks = _levels(book.get("asks"))
                now = time.monotonic()
                if bids and asks and now - last_write >= WRITE_INTERVAL_SECONDS:
                    timestamp_ms = _number(book.get("timestamp"))
                    quote_ts_us = (
                        int(timestamp_ms * 1000)
                        if timestamp_ms and timestamp_ms > 0
                        else int(time.time() * 1_000_000)
                    )
                    self.store.put(
                        venue,
                        market_type,
                        symbol,
                        bids=bids,
                        asks=asks,
                        quote_ts_us=quote_ts_us,
                    )
                    last_write = now
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect is the intended fallback.
                print(
                    f"websocket-books: {venue} {market_type} {symbol}: "
                    f"{type(exc).__name__}: {str(exc)[:120]}",
                    flush=True,
                )
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)

    def _client(self, venue: str, market_type: str) -> Any:
        key = (venue, market_type)
        current = self.clients.get(key)
        if current is not None:
            return current
        exchange_id = VENUE_IDS[venue]
        aliases = {
            "gateio": ("gateio", "gate"),
            "gate": ("gate", "gateio"),
            "coinbaseexchange": ("coinbase",),
        }
        klass = next(
            (
                getattr(ccxtpro, candidate)
                for candidate in aliases.get(exchange_id, (exchange_id,))
                if hasattr(ccxtpro, candidate)
            ),
            None,
        )
        if klass is None:
            raise AttributeError(f"CCXT Pro adapter unavailable: {exchange_id}")
        current = klass(
            {
                "enableRateLimit": True,
                "timeout": 15_000,
                "options": {"defaultType": "spot" if market_type == "Spot" else "swap"},
            }
        )
        self.clients[key] = current
        return current

    async def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for client in self.clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self.store.close()


def _desired_legs(path: Path, *, limit: int) -> set[LegKey]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    rows = [
        row
        for bucket in ("api_discovered_rows", "dex_discovered_rows")
        for row in payload.get(bucket) or []
        if isinstance(row, dict)
    ]
    ranked = sorted(
        rows,
        key=lambda row: _number(row.get("depth_weighted_spread_pct")) or -999_999.0,
        reverse=True,
    )
    legs: list[LegKey] = []
    for row in ranked:
        for side in ("long", "short"):
            key = _leg_key(row, side)
            if key is not None and key not in legs:
                legs.append(key)
                if len(legs) >= limit:
                    return set(legs)
    return set(legs)


def _leg_key(row: dict[str, Any], side: str) -> LegKey | None:
    venue = str(row.get(f"{side}_venue") or "")
    market_type = str(row.get(f"{side}_market_type") or "")
    if venue not in VENUE_IDS or market_type not in {"Spot", "Futures"}:
        return None
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    route_inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    leg = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
    symbol = str(
        leg.get("symbol") or row.get(f"{side}_market_symbol") or row.get(f"{side}_symbol") or ""
    )
    return (venue, market_type, symbol) if symbol else None


def _levels(value: Any) -> list[list[float]]:
    output: list[list[float]] = []
    for item in value or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        price = _number(item[0])
        amount = _number(item[1])
        if price and amount and price > 0 and amount > 0:
            output.append([price, amount])
    return output


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _websocket_depth_limit(venue: str, market_type: str) -> int:
    if venue == "Kraken":
        return 25
    if venue == "Bybit":
        return 50
    return 20


async def main() -> None:
    worker = BookWorker()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, worker.stop.set)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
