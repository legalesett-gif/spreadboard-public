"""What the websocket worker subscribes to must be what the board displays.

The worker used to rank the raw snapshot by spread and stream the top legs.
That set is close to the opposite of the board's: the widest raw numbers are
the dislocated rows the board filters out, so the worker held hundreds of
subscriptions while only a third of the routes on screen had a live price.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from scripts import websocket_book_worker
from scripts.websocket_book_worker import (
    BookWorker,
    _base_quantity_levels,
    _board_leg_key,
    _desired_legs,
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
        lambda *_args, **_kwargs: calls.append(fast.stat().st_mtime_ns) or set(),
    )
    worker = BookWorker.__new__(BookWorker)
    worker._desired = set()
    worker._desired_signature = None

    worker._desired_legs_cached()
    fast.write_text('{"updated":true}')
    worker._desired_legs_cached()

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
