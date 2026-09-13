#!/usr/bin/env python3
"""Bounded, fail-closed restore proof; never restore over a live directory.

Uses the existing encrypted repository configuration. Output contains metadata
only, not provider errors, credentials, SQL rows, or database contents.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path, PurePosixPath

from backup_spreadboard import DATABASE_SUFFIXES, RESTIC, _run_restic


def restic(*args: str, timeout: int = 1800) -> str:
    result = _run_restic([RESTIC, *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"restic_{args[0]}_failed_exit_{result.returncode}")
    return result.stdout


def snapshot_database_paths(listing: str) -> set[str]:
    paths = set()
    for line in listing.splitlines():
        node = json.loads(line)
        if node.get("type") != "file":
            continue
        raw = str(node.get("path") or "")
        path = PurePosixPath(raw)
        if ".." in path.parts or not path.is_absolute():
            raise RuntimeError("invalid_snapshot_path")
        if path.suffix.casefold() in DATABASE_SUFFIXES:
            paths.add(raw.lstrip("/"))
    if not paths or not any(PurePosixPath(p).name == "spreadboard_accounts.sqlite3" for p in paths):
        raise RuntimeError("snapshot_missing_accounts_database")
    return paths


def verify_databases(destination: Path, expected: set[str]) -> dict:
    actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*")
              if p.is_file() and p.suffix.casefold() in DATABASE_SUFFIXES}
    if not expected or actual != expected:
        raise RuntimeError("restored_database_inventory_mismatch")
    verified = []
    for relative in sorted(expected):
        path = destination / relative
        if path.is_symlink() or not path.resolve().is_relative_to(destination.resolve()):
            raise RuntimeError("unsafe_restored_database_path")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("restored_database_integrity_failed")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                raise RuntimeError("restored_database_has_no_tables")
            if path.name == "spreadboard_accounts.sqlite3":
                if not {"users", "sessions", "crypto_invoices", "affiliate_partners"} <= tables:
                    raise RuntimeError("restored_accounts_schema_incomplete")
                if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] < 1:
                    raise RuntimeError("restored_accounts_users_empty")
            verified.append({"database": path.name, "tables": len(tables)})
    return {"database_count": len(verified), "databases": verified}


def process_start_ticks(pid: int) -> str | None:
    try:
        # Process names can contain spaces; fields after the final ')' start
        # at field 3. starttime is field 22. PID reuse must not extend the wait.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def wait_for_process(pid: int, ticks: str, *, timeout: int = 21600) -> None:
    deadline = time.monotonic() + timeout
    while process_start_ticks(pid) == ticks:
        if time.monotonic() >= deadline:
            raise RuntimeError("restore_wait_deadline_exceeded")
        time.sleep(30)


def restore_and_verify(output_root: Path) -> dict:
    snapshots = json.loads(restic("snapshots", "--tag", "spreadboard", "--host", "spreadboard-prod", "--json"))
    if not snapshots:
        raise RuntimeError("no_backup_snapshot")
    chosen = max(snapshots, key=lambda row: row["time"])
    snapshot_id = str(chosen["id"])
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise RuntimeError("invalid_snapshot_id")
    expected = snapshot_database_paths(restic("ls", snapshot_id, "--json"))
    # mkdtemp creates a fresh private directory. A caller cannot specify a live
    # target or an existing restore directory, even accidentally.
    destination = Path(tempfile.mkdtemp(prefix="spreadboard-restore-proof-", dir=output_root))
    print(json.dumps({"status": "restoring", "snapshot": snapshot_id[:8],
                      "destination": str(destination), "expected_databases": len(expected)}), flush=True)
    restic("restore", snapshot_id, "--target", str(destination), timeout=21600)
    evidence = verify_databases(destination, expected)
    return {"status": "verified", "snapshot": snapshot_id[:8], "snapshot_time": chosen["time"],
            "destination": str(destination), **evidence}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--wait-pid", type=int)
    parser.add_argument("--wait-start-ticks")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.wait_pid:
            if not args.wait_start_ticks:
                raise RuntimeError("wait_process_identity_required")
            print(json.dumps({"status": "waiting_for_existing_prune", "pid": args.wait_pid}), flush=True)
            wait_for_process(args.wait_pid, args.wait_start_ticks)
        print(json.dumps(restore_and_verify(args.output_root)), flush=True)
        return 0
    except (RuntimeError, ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        # Never echo external exception messages: provider URLs may be signed.
        reason = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print(json.dumps({"status": "failed", "reason": reason}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
