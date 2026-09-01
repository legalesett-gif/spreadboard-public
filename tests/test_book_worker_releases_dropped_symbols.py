"""Dropped subscriptions left their order books resident in the CCXT client.

`watch_order_book` stores each book in the client's own `orderbooks` cache. The
worker cancels the task when a leg leaves the desired set, but nothing purged
that cache, so every symbol ever watched stayed resident for the lifetime of
the client -- and the desired set is recomputed whenever a new generation
lands.

Production, 2026-09-01: the worker sat at 250-600MB for an hour, stepped to
1,236MB the moment a new generation landed and it resubscribed, then climbed
~22MB/min to 1,456MB, at which point the kernel OOM-killed it (rss 1,553,252kB)
and took the collector's work with it. It holds only 96 subscriptions, so ~15MB
per subscription was never the working set -- it was accumulation.
"""

from __future__ import annotations

import asyncio

from scripts import websocket_book_worker as worker


class _FakeClient:
    """Stands in for a CCXT Pro client's per-symbol caches."""

    def __init__(self) -> None:
        self.orderbooks: dict[str, object] = {}
        self.trades: dict[str, object] = {}
        self.ohlcvs: dict[str, object] = {}


def _worker_with(client: _FakeClient) -> worker.BookWorker:
    instance = worker.BookWorker.__new__(worker.BookWorker)
    instance.clients = {("Gate", "Futures"): client}
    instance.tasks = {}
    instance._unavailable = set()
    return instance


def test_a_dropped_leg_releases_its_cached_book() -> None:
    client = _FakeClient()
    client.orderbooks["AAA/USDT:USDT"] = ["book"] * 100
    client.orderbooks["BBB/USDT:USDT"] = ["book"] * 100

    instance = _worker_with(client)
    instance._release_client_caches(("Gate", "Futures", "AAA/USDT:USDT"))

    assert "AAA/USDT:USDT" not in client.orderbooks, "dropped leg kept its order book"
    assert "BBB/USDT:USDT" in client.orderbooks, "released a leg that is still watched"


def test_every_per_symbol_cache_is_released() -> None:
    """`orderbooks` is the large one, but the others accumulate identically."""

    client = _FakeClient()
    for cache in (client.orderbooks, client.trades, client.ohlcvs):
        cache["AAA/USDT:USDT"] = ["x"] * 10

    instance = _worker_with(client)
    instance._release_client_caches(("Gate", "Futures", "AAA/USDT:USDT"))

    assert client.orderbooks == {}
    assert client.trades == {}
    assert client.ohlcvs == {}


def test_an_unknown_client_or_cache_is_not_an_error() -> None:
    """A venue with no client yet, or a client without these attributes, must
    not take down the reconcile loop that is trying to free memory."""

    instance = _worker_with(_FakeClient())
    instance._release_client_caches(("Nonexistent", "Spot", "AAA/USDT:USDT"))

    class _Bare:
        pass

    instance.clients[("Bare", "Spot")] = _Bare()
    instance._release_client_caches(("Bare", "Spot", "AAA/USDT:USDT"))


def test_the_reconcile_loop_actually_releases(monkeypatch) -> None:
    """A helper nothing calls frees nothing.

    This exact class of mutant -- a correct helper wired to no call site -- has
    passed tests on this codebase before.
    """

    client = _FakeClient()
    client.orderbooks["GONE/USDT:USDT"] = ["book"] * 100
    client.orderbooks["KEPT/USDT:USDT"] = ["book"] * 100

    instance = _worker_with(client)
    gone = ("Gate", "Futures", "GONE/USDT:USDT")
    kept = ("Gate", "Futures", "KEPT/USDT:USDT")

    async def _drive() -> None:
        instance.stop = asyncio.Event()
        instance.store = None
        instance.tasks = {gone: asyncio.create_task(asyncio.sleep(60)),
                          kept: asyncio.create_task(asyncio.sleep(60))}

        async def _desired() -> set:
            instance.stop.set()
            return {kept}

        monkeypatch.setattr(instance, "_desired_legs_cached", _desired)
        monkeypatch.setattr(instance, "_prune_stale_books", lambda: True)
        monkeypatch.setattr(instance, "close", _noop)
        monkeypatch.setattr(instance, "_watch", lambda _k: asyncio.sleep(60))
        await instance.run()

    async def _noop() -> None:
        return None

    asyncio.run(_drive())

    assert "GONE/USDT:USDT" not in client.orderbooks, (
        "the reconcile loop cancelled the task but never released its book"
    )
    assert "KEPT/USDT:USDT" in client.orderbooks
