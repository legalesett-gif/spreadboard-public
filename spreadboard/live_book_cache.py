"""Process-shared cache for public WebSocket order books."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_PATH = RUNTIME_DIR / "spreadboard_live_books.sqlite3"
_DEFAULT_STORE: LiveBookStore | None = None
_DEFAULT_STORE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class CachedBook:
    bids: list[list[float]]
    asks: list[list[float]]
    quote_ts_us: int
    source: str = "public_websocket"


class LiveBookStore:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_books (
                cache_key TEXT PRIMARY KEY,
                venue TEXT NOT NULL,
                market_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quote_ts_us INTEGER NOT NULL,
                bids_json TEXT NOT NULL,
                asks_json TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def put(
        self,
        venue: str,
        market_type: str,
        symbol: str,
        *,
        bids: list[list[float]],
        asks: list[list[float]],
        quote_ts_us: int,
    ) -> None:
        if not bids or not asks:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO live_books (
                    cache_key, venue, market_type, symbol, quote_ts_us, bids_json, asks_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    quote_ts_us = excluded.quote_ts_us,
                    bids_json = excluded.bids_json,
                    asks_json = excluded.asks_json
                """,
                (
                    cache_key(venue, market_type, symbol),
                    venue,
                    market_type,
                    symbol,
                    int(quote_ts_us),
                    json.dumps(bids[:50], separators=(",", ":")),
                    json.dumps(asks[:50], separators=(",", ":")),
                ),
            )
            self._conn.commit()

    def get(
        self,
        venue: str,
        market_type: str,
        symbol: str,
        *,
        max_age_seconds: float = 5.0,
    ) -> CachedBook | None:
        cutoff_us = int((time.time() - max(0.1, max_age_seconds)) * 1_000_000)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT quote_ts_us, bids_json, asks_json
                FROM live_books
                WHERE cache_key = ? AND quote_ts_us >= ?
                """,
                (cache_key(venue, market_type, symbol), cutoff_us),
            ).fetchone()
        if row is None:
            return None
        try:
            bids = _levels(json.loads(row[1]))
            asks = _levels(json.loads(row[2]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not bids or not asks:
            return None
        return CachedBook(
            bids=sorted(bids, key=lambda item: item[0], reverse=True),
            asks=sorted(asks, key=lambda item: item[0]),
            quote_ts_us=int(row[0]),
        )

    def load_all(self, *, max_age_seconds: float = 30.0) -> dict[str, "CachedBook"]:
        """Every book fresh enough to price with, in one query.

        The board serves thousands of routes; asking per leg would be thousands
        of round trips. The websocket worker only tracks a few hundred legs, so
        the whole set fits comfortably in memory.
        """
        cutoff_us = int((time.time() - max(0.1, max_age_seconds)) * 1_000_000)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT cache_key, quote_ts_us, bids_json, asks_json
                FROM live_books
                WHERE quote_ts_us >= ?
                """,
                (cutoff_us,),
            ).fetchall()
        books: dict[str, CachedBook] = {}
        for key, quote_ts_us, bids_json, asks_json in rows:
            try:
                bids = _levels(json.loads(bids_json))
                asks = _levels(json.loads(asks_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not bids or not asks:
                continue
            books[str(key)] = CachedBook(
                bids=sorted(bids, key=lambda item: item[0], reverse=True),
                asks=sorted(asks, key=lambda item: item[0]),
                quote_ts_us=int(quote_ts_us),
            )
        return books

    def status(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*), MAX(quote_ts_us) FROM live_books").fetchone()
        count = int(row[0] or 0) if row else 0
        latest = int(row[1] or 0) if row else 0
        return {
            "status": "ok" if latest else "empty",
            "books": count,
            "latest_quote_ts_us": latest or None,
            "age_seconds": max(0.0, time.time() - latest / 1_000_000) if latest else None,
        }

    def prune(self, *, max_age_seconds: float = 3600.0) -> int:
        cutoff_us = int((time.time() - max_age_seconds) * 1_000_000)
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM live_books WHERE quote_ts_us < ?", (cutoff_us,)
            )
            self._conn.commit()
            return max(0, int(cursor.rowcount or 0))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def cache_key(venue: str, market_type: str, symbol: str) -> str:
    return "|".join((str(venue).strip(), str(market_type).strip(), str(symbol).strip()))


def load_live_book(
    venue: str,
    market_type: str,
    symbol: str,
    *,
    max_age_seconds: float = 5.0,
    path: Path | str = DEFAULT_PATH,
) -> CachedBook | None:
    if not Path(path).exists():
        return None
    global _DEFAULT_STORE
    with _DEFAULT_STORE_LOCK:
        if _DEFAULT_STORE is None or _DEFAULT_STORE.path != Path(path):
            _DEFAULT_STORE = LiveBookStore(path)
        store = _DEFAULT_STORE
    return store.get(
        venue,
        market_type,
        symbol,
        max_age_seconds=max_age_seconds,
    )


def _levels(value: Any) -> list[list[float]]:
    output: list[list[float]] = []
    for item in value or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            price = float(item[0])
            amount = float(item[1])
        except (TypeError, ValueError):
            continue
        if price > 0 and amount > 0:
            output.append([price, amount])
    return output
