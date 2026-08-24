"""Persistent radar of funding leaders that have cooled since the live scan.

The live board is deliberately strict: a route belongs in ``Now`` only while
its current carry, books, rails, and freshness pass the client-visible gates.
That must not make a route which paid strongly earlier today or this week
undiscoverable.  This small catalogue remembers the best live route for each
funding token, together with the settled 1d/7d/30d windows observed at that
time.  Historical views can therefore retain the leader without pretending
that its old rate or basis is executable now.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterable

from spreadboard import catalog_pairs, venue_funding_history

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))
DEFAULT_CACHE_PATH = RUNTIME_DIR / "funding_radar.json"
RETENTION_DAYS = 30
MAX_RECORDS = max(
    5_000, int(os.environ.get("SPREADBOARD_FUNDING_RADAR_RECORDS", "75000"))
)
SCHEMA = "spreadboard.funding_radar.v2"

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"stamp": None, "records": {}}

# Route fields needed by the funding page, Telegram, charts, and the explicit
# research warning.  Prefix fields retain exact leg symbols/URLs/cadence while
# avoiding a second copy of unrelated token metadata in the runtime cache.
_ROUTE_FIELDS = {
    "token",
    "token_name",
    "href",
    "route_key",
    "route_kind",
    "executable_spread_pct",
    "depth_weighted_spread_pct",
    "displayed_open_spread_pct",
    "funding_daily_pct",
    "funding_spread_pct",
    "funding_apr_pct",
    "funding_24h_pct",
    "funding_projected_24h_pct",
    "mirage_guarded",
    "blockers",
    "requires_existing_spot_inventory",
    "execution_note",
    "dex_chain",
    "dex_contract",
}

_LEG_SUFFIXES = {
    "venue",
    "market_type",
    "market_symbol",
    "quote",
    "funding_pct",
    "funding_interval_hours",
    "funding_interval_assumed",
    "next_funding_ts_us",
    "exchange_url",
    "dex_chain",
    "dex_contract",
}
_LEG_FIELDS = {
    f"{side}_{suffix}" for side in ("long", "short") for suffix in _LEG_SUFFIXES
}


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, tz=timezone.utc).replace(microsecond=0).isoformat()


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _route_snapshot(route: dict[str, Any]) -> dict[str, Any]:
    snapshot = {
        key: value
        for key, value in route.items()
        if key in _ROUTE_FIELDS or key in _LEG_FIELDS
    }
    # Round-trip through JSON so the cache can never receive a dataclass,
    # Decimal, or other process-only object from a future public row.
    return json.loads(json.dumps(snapshot, default=str))


def _window_snapshot(route: dict[str, Any]) -> dict[str, float | None]:
    venue = venue_funding_history.route_windows(route)
    return {label: _float_or_none(venue.get(label)) for label in ("1d", "7d", "30d")}


def _read(cache_path: Path | str) -> dict[str, Any]:
    path = Path(cache_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"records": {}}
    if payload.get("schema") != SCHEMA:
        return {"records": {}}
    records = payload.get("records")
    return payload if isinstance(records, dict) else {"records": {}}


def refresh(
    routes: Iterable[dict[str, Any]],
    *,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    now: float | None = None,
    retention_days: int = RETENTION_DAYS,
) -> int:
    """Merge current funding leaders into the 30-day radar catalogue."""
    moment = time.time() if now is None else float(now)
    cutoff = moment - max(1, int(retention_days)) * 86_400
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        records = dict((_read(path).get("records") or {}))
        for route in routes:
            if not isinstance(route, dict):
                continue
            key = str(route.get("route_key") or "").strip()
            token = str(route.get("token") or "").strip().upper()
            if not key or not token:
                continue
            previous = records.get(key) if isinstance(records.get(key), dict) else {}
            records[key] = {
                "route": _route_snapshot({**route, "token": token}),
                "windows": _window_snapshot(route),
                "first_seen_at": previous.get("first_seen_at") or _iso(moment),
                "last_seen_at": _iso(moment),
                "last_seen_ts": moment,
            }
        records = {
            key: record
            for key, record in records.items()
            if _float_or_none(record.get("last_seen_ts")) is not None
            and float(record["last_seen_ts"]) >= cutoff
        }
        # Route-key serialization changed over time. Compact identical economic
        # legs here so every radar consumer sees one route and breadth/counts
        # cannot be inflated by legacy plus CUSTOM key spellings.
        newest_by_identity: dict[tuple[Any, ...], tuple[str, dict[str, Any]]] = {}
        for key, record in records.items():
            route = record.get("route") if isinstance(record.get("route"), dict) else {}
            identity = catalog_pairs.route_identity(route)
            previous = newest_by_identity.get(identity)
            if previous is None or float(record.get("last_seen_ts") or 0.0) >= float(
                previous[1].get("last_seen_ts") or 0.0
            ):
                newest_by_identity[identity] = (key, record)
        records = {key: record for key, record in newest_by_identity.values()}
        if len(records) > MAX_RECORDS:
            newest = sorted(
                records.items(),
                key=lambda item: float(item[1].get("last_seen_ts") or 0.0),
                reverse=True,
            )[:MAX_RECORDS]
            records = dict(newest)
        payload = {
            "schema": SCHEMA,
            "updated_at": _iso(moment),
            "retention_days": max(1, int(retention_days)),
            "records": records,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
        _CACHE["stamp"] = None
        _CACHE["records"] = {}
    return len(records)


def load_records(*, cache_path: Path | str = DEFAULT_CACHE_PATH) -> dict[str, dict[str, Any]]:
    """Load the immutable last complete radar generation."""
    path = Path(cache_path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    with _LOCK:
        if _CACHE["stamp"] != (str(path), stamp):
            _CACHE["records"] = _read(path).get("records") or {}
            _CACHE["stamp"] = (str(path), stamp)
        return dict(_CACHE["records"])


def routes_for(
    symbol: str | None = None,
    *,
    route_kind: str | None = None,
    window: str | None = None,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Historical route snapshots, explicitly marked as non-live radar rows."""
    wanted = str(symbol or "").strip().upper()
    moment = time.time() if now is None else float(now)
    selected: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
    for record in load_records(cache_path=cache_path).values():
        route = record.get("route") if isinstance(record.get("route"), dict) else {}
        if wanted and str(route.get("token") or "").upper() != wanted:
            continue
        if route_kind and not kind_matches(str(route.get("route_kind") or ""), route_kind):
            continue
        windows = record.get("windows") if isinstance(record.get("windows"), dict) else {}
        if window and _float_or_none(windows.get(window)) is None:
            continue
        row = dict(route)
        row.update(
            {
                "radar_historical": True,
                "radar_windows": dict(windows),
                "radar_last_seen_at": record.get("last_seen_at"),
                "radar_last_seen_age_min": max(
                    0.0, (moment - float(record.get("last_seen_ts") or moment)) / 60.0
                ),
                # Old age/freshness values belong to the last live snapshot;
                # the radar state below is the only honest present status.
                "freshness": "historical",
                "status": "radar",
            }
        )
        identity = catalog_pairs.route_identity(row)
        seen = selected.get(identity)
        last_seen = float(record.get("last_seen_ts") or 0.0)
        if seen is None or last_seen >= seen[0]:
            selected[identity] = (last_seen, row)
    return [row for _last_seen, row in selected.values()]


def kind_matches(route_kind: str, requested_kind: str) -> bool:
    route_kind = str(route_kind).upper()
    requested_kind = str(requested_kind).upper()
    if requested_kind == "FUTURES-SPOT-PAIR":
        return route_kind in {"SPOT-FUTURES", "FUTURES-SPOT"}
    return route_kind == requested_kind


def window_value(route: dict[str, Any], label: str) -> float | None:
    """Settled carry for a live or retained route from exact venue events."""
    # Retention keeps the exact venue symbols, but its saved windows are only
    # the trailing totals observed when the route was last live. They must not
    # masquerade as today's rolling 24h/7d/30d periods for another 30 days.
    # Once a route has an identifiable CEX futures leg, the current exact
    # archive is authoritative even when the selected window is incomplete.
    has_exact_leg = any(
        str(route.get(f"{side}_market_type") or "").casefold() == "futures"
        and "dex" not in str(route.get(f"{side}_venue") or "").casefold()
        and bool(route.get(f"{side}_venue"))
        and bool(route.get(f"{side}_market_symbol") or route.get(f"{side}_symbol"))
        for side in ("long", "short")
    )
    if has_exact_leg:
        return _float_or_none(venue_funding_history.route_windows(route).get(label))
    radar = route.get("radar_windows") if isinstance(route.get("radar_windows"), dict) else {}
    return _float_or_none(radar.get(label))
