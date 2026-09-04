"""The container hits its cap as a SUM; one process's heap cannot show that.

The collector's 4011MB peak was made of five processes, none pathological:

    1453MB run_spreadboard_service   802MB bulk_quote_worker
     676MB websocket_book_worker     630MB api_discovery_worker
     284MB token_ranking_worker

`_heap_summary` reports the supervisor's own objects, so it can never see the
other four -- they are separate processes. Three OOM kills happened after the
overlap fix while every sampler was pointed at the wrong level.

The container can read `/sys/fs/cgroup/memory.current` and `memory.max`, and
every child's `VmRSS` from `/proc`. So the composition at the moment of pressure
is recordable from inside, automatically, instead of being sampled from outside
and missed.
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


def _cgroup(tmp_path: Path, current: int, maximum: int | str) -> Path:
    (tmp_path / "memory.current").write_text(str(current), encoding="utf-8")
    (tmp_path / "memory.max").write_text(str(maximum), encoding="utf-8")
    return tmp_path


def _procfs(tmp_path: Path, processes: dict[str, int]) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    for index, (command, rss_kb) in enumerate(processes.items(), start=100):
        entry = root / str(index)
        entry.mkdir()
        (entry / "status").write_text(f"Name:\tpython\nVmRSS:\t{rss_kb} kB\n", encoding="utf-8")
        (entry / "cmdline").write_bytes(command.encode() + b"\0")
    (root / "notapid").mkdir()
    return root


def test_a_calm_container_reports_nothing(tmp_path) -> None:
    summary = service._container_pressure(
        cgroup=_cgroup(tmp_path, 1_000_000_000, 4_294_967_296),
        procfs=_procfs(tmp_path, {"python worker.py": 500_000}),
    )

    assert summary == ""


def test_a_pressed_container_names_its_processes(tmp_path) -> None:
    summary = service._container_pressure(
        cgroup=_cgroup(tmp_path, 4_000_000_000, 4_294_967_296),
        procfs=_procfs(
            tmp_path,
            {
                "python scripts/bulk_quote_worker.py --venues a": 802_000,
                "python scripts/run_spreadboard_service.py": 1_453_000,
                "python scripts/websocket_book_worker.py": 676_000,
            },
        ),
    )

    assert "pressure[3814/4096MB" in summary
    # Largest first: the point is to see what took the room.
    assert summary.index("run_spreadboard_service") < summary.index("bulk_quote_worker")
    assert "websocket_book_worker" in summary


def test_an_unlimited_cgroup_is_not_pressure(tmp_path) -> None:
    """`memory.max` reads "max" when no limit is set.

    Documents intent rather than pinning it: dropping the explicit check leaves
    `int("max")` raising into the same guard, so both spellings are silent here.
    The check stays because control flow through an exception is worse, not
    because a mutant distinguishes them.
    """

    summary = service._container_pressure(
        cgroup=_cgroup(tmp_path, 4_000_000_000, "max"),
        procfs=_procfs(tmp_path, {"python worker.py": 500_000}),
    )

    assert summary == ""


def test_a_missing_cgroup_is_silent(tmp_path) -> None:
    """Not every host exposes this; observation must not break the service."""

    summary = service._container_pressure(
        cgroup=tmp_path / "absent", procfs=_procfs(tmp_path, {"python w.py": 1})
    )

    assert summary == ""


def test_the_watchdog_logs_it(monkeypatch, tmp_path) -> None:
    lines: list[str] = []
    monkeypatch.setattr(service, "_log", lines.append)
    monkeypatch.setattr(service, "_rss_gb", lambda: 0.2)
    monkeypatch.setattr(
        service,
        "_container_pressure",
        lambda **_kwargs: " pressure[4000/4096MB a=1MB]",
    )

    class _OnceEvent:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

    service.MemoryWatchdog(_OnceEvent()).run()

    assert lines and "pressure[" in lines[0]
