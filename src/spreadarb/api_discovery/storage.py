"""Snapshot and archive persistence for read-only API discovery."""

from __future__ import annotations

import json
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


def append_archive(archive_dir: Path, payload: dict[str, Any]) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{utc_now().date().isoformat()}.jsonl"
    with archive_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")
    return archive_path
