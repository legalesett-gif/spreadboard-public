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
    tmp_path: Path, *services: str, late_scan: bool = False,
    health: str = "200", state: str = "true false healthy", mismatch: bool = False
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh_log = tmp_path / "ssh.log"
    _fake_command(
        bin_dir / "ssh",
        """
printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
case "$*" in
  *"docker top app-collector-1"*)
    if [ "$FAKE_LATE_SCAN" = "1" ] && [ ! -f "$FAKE_SSH_LOG.seen" ]; then
      touch "$FAKE_SSH_LOG.seen"; printf '0\\n'
    else printf '1\\n'; fi ;;
  *"RestartCount"*) printf '0,0\\n' ;;
  *"State.Running"*) printf '%s\\n' "$FAKE_STATE" ;;
  *"source_digest.py"*) printf '%s\\n' "$FAKE_SOURCE_DIGEST" ;;
esac
""".strip(),
    )
    _fake_command(bin_dir / "rsync", ":")
    _fake_command(bin_dir / "curl", "printf '%s' \"$FAKE_HEALTH\"")
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
        "FAKE_LATE_SCAN": "1" if late_scan else "0",
        "FAKE_SOURCE_DIGEST": "wrong" if mismatch else digest,
        "FAKE_HEALTH": health,
        "FAKE_STATE": state,
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


def test_collector_is_not_recreated_if_discovery_starts_during_build(tmp_path):
    result, log = _run_deploy(tmp_path, "collector", late_scan=True)
    assert result.returncode == 1
    assert "started during the build" in result.stderr
    assert " build collector" in log
    assert "up -d" not in log


def test_success_writes_receipt_only_after_source_verification(tmp_path):
    result, log = _run_deploy(tmp_path, "app")
    assert result.returncode == 0, result.stderr
    assert log.index("source_digest.py") < log.index("record_deployment.py")
    assert "--revision " in log and "--digest " in log


def test_source_mismatch_cannot_advance_marker(tmp_path):
    result, log = _run_deploy(tmp_path, "app", mismatch=True)
    assert result.returncode != 0
    assert "record_deployment.py" not in log


def test_unhealthy_container_cannot_advance_marker(tmp_path):
    result, log = _run_deploy(tmp_path, "app", state="true false unhealthy")
    assert result.returncode != 0
    assert "record_deployment.py" not in log


def test_failed_endpoint_cannot_advance_marker(tmp_path):
    result, log = _run_deploy(tmp_path, "app", health="503")
    assert result.returncode != 0
    assert "record_deployment.py" not in log
