"""What the websocket worker subscribes to must be what the board displays.

The worker used to rank the raw snapshot by spread and stream the top legs.
That set is close to the opposite of the board's: the widest raw numbers are
the dislocated rows the board filters out, so the worker held hundreds of
subscriptions while only a third of the routes on screen had a live price.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.websocket_book_worker import _board_leg_key, _desired_legs


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


def test_board_leg_key_matches_the_field_the_board_looks_books_up_by() -> None:
    route = _row("BTC", spread=1.0)
    # A route_inputs symbol must not win: live_prices_for keys on market_symbol,
    # so a book stored under anything else is invisible to the board.
    route["notes"] = {"route_inputs": {"long": {"symbol": "BTC-PERP-DIFFERENT"}}}

    assert _board_leg_key(route, "long") == ("Binance", "Futures", "BTC/USDT:USDT")


def test_board_leg_key_rejects_unsupported_venues_and_types() -> None:
    assert _board_leg_key({**_row("BTC", spread=1.0), "long_venue": "NoSuchVenue"}, "long") is None
    assert _board_leg_key({**_row("BTC", spread=1.0), "long_market_type": "Option"}, "long") is None
    assert _board_leg_key({**_row("BTC", spread=1.0), "long_market_symbol": ""}, "long") is None


def test_subscriptions_follow_the_board_not_the_widest_raw_spread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _row("SHOWN", spread=2.0)
    # A far wider row that the board filters out and never renders.
    hidden = _row("HIDDEN", spread=900.0, long_venue="Mexc")

    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text(
        json.dumps({"api_discovered_rows": [shown, hidden], "dex_discovered_rows": []}),
        encoding="utf-8",
    )

    from spreadboard import api_spreads

    monkeypatch.setattr(
        api_spreads,
        "load_spreads",
        lambda **_kwargs: {"groups": [{"routes": [shown]}], "rows": [shown]},
    )

    legs = _desired_legs(snapshot, limit=2)

    assert legs == {
        ("Binance", "Futures", "SHOWN/USDT:USDT"),
        ("Bybit", "Futures", "SHOWN/USDT:USDT"),
    }
    assert ("Mexc", "Futures", "HIDDEN/USDT:USDT") not in legs


def test_leftover_budget_still_covers_the_widest_unshown_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _row("SHOWN", spread=2.0)
    other = _row("OTHER", spread=50.0, long_venue="Mexc")

    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text(
        json.dumps({"api_discovered_rows": [shown, other], "dex_discovered_rows": []}),
        encoding="utf-8",
    )

    from spreadboard import api_spreads

    monkeypatch.setattr(
        api_spreads,
        "load_spreads",
        lambda **_kwargs: {"groups": [{"routes": [shown]}], "rows": [shown]},
    )

    legs = _desired_legs(snapshot, limit=8)

    # The board's legs come first, then the budget spills onto the next widest.
    assert ("Binance", "Futures", "SHOWN/USDT:USDT") in legs
    assert ("Mexc", "Futures", "OTHER/USDT:USDT") in legs


@pytest.mark.parametrize(
    "error",
    [
        'AuthenticationError:coinbaseinternational requires "apiKey" credential',
        "BadRequest:The coin pair does not currently offer",
    ],
)
def test_permanent_subscribe_failures_are_dropped_not_retried_forever(error: str) -> None:
    """Coinbase International will not serve public books without an API key.

    Retrying it is guaranteed to fail, and on a two-core box the reconnect loop
    competed for the CPU the streaming legs needed. It reconnected every few
    seconds and filled the log for hours.
    """
    import asyncio

    import ccxt.pro as ccxtpro

    from scripts.websocket_book_worker import BookWorker

    worker = BookWorker.__new__(BookWorker)
    worker.stop = asyncio.Event()
    worker.clients = {}
    worker._markets_ready = set()
    worker._market_locks = {}
    worker._unavailable = set()

    kind, _, message = error.partition(":")
    key = ("Coinbase International", "Futures", "PEPE/USDC:USDC")

    def _client(_venue: str, _market_type: str):
        raise getattr(ccxtpro, kind)(message)

    worker._client = _client

    # Returns rather than looping, and records the leg so the reconcile that
    # runs every ten seconds does not immediately resubscribe it.
    asyncio.run(asyncio.wait_for(worker._watch(key), timeout=5))

    assert key in worker._unavailable


def test_every_lane_gets_its_top_routes_before_any_lane_gets_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member on any tab expects the prices in front of them to move.

    Walking the lanes in order let the default view spend the whole budget, so
    the tail lanes -- Futures-DEX especially -- streamed nothing at all.
    """
    from scripts import websocket_book_worker as worker

    def lane_rows(prefix: str, count: int) -> list[dict]:
        return [_row(f"{prefix}{i}", spread=float(count - i)) for i in range(count)]

    # The first lane alone could consume the entire budget.
    lanes = {
        "default": lane_rows("A", 40),
        "dex": lane_rows("Z", 40),
    }

    def fake_load(**kwargs):
        rows = lanes["dex"] if kwargs.get("kind") == "DEX-SPOT" else lanes["default"]
        return {"groups": [{"routes": rows}], "rows": rows}

    from spreadboard import api_spreads

    monkeypatch.setattr(api_spreads, "load_spreads", fake_load)
    monkeypatch.setattr(worker, "BOARD_LANES", ({}, {"kind": "DEX-SPOT"}))
    monkeypatch.setattr(worker, "LANE_RESERVED_ROUTES", 3)

    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text(json.dumps({"api_discovered_rows": [], "dex_discovered_rows": []}), encoding="utf-8")

    legs = worker._board_legs(snapshot, limit=12)

    # Both lanes are represented, not just the one that came first.
    assert any(symbol.startswith("A") for _v, _t, symbol in legs)
    assert any(symbol.startswith("Z") for _v, _t, symbol in legs)


def test_funding_farm_tabs_are_lanes_the_worker_streams() -> None:
    """The three funding tabs use their own kinds; each must be subscribed."""
    from scripts.websocket_book_worker import BOARD_LANES

    funding_kinds = {
        lane.get("kind") for lane in BOARD_LANES if lane.get("funding_only")
    }
    assert {"FUTURES", "FUTURES-SPOT-PAIR", "DEX-FUTURES"} <= funding_kinds


def test_a_venue_is_watched_in_chunks_not_one_task_per_symbol() -> None:
    """One asyncio task per leg is what capped the feed at a few hundred.

    Every symbol paid for its own connection and its own reconnect loop.
    Nineteen of the twenty-one venues accept a list of symbols.
    """
    from scripts.websocket_book_worker import _chunks

    symbols = [f"T{i}/USDT" for i in range(130)]

    chunks = _chunks(symbols, 60)

    assert [len(c) for c in chunks] == [60, 60, 10]
    assert sum(len(c) for c in chunks) == len(symbols)


def test_venue_mode_prefers_books_then_tickers() -> None:
    """A ticker carries the bid and the ask, which is what a spread is made of,
    so a venue without batched books is still worth batching."""
    from scripts.websocket_book_worker import _venue_mode

    class _C:
        def __init__(self, has):
            self.has = has

    assert _venue_mode(_C({"watchOrderBookForSymbols": True, "watchTickers": True})) == "books"
    assert _venue_mode(_C({"watchTickers": True})) == "tickers"
    assert _venue_mode(_C({})) == "single"


def test_a_ticker_is_stored_as_a_one_level_book() -> None:
    from scripts.websocket_book_worker import BookWorker

    worker = BookWorker.__new__(BookWorker)
    worker._last_write = {}
    stored: dict = {}

    class _Store:
        def put(self, venue, market_type, symbol, *, bids, asks, quote_ts_us):
            stored.update(
                venue=venue, symbol=symbol, bids=bids, asks=asks, ts=quote_ts_us
            )

    worker.store = _Store()
    worker._store_ticker(
        "Gate", "Spot", "T/USDT",
        {"bid": 1.0, "ask": 1.02, "bidVolume": 5.0, "askVolume": 7.0, "timestamp": 1785000000000},
    )

    assert stored["bids"] == [[1.0, 5.0]]
    assert stored["asks"] == [[1.02, 7.0]]
    assert stored["ts"] == 1785000000000 * 1000


def test_a_ticker_without_both_sides_is_not_stored() -> None:
    """A one-sided quote cannot price a spread."""
    from scripts.websocket_book_worker import BookWorker

    worker = BookWorker.__new__(BookWorker)
    worker._last_write = {}
    calls: list = []

    class _Store:
        def put(self, *a, **k):
            calls.append(1)

    worker.store = _Store()
    worker._store_ticker("Gate", "Spot", "T/USDT", {"bid": 1.0, "ask": None})
    worker._store_ticker("Gate", "Spot", "T/USDT", {"bid": 0, "ask": 1.0})

    assert calls == []


def test_a_venue_with_a_lower_cap_gets_smaller_chunks() -> None:
    """BitMart rejects anything over twenty symbols in one request."""
    from scripts.websocket_book_worker import _chunk_size_for

    assert _chunk_size_for("BitMart") <= 20
    assert _chunk_size_for("Coinbase International") <= 10


def test_a_market_type_without_batching_is_listed() -> None:
    """Mexc offers watchTickers for swaps but not spot, and repeated the same
    refusal seventy times in eight minutes while streaming nothing."""
    from scripts.websocket_book_worker import NO_BATCH

    assert ("Mexc", "Spot") in NO_BATCH


def test_writes_are_throttled_per_symbol() -> None:
    """The per-leg loop throttled writes; the batched one lost it.

    Every update then hit SQLite and they serialised -- 2,271 books with a
    median age of 550s and only 488 fresh.
    """
    from scripts.websocket_book_worker import BookWorker

    worker = BookWorker.__new__(BookWorker)
    worker._last_write = {}
    key = ("Gate", "Spot", "T/USDT")

    assert worker._should_write(key) is True
    assert worker._should_write(key) is False, "a second write inside the window must be skipped"


def test_throttling_is_per_symbol_not_global() -> None:
    from scripts.websocket_book_worker import BookWorker

    worker = BookWorker.__new__(BookWorker)
    worker._last_write = {}

    assert worker._should_write(("Gate", "Spot", "A/USDT")) is True
    assert worker._should_write(("Gate", "Spot", "B/USDT")) is True
