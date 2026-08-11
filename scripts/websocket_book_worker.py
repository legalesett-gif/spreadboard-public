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
from spreadboard import accounts  # noqa: E402
from spreadboard.live_book_cache import LiveBookStore  # noqa: E402


RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
SNAPSHOT_PATH = RUNTIME_DIR / "api_discovery_latest.json"
ACCOUNTS_PATH = RUNTIME_DIR / "spreadboard_accounts.sqlite3"
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
        self._desired_signature: tuple[int | None, int | None, int | None] | None = None
        self._market_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._markets_ready: set[tuple[str, str]] = set()
        #: Legs a venue refuses to serve without credentials. Kept out of the
        #: reconcile so they are not resubscribed every ten seconds.
        self._unavailable: set[LegKey] = set()

    async def run(self) -> None:
        while not self.stop.is_set():
            desired = self._desired_legs_cached() - self._unavailable
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
        def stamp(path: Path) -> int | None:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return None

        accounts_wal = ACCOUNTS_PATH.with_name(f"{ACCOUNTS_PATH.name}-wal")
        signature = (
            stamp(SNAPSHOT_PATH),
            stamp(ACCOUNTS_PATH),
            stamp(accounts_wal),
        )
        if signature != self._desired_signature:
            self._desired = _desired_legs(
                SNAPSHOT_PATH,
                limit=MAX_SUBSCRIPTIONS,
                accounts_path=ACCOUNTS_PATH,
            )
            self._desired_signature = signature
            gc.collect()
        return self._desired

    async def _watch(self, key: LegKey) -> None:
        venue, market_type, symbol = key
        last_write = 0.0
        delay = 1.0
        while not self.stop.is_set():
            try:
                client = self._client(venue, market_type)
                await self._ensure_markets(venue, market_type, client)
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
            except (ccxtpro.AuthenticationError, ccxtpro.BadRequest) as exc:
                # Neither of these gets better by asking again: the venue either
                # will not serve public books without a key (Coinbase
                # International) or does not stream that symbol at all (HTX
                # answers "the coin pair does not currently offer" for a long
                # tail of spot pairs). Retrying them forever spent the CPU the
                # streaming legs needed -- 219 log lines in eight minutes while
                # the store held no books at all. Dropped until the process
                # restarts, by which time credentials or listings may differ.
                self._unavailable.add(key)
                print(
                    f"websocket-books: {venue} {market_type} {symbol}: "
                    f"dropped, will not stream ({type(exc).__name__}: {str(exc)[:70]})",
                    flush=True,
                )
                return
            except Exception as exc:  # noqa: BLE001 - reconnect is the intended fallback.
                print(
                    f"websocket-books: {venue} {market_type} {symbol}: "
                    f"{type(exc).__name__}: {str(exc)[:120]}",
                    flush=True,
                )
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)

    async def _ensure_markets(self, venue: str, market_type: str, client: Any) -> None:
        """Load a venue's markets once, not once per subscription.

        CCXT Pro loads markets implicitly on the first watch call. With hundreds
        of tasks starting together they all fired the same metadata request at
        the same venue: 114 Gate timeouts, 42 Binance, 32 Bybit and 30 XT in
        eight minutes, every one retrying with backoff, and not a single book
        written. Serialising it per venue turns a thundering herd into one
        request.
        """
        key = (venue, market_type)
        if key in self._markets_ready:
            return
        lock = self._market_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._markets_ready:
                return
            await client.load_markets()
            self._markets_ready.add(key)

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
                "timeout": 45_000,
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


#: Every lane the board renders, including the three funding-farm tabs, which
#: use their own kinds. A member on any tab expects the prices in front of them
#: to move, so each lane gets a guaranteed share before depth is added anywhere.
BOARD_LANES: tuple[dict[str, Any], ...] = (
    {},
    {"kind": "FUTURES"},
    {"kind": "FUTURES-SPOT-PAIR"},
    {"kind": "SPOT"},
    {"kind": "DEX-FUTURES"},
    {"kind": "DEX-SPOT"},
    {"funding_only": True},
    {"funding_only": True, "kind": "FUTURES"},
    {"funding_only": True, "kind": "FUTURES-SPOT-PAIR"},
    {"funding_only": True, "kind": "DEX-FUTURES"},
)

#: Routes per lane that must be streaming before any lane gets depth. Ten
#: routes is two legs each at worst, so the reservation costs at most 20
#: subscriptions per lane and leaves the rest of the budget for depth.
LANE_RESERVED_ROUTES = max(1, int(os.environ.get("SPREADBOARD_WS_LANE_ROUTES", "10")))


def _board_legs(path: Path, *, limit: int) -> list[LegKey]:
    """The legs behind the routes the board actually shows, in rank order.

    Ranking the raw snapshot by spread subscribes to whatever prints the widest
    number, which is exactly the set of dislocated rows the board filters out --
    the worker streamed hundreds of books while only a third of the routes on
    screen had a live price behind them. Selecting through the board's own
    loader is what keeps the two from drifting apart again.
    """
    from spreadboard import api_spreads

    routes_by_lane: list[list[dict[str, Any]]] = []
    for lane in BOARD_LANES:
        try:
            data = api_spreads.load_spreads(board_path=path, limit=250, **lane)
        except Exception:
            routes_by_lane.append([])
            continue
        routes_by_lane.append(
            [
                route
                for group in data.get("groups") or []
                for route in group.get("routes") or []
                if isinstance(route, dict)
            ]
            or [row for row in data.get("rows") or [] if isinstance(row, dict)]
        )

    legs: list[LegKey] = []
    seen: set[LegKey] = set()

    def take(route: dict[str, Any]) -> bool:
        """Add both legs of a route. False once the budget is spent."""
        for side in ("long", "short"):
            key = _board_leg_key(route, side)
            if key is not None and key not in seen:
                seen.add(key)
                legs.append(key)
                if len(legs) >= limit:
                    return False
        return True

    # Reserve every lane's top routes first. Walking the lanes in order instead
    # let the default view spend the whole budget, so a member on the
    # Futures-DEX tab watched numbers that never moved.
    for routes in routes_by_lane:
        for route in routes[:LANE_RESERVED_ROUTES]:
            if not take(route):
                return legs
    # Whatever is left buys depth, in the same lane order.
    for routes in routes_by_lane:
        for route in routes[LANE_RESERVED_ROUTES:]:
            if not take(route):
                return legs
    return legs


def _desired_legs(
    path: Path,
    *,
    limit: int,
    accounts_path: Path | None = None,
) -> set[LegKey]:
    # A saved, open position is already real user exposure.  Its exact public
    # books take priority over opportunity rows that merely happen to rank on
    # this scan.  DEX legs are intentionally absent: they use the verified
    # exact-route HTTP sampler instead of a CEX websocket adapter.
    legs: list[LegKey] = []
    if accounts_path is not None:
        try:
            canonical_venues = {venue.casefold(): venue for venue in VENUE_IDS}
            for raw_venue, market_type, symbol in accounts.all_open_position_market_legs(
                db_path=accounts_path
            ):
                venue = canonical_venues.get(raw_venue.casefold())
                key = (venue, market_type, symbol) if venue else None
                if key is not None and key not in legs:
                    legs.append(key)
                    if len(legs) >= limit:
                        return set(legs)
        except Exception:  # noqa: BLE001 - board subscriptions remain available.
            pass

    for key in _board_legs(path, limit=limit):
        if key not in legs:
            legs.append(key)
            if len(legs) >= limit:
                return set(legs)
    if len(legs) >= limit:
        return set(legs)

    # Spend whatever budget the visible board did not use on the widest
    # remaining routes, so a token promoted between scans is already streaming.
    seen = set(legs)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(legs)
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
    for row in ranked:
        for side in ("long", "short"):
            key = _leg_key(row, side)
            if key is not None and key not in seen:
                seen.add(key)
                legs.append(key)
                if len(legs) >= limit:
                    return set(legs)
    return set(legs)


def _board_leg_key(route: dict[str, Any], side: str) -> LegKey | None:
    """Key a displayed route's leg exactly as the board looks it up.

    api_spreads.live_prices_for keys books on `<side>_market_symbol`, so a
    subscription stored under the route_inputs symbol is a book the board can
    never find. The two must be built from the same field.
    """
    venue = str(route.get(f"{side}_venue") or "")
    market_type = str(route.get(f"{side}_market_type") or "")
    symbol = str(route.get(f"{side}_market_symbol") or "")
    if venue not in VENUE_IDS or market_type not in {"Spot", "Futures"} or not symbol:
        return None
    return (venue, market_type, symbol)


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
