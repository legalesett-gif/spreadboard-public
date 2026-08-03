"""Raw scan output is written on every scan and read by nothing.

It reached 1.98GB in a single day and 3.7GB in total. A few days is enough to
inspect after the fact; the rest is I/O and disk for nobody.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from src.spreadarb.api_discovery import storage


def test_days_past_the_window_are_dropped(tmp_path: Path) -> None:
    today = date.today()
    kept = tmp_path / f"{today.isoformat()}.jsonl"
    recent = tmp_path / f"{(today - timedelta(days=1)).isoformat()}.jsonl"
    stale = tmp_path / f"{(today - timedelta(days=30)).isoformat()}.jsonl"
    for path in (kept, recent, stale):
        path.write_text("{}\n", encoding="utf-8")

    storage._prune_archive(tmp_path)

    assert kept.exists() and recent.exists()
    assert not stale.exists(), "a month-old archive is 2GB nobody reads"


def test_files_that_are_not_dated_archives_are_left_alone(tmp_path: Path) -> None:
    other = tmp_path / "notes.jsonl"
    other.write_text("{}\n", encoding="utf-8")

    storage._prune_archive(tmp_path)

    assert other.exists()


def test_appending_still_returns_todays_path(tmp_path: Path) -> None:
    path = storage.append_archive(tmp_path, {"schema": "x"})

    assert path.name == f"{date.today().isoformat()}.jsonl"
    assert path.read_text(encoding="utf-8").strip()
