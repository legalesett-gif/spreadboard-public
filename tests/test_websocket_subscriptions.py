"""What the websocket worker subscribes to must be what the board displays.

The worker used to rank the raw snapshot by spread and stream the top legs.
That set is close to the opposite of the board's: the widest raw numbers are
the dislocated rows the board filters out, so the worker held hundreds of
subscriptions while only a third of the routes on screen had a live price.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

import pytest
from aiohttp import ClientConnectionResetError

from scripts import websocket_book_worker
from scripts.websocket_book_worker import (
    BookWorker,
    _asyncio_exception_handler,
    _base_quantity_levels,
    _board_leg_key,
    _desired_legs,
    _expected_closing_pong,
)


def _row(token: str, *, spread: float, long_venue: str = "Binance") -> dict[str, Any]:
    return {
        "route_key": f"{token}|{long_venue}|Futures|Bybit|Futures",
        "token": token,
        "route_kind": "FUTURES",
        "long_venue": long_venue,
        "long_market_type": "Futures",
        "long_market_symbol": f"{token}/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": f"{token}/USDT:USDT",
        "depth_weighted_spread_pct": spread,
        "executable_spread_pct": spread,
    }


def test_fast_quote_generation_reconciles_websocket_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "api_discovery_latest.json"
    fast = tmp_path / "api_discovery_fast_quotes.json"
    accounts = tmp_path / "accounts.sqlite3"
    snapshot.write_text("{}")
    fast.write_text("{}")
    accounts.write_text("")
    monkeypatch.setattr(websocket_book_worker, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(websocket_book_worker, "FAST_QUOTE_PATH", fast)
    monkeypatch.setattr(websocket_book_worker, "ACCOUNTS_PATH", accounts)
    calls = []
    monkeypatch.setattr(
        websocket_book_worker,
        "_desired_legs",
        lambda *_args, **_kwargs: (
            calls.append(fast.stat().st_mtime_ns),
            {("Binance", "Spot", "BTC/USDT")},
        )[1],
    )
    worker = BookWorker.__new__(BookWorker)
    worker._desired = set()
    worker._desired_signature = None
    worker._desired_computed_at = -websocket_book_worker.DESIRED_REFRESH_SECONDS

    asyncio.run(worker._desired_legs_cached())
    fast.write_text('{"updated":true}')
    # A changed mtime alone no longer re-selects: selection costs 158s and
    # these files move every few minutes. The floor has not elapsed.
    asyncio.run(worker._desired_legs_cached())
    assert len(calls) == 1

    worker._desired_computed_at -= websocket_book_worker.DESIRED_REFRESH_SECONDS + 1
    asyncio.run(worker._desired_legs_cached())

    assert len(calls) == 2


def test_futures_contract_counts_are_normalised_to_base_quantity() -> None:
    assert _base_quantity_levels([[0.25, 40.0]], 100.0) == [[0.25, 4000.0]]


def test_transient_prune_lock_does_not_kill_websocket_worker() -> None:
    class LockedStore:
        def prune(self, *, max_age_seconds: float) -> None:
            assert max_age_seconds == 3600
            raise sqlite3.OperationalError("database is locked")

    worker = BookWorker.__new__(BookWorker)
    worker.store = LockedStore()

    assert worker._prune_stale_books() is False


def test_non_lock_prune_database_failure_is_not_hidden() -> None:
    class BrokenStore:
        def prune(self, *, max_age_seconds: float) -> None:
            raise sqlite3.OperationalError("disk I/O error")

    worker = BookWorker.__new__(BookWorker)
    worker.store = BrokenStore()

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        worker._prune_stale_books()


def test_expected_pong_after_transport_close_is_quiet() -> None:
    async def pong() -> None:
        return None

    task_coro = pong()

    class Future:
        def get_coro(self):
            return task_coro

    try:
        context = {
            "exception": ClientConnectionResetError(
                "Cannot write to closing transport"
            ),
            "future": Future(),
        }
        assert _expected_closing_pong(context) is True
    finally:
        task_coro.close()


def test_async_exception_handler_preserves_unexpected_fault() -> None:
    seen = []

    class Loop:
        def default_exception_handler(self, context):
            seen.append(context)

    context = {"exception": RuntimeError("real failure")}
    _asyncio_exception_handler(Loop(), context)

    assert seen == [context]


# ---------------------------------------------------------------------------
# Selecting the subscription set must never stall the sockets
# ---------------------------------------------------------------------------


def _bare_worker() -> BookWorker:
    """A worker without the real book store, as the sibling tests build it."""
    worker = BookWorker.__new__(BookWorker)
    worker._desired = set()
    worker._desired_signature = None
    worker._desired_computed_at = -websocket_book_worker.DESIRED_REFRESH_SECONDS
    return worker


class _SlowSelection:
    """Stands in for the real leg selection, which measured 158s in production.

    Ten board lanes each re-parse a 62.7MB snapshot. That cost is not the bug;
    paying it on the event loop is.
    """

    def __init__(self, seconds: float = 0.4) -> None:
        self.seconds = seconds
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> set:
        self.calls += 1
        time.sleep(self.seconds)
        return {("Binance", "Spot", "BTC/USDT")}


def test_choosing_legs_never_blocks_the_event_loop(monkeypatch) -> None:
    """The whole failure: sockets cannot tick while this runs inline.

    The worker called this synchronously inside `run`, so for ~160 seconds no
    `watch_order_book` coroutine could be scheduled. It streamed in the few
    seconds between selections, which is why venues carrying many legs wrote
    almost nothing while one-leg venues got through.
    """
    selection = _SlowSelection(0.4)
    monkeypatch.setattr(websocket_book_worker, "_desired_legs", selection)
    worker = _bare_worker()

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    async def drive() -> None:
        beat = asyncio.create_task(heartbeat())
        await worker._desired_legs_cached()
        beat.cancel()

    asyncio.run(drive())

    # Inline, the loop is frozen and this stays at 0.
    assert ticks > 5, f"event loop was blocked during selection (ticks={ticks})"


def test_the_selection_is_not_repeated_every_reconcile(monkeypatch) -> None:
    """Signatures change constantly, so mtimes alone are not a brake.

    The fast-quote file rewrites every few minutes and the accounts WAL moves
    on any user action, so the mtime cache invalidated almost every tick and
    the worker spent its life re-selecting.
    """
    selection = _SlowSelection(0.0)
    monkeypatch.setattr(websocket_book_worker, "_desired_legs", selection)
    worker = _bare_worker()

    asyncio.run(worker._desired_legs_cached())
    for _ in range(5):
        # Force the signature to look different every time.
        worker._desired_signature = ("changed", None, None, None)
        asyncio.run(worker._desired_legs_cached())

    assert selection.calls == 1, (
        f"re-selected {selection.calls} times inside the refresh interval"
    )


def test_the_set_still_refreshes_once_the_interval_passes(monkeypatch) -> None:
    """Throttling must not freeze the subscription set forever."""
    selection = _SlowSelection(0.0)
    monkeypatch.setattr(websocket_book_worker, "_desired_legs", selection)
    worker = _bare_worker()

    asyncio.run(worker._desired_legs_cached())
    worker._desired_computed_at -= websocket_book_worker.DESIRED_REFRESH_SECONDS + 1
    worker._desired_signature = ("changed", None, None, None)
    asyncio.run(worker._desired_legs_cached())

    assert selection.calls == 2
