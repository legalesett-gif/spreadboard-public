#!/usr/bin/env python3
"""Record bounded, secret-free production evidence without depending on a Mac.

Observation only: no restart, notification, trading or database mutation.
Each container ID is recorded so recreation cannot hide a reset OOM counter.
HTTP probes run separately from the 15-second Docker health observations.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import container_health_watchdog as watchdog

PUBLIC_ORIGIN = "https://spreadarbitrage.ink"


def endpoint_sample(path: str) -> dict:
    started = time.time()
    result = {"kind": "endpoint", "path": path, "requested_at": started}
    try:
        with urllib.request.urlopen(PUBLIC_ORIGIN + path, timeout=45) as response:
            result["code"] = response.status
            if path == "/api/health":
                payload = json.load(response)
                universe = ((payload.get("source_health") or {}).get("materialized_views") or {}).get("live_query_universe") or {}
                result["universe"] = {key: universe.get(key) for key in (
                    "ready", "route_count", "current_priced_route_count",
                    "current_priced_token_count", "funding_only_route_count",
                    "current_priced_route_kind_counts", "generation", "refresh_seconds",
                )}
                funding = payload.get("funding_history") or {}
                result["funding_history"] = {key: funding.get(key) for key in (
                    "catalog_leg_count", "window_leg_counts", "window_coverage_pct",
                    "overdue_window_leg_counts", "retryable_error_leg_count",
                    "deep_history_pending_leg_count", "current_window_catch_up_complete",
                )}
    except urllib.error.HTTPError as exc:
        result["code"] = exc.code
    except Exception as exc:  # noqa: BLE001 - keep collecting evidence after probe failures.
        # Exception text can include a URL. Only the type belongs in evidence.
        result["error"] = type(exc).__name__
    result["at"] = time.time()
    result["seconds"] = round(result["at"] - started, 3)
    return result


def process_sample(container: str) -> list[dict]:
    result = subprocess.run(
        ["docker", "top", container, "-eo", "pid"], capture_output=True,
        text=True, check=False, timeout=10,
    )
    processes = []
    for line in result.stdout.splitlines()[1:]:
        pid = line.strip()
        if not pid.isdigit():
            continue
        try:
            proc = Path("/proc") / pid
            parts = (proc / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
            # Never save argv: it can contain credentials. Only a script's
            # basename and kernel memory counters are recorded.
            script = next((Path(p).name for p in parts if p.endswith(".py")), "other")
            status = {}
            for entry in (proc / "status").read_text().splitlines():
                key, _, value = entry.partition(":")
                if key in {"VmRSS", "VmHWM"}:
                    status[key] = int(value.split()[0])
            processes.append({"pid": int(pid), "script": script, **status})
        except (OSError, ValueError):
            continue
    return processes


def host_sample() -> dict:
    containers = []
    for name in watchdog.DEFAULT_CONTAINERS:
        item = watchdog.inspect_container(name)
        base = watchdog._cgroup_base(str(item.get("container_id") or ""))
        if base:
            item["memory_peak_bytes"] = watchdog._read_int(base / "memory.peak")
            try:
                item["cpu"] = {key: int(value) for key, value in (
                    line.split() for line in (base / "cpu.stat").read_text().splitlines()
                )}
            except (OSError, ValueError):
                item["cpu"] = None
        item["processes"] = process_sample(name) if item.get("present") else []
        containers.append(item)
    return {"kind": "host", "at": time.time(), "containers": containers,
            "host": watchdog._host_memory()}


def backup_sample() -> dict:
    properties = (
        "ActiveState", "Result", "ExecMainStatus", "ExecMainStartTimestamp",
        "ExecMainExitTimestamp",
    )
    result = subprocess.run(
        ["systemctl", "show", "spreadboard-backup.service",
         *[f"--property={key}" for key in properties]],
        capture_output=True, text=True, timeout=10, check=False,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {"kind": "backup", "at": time.time(), "returncode": result.returncode,
            **{key: values.get(key) for key in properties}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=48)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    next_host = next_health = next_free = next_backup = started
    pending = {}
    with (args.output_dir / "samples.jsonl").open("a", encoding="utf-8") as handle:
        def record(item):
            handle.write(json.dumps(item, separators=(",", ":")) + "\n")
            handle.flush()

        record({"kind": "start", "at": time.time(), "hours": args.hours})
        with ThreadPoolExecutor(max_workers=2) as executor:
            while time.monotonic() - started <= args.hours * 3600:
                now = time.monotonic()
                for path, future in list(pending.items()):
                    if future.done():
                        record(future.result())
                        del pending[path]
                if now >= next_host:
                    try:
                        record(host_sample())
                    except Exception as exc:  # noqa: BLE001 - missing samples must be recorded.
                        record({"kind": "host_error", "at": time.time(), "error": type(exc).__name__})
                    next_host = now + 15
                if now >= next_health and "/api/health" not in pending:
                    pending["/api/health"] = executor.submit(endpoint_sample, "/api/health")
                    next_health = now + (120 if now - started <= 3600 else 300)
                if now >= next_free and "/free" not in pending:
                    pending["/free"] = executor.submit(endpoint_sample, "/free")
                    next_free = now + 300
                if now >= next_backup:
                    record(backup_sample())
                    next_backup = now + 300
                time.sleep(1)
            for future in pending.values():
                record(future.result())
        record({"kind": "finished", "at": time.time()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
