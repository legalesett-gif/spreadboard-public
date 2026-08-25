"""Cross-process demand lane for exact funding-history legs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from collections.abc import Iterable


RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))
DEFAULT_PATH = RUNTIME_DIR / "funding_history_demand.json"
_LOCK = threading.Lock()
_TTL_SECONDS = 21_600.0
_MAX_LEGS = 500


def enqueue(
    legs: Iterable[tuple[str, str]], *, path: Path | str | None = None
) -> int:
    """Persist exact futures legs; this function never calls a provider."""

    destination = Path(path) if path is not None else DEFAULT_PATH
    now = time.time()
    with _LOCK:
        records = _read(destination)
        added = 0
        for venue, symbol in legs:
            venue_text = str(venue or "")
            symbol_text = str(symbol or "")
            if not venue_text or not symbol_text:
                continue
            key = f"{venue_text}|{symbol_text}"
            if key not in records:
                added += 1
            records[key] = {
                "venue": venue_text,
                "symbol": symbol_text,
                "seen_at": now,
            }
        records = {
            key: value
            for key, value in sorted(
                records.items(),
                key=lambda item: float(item[1].get("seen_at") or 0.0),
                reverse=True,
            )[:_MAX_LEGS]
            if now - float(value.get("seen_at") or 0.0) <= _TTL_SECONDS
        }
        _atomic_json(destination, {"schema": "spreadboard.funding_history_demand.v1", "legs": records})
    return added


def legs(*, path: Path | str | None = None) -> list[tuple[str, str]]:
    now = time.time()
    records = _read(Path(path) if path is not None else DEFAULT_PATH)
    selected = [
        (
            float(value.get("seen_at") or 0.0),
            (str(value.get("venue") or ""), str(value.get("symbol") or "")),
        )
        for value in records.values()
        if isinstance(value, dict)
        and now - float(value.get("seen_at") or 0.0) <= _TTL_SECONDS
        and value.get("venue")
        and value.get("symbol")
    ]
    selected.sort(reverse=True)
    return [item for _seen, item in selected]


def _read(path: Path) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = payload.get("legs") if isinstance(payload, dict) else None
    return records if isinstance(records, dict) else {}


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, separators=(",", ":"))
        temporary = Path(handle.name)
    temporary.replace(path)
