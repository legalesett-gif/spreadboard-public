from __future__ import annotations

import sqlite3

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
