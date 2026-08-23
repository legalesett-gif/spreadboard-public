#!/usr/bin/env python3
"""Sync validated OKX OnchainOS credentials to production without printing them."""

from __future__ import annotations

import argparse
import importlib
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

okx_quotes = importlib.import_module("spreadarb.dex.okx_quotes")


REMOTE_UPDATE = r"""
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone

path = Path('/opt/spreadboard/secrets/collector.env')
payload = json.load(sys.stdin)
allowed = {
    'SPREADARB_OKX_DEX_API_KEY',
    'SPREADARB_OKX_DEX_SECRET',
    'SPREADARB_OKX_DEX_PASSPHRASE',
    'SPREADARB_OKX_DEX_PROJECT_ID',
}
if set(payload) - allowed:
    raise SystemExit('unexpected_secret_name')
if not all(payload.get(name) for name in allowed - {'SPREADARB_OKX_DEX_PROJECT_ID'}):
    raise SystemExit('required_okx_onchainos_secret_missing')

existing = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
output = []
seen = set()
for line in existing:
    if not line or line.lstrip().startswith('#') or '=' not in line:
        output.append(line)
        continue
    name, _value = line.split('=', 1)
    if name not in allowed:
        output.append(line)
        continue
    seen.add(name)
    if payload.get(name):
        output.append(f'{name}={payload[name]}')
for name in sorted(allowed - seen):
    if payload.get(name):
        output.append(f'{name}={payload[name]}')

path.parent.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup = path.with_name(f'{path.name}.bak.{stamp}')
if path.exists():
    shutil.copy2(path, backup)
temporary = path.with_name(f'{path.name}.updating')
temporary.write_text('\n'.join(output).rstrip() + '\n', encoding='utf-8')
temporary.chmod(0o600)
os.replace(temporary, path)
print(json.dumps({
    'ok': True,
    'updated_names': sorted(name for name in allowed if payload.get(name)),
    'project_id_removed': not bool(payload.get('SPREADARB_OKX_DEX_PROJECT_ID')),
    'backup_created': backup.exists(),
}))
"""


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=False, **kwargs)  # type: ignore[arg-type]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default="root@178.128.126.204")
    parser.add_argument(
        "--ssh-key",
        type=Path,
        default=Path.home() / ".ssh" / "spreadboard_digitalocean",
    )
    args = parser.parse_args()

    validation = _run(
        [sys.executable, str(ROOT / "scripts/verify_okx_dex_access.py"), "--json"],
        capture_output=True,
    )
    if validation.returncode != 0:
        print(validation.stdout.strip() or '{"ok": false, "blockers": ["local_validation_failed"]}')
        return 2

    credentials = okx_quotes.load_okx_dex_credentials()
    if credentials is None:
        print(json.dumps({"ok": False, "blockers": ["credentials_missing_after_validation"]}))
        return 2
    payload = {
        "SPREADARB_OKX_DEX_API_KEY": credentials.api_key,
        "SPREADARB_OKX_DEX_SECRET": credentials.secret,
        "SPREADARB_OKX_DEX_PASSPHRASE": credentials.passphrase,
        "SPREADARB_OKX_DEX_PROJECT_ID": credentials.project_id or "",
    }
    ssh = [
        "ssh",
        "-i",
        str(args.ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        args.ssh_host,
    ]
    updated = _run(
        [*ssh, f"python3 -c {shlex.quote(REMOTE_UPDATE)}"],
        input=json.dumps(payload),
        capture_output=True,
    )
    payload.clear()
    if updated.returncode != 0:
        print(json.dumps({"ok": False, "blockers": ["production_secret_sync_failed"]}))
        return 3
    print(updated.stdout.strip())

    restarted = _run(
        [
            *ssh,
            (
                "cd /opt/spreadboard/app && docker compose -f compose.production.yml "
                "up -d --force-recreate --no-deps collector"
            ),
        ],
        capture_output=True,
    )
    if restarted.returncode != 0:
        print(json.dumps({"ok": False, "blockers": ["collector_restart_failed"]}))
        return 4

    health = ""
    for _attempt in range(18):
        checked = _run(
            [
                *ssh,
                (
                    "cd /opt/spreadboard/app && "
                    "container=$(docker compose -f compose.production.yml ps -q collector) && "
                    "docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}"
                    "{{else}}{{.State.Status}}{{end}}' \"$container\""
                ),
            ],
            capture_output=True,
        )
        health = checked.stdout.strip()
        if health == "healthy":
            break
        if health in {"unhealthy", "exited", "dead"}:
            break
        time.sleep(5)
    if health != "healthy":
        print(json.dumps({"ok": False, "blockers": [f"collector_{health or 'health_unknown'}"]}))
        return 5

    verified = _run(
        [
            *ssh,
            (
                "cd /opt/spreadboard/app && docker compose -f compose.production.yml exec -T "
                "collector /app/.venv/bin/python scripts/verify_okx_dex_access.py --json"
            ),
        ],
        capture_output=True,
    )
    print(verified.stdout.strip() or json.dumps({"ok": False, "blockers": ["remote_validation_missing"]}))
    return 0 if verified.returncode == 0 else 6


if __name__ == "__main__":
    raise SystemExit(main())
