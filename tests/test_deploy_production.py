from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_production.sh"


def _fake_command(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_deploy(
    tmp_path: Path, *services: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh_log = tmp_path / "ssh.log"
    _fake_command(
        bin_dir / "ssh",
        """
printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
case "$*" in
  *"docker top app-collector-1"*) printf '1\\n' ;;
  *"RestartCount"*) printf '0,0\\n' ;;
  *"source_digest.py"*) printf '%s\\n' "$FAKE_SOURCE_DIGEST" ;;
esac
""".strip(),
    )
    _fake_command(bin_dir / "rsync", ":")
    _fake_command(bin_dir / "curl", "printf '200'")
    _fake_command(bin_dir / "sleep", ":")
    digest = subprocess.run(
        ["python3", str(ROOT / "scripts" / "source_digest.py"), str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_SSH_LOG": str(ssh_log),
        "FAKE_SOURCE_DIGEST": digest,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), *services],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, ssh_log.read_text(encoding="utf-8")


def test_app_only_deploy_does_not_query_or_block_on_collector_scan(tmp_path: Path) -> None:
    result, ssh_log = _run_deploy(tmp_path, "app")

    assert result.returncode == 0, result.stderr
    assert "docker top app-collector-1" not in ssh_log
    assert "deploy OK" in result.stdout


def test_collector_deploy_still_refuses_during_active_scan(tmp_path: Path) -> None:
    result, ssh_log = _run_deploy(tmp_path, "collector")

    assert result.returncode == 1
    assert "docker top app-collector-1" in ssh_log
    assert "REFUSING: a discovery scan is in flight" in result.stderr
