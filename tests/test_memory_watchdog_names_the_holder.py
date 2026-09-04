"""When the process is large, the watchdog must say what it is holding.

`MemoryWatchdog` logged three cache sizes and a thread count. On the collector
those read `market=0 result=0 tick=0` at 1.54GB, so the line proved only that
the memory was somewhere else. The supervisor oscillates 0.23GB to 1.54GB, its
heavy work all runs in child processes, and nothing on the outside can say what
the growth is -- which left the question needing yet another investigation each
time instead of answering itself.

Object counts by type are cheap enough to take only when already large, and
they separate the cases that matter: millions of small dicts is a retained
structure, a handful of huge bytes is a serialisation buffer.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "run_spreadboard_service",
    Path(__file__).resolve().parents[1] / "scripts" / "run_spreadboard_service.py",
)
assert _SPEC and _SPEC.loader
service = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(service)


def test_a_small_process_is_not_walked() -> None:
    """The walk is only worth its cost when the number is already bad."""

    assert service._heap_summary(rss_gb=0.3) == ""


def test_a_large_process_reports_what_it_holds() -> None:
    summary = service._heap_summary(rss_gb=9.9)

    assert "dict" in summary or "tuple" in summary or "list" in summary
    assert "=" in summary


def test_the_threshold_is_configurable(monkeypatch) -> None:
    monkeypatch.setattr(service, "HEAP_SUMMARY_MIN_GB", 0.0)

    assert service._heap_summary(rss_gb=0.1) != ""


def test_the_walk_never_raises(monkeypatch) -> None:
    """Observation must not be able to take the service down."""

    monkeypatch.setattr(service.gc, "get_objects", lambda: (_ for _ in ()).throw(RuntimeError))

    assert service._heap_summary(rss_gb=9.9) == ""


def test_the_watchdog_actually_logs_it(monkeypatch) -> None:
    """A summary nobody logs answers nothing. This is the whole point."""

    lines: list[str] = []
    monkeypatch.setattr(service, "_log", lines.append)
    monkeypatch.setattr(service, "_rss_gb", lambda: 9.9)

    class _OnceEvent:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

    service.MemoryWatchdog(_OnceEvent()).run()

    assert len(lines) == 1
    assert "heap[" in lines[0], lines[0]


def test_a_small_process_logs_no_heap_section(monkeypatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(service, "_log", lines.append)
    monkeypatch.setattr(service, "_rss_gb", lambda: 0.2)

    class _OnceEvent:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

    service.MemoryWatchdog(_OnceEvent()).run()

    assert lines and "heap[" not in lines[0]


def test_the_watchdog_counts_the_caches_that_actually_hold_rows(monkeypatch) -> None:
    """`market=0 result=0 tick=0` was reported beside 1.45GB of retained rows.

    The heap walk named them: 31,831 SpreadTerminalRow and 26,058 CachedBook,
    stable across every sample. `_ROW_CACHE` (900s TTL) and
    `_LAST_GOOD_LIVE_BOOKS` hold exactly those, and neither was in the line, so
    the counters said the memory was somewhere else while sitting on top of it.
    """

    lines: list[str] = []
    monkeypatch.setattr(service, "_log", lines.append)
    monkeypatch.setattr(service, "_rss_gb", lambda: 0.2)
    monkeypatch.setattr(service, "_container_pressure", lambda **_kwargs: "")
    monkeypatch.setattr(service.api_spreads, "_ROW_CACHE", {"a": 1, "b": 2})
    monkeypatch.setattr(
        service.api_spreads, "_LAST_GOOD_LIVE_BOOKS", dict.fromkeys(range(7))
    )

    class _OnceEvent:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

    service.MemoryWatchdog(_OnceEvent()).run()

    assert "rows=2" in lines[0], lines[0]
    assert "books=7" in lines[0], lines[0]
