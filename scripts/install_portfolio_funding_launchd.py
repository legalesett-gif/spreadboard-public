#!/usr/bin/env python3
"""Install the exact private funding sync as a secret-free LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.spreadboard.portfolio-funding"
DEFAULT_SSH_KEY = Path.home() / ".ssh" / "spreadboard_digitalocean"


def launch_agent_payload(
    *,
    uv_path: Path,
    user_id: int,
    ssh_host: str,
    ssh_key: Path,
    interval_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(uv_path),
            "run",
            "python",
            str(ROOT / "scripts" / "sync_portfolio_funding.py"),
            "--user-id",
            str(user_id),
            "--ssh-host",
            ssh_host,
            "--ssh-key",
            str(ssh_key),
        ],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"UV_CACHE_DIR": "/tmp/uv-cache"},
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


def write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        plistlib.dump(payload, handle, sort_keys=True)
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, default=9)
    parser.add_argument("--ssh-host", default="root@178.128.126.204")
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--no-load", action="store_true")
    args = parser.parse_args()
    ssh_key = args.ssh_key.expanduser().resolve()
    if not ssh_key.is_file():
        raise SystemExit("SSH key is missing.")
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is not available on PATH.")
    interval = max(300, min(86_400, int(args.interval_seconds)))
    logs = ROOT / "runtime" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    payload = launch_agent_payload(
        uv_path=Path(uv).resolve(),
        user_id=args.user_id,
        ssh_host=args.ssh_host,
        ssh_key=ssh_key,
        interval_seconds=interval,
        stdout_path=logs / "portfolio-funding.stdout.log",
        stderr_path=logs / "portfolio-funding.stderr.log",
    )
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    write_atomic(plist_path, payload)
    if not args.no_load:
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(plist_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    print(
        json.dumps(
            {
                "ok": True,
                "label": LABEL,
                "interval_seconds": interval,
                "loaded": not args.no_load,
                "read_only": True,
            }
        )
    )


if __name__ == "__main__":
    main()
