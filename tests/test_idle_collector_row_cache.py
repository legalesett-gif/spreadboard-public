"""A TTL without a later reader cannot release idle collector memory."""

import pytest

from scripts import run_spreadboard_service as service


@pytest.mark.parametrize("role,expected", [("collector", 1), ("web", 2)])
def test_memory_watchdog_releases_only_expired_collector_rows(monkeypatch, role, expected):
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", role)
    api = service.api_spreads
    monkeypatch.setattr(api, "_ROW_CACHE_TTL_SECONDS", 180)
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    monkeypatch.setattr(api, "_ROW_CACHE", {
        "old": (819, [object()], {}), "fresh": (821, [object()], {}),
    })
    freed = []
    monkeypatch.setattr(service, "_return_freed_memory", lambda: freed.append(True))
    monkeypatch.setattr(service, "_heap_summary", lambda _: "")
    monkeypatch.setattr(service, "_container_pressure", lambda: "")
    monkeypatch.setattr(service, "_log", lambda _: None)

    class Once:
        calls = 0

        def wait(self, _):
            self.calls += 1
            return self.calls > 1

    service.MemoryWatchdog(Once()).run()
    assert len(api._ROW_CACHE) == expected
    assert "fresh" in api._ROW_CACHE
    assert len(freed) == (role == "collector")


@pytest.mark.parametrize("role,rss,expected", [("web", 2.8, 2), ("web", 1.8, 0), ("collector", 2.8, 0)])
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
    monkeypatch.setattr(service, "_return_freed_memory", lambda: trimmed.append(clock[0]))
    monkeypatch.setattr(service, "_log", lines.append)
    class Ticks:
        def wait(self, _):
            clock[0] += 20
            return clock[0] > 220
    service.MemoryWatchdog(Ticks()).run()
    assert len(trimmed) == expected
    if expected:
        assert trimmed == [20, 200]
        assert sum("allocator trim before=" in x and "after=" in x for x in lines) == expected
    assert api._ROW_CACHE is rows and "row" in rows
    assert api._LAST_GOOD_LIVE_BOOKS is books and "book" in books
