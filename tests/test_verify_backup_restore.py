from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from scripts import backup_spreadboard

sys.modules.setdefault("backup_spreadboard", backup_spreadboard)
SPEC = importlib.util.spec_from_file_location("restore_proof", Path(__file__).parents[1] / "scripts/verify_backup_restore.py")
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


def accounts_fixture(root):
    path = root / "spreadboard_accounts.sqlite3"
    with sqlite3.connect(path) as db:
        for table in ("users", "sessions", "crypto_invoices", "affiliate_partners"):
            db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO users VALUES (1)")
    return path


def test_no_databases_cannot_be_reported_verified(tmp_path):
    with pytest.raises(RuntimeError, match="inventory_mismatch"):
        proof.verify_databases(tmp_path, set())


def test_missing_database_fails_even_if_accounts_is_valid(tmp_path):
    accounts_fixture(tmp_path)
    with pytest.raises(RuntimeError, match="inventory_mismatch"):
        proof.verify_databases(tmp_path, {"spreadboard_accounts.sqlite3", "market_history.sqlite3"})


def test_restored_accounts_require_schema_and_users(tmp_path):
    path = accounts_fixture(tmp_path)
    assert proof.verify_databases(tmp_path, {path.name})["database_count"] == 1
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM users")
    with pytest.raises(RuntimeError, match="users_empty"):
        proof.verify_databases(tmp_path, {path.name})


def test_corrupt_database_cannot_pass(tmp_path):
    accounts_fixture(tmp_path)
    (tmp_path / "history.db").write_bytes(b"broken")
    with pytest.raises(sqlite3.DatabaseError):
        proof.verify_databases(tmp_path, {"spreadboard_accounts.sqlite3", "history.db"})


def test_foreign_key_failure_rejected(tmp_path):
    path = accounts_fixture(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE bad (user_id INTEGER REFERENCES users(id))")
        db.execute("INSERT INTO bad VALUES (99)")
    with pytest.raises(RuntimeError, match="integrity_failed"):
        proof.verify_databases(tmp_path, {path.name})


@pytest.mark.parametrize("listing", ["", '{"type":"file","path":"/history.db"}',
                                    '{"type":"file","path":"/../outside.db"}'])
def test_invalid_or_empty_snapshot_inventory_fails(listing):
    with pytest.raises(RuntimeError):
        proof.snapshot_database_paths(listing)


def test_failed_restore_never_runs_sqlite_verification(tmp_path, monkeypatch):
    snapshot = "a" * 64
    def restic(*args, **kwargs):
        if args[0] == "snapshots":
            return json.dumps([{"id": snapshot, "time": "2026-09-13T00:00:00Z"}])
        if args[0] == "ls":
            return '{"type":"file","path":"/tmp/stage/spreadboard_accounts.sqlite3"}'
        raise RuntimeError("restic_restore_failed_exit_1")
    monkeypatch.setattr(proof, "restic", restic)
    monkeypatch.setattr(proof, "verify_databases", lambda *a: pytest.fail("verified failed restore"))
    with pytest.raises(RuntimeError, match="restore_failed"):
        proof.restore_and_verify(tmp_path)


def test_reused_pid_does_not_keep_verifier_waiting(monkeypatch):
    monkeypatch.setattr(proof, "process_start_ticks", lambda pid: "new-process")
    monkeypatch.setattr(proof.time, "sleep", lambda *a: pytest.fail("waited on reused PID"))
    proof.wait_for_process(12, "old-process")


def test_process_wait_has_deadline(monkeypatch):
    monkeypatch.setattr(proof, "process_start_ticks", lambda pid: "same")
    ticks = iter([0, 100])
    monkeypatch.setattr(proof.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeError, match="deadline"):
        proof.wait_for_process(12, "same", timeout=60)
