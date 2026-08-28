"""The host watchdog must page on real pressure, not on page cache.

A process killed by the OOM killer cannot alert through itself, which is why
none of the eight web-container kills on 2026-08-28 raised an owner alert. This
check therefore runs on the host, outside both cgroups.

Its first production run then produced a false positive: it reported
"memory 100.0% of cgroup limit" on a container with oom_kill=0, because
``memory.current`` includes reclaimable page cache that the kernel grows to
fill the limit by design. Pressure is measured on ``anon``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "container_health_watchdog",
    Path(__file__).resolve().parents[1] / "scripts" / "container_health_watchdog.py",
)
watchdog = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(watchdog)


def _observation(**overrides) -> dict:
    base = {
        "name": "app-app-1",
        "present": True,
        "status": "running",
        "restart_count": 0,
        "cgroup_oom_kill": 0,
        "memory_pct": 40.0,
    }
    base.update(overrides)
    return base


def _host(**overrides) -> dict:
    base = {"mem_available_kb": 3_000_000, "swap_total_kb": 2_097_148, "swap_free_kb": 1_900_000}
    base.update(overrides)
    return base


def test_a_full_page_cache_is_not_reported_as_memory_pressure() -> None:
    """The exact first-run false positive: anon 70%, memory.current 99.98%."""

    report = watchdog.evaluate(
        [_observation(memory_pct=70.35)],
        {"containers": {"app-app-1": {"restart_count": 0, "cgroup_oom_kill": 0}}},
        _host(),
    )

    assert report["status"] == "ok", (
        f"a healthy container was flagged: {report['detail']}"
    )
    assert not report["warnings"]


def test_a_restart_increase_is_a_fault() -> None:
    """This is the signal that went unseen while restarts climbed 6 -> 10."""

    report = watchdog.evaluate(
        [_observation(restart_count=10)],
        {"containers": {"app-app-1": {"restart_count": 9, "cgroup_oom_kill": 0}}},
        _host(),
    )

    assert report["status"] == "failed"
    assert "restarted (count 9 -> 10)" in report["detail"]


def test_an_oom_kill_increase_is_a_fault() -> None:
    report = watchdog.evaluate(
        [_observation(cgroup_oom_kill=5)],
        {"containers": {"app-app-1": {"restart_count": 0, "cgroup_oom_kill": 2}}},
        _host(),
    )

    assert report["status"] == "failed"
    assert "oom_kill 2 -> 5" in report["detail"]


def test_sustained_anon_pressure_warns_before_the_kernel_acts() -> None:
    report = watchdog.evaluate(
        [_observation(memory_pct=94.1)],
        {"containers": {"app-app-1": {"restart_count": 0, "cgroup_oom_kill": 0}}},
        _host(),
    )

    assert report["status"] == "warn"
    assert "anon memory 94.1%" in report["detail"]


def test_a_missing_container_is_a_fault() -> None:
    report = watchdog.evaluate(
        [{"name": "app-collector-1", "present": False}], {}, _host()
    )

    assert report["status"] == "failed"
    assert "app-collector-1 is not running" in report["detail"]


def test_a_first_run_with_no_previous_sample_still_reports_an_existing_oom() -> None:
    """A watchdog started after the damage must not report all-clear."""

    report = watchdog.evaluate([_observation(cgroup_oom_kill=3)], {}, _host())

    assert report["status"] == "failed"
    assert "oom_kill=3" in report["detail"]


def test_exhausted_host_swap_is_surfaced() -> None:
    report = watchdog.evaluate(
        [_observation()],
        {"containers": {"app-app-1": {"restart_count": 0, "cgroup_oom_kill": 0}}},
        _host(swap_free_kb=1000),
    )

    assert "host swap is effectively exhausted" in report["detail"]


@pytest.mark.parametrize(
    "value,expected",
    [("2026-08-28T19:38:08.917333659Z", 1787945888.917333), ("", None), ("nonsense", None)],
)
def test_docker_nanosecond_timestamps_parse(value: str, expected: float | None) -> None:
    result = watchdog._iso_to_unix(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=0.001)
