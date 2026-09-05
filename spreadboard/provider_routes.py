"""Bounded discovery handoff for instruments absent from the CEX catalogue.

The disposable snapshot finalizer already owns the large parsed discovery
tree. It publishes this small subset for the frequent route-index builds;
those builds must never parse the full snapshot just to recover builder legs.
Quote timestamps and all identity blockers remain unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SOURCE_NAMES = frozenset({"hyperliquid_builder_dex"})
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 4096
BUCKETS = ("api_discovered_rows", "dex_discovered_rows")


def artifact_path(discovery_path: Path) -> Path:
    return discovery_path.with_name(discovery_path.stem + ".providers.json")


def signature(path: Path) -> list[int] | None:
    try:
        stat = path.stat()
        return [stat.st_mtime_ns, stat.st_size]
    except OSError:
        return None


def publish(
    snapshot: dict[str, Any], discovery_path: Path,
    *, source_signature: list[int] | None = None,
) -> dict[str, Any]:
    buckets = {
        bucket: [row for row in snapshot.get(bucket, [])
                 if isinstance(row, dict) and row.get("source_name") in SOURCE_NAMES]
        for bucket in BUCKETS
    }
    count = sum(len(rows) for rows in buckets.values())
    payload = {
        "schema": 1, "source_signature": source_signature or signature(discovery_path),
        "updated_at": snapshot.get("updated_at"), **buckets,
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if count > MAX_ROWS or len(encoded) > MAX_BYTES:
        raise ValueError("provider_artifact_exceeds_budget")
    path = artifact_path(discovery_path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"provider_rows": count, "provider_bytes": len(encoded)}


def load(discovery_path: Path) -> dict[str, Any] | None:
    """Reject missing, corrupt, oversized or mismatched generations cheaply."""
    try:
        with artifact_path(discovery_path).open("rb") as handle:
            encoded = handle.read(MAX_BYTES + 1)
        if len(encoded) > MAX_BYTES:
            return None
        payload = json.loads(encoded)
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            return None
        expected = signature(discovery_path)
        if expected is None or payload.get("source_signature") != expected:
            return None
        count = 0
        for bucket in BUCKETS:
            rows = payload.get(bucket)
            if not isinstance(rows, list):
                return None
            count += len(rows)
            if count > MAX_ROWS or any(
                not isinstance(row, dict) or row.get("source_name") not in SOURCE_NAMES
                for row in rows
            ):
                return None
        return payload
    except (OSError, ValueError):
        return None
