"""Validated read-only market events overlaid on public route evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_CACHE_PATH = RUNTIME_DIR / "market_events.json"
ALLOWED_TYPES = {"delisting", "position_limit", "maintenance", "rail_closed", "rail_reopened", "listing"}
ALLOWED_SEVERITIES = {"info", "watch", "block"}
_CACHE: dict[str, Any] = {"stamp": None, "events": []}


def load(path: Path | str = DEFAULT_CACHE_PATH) -> list[dict[str, Any]]:
    path = Path(path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return []
    if _CACHE["stamp"] == stamp:
        return list(_CACHE["events"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = payload.get("events") if isinstance(payload, dict) else []
    events = []
    for item in raw if isinstance(raw, list) else []:
        normalized = _normalize_event(item)
        if normalized is not None:
            events.append(normalized)
        if len(events) >= 500:
            break
    _CACHE.update({"stamp": stamp, "events": events})
    return list(events)


def events_for_route(
    route: dict[str, Any],
    *,
    path: Path | str = DEFAULT_CACHE_PATH,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current = now or datetime.now(tz=timezone.utc)
    token = str(route.get("token") or "").upper()
    venues = {str(route.get("long_venue") or ""), str(route.get("short_venue") or "")}
    symbols = {
        str(route.get("long_market_symbol") or ""),
        str(route.get("short_market_symbol") or ""),
    }
    output = [
        event
        for event in load(path)
        if event["token"] == token
        and (not event.get("venue") or event["venue"] in venues)
        and (not event.get("market_symbol") or event["market_symbol"] in symbols)
        and _active(event, current)
    ]
    output.extend(_rail_events(route, current))
    deduped = {str(item["id"]): item for item in output}
    return sorted(
        deduped.values(),
        key=lambda item: (item["severity"] == "block", item.get("effective_at") or ""),
        reverse=True,
    )


def _rail_events(route: dict[str, Any], current: datetime) -> list[dict[str, Any]]:
    checks = (
        ("long", "withdraw", "Buy-side withdrawal is closed"),
        ("short", "deposit", "Sell-side deposit is closed"),
    )
    output = []
    for side, action, title in checks:
        if route.get(f"{side}_{action}_enabled") is not False:
            continue
        venue = str(route.get(f"{side}_venue") or "Unknown venue")
        token = str(route.get("token") or "").upper()
        output.append(
            {
                "id": f"live-rail-{token}-{venue}-{action}".casefold().replace(" ", "-"),
                "type": "rail_closed",
                "severity": "block",
                "token": token,
                "venue": venue,
                "market_symbol": str(route.get(f"{side}_market_symbol") or ""),
                "title": title,
                "detail": f"Current public rail metadata reports {action} unavailable at {venue}.",
                "effective_at": current.replace(microsecond=0).isoformat(),
                "ends_at": None,
                "source_label": "live transfer-rail cache",
                "source_url": None,
                "derived": True,
            }
        )
    return output


def _normalize_event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    event_type = str(value.get("type") or "").casefold().strip()
    severity = str(value.get("severity") or "watch").casefold().strip()
    token = "".join(char for char in str(value.get("token") or "").upper() if char.isalnum() or char in "_-")[:24]
    event_id = str(value.get("id") or "").strip()[:120]
    title = " ".join(str(value.get("title") or "").split())[:160]
    if event_type not in ALLOWED_TYPES or severity not in ALLOWED_SEVERITIES:
        return None
    if not token or not event_id or not title:
        return None
    source_url = str(value.get("source_url") or "").strip()[:500] or None
    if source_url and urlparse(source_url).scheme != "https":
        source_url = None
    return {
        "id": event_id,
        "type": event_type,
        "severity": severity,
        "token": token,
        "venue": " ".join(str(value.get("venue") or "").split())[:80],
        "market_symbol": str(value.get("market_symbol") or "").strip()[:100],
        "title": title,
        "detail": " ".join(str(value.get("detail") or "").split())[:500],
        "effective_at": _timestamp(value.get("effective_at")),
        "ends_at": _timestamp(value.get("ends_at")),
        "source_label": " ".join(str(value.get("source_label") or "exchange notice").split())[:100],
        "source_url": source_url,
        "derived": False,
    }


def _timestamp(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _active(event: dict[str, Any], now: datetime) -> bool:
    start = _parse(event.get("effective_at"))
    end = _parse(event.get("ends_at"))
    return (start is None or start <= now) and (end is None or end >= now)


def _parse(value: Any) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
