"""Cross-process demand lane for exact funding-history legs."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))
DEFAULT_PATH = RUNTIME_DIR / "funding_history_demand.json"
_LOCK = threading.Lock()
_TTL_SECONDS = 21_600.0
# Warm Funding pages contain every exact pair in their JSON/export even though
# HTML previews only three. Routes are combinatorial but their futures legs are
# heavily shared; five thousand entries comfortably covers all principal views
# plus recent exact-token, watchlist and portfolio demand.
_MAX_LEGS = 5_000


def payload_legs(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Exact futures legs represented by one complete Funding payload."""

    if not ((payload.get("filters") or {}).get("funding_only")):
        return []
    selected: list[tuple[str, str]] = []
    for group in payload.get("groups") or []:
        if not isinstance(group, dict):
            continue
        routes = [group.get("best_funding_route"), *(group.get("routes") or [])]
        for route in routes:
            if not isinstance(route, dict):
                continue
            for side in ("long", "short"):
                if str(route.get(f"{side}_market_type") or "") != "Futures":
                    continue
                venue = str(route.get(f"{side}_venue") or "")
                symbol = str(
                    route.get(f"{side}_market_symbol")
                    or route.get(f"{side}_symbol")
                    or ""
                )
                if venue and symbol:
                    selected.append((venue, symbol))
    return list(dict.fromkeys(selected))


def enqueue_payload(payload: dict[str, Any], *, path: Path | str | None = None) -> int:
    """Persist a warm view's exact legs without contacting a provider."""

    return enqueue(payload_legs(payload), path=path)


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
