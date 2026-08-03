"""Snapshot and archive persistence for read-only API discovery."""

from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from spreadarb.api_discovery.models import utc_now


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


#: How many days of raw scan output to keep. Nothing in the product reads these
#: back -- they exist for after-the-fact inspection -- and a full snapshot is
#: written on every scan, which reached 1.98GB in a single day and 3.7GB in
#: total. A few days is enough to look at; the rest is I/O and disk for nobody.
ARCHIVE_RETENTION_DAYS = max(1, int(os.environ.get("SPREADARB_ARCHIVE_RETENTION_DAYS", "3")))


def append_archive(archive_dir: Path, payload: dict[str, Any]) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{utc_now().date().isoformat()}.jsonl"
    with archive_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")
    _prune_archive(archive_dir)
    return archive_path


def _prune_archive(archive_dir: Path) -> int:
    """Drop archive days past the retention window."""
    cutoff = utc_now().date() - timedelta(days=ARCHIVE_RETENTION_DAYS)
    removed = 0
    for path in archive_dir.glob("*.jsonl"):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed
