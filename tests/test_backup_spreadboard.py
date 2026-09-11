from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import backup_spreadboard

ROOT = Path(__file__).resolve().parents[1]


def test_stage_snapshot_uses_consistent_sqlite_copy_and_excludes_cache(tmp_path) -> None:
    source = tmp_path / "runtime"
    source.mkdir()
    database = source / "accounts.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
        connection.execute("INSERT INTO users (email) VALUES ('member@example.test')")
    (source / "latest.json").write_text('{"ok":true}', encoding="utf-8")
    cache = source / "historical_spread_cache"
    cache.mkdir()
    (cache / "large.json").write_text("discard", encoding="utf-8")

    target = tmp_path / "staged"
    target.mkdir()
    copied = backup_spreadboard.stage_snapshot(source, target)

    assert set(copied) == {database.relative_to(source), (source / "latest.json").relative_to(source)}
    with sqlite3.connect(target / "accounts.sqlite3") as connection:
        assert connection.execute("SELECT email FROM users").fetchone()[0] == "member@example.test"
    assert not (target / "historical_spread_cache" / "large.json").exists()


def test_stage_snapshot_keeps_databases_that_exceed_the_size_cap(tmp_path, monkeypatch) -> None:
    """Market history is gigabytes; a size cap must never silently drop it.

    The cap exists to keep raw discovery archives out of the repository. When it
    applied to databases too, the backup still reported success while containing
    no route history at all.
    """
    monkeypatch.setattr(backup_spreadboard, "MAX_COPIED_FILE_BYTES", 128)

    source = tmp_path / "runtime"
    source.mkdir()
    database = source / "market_history.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE route_points (route_key TEXT, payload TEXT)")
        connection.executemany(
            "INSERT INTO route_points (route_key, payload) VALUES (?, ?)",
            [(f"route-{index}", "x" * 512) for index in range(64)],
        )
    assert database.stat().st_size > 128
    archive = source / "discovery.jsonl"
    archive.write_text("y" * 4096, encoding="utf-8")

    target = tmp_path / "staged"
    target.mkdir()
    copied = backup_spreadboard.stage_snapshot(source, target)

    assert database.relative_to(source) in copied
    assert archive.relative_to(source) not in copied
    with sqlite3.connect(target / "market_history.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM route_points").fetchone()[0] == 64


def test_stage_snapshot_names_a_database_that_cannot_be_copied(tmp_path) -> None:
    source = tmp_path / "runtime"
    source.mkdir()
    broken = source / "broken.sqlite3"
    broken.write_bytes(b"not a sqlite database")
    target = tmp_path / "staged"
    target.mkdir()

    with pytest.raises(RuntimeError, match=r"backup_sqlite_failed:broken\.sqlite3"):
        backup_spreadboard.stage_snapshot(source, target)


# --------------------------------------------------------------------------
# Backend-appropriate configuration
# --------------------------------------------------------------------------


def test_an_s3_repository_still_demands_its_credentials(monkeypatch) -> None:
    monkeypatch.setenv("RESTIC_REPOSITORY", "s3:https://x.r2.cloudflarestorage.com/b")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/tmp/pw")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    with pytest.raises(RuntimeError, match="backup_configuration_missing"):
        backup_spreadboard._require_restic_configuration()


def test_an_rclone_repository_does_not_need_aws_keys(monkeypatch) -> None:
    """rclone carries its own credentials; demanding AWS keys blocks it entirely."""
    monkeypatch.setenv("RESTIC_REPOSITORY", "rclone:gdrive:spreadboard-backup")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/tmp/pw")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    backup_spreadboard._require_restic_configuration()


def test_every_backend_still_needs_a_repository_and_a_password(monkeypatch) -> None:
    """The password is what makes the snapshot ciphertext; never optional."""
    monkeypatch.setenv("RESTIC_REPOSITORY", "rclone:gdrive:spreadboard-backup")
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)

    with pytest.raises(RuntimeError, match="backup_configuration_missing"):
        backup_spreadboard._require_restic_configuration()


def test_hardened_backup_unit_does_not_depend_on_root_home_for_rclone() -> None:
    unit = (ROOT / "deploy" / "spreadboard-backup.service").read_text(encoding="utf-8")

    assert "ProtectHome=true" in unit
    assert "RCLONE_CONFIG=/opt/spreadboard/secrets/rclone/rclone.conf" in unit
    assert "ReadWritePaths=/opt/spreadboard/secrets/rclone\n" in unit
    assert "ReadWritePaths=/opt/spreadboard/secrets\n" not in unit
    assert "UMask=0077" in unit
    assert "ReadWritePaths=/opt/spreadboard/runtime" in unit
    assert "ReadOnlyPaths=/opt/spreadboard/app" in unit
    assert "ReadOnlyPaths=/opt/spreadboard/runtime" not in unit


def test_backup_retries_transient_repository_probe_before_staging(monkeypatch, tmp_path, capsys):
    calls = []
    sleeps = []
    source = tmp_path / "runtime"
    source.mkdir()
    monkeypatch.setattr(backup_spreadboard, "RUNTIME_DIR", source)
    monkeypatch.setattr(backup_spreadboard, "_require_restic_configuration", lambda: None)
    monkeypatch.setattr(backup_spreadboard.time, "sleep", sleeps.append)
    monkeypatch.setattr(backup_spreadboard, "stage_snapshot", lambda *a: [Path("evidence.json")])
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "snapshots" and len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="RATE_LIMIT_EXCEEDED private-fixture-value")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(backup_spreadboard.subprocess, "run", run)
    backup_spreadboard.run_backup()
    assert [x[1] for x in calls] == ["snapshots", "snapshots", "backup", "forget", "check"]
    assert sleeps == [10]
    assert "private-fixture-value" not in capsys.readouterr().out


@pytest.mark.parametrize("timeout", [False, True])
def test_repository_probe_exhaustion_is_bounded_and_cannot_stage(monkeypatch, tmp_path, timeout, capsys):
    calls = []
    sleeps = []
    monkeypatch.setattr(backup_spreadboard, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(backup_spreadboard, "_require_restic_configuration", lambda: None)
    monkeypatch.setattr(backup_spreadboard.time, "sleep", sleeps.append)
    monkeypatch.setattr(backup_spreadboard, "stage_snapshot", lambda *a: pytest.fail("staged after failed repository probe"))
    def run(command, **kwargs):
        calls.append(command)
        assert command[1] == "snapshots" and kwargs["timeout"] == 120
        if timeout:
            raise subprocess.TimeoutExpired(command, 120)
        return SimpleNamespace(returncode=1, stdout="", stderr="read-only file system private-fixture-value")
    monkeypatch.setattr(backup_spreadboard.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="backup_repository_unavailable:backend_"):
        backup_spreadboard.run_backup()
    assert len(calls) == 3 and sleeps == [10, 30]
    assert "private-fixture-value" not in capsys.readouterr().out


def test_actual_rclone_backup_paces_every_phase_and_allows_connection_window(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("RESTIC_REPOSITORY", "rclone:fixture:backup")
    monkeypatch.delenv("RCLONE_TPSLIMIT", raising=False)
    monkeypatch.delenv("RCLONE_TPSLIMIT_BURST", raising=False)
    monkeypatch.setattr(backup_spreadboard, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(backup_spreadboard, "_require_restic_configuration", lambda: None)
    monkeypatch.setattr(backup_spreadboard, "stage_snapshot", lambda *a: [Path("evidence.json")])
    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert command[-4:] == ["-o", "rclone.timeout=5m", "-o", "rclone.connections=2"]
        assert kwargs["env"]["RCLONE_TPSLIMIT"] == "4"
        assert kwargs["env"]["RCLONE_TPSLIMIT_BURST"] == "1"
        if command[1] == "snapshots":
            assert kwargs["timeout"] == 360
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(backup_spreadboard.subprocess, "run", run)
    backup_spreadboard.run_backup()
    assert [command[1] for command, _ in calls] == ["snapshots", "backup", "forget", "check"]


def test_non_rclone_commands_and_operator_pacing_are_preserved(monkeypatch):
    calls = []
    monkeypatch.setattr(backup_spreadboard.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    monkeypatch.setenv("RESTIC_REPOSITORY", "s3:fixture")
    backup_spreadboard._run_restic(["restic", "snapshots"], timeout=120)
    assert calls[-1] == (["restic", "snapshots"], {"timeout": 120, "check": False})
    monkeypatch.setenv("RESTIC_REPOSITORY", "rclone:fixture:backup")
    monkeypatch.setenv("RCLONE_TPSLIMIT", "1")
    backup_spreadboard._run_restic(["restic", "check"])
    assert calls[-1][1]["env"]["RCLONE_TPSLIMIT"] == "1"


def test_repository_lock_recovery_uses_only_stale_unlock_and_rechecks(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout='', stderr='repository is already locked exclusively')
        return SimpleNamespace(returncode=0, stdout='[]', stderr='')
    monkeypatch.setattr(backup_spreadboard, '_run_restic', run)
    monkeypatch.setattr(backup_spreadboard.time, 'sleep', lambda _: None)
    backup_spreadboard._ensure_repository()
    assert [c[1] for c in calls] == ['snapshots', 'unlock', 'snapshots']
    assert all('--remove-all' not in c for c in calls)


def test_live_repository_lock_remains_a_failure(monkeypatch):
    def run(command, **kwargs):
        if command[1] == 'unlock':
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr='repository is already locked exclusively')
    monkeypatch.setattr(backup_spreadboard, '_run_restic', run)
    monkeypatch.setattr(backup_spreadboard.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='backend_locked'):
        backup_spreadboard._ensure_repository()


def test_snapshot_closes_both_database_handles_before_upload(tmp_path, monkeypatch):
    closed = []
    class Connection:
        def __init__(self, name): self.name = name
        def backup(self, target): assert target.name == 'destination'
        def close(self): closed.append(self.name)
    connections = iter([Connection('source'), Connection('destination')])
    monkeypatch.setattr(backup_spreadboard.sqlite3, 'connect', lambda *a, **kw: next(connections))
    backup_spreadboard._backup_sqlite(tmp_path/'source', tmp_path/'destination')
    assert closed == ['destination', 'source']


def test_persistent_repository_cache_is_never_backed_up_recursively(tmp_path):
    root = tmp_path/'runtime'
    cache = root/'restic-cache'/'repository'/'index'
    cache.mkdir(parents=True)
    path = cache/'cached-index'
    path.write_text('repository metadata')
    assert backup_spreadboard._excluded(path, root)
    unit = (ROOT/'deploy'/'spreadboard-backup.service').read_text()
    assert 'RESTIC_CACHE_DIR=/opt/spreadboard/runtime/restic-cache' in unit
