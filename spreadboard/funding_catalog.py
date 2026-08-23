"""Complete, globally ranked Funding-page pair catalogue.

The discovery snapshot is deliberately bounded per token.  It is a useful
index, but ranking that index before expanding its visible tokens biases both
the current and settled Funding lanes.  This module derives every currently
book-verified CEX pair from the shared market catalogue, live-book store and
per-leg funding files, then ranks the complete universe before token
pagination.  Historical lanes add retained routes only after exact settlement
windows have been validated; a missing window remains missing.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from spreadboard import api_spreads, catalog_pairs, chart_catalog, funding_radar

CACHE_SECONDS = max(
    30.0,
    float(os.environ.get("SPREADBOARD_COMPLETE_FUNDING_CACHE_SECONDS", "300")),
)
COLD_BUILD_WAIT_SECONDS = max(
    15.0,
    float(os.environ.get("SPREADBOARD_COMPLETE_FUNDING_COLD_WAIT_SECONDS", "120")),
)
_CACHE_LOCK = threading.Lock()
_CACHE_AT = 0.0
_CACHE_PAYLOADS: dict[str, dict[str, Any]] = {}
_CACHE_BUILDING = False
_CACHE_BUILD_DONE = threading.Event()
_CACHE_BUILD_DONE.set()


def clear_cache() -> None:
    """Make the current generation stale without taking it away from readers.

    Rebuilding every catalogue pair can take longer than an ordinary HTTP
    timeout on the production host.  A funding or discovery refresh must not
    turn that background rebuild into a page-wide lock: readers keep the last
    complete immutable generation until its replacement is ready.
    """

    global _CACHE_AT
    with _CACHE_LOCK:
        _CACHE_AT = 0.0


def _complete_payloads() -> dict[str, dict[str, Any]]:
    """One coherent all-token generation from already-warm local artifacts."""

    global _CACHE_AT, _CACHE_PAYLOADS, _CACHE_BUILDING
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE_PAYLOADS and now - _CACHE_AT <= CACHE_SECONDS:
            return _CACHE_PAYLOADS
        previous = _CACHE_PAYLOADS
        if _CACHE_BUILDING:
            owns_build = False
        else:
            _CACHE_BUILDING = True
            _CACHE_BUILD_DONE.clear()
            owns_build = True

    if not owns_build:
        # A member request must never wait behind a background all-market
        # rebuild when a complete prior generation exists.  On a genuinely
        # cold start there is no safe prior answer, so wait for the sole owner
        # rather than starting a duplicate multi-gigabyte build.
        if previous:
            return previous
        _CACHE_BUILD_DONE.wait(timeout=COLD_BUILD_WAIT_SECONDS)
        with _CACHE_LOCK:
            return _CACHE_PAYLOADS

    built: dict[str, dict[str, Any]] = {}
    try:
        catalog = chart_catalog.load()
        tokens = sorted(
            {
                str(item.get("token") or "").strip().upper()
                for item in catalog.get("markets") or []
                if isinstance(item, dict) and item.get("token")
            }
        )
        built = catalog_pairs.for_tokens(
            tokens,
            # Funding eligibility follows the fresh per-leg rate.  A book up
            # to the board's ordinary four-minute boundary may retain the
            # route and its identity while the UI honestly labels its basis as
            # refreshing; the stricter 90-second execution gate still controls
            # whether that basis is called current.
            max_age_seconds=api_spreads.DEFAULT_MAX_AGE_MIN * 60.0,
            include_history=True,
            include_short_spot=True,
        )
    except Exception:
        if not previous:
            raise
    finally:
        with _CACHE_LOCK:
            if built:
                _CACHE_PAYLOADS = built
                _CACHE_AT = time.monotonic()
            _CACHE_BUILDING = False
            _CACHE_BUILD_DONE.set()
    with _CACHE_LOCK:
        return _CACHE_PAYLOADS or previous


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kind_matches(route: dict[str, Any], wanted: str | None) -> bool:
    route_kind = str(route.get("route_kind") or "").upper()
    if wanted:
        return funding_radar.kind_matches(route_kind, wanted)
    return route_kind in {"FUTURES", "SPOT-FUTURES", "FUTURES-SPOT"}


def _common_eligible(
    route: dict[str, Any],
    *,
    route_kind: str | None,
    symbol: str | None,
    exchange: str | None,
    quote: str | None,
) -> bool:
    if not _kind_matches(route, route_kind):
        return False
    token = str(route.get("token") or "").upper()
    wanted_symbol = str(symbol or "").strip().upper()
    if wanted_symbol and wanted_symbol not in token:
        return False
    wanted_exchange = str(exchange or "").strip().casefold()
    if (
        wanted_exchange
        and wanted_exchange
        not in " ".join(
            str(route.get(key) or "") for key in ("long_venue", "short_venue")
        ).casefold()
    ):
        return False
    wanted_quote = str(quote or "").strip().upper()
    if wanted_quote and wanted_quote not in {
        str(route.get("long_quote") or "").upper(),
        str(route.get("short_quote") or "").upper(),
    }:
        return False
    guard = route.get("tokenized_guard") or {}
    return not (
        route.get("mirage_guarded")
        or route.get("identity_mismatch")
        or route.get("quote_mismatch")
        or route.get("deliverable") is False
        or (isinstance(guard, dict) and guard.get("rankable") is False)
    )


def _current_value(route: dict[str, Any]) -> float | None:
    for key in ("funding_daily_pct", "funding_projected_24h_pct"):
        value = _number(route.get(key))
        if value is not None:
            return value
    return None


def _window_value(route: dict[str, Any], label: str) -> float | None:
    """Use an exact window already attached to this coherent generation."""

    attached = (
        route.get("settled_funding_windows")
        if isinstance(route.get("settled_funding_windows"), dict)
        else {}
    )
    if route.get("catalog_history_loaded"):
        value = _number(attached.get(label))
        if value is not None:
            return value
    return funding_radar.window_value(route, label)


def _copy_route(route: dict[str, Any], *, historical: bool) -> dict[str, Any]:
    row = dict(route)
    if not historical:
        row["radar_historical"] = False
    inventory_required = (
        str(row.get("long_market_type") or "") == "Futures"
        and str(row.get("short_market_type") or "") == "Spot"
    )
    row["requires_existing_spot_inventory"] = inventory_required
    if inventory_required:
        row["execution_note"] = "Short spot inventory or borrow is required."
    return row


def _all_routes(
    *,
    route_kind: str | None,
    symbol: str | None,
    exchange: str | None,
    quote: str | None,
    include_retained: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    live_keys: set[str] = set()
    for payload in _complete_payloads().values():
        for route in payload.get("routes") or []:
            if not isinstance(route, dict) or not _common_eligible(
                route,
                route_kind=route_kind,
                symbol=symbol,
                exchange=exchange,
                quote=quote,
            ):
                continue
            copied = _copy_route(route, historical=False)
            rows.append(copied)
            if copied.get("route_key"):
                live_keys.add(str(copied["route_key"]))
    if include_retained:
        for route in funding_radar.routes_for(
            symbol,
            route_kind=route_kind,
        ):
            key = str(route.get("route_key") or "")
            if key in live_keys or not _common_eligible(
                route,
                route_kind=route_kind,
                symbol=symbol,
                exchange=exchange,
                quote=quote,
            ):
                continue
            rows.append(_copy_route(route, historical=True))
    return rows


def _group(
    token: str,
    routes: list[tuple[float, dict[str, Any]]],
    *,
    window: str,
) -> dict[str, Any]:
    routes.sort(key=lambda item: item[0], reverse=True)
    best_value, best = routes[0]
    rows = [route for _value, route in routes]
    venues = sorted(
        {
            str(route.get(key))
            for route in rows
            for key in ("long_venue", "short_venue")
            if route.get(key)
        }
    )
    route_kinds = sorted({str(route.get("route_kind") or "") for route in rows})
    group = {
        "token": token,
        "token_name": best.get("token_name"),
        "href": best.get("href") or f"/token/{token}",
        "coverage_mode": "complete_funding_catalogue",
        "venues": venues,
        "route_kinds": route_kinds,
        "route_count": len(rows),
        "displayed_route_count": len(rows),
        "routes": rows,
        "best_funding_route": best,
    }
    if window == "now":
        group.update(
            {
                "best_funding_24h_pct": best_value,
                "best_funding_apr_pct": best_value * 365.0,
                "best_funding_24h_basis": "projected_current_rate",
            }
        )
    else:
        group["best_funding_window_pct"] = best_value
        group["best_funding_window_aggregate_pct"] = best_value
    return group


def page(
    *,
    route_kind: str | None = None,
    window: str = "now",
    symbol: str | None = None,
    exchange: str | None = None,
    quote: str | None = None,
    min_abs_funding_24h_pct: float | None = None,
    min_abs_funding_apr_pct: float | None = None,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """Return complete funding groups, globally ranked then paginated."""

    selected_window = str(window or "now").casefold()
    if selected_window not in {"now", "1d", "7d", "30d"}:
        selected_window = "now"
    rows = _all_routes(
        route_kind=route_kind,
        symbol=symbol,
        exchange=exchange,
        quote=quote,
        include_retained=selected_window != "now",
    )
    grouped: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    window_routes = {label: 0 for label in ("1d", "7d", "30d")}
    window_tokens = {label: set() for label in ("1d", "7d", "30d")}
    for route in rows:
        token = str(route.get("token") or "").upper()
        if not token:
            continue
        for label in window_routes:
            value = _window_value(route, label)
            if value is not None and value > 0:
                window_routes[label] += 1
                window_tokens[label].add(token)
        if selected_window == "now":
            value = _current_value(route)
            if value is None or value <= 0:
                continue
            if min_abs_funding_24h_pct is not None and abs(value) < float(min_abs_funding_24h_pct):
                continue
            if min_abs_funding_apr_pct is not None and abs(value) * 365.0 < float(
                min_abs_funding_apr_pct
            ):
                continue
        else:
            value = _window_value(route, selected_window)
            if value is None or value <= 0:
                continue
        grouped.setdefault(token, []).append((float(value), route))

    groups = [
        _group(token, candidates, window=selected_window)
        for token, candidates in grouped.items()
        if candidates
    ]
    rank_field = "best_funding_24h_pct" if selected_window == "now" else "best_funding_window_pct"
    groups.sort(key=lambda group: float(group.get(rank_field) or float("-inf")), reverse=True)
    start = max(0, int(offset or 0))
    page_limit = max(1, min(500, int(limit or 25)))
    visible = groups[start : start + page_limit]
    return {
        "ok": bool(visible),
        "mode": "complete_funding_catalogue_ranked_before_pagination",
        "window": selected_window,
        "window_value_kind": (
            "current_rate_projected_24h"
            if selected_window == "now"
            else "aggregate_exact_settlements"
        ),
        "window_duration_days": (
            None
            if selected_window == "now"
            else {"1d": 1, "7d": 7, "30d": 30}[selected_window]
        ),
        "now_is_independent": True,
        "groups": visible,
        "rows": [route for group in visible for route in group.get("routes") or []],
        "matching_token_count": len(groups),
        "matching_route_count": sum(int(group.get("route_count") or 0) for group in groups),
        "returned_token_count": len(visible),
        "returned_route_count": sum(len(group.get("routes") or []) for group in visible),
        "offset": start,
        "limit": page_limit,
        "largest_value": (groups[0].get(rank_field) if groups else None),
        "window_route_counts": dict(window_routes),
        "window_token_counts": {label: len(tokens) for label, tokens in window_tokens.items()},
    }


def archive_routes() -> list[dict[str, Any]]:
    """Every current route worth retaining for a settled historical lane."""

    rows = _all_routes(
        route_kind=None,
        symbol=None,
        exchange=None,
        quote=None,
        include_retained=False,
    )
    retained: list[dict[str, Any]] = []
    for route in rows:
        current = _current_value(route)
        if (current is not None and current > 0) or any(
            (_window_value(route, label) or 0.0) > 0.0 for label in ("1d", "7d", "30d")
        ):
            retained.append(route)
    return retained
