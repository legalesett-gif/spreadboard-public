"""Compact, always-warm rankings for individual assets.

The full market payload is intentionally large: tens of thousands of routes
and every field needed by the route detail pages.  A rankings page needs one
row per token, not another copy of that route universe.  This module builds the
small public artifact in a worker process and the web server only reads the
last complete generation.

Spread, current funding and settled funding windows remain separate metrics.
Combining them into one opaque score would make a high carry farm look like a
convergent spread (or vice versa), which is not what either number proves.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import quote

from spreadboard import api_spreads, catalog_pairs, chart_catalog, funding_radar


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_PATH = RUNTIME_DIR / "token_rankings.json"
SCHEMA = "spreadboard.token_rankings.v1"
WINDOWS = ("1d", "7d", "30d")

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"signature": None, "payload": None}


def build(
    *,
    board_path: Path | str,
    output_path: Path | str = DEFAULT_PATH,
    market_payload: dict[str, Any] | None = None,
    catalog_payload: dict[str, Any] | None = None,
    radar_routes: list[dict[str, Any]] | None = None,
    catalogue_summaries: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one truthful ranking row per current or retained token."""

    generated_at = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
    market = market_payload or api_spreads.load_spreads(
        board_path=board_path,
        include_stale=False,
        include_unverified=False,
        require_deliverable=True,
        sort_by="edge",
        direction="desc",
        limit=None,
    )
    catalog = catalog_payload or chart_catalog.load()
    retained = radar_routes if radar_routes is not None else funding_radar.routes_for()
    current_catalogue = (
        catalogue_summaries
        if catalogue_summaries is not None
        else catalog_pairs.all_token_summaries()
    )

    catalog_by_token: dict[str, list[dict[str, Any]]] = {}
    for item in catalog.get("markets") or []:
        if not isinstance(item, dict):
            continue
        token = _token(item.get("token"))
        if token:
            catalog_by_token.setdefault(token, []).append(item)

    records: dict[str, dict[str, Any]] = {
        token: _base_record(token, status="catalogued")
        for token in catalog_by_token
    }
    live_routes_by_token: dict[str, list[dict[str, Any]]] = {}
    current_dex_routes: dict[str, list[dict[str, Any]]] = {}
    for group in market.get("groups") or []:
        if not isinstance(group, dict):
            continue
        token = _token(group.get("token"))
        if not token:
            continue
        routes = [row for row in group.get("routes") or [] if isinstance(row, dict)]
        live_routes_by_token[token] = routes
        dex_routes = [dict(row) for row in routes if _is_dex_route(row)]
        if dex_routes:
            current_dex_routes[token] = dex_routes
        spread_route = group.get("best_route") if isinstance(group.get("best_route"), dict) else {}
        verified_spread_routes = [
            row
            for row in routes
            if not row.get("depth_unverified")
            and not row.get("mirage_guarded")
            and _number(row.get("depth_weighted_spread_pct")) is not None
        ]
        if verified_spread_routes:
            spread_route = max(
                verified_spread_routes,
                key=lambda row: _number(row.get("depth_weighted_spread_pct")) or float("-inf"),
            )
        # Current top-of-book still belongs on the token page, but it is not a
        # matched-$50 ranking observation until both ladders fill the probe.
        ranked_spread = (
            None
            if spread_route.get("depth_unverified")
            else _number(spread_route.get("depth_weighted_spread_pct"))
        )
        verified_funding_routes = [
            row
            for row in routes
            if not row.get("mirage_guarded") and _current_funding(row) is not None
        ]
        funding_route = (
            max(verified_funding_routes, key=lambda row: _current_funding(row) or float("-inf"))
            if verified_funding_routes
            else {}
        )
        funding_now = _current_funding(funding_route)
        records[token] = {
            **_base_record(token, status="live"),
            "token_name": group.get("token_name"),
            "live_route_count": len(routes),
            "live_venue_count": len(group.get("venues") or []),
            "route_kinds": list(group.get("route_kinds") or []),
            "best_spread_pct": ranked_spread,
            "best_spread_route": _route_summary(spread_route) if ranked_spread is not None else None,
            "funding_now_24h_pct": funding_now,
            "funding_now_basis": "projected_current_rate" if funding_now is not None else None,
            "best_funding_route": _route_summary(funding_route) if funding_now is not None else None,
            "age_min": _number(group.get("age_min")),
        }

    # Historical ranking is a union, not a replacement.  A cooled leader stays
    # discoverable without its last rate or basis being presented as live.
    history_candidates: dict[str, list[dict[str, Any]]] = {}
    for route in retained:
        if not isinstance(route, dict):
            continue
        token = _token(route.get("token"))
        if token:
            history_candidates.setdefault(token, []).append(route)
            if token not in live_routes_by_token:
                records[token] = {
                    **records.get(token, _base_record(token, status="cooled")),
                    "token_name": records.get(token, {}).get("token_name") or route.get("token_name"),
                    "status": "cooled",
                }

    # Overlay current full-catalogue quotes after canonical scanner rows.  This
    # does not replace canonical metadata; it fills tokens/pairs omitted solely
    # by the scanner quota.  For a CEX-only leader, the complete book catalogue
    # is newer and must replace the bounded scanner result even when the newer
    # value is lower. Otherwise a recently vanished edge remains ranked merely
    # because the old snapshot happened to be larger. DEX leaders remain in the
    # canonical lane because the CEX catalogue intentionally does not model
    # their stablecoin conversion path.
    for token, summary in current_catalogue.items():
        record = records.setdefault(token, _base_record(token, status="catalogued"))
        best_spread = _number(summary.get("best_spread_pct"))
        current_spread = _number(record.get("best_spread_pct"))
        current_spread_route = record.get("best_spread_route") or {}
        if best_spread is not None and (
            current_spread is None
            or not _is_dex_route(current_spread_route)
            or best_spread > current_spread
        ):
            record["best_spread_pct"] = best_spread
            record["best_spread_route"] = _route_summary(
                summary.get("best_spread_route") or {}
            )
        funding_now = _number(summary.get("funding_now_24h_pct"))
        current_funding = _number(record.get("funding_now_24h_pct"))
        current_funding_route = record.get("best_funding_route") or {}
        if funding_now is not None and (
            current_funding is None
            or not _is_dex_route(current_funding_route)
            or funding_now > current_funding
        ):
            record["funding_now_24h_pct"] = funding_now
            record["funding_now_basis"] = "projected_current_rate"
            record["best_funding_route"] = _route_summary(
                summary.get("best_funding_route") or {}
            )
        record["status"] = "live"
        record["live_route_count"] = max(
            int(record.get("live_route_count") or 0),
            int(summary.get("quoteable_pair_count") or 0),
        )
        record["live_venue_count"] = max(
            int(record.get("live_venue_count") or 0),
            int(summary.get("fresh_venue_count") or 0),
        )
        record["age_min"] = _number(summary.get("age_min"))

    for token, record in records.items():
        candidates = [
            *(live_routes_by_token.get(token) or []),
            *(history_candidates.get(token) or []),
            *(
                [current_catalogue[token].get("best_spread_route")]
                if token in current_catalogue
                and isinstance(current_catalogue[token].get("best_spread_route"), dict)
                else []
            ),
            *(
                [current_catalogue[token].get("best_funding_route")]
                if token in current_catalogue
                and isinstance(current_catalogue[token].get("best_funding_route"), dict)
                else []
            ),
        ]
        for label in WINDOWS:
            measured = [
                (funding_radar.window_value(route, label), route)
                for route in candidates
            ]
            measured = [
                (float(value), route)
                for value, route in measured
                if value is not None
            ]
            if measured:
                value, route = max(measured, key=lambda item: item[0])
                record["settled_windows"][label] = value
                record["settled_window_routes"][label] = _route_summary(route)

        markets = catalog_by_token.get(token) or []
        coverage = _catalog_coverage(markets)
        record.update(coverage)
        encoded = quote(token, safe="")
        record["chart_url"] = f"/charts?token={encoded}"
        record["token_url"] = f"/token/{encoded}"
        record["identity_warning"] = any(
            bool(route.get("mirage_guarded")) for route in live_routes_by_token.get(token) or []
        )

    health = (market.get("source_health") or {}).get("canonical_api") or {}
    payload = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "source_updated_at": health.get("updated_at"),
        "source_age_min": health.get("age_min"),
        "source_status": health.get("status"),
        "token_count": len(records),
        "live_token_count": sum(row.get("status") == "live" for row in records.values()),
        "cooled_token_count": sum(row.get("status") == "cooled" for row in records.values()),
        "catalogued_token_count": sum(row.get("status") == "catalogued" for row in records.values()),
        # DEX quotes come from the bounded identity-aware scanner rather than
        # the CEX ticker catalogue. Keep their full current rows in the same
        # atomic generation so obscure token pages and Telegram lookups do not
        # lose DEX coverage merely because the top-500 navigation view omitted
        # that token. They are deliberately outside `records`, so the compact
        # rankings API does not send route payloads for every table row.
        "current_dex_routes": current_dex_routes,
        "records": sorted(records.values(), key=lambda row: str(row.get("token") or "")),
    }
    _write_atomic(Path(output_path), payload)
    return payload


def load(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    """Read the last complete generation, cached by file signature."""

    candidate = Path(path)
    try:
        stat = candidate.stat()
        signature = (str(candidate.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return _empty_payload()
    with _CACHE_LOCK:
        if _CACHE["signature"] == signature and isinstance(_CACHE["payload"], dict):
            return _CACHE["payload"]
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_payload()
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return _empty_payload()
    with _CACHE_LOCK:
        _CACHE.update({"signature": signature, "payload": payload})
    return payload


def ranked(
    payload: dict[str, Any],
    *,
    metric: str = "spread",
    query: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Filter and rank without rebuilding any market data."""

    normalized_metric = metric if metric in {"spread", "funding", *WINDOWS, "coverage", "token"} else "spread"
    needle = str(query or "").upper().strip()
    wanted_status = str(status or "").casefold().strip()
    rows = [row for row in payload.get("records") or [] if isinstance(row, dict)]
    if needle:
        rows = [
            row
            for row in rows
            if needle in f"{row.get('token') or ''} {row.get('token_name') or ''}".upper()
        ]
    if wanted_status in {"live", "cooled", "catalogued"}:
        rows = [row for row in rows if str(row.get("status") or "").casefold() == wanted_status]

    def metric_value(row: dict[str, Any]) -> Any:
        if normalized_metric == "token":
            return str(row.get("token") or "")
        if normalized_metric == "spread":
            return _number(row.get("best_spread_pct"))
        if normalized_metric == "funding":
            return _number(row.get("funding_now_24h_pct"))
        if normalized_metric == "coverage":
            return _number(row.get("catalog_pair_count"))
        return _number((row.get("settled_windows") or {}).get(normalized_metric))

    if normalized_metric == "token":
        rows.sort(key=lambda row: str(row.get("token") or ""))
    else:
        rows.sort(
            key=lambda row: (
                metric_value(row) is not None,
                metric_value(row) if metric_value(row) is not None else float("-inf"),
                row.get("status") == "live",
                str(row.get("token") or ""),
            ),
            reverse=True,
        )
    return rows[: max(1, min(500, int(limit)))]


def age_seconds(payload: dict[str, Any], *, now: float | None = None) -> float | None:
    text = str(payload.get("generated_at") or "")
    if not text:
        return None
    try:
        generated = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return max(0.0, (time.time() if now is None else float(now)) - generated)


def dex_routes_for(
    payload: dict[str, Any], token: str, *, now: float | None = None
) -> list[dict[str, Any]]:
    """Current DEX routes for one token, with the live freshness bound reapplied."""

    moment = time.time() if now is None else float(now)
    max_age_seconds = max(
        60.0,
        float(os.environ.get("SPREADBOARD_LIVE_MAX_AGE_MIN", "5")) * 60.0,
    )
    output: list[dict[str, Any]] = []
    routes = (payload.get("current_dex_routes") or {}).get(_token(token)) or []
    for route in routes:
        if not isinstance(route, dict):
            continue
        quote_ts_us = _number(route.get("quote_ts_us"))
        if quote_ts_us is not None:
            age = moment - quote_ts_us / 1_000_000.0
        else:
            route_age = _number(route.get("age_min"))
            artifact_age = age_seconds(payload, now=moment)
            age = (
                route_age * 60.0 + artifact_age
                if route_age is not None and artifact_age is not None
                else float("inf")
            )
        if 0.0 <= age <= max_age_seconds:
            output.append(dict(route))
    return output


def _catalog_coverage(markets: list[dict[str, Any]]) -> dict[str, Any]:
    unique_by_key = {
        (
            str(item.get("venue") or ""),
            _catalog_kind(item),
            str(item.get("symbol") or ""),
        ): item
        for item in markets
        if item.get("venue") and item.get("symbol")
    }
    unique = list(unique_by_key)
    counts = {
        kind: sum(item[1] == kind for item in unique)
        for kind in ("spot", "futures", "dex")
    }
    lane_pairs = {
        "futures_futures": 0,
        "futures_spot": 0,
        "spot_spot": 0,
        "futures_dex": 0,
        "spot_dex": 0,
    }
    for left_index, left in enumerate(unique):
        for right in unique[left_index + 1 :]:
            left_kind, right_kind = left[1], right[1]
            if left[0] == right[0] and left_kind == right_kind:
                continue
            if "dex" not in {left_kind, right_kind}:
                left_quote = _catalog_quote(unique_by_key[left])
                right_quote = _catalog_quote(unique_by_key[right])
                if left_quote and right_quote and left_quote != right_quote:
                    continue
            kinds = {left_kind, right_kind}
            if kinds == {"futures"}:
                lane_pairs["futures_futures"] += 1
            elif kinds == {"futures", "spot"}:
                lane_pairs["futures_spot"] += 1
            elif kinds == {"spot"}:
                lane_pairs["spot_spot"] += 1
            elif kinds == {"futures", "dex"}:
                lane_pairs["futures_dex"] += 1
            elif kinds == {"spot", "dex"}:
                lane_pairs["spot_dex"] += 1
    return {
        "catalog_market_count": len(unique),
        "catalog_venue_count": len({item[0] for item in unique}),
        "catalog_pair_count": sum(lane_pairs.values()),
        "catalog_lane_pairs": lane_pairs,
        "catalog_market_types": counts,
    }


def _base_record(token: str, *, status: str) -> dict[str, Any]:
    return {
        "token": token,
        "token_name": None,
        "status": status,
        "live_route_count": 0,
        "live_venue_count": 0,
        "route_kinds": [],
        "best_spread_pct": None,
        "best_spread_route": None,
        "funding_now_24h_pct": None,
        "funding_now_basis": None,
        "best_funding_route": None,
        "age_min": None,
        "settled_windows": {label: None for label in WINDOWS},
        "settled_window_routes": {label: None for label in WINDOWS},
    }


def _catalog_kind(item: dict[str, Any]) -> str:
    if "dex" in str(item.get("venue") or "").casefold():
        return "dex"
    return "futures" if str(item.get("market_type") or "").casefold() == "futures" else "spot"


def _catalog_quote(item: dict[str, Any]) -> str:
    value = str(item.get("quote") or "").upper().strip()
    if value:
        return value
    symbol = str(item.get("symbol") or "").upper()
    return symbol.split("/", 1)[1].split(":", 1)[0] if "/" in symbol else ""


def _is_dex_route(route: dict[str, Any]) -> bool:
    if "DEX" in str(route.get("route_kind") or "").upper():
        return True
    return any(
        "DEX" in str(route.get(key) or "").upper()
        for key in ("long_venue", "short_venue")
    )


def _route_summary(route: dict[str, Any]) -> dict[str, Any] | None:
    if not route:
        return None
    return {
        key: route.get(key)
        for key in (
            "route_key",
            "route_kind",
            "long_venue",
            "long_market_type",
            "long_market_symbol",
            "short_venue",
            "short_market_type",
            "short_market_symbol",
            "executable_spread_pct",
            "depth_weighted_spread_pct",
            "depth_unverified",
            "funding_24h_pct",
            "funding_projected_24h_pct",
            "mirage_guarded",
        )
    }


def _token(value: Any) -> str:
    return str(value or "").strip().upper()


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _current_funding(route: dict[str, Any]) -> float | None:
    for key in ("funding_projected_24h_pct", "funding_daily_pct", "funding_spread_pct"):
        value = _number(route.get(key))
        if value is not None:
            return value
    return None


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
    with _CACHE_LOCK:
        _CACHE.update({"signature": None, "payload": None})


def _empty_payload() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "generated_at": None,
        "source_updated_at": None,
        "source_age_min": None,
        "source_status": "warming",
        "token_count": 0,
        "live_token_count": 0,
        "cooled_token_count": 0,
        "catalogued_token_count": 0,
        "current_dex_routes": {},
        "records": [],
    }
