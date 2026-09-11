"""Both roles must release idle rows only after their configured TTL."""

import pytest

from scripts import run_spreadboard_service as service


@pytest.mark.parametrize("role,ttl,rss", [("collector", 180, 0.2), ("web", 900, 0.2), ("web", 900, 2.8)])
def test_memory_watchdog_releases_only_expired_rows_at_role_ttl(monkeypatch, role, ttl, rss):
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", role)
    api = service.api_spreads
    monkeypatch.setattr(api, "_ROW_CACHE_TTL_SECONDS", ttl)
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    reader_rows = [object()]
    live_book = object()
    monkeypatch.setattr(api, "_ROW_CACHE", {
        "old": (1000-ttl, reader_rows, {}), "fresh": (1001-ttl, [object()], {}),
    })
    monkeypatch.setattr(api, "_LAST_GOOD_LIVE_BOOKS", {"book": live_book})
    freed = []
    monkeypatch.setattr(service, "_return_freed_memory", lambda: freed.append(True))
    monkeypatch.setattr(service, "_heap_summary", lambda _: "")
    monkeypatch.setattr(service, "_container_pressure", lambda: "")
    monkeypatch.setattr(service, "_rss_gb", lambda: rss)
    lines = []
    monkeypatch.setattr(service, "_log", lines.append)

    class Once:
        calls = 0

        def wait(self, _):
            self.calls += 1
            return self.calls > 1

    service.MemoryWatchdog(Once()).run()
    assert len(api._ROW_CACHE) == 1
    assert "fresh" in api._ROW_CACHE
    assert len(freed) == 1
    assert len(reader_rows) == 1  # Existing consumers retain their own reference.
    assert api._LAST_GOOD_LIVE_BOOKS["book"] is live_book
    assert "rows_expired=1" in lines[0]


@pytest.mark.parametrize("role,rss,expected", [("web", 2.8, 11), ("web", 2.3, 4), ("web", 1.8, 0), ("collector", 2.8, 0)])
def test_web_trim_is_bounded_and_preserves_live_caches(monkeypatch, role, rss, expected):
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", role)
    api = service.api_spreads
    rows = {"row": (1e15, [object()], {})}
    books = {"book": object()}
    monkeypatch.setattr(api, "_ROW_CACHE", rows)
    monkeypatch.setattr(api, "_LAST_GOOD_LIVE_BOOKS", books)
    monkeypatch.setattr(service, "_rss_gb", lambda: rss)
    monkeypatch.setattr(service, "_heap_summary", lambda _: "")
    monkeypatch.setattr(service, "_container_pressure", lambda: "")
    clock = [0.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    trimmed = []
    lines = []
    monkeypatch.setattr(service, "_return_freed_memory", lambda **kwargs: trimmed.append((clock[0], kwargs.get("collect"))))
    monkeypatch.setattr(service, "_log", lines.append)
    class Ticks:
        def wait(self, _):
            clock[0] += 20
            return clock[0] > 220
    service.MemoryWatchdog(Ticks()).run()
    assert len(trimmed) == expected
    if expected:
        if rss >= 2.5:
            assert trimmed == [(at, at in (20, 200)) for at in range(20, 221, 20)]
        else:
            assert trimmed == [(20, True), (80, False), (140, False), (200, True)]
        assert sum("allocator trim before=" in x and "after=" in x for x in lines) == expected
    assert api._ROW_CACHE is rows and "row" in rows
    assert api._LAST_GOOD_LIVE_BOOKS is books and "book" in books


def test_allocator_only_cleanup_does_not_run_gc(monkeypatch):
    calls = []
    class Allocator:
        def malloc_trim(self, value):
            calls.append(value)
    monkeypatch.setattr(service.ctypes, "CDLL", lambda _: Allocator())
    monkeypatch.setattr(service.gc, "collect", lambda: pytest.fail("allocator-only trim must not walk the heap"))
    monkeypatch.setattr(service, "_rss_gb", lambda: 2.3)
    result = service._return_freed_memory(measure=True, collect=False)
    assert calls == [0]
    assert result["gc_ran"] is False and result["gc_collected"] == 0
