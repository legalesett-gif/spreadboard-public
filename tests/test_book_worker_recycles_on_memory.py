"""The book worker must bound its own memory, because it cannot free it.

Its footprint is dominated by `_ensure_markets` loading each VENUE's full
market catalogue into the CCXT client -- so it scales with venues touched, not
with symbols held. Measured on production 2026-09-01: it reached ~1,300MB
within 20 minutes of a generation rotation at 96 subscriptions AND at 64, and
releasing per-symbol caches on unsubscribe did not lower that ceiling. It
OOM-killed the collector at 1,553MB, and later drove collector anon to 3,934MB
of a 4,096MB limit.

Nothing in the client can be released safely -- CCXT needs `markets`,
`markets_by_id`, `symbols` and `ids` to resolve a symbol at all -- so the only
sound bound is to recycle the process. `_ensure_websocket_worker` respawns it
whenever it exits, and every board row is quoted by the bulk/fast path
regardless, so a few seconds without websocket books blanks nothing.
"""

from __future__ import annotations

import asyncio

from scripts import websocket_book_worker as worker


def _instance() -> worker.BookWorker:
    instance = worker.BookWorker.__new__(worker.BookWorker)
    instance.stop = asyncio.Event()
    instance.clients = {}
    instance.tasks = {}
    instance._unavailable = set()
    return instance


def test_self_rss_is_readable_or_honestly_unknown() -> None:
    """None means "cannot tell" on this platform, never zero.

    Zero would read as "plenty of headroom" and disable the guard silently.
    """

    value = worker._self_rss_mb()
    assert value is None or value > 0


def test_the_loop_exits_once_the_ceiling_is_passed(monkeypatch) -> None:
    monkeypatch.setattr(worker, "RSS_LIMIT_MB", 900)
    monkeypatch.setattr(worker, "_self_rss_mb", lambda: 950)

    instance = _instance()
    assert instance._should_recycle() is True


def test_the_loop_keeps_running_below_the_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(worker, "RSS_LIMIT_MB", 900)
    monkeypatch.setattr(worker, "_self_rss_mb", lambda: 400)

    instance = _instance()
    assert instance._should_recycle() is False


def test_an_unreadable_rss_never_recycles(monkeypatch) -> None:
    """"We cannot tell" must not become a restart loop."""

    monkeypatch.setattr(worker, "RSS_LIMIT_MB", 900)
    monkeypatch.setattr(worker, "_self_rss_mb", lambda: None)

    instance = _instance()
    assert instance._should_recycle() is False


def test_a_zero_limit_disables_the_guard(monkeypatch) -> None:
    monkeypatch.setattr(worker, "RSS_LIMIT_MB", 0)
    monkeypatch.setattr(worker, "_self_rss_mb", lambda: 5000)

    instance = _instance()
    assert instance._should_recycle() is False


def test_the_reconcile_loop_actually_resets_on_the_ceiling(monkeypatch) -> None:
    """A guard the loop never consults bounds nothing."""

    monkeypatch.setattr(worker, "RSS_LIMIT_MB", 900)
    monkeypatch.setattr(worker, "_self_rss_mb", lambda: 5000)

    instance = _instance()
    reset: list[str] = []

    async def _drive() -> None:
        async def _desired() -> set:
            instance.stop.set()
            return set()

        async def _reset() -> None:
            reset.append("reset")

        async def _close() -> None:
            return None

        monkeypatch.setattr(instance, "_desired_legs_cached", _desired)
        monkeypatch.setattr(instance, "_prune_stale_books", lambda: True)
        monkeypatch.setattr(instance, "_reset_clients", _reset)
        monkeypatch.setattr(instance, "close", _close)
        await asyncio.wait_for(instance.run(), timeout=5)

    asyncio.run(_drive())

    assert reset == ["reset"], "the loop ran past its own memory ceiling"


def test_the_reset_frees_clients_and_market_state() -> None:
    """The point of the reset is the CCXT market catalogues, so they must go."""

    class _Client:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    instance = _instance()
    client = _Client()
    instance.clients = {("Gate", "Futures"): client}
    instance._markets_ready = {("Gate", "Futures")}
    instance._market_locks = {("Gate", "Futures"): asyncio.Lock()}

    asyncio.run(instance._reset_clients())

    assert client.closed is True
    assert instance.clients == {}
    assert instance._markets_ready == set()
    assert instance._market_locks == {}


def test_the_reset_returns_arenas_to_the_os(monkeypatch) -> None:
    """Freeing the objects is not freeing the memory.

    Each venue catalogue is many medium-sized dicts, which fragments the
    allocator: `gc.collect()` drops them while glibc keeps the arenas and RSS
    never falls. Production measured five resets returning 48MB, 9MB, 1MB, 0MB
    and 0MB -- a guard that fired correctly and bounded nothing.
    """

    calls: list[str] = []
    monkeypatch.setattr(worker.gc, "collect", lambda: calls.append("gc"))

    class _Libc:
        def malloc_trim(self, _arg):
            calls.append("malloc_trim")
            return 1

    monkeypatch.setattr(worker.ctypes, "CDLL", lambda _name: _Libc())

    instance = _instance()
    instance._markets_ready = set()
    instance._market_locks = {}
    asyncio.run(instance._reset_clients())

    assert calls == ["gc", "malloc_trim"], (
        f"reset performed {calls}; dropping references alone does not lower RSS"
    )
