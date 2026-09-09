"""Nothing acted when a container went unhealthy, so the site stayed down.

`restart: unless-stopped` restarts on container EXIT, never on unhealthy, and
this watchdog observed without ever restarting -- `evaluate()` looked at
`status != running`, restart count, oom_kill and memory percent, so a container
that was `running` and `unhealthy` produced no fault at all. Its only output was
Pushover, now off by request, so the system has to heal itself rather than tell
anyone.

Observed: `app-app-1` unhealthy with `/api/health` at 13.6s against a 12s
healthcheck timeout, serving slowly for hours until the owner noticed.

The collector is deliberately harder to restart than the app: a restart destroys
a 45-60 minute discovery scan, so it must be unhealthy for much longer, and not
mid-scan, before that trade is worth making.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "container_health_watchdog",
    Path(__file__).resolve().parents[1] / "scripts" / "container_health_watchdog.py",
)
assert _SPEC and _SPEC.loader
watchdog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watchdog)


def _observation(name: str, health: str = "healthy", **over) -> dict:
    item = {
        "name": name,
        "container_id": "observed-container-id",
        "present": True,
        "status": "running",
        "health": health,
        "restart_count": 0,
        "cgroup_oom_kill": 0,
        "memory_pct": 40.0,
        "uptime_seconds": 7200.0,
    }
    item.update(over)
    return item


def _state(**over) -> dict:
    state = {"containers": {}}
    state.update(over)
    return state


def _run(observations, previous, *, scan_running=False, now=1000.0):
    restarts: list[str] = []
    report = watchdog.evaluate(observations, previous, {"mem_available_kb": 4_000_000})
    watchdog.remediate(
        report,
        previous,
        restart=lambda name: restarts.append(name) or True,
        scan_is_running=lambda: scan_running,
        now=now,
    )
    return report, restarts


def test_a_healthy_container_is_left_alone() -> None:
    _report, restarts = _run([_observation("app-app-1")], _state())

    assert restarts == []


def test_one_unhealthy_check_does_not_restart() -> None:
    """A single slow build must not bounce the site."""

    _report, restarts = _run([_observation("app-app-1", "unhealthy")], _state())

    assert restarts == []


def test_sustained_unhealthy_restarts_the_app() -> None:
    previous = _state(
        containers={"app-app-1": {"unhealthy_checks": watchdog.APP_UNHEALTHY_CHECKS - 1}}
    )

    report, restarts = _run([_observation("app-app-1", "unhealthy")], previous)

    assert restarts == ["app-app-1"]
    assert any("unhealthy" in fault for fault in report["faults"])


def test_the_collector_is_not_restarted_mid_scan() -> None:
    """A restart there destroys 45-60 minutes of discovery work."""

    previous = _state(
        containers={
            "app-collector-1": {"unhealthy_checks": watchdog.COLLECTOR_UNHEALTHY_CHECKS}
        }
    )

    _report, restarts = _run(
        [_observation("app-collector-1", "unhealthy")], previous, scan_running=True
    )

    assert restarts == []


def test_a_wedged_collector_is_restarted_even_mid_scan() -> None:
    """Past a point, the scan is not going to finish anyway."""

    previous = _state(
        containers={
            "app-collector-1": {"unhealthy_checks": watchdog.COLLECTOR_WEDGED_CHECKS}
        }
    )

    _report, restarts = _run(
        [_observation("app-collector-1", "unhealthy")], previous, scan_running=True
    )

    assert restarts == ["app-collector-1"]


def test_restarts_are_capped_per_hour() -> None:
    """A crash loop must be contained and visible, not hammered."""

    previous = _state(
        containers={
            "app-app-1": {
                "unhealthy_checks": watchdog.APP_UNHEALTHY_CHECKS,
                "restarts": [900.0, 800.0, 700.0],
            }
        }
    )

    _report, restarts = _run([_observation("app-app-1", "unhealthy")], previous, now=1000.0)

    assert restarts == []


def test_the_cap_forgets_restarts_older_than_an_hour() -> None:
    previous = _state(
        containers={
            "app-app-1": {
                "unhealthy_checks": watchdog.APP_UNHEALTHY_CHECKS,
                "restarts": [100.0, 200.0, 300.0],
            }
        }
    )

    _report, restarts = _run([_observation("app-app-1", "unhealthy")], previous, now=5000.0)

    assert restarts == ["app-app-1"]


def test_recovery_clears_the_unhealthy_counter() -> None:
    previous = _state(containers={"app-app-1": {"unhealthy_checks": 2}})

    report, _restarts = _run([_observation("app-app-1", "healthy")], previous)

    assert report["containers"]["app-app-1"]["unhealthy_checks"] == 0


@pytest.mark.parametrize("overrides", [
    {"status": "restarting"}, {"status": "exited"}, {"status": "paused"},
    {"uptime_seconds": 179}, {"uptime_seconds": None},
    {"uptime_seconds": 250, "start_period_seconds": 300},
    {"health": "starting"},
])
def test_docker_recovery_and_startup_are_left_alone(overrides) -> None:
    previous = _state(containers={"app-app-1": {"unhealthy_checks": 99}})
    observation = _observation("app-app-1", "unhealthy")
    observation.update(overrides)
    report, restarts = _run([observation], previous)
    assert restarts == []
    assert report["containers"]["app-app-1"]["unhealthy_checks"] == 0


def test_a_new_container_does_not_inherit_the_unhealthy_streak() -> None:
    previous = _state(containers={"app-app-1": {
        "unhealthy_checks": 99, "started_at_unix": 100,
    }})
    report, restarts = _run([
        _observation("app-app-1", "unhealthy", started_at_unix=200),
    ], previous)
    assert restarts == []
    assert report["containers"]["app-app-1"]["unhealthy_checks"] == 1


@pytest.mark.parametrize("successful", [True, False])
def test_main_persists_budget_before_docker_and_through_missing_inspect(
    tmp_path, monkeypatch, successful,
) -> None:
    calls = []
    missing = False
    monkeypatch.setattr(watchdog, "_host_memory", lambda: {"mem_available_kb": 4_000_000})
    monkeypatch.setattr(watchdog, "inspect_container", lambda name: (
        {"name": name, "present": False} if missing
        else _observation(name, "unhealthy")
    ))

    def restart(command, **kwargs):
        persisted = json.loads((tmp_path / watchdog.STATE_FILENAME).read_text())
        item = persisted["containers"]["app-app-1"]
        assert len(item["restart_attempts"]) == len(calls) + 1
        assert item["restart_result"] == "reserved"
        assert command == ["docker", "restart", "observed-container-id"]
        calls.append(command)
        if not successful:
            raise watchdog.subprocess.TimeoutExpired(command, 120)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(watchdog.subprocess, "run", restart)
    for check in range(18):
        # Losing inspect data used to discard all restart history.
        missing = check == 10
        assert watchdog.main(["--runtime-dir", str(tmp_path), "--container", "app-app-1"]) == 0
    state = json.loads((tmp_path / watchdog.STATE_FILENAME).read_text())
    assert len(calls) == 3
    assert state["containers"]["app-app-1"]["restart_suppressed"] == "cap_reached"
    ledger = [json.loads(line) for line in (tmp_path / watchdog.LEDGER_FILENAME).read_text().splitlines()]
    assert len(ledger) == 6


def test_inspect_requests_only_health_fields(monkeypatch):
    def run(command, **kwargs):
        assert "--format" in command
        template = command[command.index("--format") + 1]
        assert ".Config.Env" not in template and ".Config.Cmd" not in template
        assert "{{json .State}}" not in template and "{{json .Config}}" not in template
        assert ".State.Health.Status" in template and ".Config.Healthcheck.StartPeriod" in template
        return SimpleNamespace(returncode=0, stdout='[{"Id":"safe-id"}]')
    monkeypatch.setattr(watchdog.subprocess, "run", run)
    assert watchdog._docker_inspect("app-app-1") == {"Id": "safe-id"}


def test_the_accounting_worker_is_supervised() -> None:
    """Funding goes silently "unknown" if nothing restarts this worker.

    `app-accounting-worker-1` declares a healthcheck asserting its status file
    is under 900s old, and the portfolio treats a snapshot older than 900s as
    `stale` -- so a wedged worker blanks funding for every open position. But
    `restart: unless-stopped` fires on exit only, and the watchdog supervised
    just the app and the collector, so nothing acted on it.
    """

    assert "app-accounting-worker-1" in watchdog.DEFAULT_CONTAINERS


def test_a_stalled_accounting_worker_is_restarted() -> None:
    previous = _state(
        containers={
            "app-accounting-worker-1": {
                "unhealthy_checks": watchdog.APP_UNHEALTHY_CHECKS - 1
            }
        }
    )

    _report, restarts = _run(
        [_observation("app-accounting-worker-1", "unhealthy")], previous
    )

    assert restarts == ["app-accounting-worker-1"]


def test_the_accounting_worker_is_not_shielded_like_the_collector() -> None:
    """It holds no scan, so a running discovery scan must not protect it."""

    previous = _state(
        containers={
            "app-accounting-worker-1": {
                "unhealthy_checks": watchdog.APP_UNHEALTHY_CHECKS - 1
            }
        }
    )

    _report, restarts = _run(
        [_observation("app-accounting-worker-1", "unhealthy")],
        previous,
        scan_running=True,
    )

    assert restarts == ["app-accounting-worker-1"]
    assert watchdog._restart_threshold("app-accounting-worker-1") == (
        watchdog.APP_UNHEALTHY_CHECKS
    )
