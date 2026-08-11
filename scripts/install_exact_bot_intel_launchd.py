#!/usr/bin/env python3
"""Install the privacy-safe exact-bot Intel sync as a macOS LaunchAgent."""

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
LABEL = "com.spreadboard.exact-bot-intel"
DEFAULT_SESSION = ROOT / "runtime" / "exact-bot-intel.session"
DEFAULT_OUTPUT = ROOT / "runtime" / "community" / "external_bot_events.jsonl"
DEFAULT_SSH_KEY = Path.home() / ".ssh" / "spreadboard_digitalocean"
DEFAULT_REMOTE = "/opt/spreadboard/runtime/community/external_bot_events.jsonl"


def launch_agent_payload(
    *,
    uv_path: Path,
    session_path: Path,
    output_path: Path,
    ssh_host: str,
    ssh_key: Path,
    remote_path: str,
    interval_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, object]:
    """Return a secret-free LaunchAgent payload for the recurring sync."""
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(uv_path),
            "run",
            "--with",
            "telethon",
            "python",
            str(ROOT / "scripts" / "sync_exact_bot_intel.py"),
            "--session-path",
            str(session_path),
            "--output-path",
            str(output_path),
            "--ssh-host",
            ssh_host,
            "--ssh-key",
            str(ssh_key),
            "--remote-path",
            remote_path,
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


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        plistlib.dump(payload, handle, sort_keys=True)
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-path", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ssh-host", default="root@178.128.126.204")
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    parser.add_argument("--remote-path", default=DEFAULT_REMOTE)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--no-load", action="store_true")
    args = parser.parse_args()

    session_path = args.session_path.expanduser().resolve()
    output_path = args.output_path.expanduser().resolve()
    ssh_key = args.ssh_key.expanduser().resolve()
    if not session_path.is_file():
        raise SystemExit(
            "Exact-bot session is missing. Run sync_exact_bot_intel.py once "
            "interactively before installing the LaunchAgent."
        )
    if not ssh_key.is_file():
        raise SystemExit("SSH key is missing.")
    if not args.ssh_host.strip():
        raise SystemExit("SSH host is required.")
    interval = max(300, min(86_400, int(args.interval_seconds)))
    session_path.chmod(0o600)

    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is not available on PATH.")
    logs = ROOT / "runtime" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    payload = launch_agent_payload(
        uv_path=Path(uv).resolve(),
        session_path=session_path,
        output_path=output_path,
        ssh_host=args.ssh_host.strip(),
        ssh_key=ssh_key,
        remote_path=args.remote_path,
        interval_seconds=interval,
        stdout_path=logs / "exact-bot-intel.stdout.log",
        stderr_path=logs / "exact-bot-intel.stderr.log",
    )
    _write_atomic(plist_path, payload)

    if not args.no_load:
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(plist_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            check=True,
        )
    print(json.dumps({
        "ok": True,
        "label": LABEL,
        "interval_seconds": interval,
        "loaded": not args.no_load,
        "raw_text_stored": False,
    }))


if __name__ == "__main__":
    main()
