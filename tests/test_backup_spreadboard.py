from __future__ import annotations

import sqlite3

import pytest

from scripts import backup_spreadboard


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
