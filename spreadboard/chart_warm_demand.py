"""Cross-process demand lane for chart history members actually reveal."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))
DEFAULT_PATH = RUNTIME_DIR / "chart_warm_demand.json"
_LOCK = threading.Lock()
_MAX_ROUTES = 2_000
_TTL_SECONDS = 21_600.0


def enqueue(
    route_keys: Iterable[str],
    *,
    hours: float = 24.0,
    path: Path | str | None = None,
) -> int:
    """Persist self-contained route keys without making a provider call."""

    destination = Path(path) if path is not None else DEFAULT_PATH
    now = time.time()
    requested_hours = max(1 / 60, min(float(hours), 720.0))
    with _LOCK:
        records = _read(destination)
        added = 0
        for value in route_keys:
            key = str(value or "")
            if not key:
                continue
            existing = records.get(key) or {}
            if key not in records:
                added += 1
            records[key] = {
                "route_key": key,
                "hours": max(requested_hours, float(existing.get("hours") or 0.0)),
                "seen_at": now,
            }
        records = {
            key: value
            for key, value in sorted(
                records.items(),
                key=lambda item: float(item[1].get("seen_at") or 0.0),
                reverse=True,
            )[:_MAX_ROUTES]
            if now - float(value.get("seen_at") or 0.0) <= _TTL_SECONDS
        }
        _atomic_json(
            destination,
            {"schema": "spreadboard.chart_warm_demand.v2", "routes": records},
        )
    return added


def requests(*, path: Path | str | None = None) -> list[tuple[str, float]]:
    """Return newest requested routes with their widest requested horizon."""

    now = time.time()
    records = _read(Path(path) if path is not None else DEFAULT_PATH)
    selected = [
        (
            float(value.get("seen_at") or 0.0),
            str(value.get("route_key") or key),
            max(1 / 60, min(float(value.get("hours") or 24.0), 720.0)),
        )
        for key, value in records.items()
        if isinstance(value, dict)
        and now - float(value.get("seen_at") or 0.0) <= _TTL_SECONDS
        and (value.get("route_key") or key)
    ]
    selected.sort(reverse=True)
    return [(key, hours) for _seen, key, hours in selected]


def route_keys(*, path: Path | str | None = None) -> list[str]:
    return [key for key, _hours in requests(path=path)]


def _read(path: Path) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = payload.get("routes") if isinstance(payload, dict) else None
    return records if isinstance(records, dict) else {}


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, separators=(",", ":"))
        temporary = Path(handle.name)
    temporary.replace(path)
