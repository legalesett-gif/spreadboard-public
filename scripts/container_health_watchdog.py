#!/usr/bin/env python3
"""Host-side container/cgroup watchdog.

A process killed by the OOM killer cannot alert through itself. On 2026-08-28
the web container was OOM-restarted at 16:21, 16:48, 17:16, 17:54, 18:19, 18:49,
19:20 and 19:34 UTC, taking its restart count from 6 to 8, while every in-app
health check kept returning ``ok=true`` between restarts and no owner alert was
raised for a single one of them.

This runs on the host from a systemd timer, outside both application cgroups,
and writes a secret-free health artifact into the shared runtime directory. The
application containers read that artifact and deliver the owner-only Pushover
transition through the existing encrypted, opted-in path -- whichever container
is alive wins, because ``operator_alerts`` deduplicates on the shared state
file. Nothing here talks to Telegram and nothing here reads a secret.

Standard library only: this executes with the host ``/usr/bin/python3``, not the
application virtualenv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CONTAINERS = ("app-app-1", "app-collector-1")
DEFAULT_RUNTIME = Path("/opt/spreadboard/runtime")
HEALTH_FILENAME = "container_health.json"
STATE_FILENAME = "container_health_state.json"
#: Sustained use above this share of the cgroup limit is reported before the
#: kernel kills the process, so the owner sees pressure rather than only the
#: aftermath.
MEMORY_PRESSURE_PCT = 90.0
#: A recurring fault must not produce a notification per occurrence. The
#: collector OOM-kills its navigation child roughly every 20 minutes; the first
#: implementation reported "failed" on each kill and "ok" on the very next
#: 2-minute check, which sent 8 owner pushes in 84 minutes for ONE known
#: ongoing condition. An incident now stays open across recurrences and only
#: clears after a genuinely quiet period. The hold-down must exceed the typical
#: recurrence interval (the collector kill cycle is ~20 minutes) or the incident
#: simply closes and reopens between occurrences; 15 checks is 30 minutes at the
#: 2-minute timer cadence.
RECOVERY_QUIET_CHECKS = 15

#: Consecutive unhealthy checks before this watchdog restarts a container. The
#: timer runs every two minutes, so the app is given ~6 minutes of genuine
#: failure. Nothing else in the system acts on unhealthy: `restart:
#: unless-stopped` fires on EXIT only, so before this the site could stay
#: degraded indefinitely -- and did, for hours, until the owner reported it.
APP_UNHEALTHY_CHECKS = 3
#: The collector is dearer to restart: it destroys a 45-60 minute discovery
#: scan. Give it ~16 minutes, and only when no scan is running.
COLLECTOR_UNHEALTHY_CHECKS = 8
#: Past ~30 minutes unhealthy the scan is not going to finish anyway, so restart
#: regardless.
COLLECTOR_WEDGED_CHECKS = 15
#: A crash loop must be contained and visible rather than hammered.
MAX_RESTARTS_PER_HOUR = 3
RESTART_WINDOW_SECONDS = 3600.0


def _docker_inspect(container: str) -> dict[str, Any] | None:
    try:
        raw = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if raw.returncode != 0:
        return None
    try:
        payload = json.loads(raw.stdout)
    except json.JSONDecodeError:
        return None
    return payload[0] if isinstance(payload, list) and payload else None


def _cgroup_base(container_id: str) -> Path | None:
    for candidate in (
        Path(f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope"),
        Path(f"/sys/fs/cgroup/docker/{container_id}"),
    ):
        if candidate.is_dir():
            return candidate
    return None


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _memory_stat(base: Path) -> dict[str, int]:
    """Anonymous memory is the part that cannot be reclaimed under pressure.

    ``memory.current`` includes page cache, which the kernel deliberately grows
    to fill the cgroup limit and drops on demand. Using it as the pressure
    signal reported 100% on a perfectly healthy container.
    """

    stats: dict[str, int] = {}
    try:
        for line in (base / "memory.stat").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    stats[parts[0]] = int(parts[1])
                except ValueError:
                    continue
    except OSError:
        return {}
    return stats


def _memory_events(base: Path) -> dict[str, int]:
    events: dict[str, int] = {}
    try:
        for line in (base / "memory.events").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    events[parts[0]] = int(parts[1])
                except ValueError:
                    continue
    except OSError:
        return {}
    return events


def _host_memory() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                try:
                    values[key] = int(parts[0])
                except ValueError:
                    continue
    except OSError:
        return {}
    return {
        "mem_available_kb": values.get("MemAvailable", 0),
        "swap_total_kb": values.get("SwapTotal", 0),
        "swap_free_kb": values.get("SwapFree", 0),
    }


def _iso_to_unix(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    # Docker reports nanoseconds; fromisoformat accepts at most microseconds.
    text = re.sub(r"\.(\d{6})\d*", r".\1", text)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def inspect_container(name: str) -> dict[str, Any]:
    """Secret-free health facts for one container."""

    info = _docker_inspect(name)
    if info is None:
        return {"name": name, "present": False}
    state = info.get("State") if isinstance(info.get("State"), dict) else {}
    container_id = str(info.get("Id") or "")
    base = _cgroup_base(container_id) if container_id else None
    events = _memory_events(base) if base else {}
    stats = _memory_stat(base) if base else {}
    current = _read_int(base / "memory.current") if base else None
    anon = stats.get("anon")
    limit = _read_int(base / "memory.max") if base else None
    host_config = info.get("HostConfig") if isinstance(info.get("HostConfig"), dict) else {}
    if not limit or limit <= 0:
        limit = int(host_config.get("Memory") or 0) or None
    started_at = _iso_to_unix(str(state.get("StartedAt") or ""))
    health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
    return {
        "name": name,
        "present": True,
        "status": str(state.get("Status") or "unknown"),
        "health": str(health.get("Status") or "none"),
        "restart_count": int(info.get("RestartCount") or 0),
        "oom_killed": bool(state.get("OOMKilled")),
        "started_at_unix": started_at,
        "uptime_seconds": (time.time() - started_at) if started_at else None,
        "cgroup_oom": int(events.get("oom") or 0),
        "cgroup_oom_kill": int(events.get("oom_kill") or 0),
        "memory_current_bytes": current,
        "memory_anon_bytes": anon,
        "memory_limit_bytes": limit,
        # Pressure is measured on anon, not memory.current: page cache fills the
        # limit by design and is reclaimed rather than causing an OOM kill.
        "memory_pct": (
            round(100.0 * anon / limit, 2) if anon and limit else None
        ),
        "memory_current_pct": (
            round(100.0 * current / limit, 2) if current and limit else None
        ),
    }


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        # This runs as root on the host while the containers read it as an
        # unprivileged user. mkstemp creates 0600, which made the artifact
        # unreadable to the very processes that must relay it. The payload is
        # deliberately secret-free.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def evaluate(
    observations: list[dict[str, Any]],
    previous: dict[str, Any],
    host: dict[str, Any],
) -> dict[str, Any]:
    """Compare against the previous sample and describe what changed."""

    faults: list[str] = []
    warnings: list[str] = []
    previous_containers = previous.get("containers")
    previous_containers = previous_containers if isinstance(previous_containers, dict) else {}

    for item in observations:
        name = str(item.get("name"))
        if not item.get("present"):
            faults.append(f"{name} is not running")
            continue
        before = previous_containers.get(name)
        before = before if isinstance(before, dict) else {}
        restarts = int(item.get("restart_count") or 0)
        was_restarts = before.get("restart_count")
        if isinstance(was_restarts, int) and restarts > was_restarts:
            faults.append(
                f"{name} restarted (count {was_restarts} -> {restarts})"
            )
        oom_kills = int(item.get("cgroup_oom_kill") or 0)
        was_kills = before.get("cgroup_oom_kill")
        if isinstance(was_kills, int) and oom_kills > was_kills:
            faults.append(f"{name} cgroup oom_kill {was_kills} -> {oom_kills}")
        elif not isinstance(was_kills, int) and oom_kills > 0:
            faults.append(f"{name} cgroup oom_kill={oom_kills}")
        pct = item.get("memory_pct")
        if isinstance(pct, (int, float)) and pct >= MEMORY_PRESSURE_PCT:
            warnings.append(f"{name} anon memory {pct:.1f}% of cgroup limit")
        if str(item.get("status")) != "running":
            faults.append(f"{name} status={item.get('status')}")
        # A container can be `running` and `unhealthy` for hours. That produced
        # no fault at all before, which is how the site stayed down.
        streak = int(before.get("unhealthy_checks") or 0)
        streak = streak + 1 if str(item.get("health")) == "unhealthy" else 0
        item["unhealthy_checks"] = streak
        item["restarts"] = list(before.get("restarts") or [])
        if streak >= _restart_threshold(name):
            faults.append(f"{name} unhealthy for {streak} checks")

    swap_total = int(host.get("swap_total_kb") or 0)
    swap_free = int(host.get("swap_free_kb") or 0)
    if swap_total and swap_free <= swap_total * 0.05:
        warnings.append("host swap is effectively exhausted")
    if int(host.get("mem_available_kb") or 0) and host["mem_available_kb"] < 512 * 1024:
        warnings.append("host has under 512MB available")

    # Hold an open incident across recurrences instead of flapping. Without
    # this, a fault that repeats every 20 minutes alternates failed/ok on a
    # 2-minute cadence and notifies the owner on every edge.
    was_open = bool(previous.get("incident_open"))
    quiet = int(previous.get("quiet_checks") or 0)
    if faults:
        incident_open = True
        quiet = 0
    elif was_open:
        quiet += 1
        # Stay open until the condition has been absent long enough to mean it.
        incident_open = quiet < RECOVERY_QUIET_CHECKS
    else:
        incident_open = False

    if faults:
        status = "failed"
        detail = "; ".join(faults + warnings)
    elif incident_open:
        # Still inside the hold-down: report it, but do not claim recovery and
        # do not open a second incident.
        status = "failed"
        remaining = RECOVERY_QUIET_CHECKS - quiet
        detail = (
            f"{previous.get('open_detail') or 'Container fault'} "
            f"(no recurrence for {quiet} checks; confirming recovery in {remaining})"
        )
    elif warnings:
        status = "warn"
        detail = "; ".join(warnings)
    else:
        status = "ok"
        detail = "All containers stable; no restart or OOM since the previous check."

    return {
        "schema": "spreadboard.container_health.v1",
        "checked_at_unix": time.time(),
        "status": status,
        "detail": detail[:480],
        "faults": faults,
        "warnings": warnings,
        "incident_open": incident_open,
        "quiet_checks": quiet,
        "open_detail": ("; ".join(faults) if faults else previous.get("open_detail"))
        if incident_open
        else None,
        "containers": {str(item.get("name")): item for item in observations},
        "host": host,
    }


def _restart_threshold(name: str) -> int:
    return COLLECTOR_UNHEALTHY_CHECKS if "collector" in name else APP_UNHEALTHY_CHECKS


def _docker_restart(name: str) -> bool:
    try:
        subprocess.run(
            ["docker", "restart", name],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _discovery_scan_running() -> bool:
    try:
        result = subprocess.run(
            ["docker", "top", "app-collector-1"],
            check=False,
            capture_output=True,
            timeout=30,
            text=True,
        )
        return "api_discovery_worker" in (result.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def remediate(
    report: dict[str, Any],
    previous: dict[str, Any],
    *,
    restart: Callable[[str], bool] = _docker_restart,
    scan_is_running: Callable[[], bool] = _discovery_scan_running,
    now: float | None = None,
) -> list[str]:
    """Restart what has been unhealthy long enough, within a per-hour cap.

    Nothing else in this system acts on `unhealthy`, and with Pushover switched
    off there is nobody to tell, so recovery has to happen here. Every restart
    is recorded in the state file, which is the audit trail.
    """

    moment = time.time() if now is None else now
    restarted: list[str] = []
    for name, item in (report.get("containers") or {}).items():
        if not isinstance(item, dict) or str(item.get("health")) != "unhealthy":
            continue
        streak = int(item.get("unhealthy_checks") or 0)
        threshold = _restart_threshold(name)
        if streak < threshold:
            continue
        if "collector" in name and streak < COLLECTOR_WEDGED_CHECKS and scan_is_running():
            # A restart here throws away 45-60 minutes of scan. Only worth it
            # once the container is wedged rather than merely slow.
            continue
        recent = [
            stamp
            for stamp in (item.get("restarts") or [])
            if moment - float(stamp) < RESTART_WINDOW_SECONDS
        ]
        if len(recent) >= MAX_RESTARTS_PER_HOUR:
            item["restart_suppressed"] = "cap_reached"
            continue
        if restart(name):
            recent.append(moment)
            item["restarts"] = recent
            item["unhealthy_checks"] = 0
            restarted.append(name)
    return restarted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME))
    parser.add_argument("--container", action="append", default=None)
    parser.add_argument(
        "--print", action="store_true", help="print the report without writing state"
    )
    args = parser.parse_args(argv)

    runtime = Path(args.runtime_dir)
    containers = tuple(args.container or DEFAULT_CONTAINERS)
    observations = [inspect_container(name) for name in containers]
    host = _host_memory()
    state_path = runtime / STATE_FILENAME
    previous = _load_state(state_path)
    report = evaluate(observations, previous, host)
    if not args.print:
        restarted = remediate(report, previous)
        if restarted:
            report["detail"] = (
                f"restarted {', '.join(restarted)}; {report['detail']}"
            )[:480]

    if args.print:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    _atomic_write(runtime / HEALTH_FILENAME, report)
    _atomic_write(
        state_path,
        {
            "checked_at_unix": report["checked_at_unix"],
            "containers": report["containers"],
            "incident_open": report["incident_open"],
            "quiet_checks": report["quiet_checks"],
            "open_detail": report["open_detail"],
        },
    )
    print(f"container health status={report['status']} detail={report['detail']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
