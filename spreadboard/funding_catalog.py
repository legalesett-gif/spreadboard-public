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
from contextlib import suppress
from pathlib import Path
from typing import Any

import orjson

from spreadboard import (
    api_spreads,
    bulk_quotes,
    catalog_pairs,
    chart_catalog,
    funding_radar,
    venue_funding_history,
)

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
PERSISTED_SCHEMA = "spreadboard.complete_funding_catalog.v1"
DEFAULT_CACHE_PATH = Path(
    os.environ.get(
        "SPREADBOARD_COMPLETE_FUNDING_CATALOG_PATH",
        str(chart_catalog.RUNTIME_DIR / "complete_funding_catalog.json"),
    )
)
_CACHE_RESTORE_ATTEMPTED = False
_CACHE_SAVED_AT: float | None = None
_CACHE_PERSIST_ERROR: str | None = None


def _read_persisted_cache() -> tuple[dict[str, dict[str, Any]], float]:
    """Decode and validate one atomically published catalogue envelope."""

    envelope = orjson.loads(DEFAULT_CACHE_PATH.read_bytes())
    payloads = envelope.get("payloads")
    saved_at = float(envelope.get("saved_at_unix") or 0.0)
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema") != PERSISTED_SCHEMA
        or not isinstance(payloads, dict)
        or not payloads
        or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in payloads.items()
        )
    ):
        raise ValueError("invalid_persisted_funding_catalog")
    return payloads, saved_at


def _restore_cache_unlocked() -> None:
    global _CACHE_PAYLOADS, _CACHE_AT, _CACHE_RESTORE_ATTEMPTED, _CACHE_SAVED_AT
    if _CACHE_RESTORE_ATTEMPTED:
        return
    _CACHE_RESTORE_ATTEMPTED = True
    try:
        payloads, saved_at = _read_persisted_cache()
    except (OSError, AttributeError, TypeError, ValueError, orjson.JSONDecodeError):
        return
    _CACHE_PAYLOADS = payloads
    _CACHE_AT = time.monotonic()
    _CACHE_SAVED_AT = saved_at or None


def _persist_cache(payloads: dict[str, dict[str, Any]]) -> None:
    """Atomically retain the all-token catalogue across app restarts."""

    path = DEFAULT_CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    saved_at = time.time()
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            # The catalogue is roughly 200 MB encoded and much larger as
            # Python objects. Serialising the whole envelope made another
            # 200 MB copy while the complete object graph was resident, which
            # pushed the bounded collector child over its cgroup limit. Each
            # token is an independent JSON value, so stream the envelope one
            # token at a time without changing its schema or reader contract.
            handle.write(
                b'{"schema":'
                + orjson.dumps(PERSISTED_SCHEMA)
                + b',"saved_at_unix":'
                + orjson.dumps(saved_at)
                + b',"payloads":{'
            )
            first = True
            for token, payload in payloads.items():
                if not first:
                    handle.write(b",")
                first = False
                handle.write(orjson.dumps(str(token)))
                handle.write(b":")
                handle.write(orjson.dumps(payload))
            handle.write(b"}}")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink()


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


def refresh_cache() -> dict[str, dict[str, Any]]:
    """Build the replacement generation for a background service worker."""

    return _complete_payloads(force_refresh=True)


def _complete_payloads(*, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """One coherent all-token generation from already-warm local artifacts."""

    global _CACHE_AT, _CACHE_PAYLOADS, _CACHE_BUILDING, _CACHE_SAVED_AT
    global _CACHE_PERSIST_ERROR
    now = time.monotonic()
    with _CACHE_LOCK:
        if not _CACHE_PAYLOADS:
            _restore_cache_unlocked()
        if (
            _CACHE_PAYLOADS
            and not force_refresh
            and now - _CACHE_AT <= CACHE_SECONDS
        ):
            return _CACHE_PAYLOADS
        previous = _CACHE_PAYLOADS
        # HTTP readers never become the owner of a multi-minute refresh after
        # a complete generation has been published. The service's explicit
        # refresh_cache() call owns that work; readers keep the last immutable
        # generation while it is replaced.
        if not force_refresh:
            return previous
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
            # Exact 1d/7d/30d windows are looked up from the independently
            # refreshed settlement archive at request time. Calling that
            # lookup for every candidate pair made this structural catalogue
            # take 9+ minutes and immediately froze its windows in a 200 MB
            # file. Keep the catalogue current-only; historical completeness
            # and blank-window semantics remain owned by funding_radar.
            include_history=False,
            include_short_spot=True,
        )
    except Exception:
        if not previous:
            raise
    finally:
        if built:
            try:
                _persist_cache(built)
                _CACHE_PERSIST_ERROR = None
            except Exception as exc:  # noqa: BLE001 - memory generation still publishes.
                # Persistence is an availability optimization. The coherent
                # in-memory generation is still safe to publish when disk is
                # temporarily unavailable.
                _CACHE_PERSIST_ERROR = f"{type(exc).__name__}: {str(exc)[:160]}"
        with _CACHE_LOCK:
            if built:
                _CACHE_PAYLOADS = built
                _CACHE_AT = time.monotonic()
                _CACHE_SAVED_AT = time.time()
            _CACHE_BUILDING = False
            _CACHE_BUILD_DONE.set()
    with _CACHE_LOCK:
        return _CACHE_PAYLOADS or previous


def restore_persisted_cache() -> dict[str, Any]:
    """Decode the last complete catalogue before the HTTP socket opens."""

    return status(restore=True)


def reload_persisted_cache() -> dict[str, Any]:
    """Atomically install a catalogue published by an isolated worker.

    A web process can start before the first catalogue file exists. Its initial
    restore attempt must not prevent a later background child from making the
    completed file live without another app restart.
    """

    global _CACHE_PAYLOADS, _CACHE_AT, _CACHE_RESTORE_ATTEMPTED, _CACHE_SAVED_AT
    global _CACHE_PERSIST_ERROR
    try:
        payloads, saved_at = _read_persisted_cache()
    except (OSError, AttributeError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
        with _CACHE_LOCK:
            _CACHE_PERSIST_ERROR = f"{type(exc).__name__}: {str(exc)[:160]}"
        return status()
    with _CACHE_LOCK:
        if not _CACHE_BUILDING:
            _CACHE_PAYLOADS = payloads
            _CACHE_AT = time.monotonic()
            _CACHE_RESTORE_ATTEMPTED = True
            _CACHE_SAVED_AT = saved_at or None
            _CACHE_PERSIST_ERROR = None
    return status()


def persisted_status() -> dict[str, Any]:
    """Describe the published catalogue without decoding its large payload.

    The collector owns publication but never reads catalogue rows.  A stat-only
    readiness check prevents that process from retaining roughly 1.5 GB of
    Python objects merely to decide whether the atomic file is due for refresh.
    """

    try:
        stat = DEFAULT_CACHE_PATH.stat()
    except OSError:
        return {
            "ready": False,
            "token_count": None,
            "saved_at_unix": None,
            "age_seconds": None,
            "building": False,
            "path": str(DEFAULT_CACHE_PATH),
            "persist_error": None,
        }
    return {
        "ready": stat.st_size > 0,
        "token_count": None,
        "saved_at_unix": stat.st_mtime,
        "age_seconds": max(0.0, time.time() - stat.st_mtime),
        "building": False,
        "path": str(DEFAULT_CACHE_PATH),
        "persist_error": None,
    }


def status(*, restore: bool = False) -> dict[str, Any]:
    with _CACHE_LOCK:
        if restore and not _CACHE_PAYLOADS:
            _restore_cache_unlocked()
        return {
            "ready": bool(_CACHE_PAYLOADS),
            "token_count": len(_CACHE_PAYLOADS),
            "saved_at_unix": _CACHE_SAVED_AT,
            "age_seconds": (
                max(0.0, time.time() - _CACHE_SAVED_AT)
                if _CACHE_SAVED_AT
                else None
            ),
            "building": _CACHE_BUILDING,
            "path": str(DEFAULT_CACHE_PATH),
            "persist_error": _CACHE_PERSIST_ERROR,
        }


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


def _live_current_value(
    route: dict[str, Any], funding: dict[str, dict[str, Any]]
) -> float | None:
    """Net daily carry from the current exact per-leg funding cache."""

    daily: dict[str, float] = {}
    has_futures = False
    for side in ("long", "short"):
        if str(route.get(f"{side}_market_type") or "") != "Futures":
            daily[side] = 0.0
            continue
        has_futures = True
        venue = str(route.get(f"{side}_venue") or "")
        symbol = str(
            route.get(f"{side}_market_symbol")
            or route.get(f"{side}_symbol")
            or ""
        )
        leg = funding.get(f"{venue}|{symbol}")
        if not isinstance(leg, dict):
            return None
        rate = _number(leg.get("rate_pct"))
        interval = _number(leg.get("interval_hours"))
        if rate is None or interval is None or interval <= 0:
            return None
        daily[side] = rate * 24.0 / interval
    return daily["short"] - daily["long"] if has_futures else None


def _apply_live_current_value(route: dict[str, Any], value: float | None) -> None:
    route["funding_daily_pct"] = value
    route["funding_projected_24h_pct"] = value
    route["funding_spread_pct"] = value
    route["funding_apr_pct"] = value * 365.0 if value is not None else None


def _live_current_age(
    route: dict[str, Any], funding: dict[str, dict[str, Any]]
) -> float | None:
    """Oldest exact futures-leg funding observation, in minutes."""

    ages: list[float] = []
    for side in ("long", "short"):
        if str(route.get(f"{side}_market_type") or "") != "Futures":
            continue
        venue = str(route.get(f"{side}_venue") or "")
        symbol = str(
            route.get(f"{side}_market_symbol")
            or route.get(f"{side}_symbol")
            or ""
        )
        leg = funding.get(f"{venue}|{symbol}")
        if not isinstance(leg, dict):
            return None
        age = _number(leg.get("age_seconds"))
        if age is None:
            return None
        ages.append(max(0.0, age) / 60.0)
    return max(ages) if ages else None


def _window_value(
    route: dict[str, Any],
    label: str,
    *,
    exact_legs: dict[str, dict[str, float | None]] | None = None,
) -> float | None:
    """Use an exact window already attached to this coherent generation."""

    # Production keeps exact venue settlements in a small independently
    # refreshed archive. Read that current rolling window at request time so a
    # durable catalogue restored after a restart never freezes yesterday's
    # 24h/7d/30d total. An incomplete exact leg intentionally remains blank.
    if os.environ.get("SPREADBOARD_SERVICE_ROLE", "").casefold() in {
        "web",
        "combined",
    }:
        return funding_radar.window_value(route, label, exact_legs=exact_legs)
    attached = (
        route.get("settled_funding_windows")
        if isinstance(route.get("settled_funding_windows"), dict)
        else {}
    )
    if route.get("catalog_history_loaded"):
        value = _number(attached.get(label))
        if value is not None:
            return value
    return funding_radar.window_value(route, label, exact_legs=exact_legs)


def _resident_live_overlay(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge ten-second books/carry into a durable structural catalogue."""

    if os.environ.get("SPREADBOARD_SERVICE_ROLE", "").casefold() not in {
        "web",
        "combined",
    }:
        return rows
    try:
        from spreadboard import warm_query_projection

        updates, status = warm_query_projection.LIVE_UNIVERSE.update_snapshot()
    except Exception:  # noqa: BLE001 - durable catalogue stays available.
        return rows
    if not status.get("ready") or not updates:
        return rows
    now = time.time()
    return [
        warm_query_projection._overlay(
            route,
            updates.get(str(route.get("route_key") or "")),
            now=now,
        )
        if str(route.get("route_key") or "") in updates
        else route
        for route in rows
    ]


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
    payloads: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    selected_payloads = payloads if payloads is not None else _complete_payloads()
    wanted_symbol = str(symbol or "").strip().upper()
    if wanted_symbol and wanted_symbol in selected_payloads:
        # Exact token detail is an O(routes-for-one-token) lookup.  Walking the
        # whole 100k-pair catalogue made a GUA page take 7-12 seconds even
        # though the required token payload was already resident.
        selected_payloads = {wanted_symbol: selected_payloads[wanted_symbol]}
    if not include_retained:
        # The bulk catalogue emits each exact leg pair once inside its token.
        # Economic-identity dedupe is required only when retained radar rows
        # are merged into a historical lane. Running that tuple construction
        # over 100k current candidates added ~1.6 seconds to every Now request.
        return [
            _copy_route(route, historical=False)
            for payload in selected_payloads.values()
            for route in payload.get("routes") or []
            if isinstance(route, dict)
            and _common_eligible(
                route,
                route_kind=route_kind,
                symbol=symbol,
                exchange=exchange,
                quote=quote,
            )
        ]
    rows: list[dict[str, Any]] = []
    live_identities: set[tuple[Any, ...]] = set()
    identity_indexes: dict[tuple[Any, ...], int] = {}
    for payload in selected_payloads.values():
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
            identity = catalog_pairs.route_identity(copied)
            if identity in identity_indexes:
                existing = rows[identity_indexes[identity]]
                for key, value in copied.items():
                    if key not in existing or existing.get(key) is None:
                        existing[key] = value
                continue
            identity_indexes[identity] = len(rows)
            live_identities.add(identity)
            rows.append(copied)
    if include_retained:
        for route in funding_radar.routes_for(
            symbol,
            route_kind=route_kind,
        ):
            identity = catalog_pairs.route_identity(route)
            if identity in live_identities or identity in identity_indexes or not _common_eligible(
                route,
                route_kind=route_kind,
                symbol=symbol,
                exchange=exchange,
                quote=quote,
            ):
                continue
            identity_indexes[identity] = len(rows)
            rows.append(_copy_route(route, historical=True))
    return rows


def _group(
    token: str,
    routes: list[tuple[float | None, dict[str, Any]]],
    *,
    window: str,
) -> dict[str, Any]:
    routes.sort(
        key=lambda item: item[0] if item[0] is not None else float("-inf"),
        reverse=True,
    )
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
                "best_funding_apr_pct": (
                    best_value * 365.0 if best_value is not None else None
                ),
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
    payloads = _complete_payloads()
    if not payloads:
        return {
            "ok": False,
            "status": "warming",
            "mode": "complete_funding_catalogue_background_warming",
            "window": selected_window,
            "window_value_kind": (
                "current_rate_projected_24h"
                if selected_window == "now"
                else "aggregate_exact_settlements"
            ),
            "now_is_independent": True,
            "groups": [],
            "rows": [],
            "matching_token_count": None,
            "matching_route_count": None,
            "returned_token_count": 0,
            "returned_route_count": 0,
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(500, int(limit or 25))),
        }
    wanted_symbol = str(symbol or "").strip().upper()
    exact_symbol_detail = bool(wanted_symbol and wanted_symbol in payloads)
    rows = _all_routes(
        route_kind=route_kind,
        symbol=symbol,
        exchange=exchange,
        quote=quote,
        include_retained=selected_window != "now" or exact_symbol_detail,
        payloads=payloads,
    )
    rows = _resident_live_overlay(rows)
    grouped: dict[str, list[tuple[float | None, dict[str, Any]]]] = {}
    history_labels = ("1d", "7d", "30d")
    window_routes = (
        {label: 0 for label in history_labels} if selected_window != "now" else {}
    )
    window_tokens = (
        {label: set() for label in history_labels}
        if selected_window != "now"
        else {}
    )
    production_reader = os.environ.get(
        "SPREADBOARD_SERVICE_ROLE", ""
    ).casefold() in {"web", "combined"}
    current_funding = bulk_quotes.load_funding() if production_reader else None
    if current_funding == {}:
        # Keep the last complete catalogue useful during a transient atomic
        # funding-handoff gap. Individual missing live legs remain unknown and
        # are never backfilled from stale values once the cache is populated.
        current_funding = None
    exact_legs = (
        venue_funding_history.load()
        if production_reader and selected_window != "now"
        else None
    )
    for route in rows:
        token = str(route.get("token") or "").upper()
        if not token:
            continue
        current_value = (
            _live_current_value(route, current_funding)
            if current_funding is not None
            else _current_value(route)
        )
        _apply_live_current_value(route, current_value)
        route["funding_age_min"] = (
            _live_current_age(route, current_funding)
            if current_funding is not None
            else route.get("funding_age_min")
        )
        if selected_window == "now":
            value = current_value
            if not exact_symbol_detail and (value is None or value <= 0):
                continue
            if (
                not exact_symbol_detail
                and min_abs_funding_24h_pct is not None
                and abs(value) < float(min_abs_funding_24h_pct)
            ):
                continue
            if (
                not exact_symbol_detail
                and min_abs_funding_apr_pct is not None
                and abs(value) * 365.0 < float(min_abs_funding_apr_pct)
            ):
                continue
        else:
            windows = {
                label: _window_value(route, label, exact_legs=exact_legs)
                for label in history_labels
            }
            for label, window_value in windows.items():
                if window_value is not None and window_value > 0:
                    window_routes[label] += 1
                    window_tokens[label].add(token)
            value = windows[selected_window]
            if not exact_symbol_detail and (value is None or value <= 0):
                continue
        grouped.setdefault(token, []).append((value, route))

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
        "exact_symbol_detail": exact_symbol_detail,
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


def build_navigation_pages(
    *,
    limit: int = 500,
    preview_limit: int = 3,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build every principal Funding lane in one exact all-route pass.

    Calling :func:`page` for twelve farm/window combinations walks the same
    100k-ish structural routes twelve times.  This collector-only builder loads
    live funding and exact settlements once, decorates each economic route
    once, then shares it between the four rankings for its farm.  Only the
    compact HTML preview is retained; exact token lookups and JSON export keep
    using the complete catalogue.
    """

    payloads = _complete_payloads()
    if not payloads:
        return {}
    kinds = ("FUTURES", "FUTURES-SPOT-PAIR", "DEX-FUTURES")
    rows = [
        route
        for kind in kinds
        for route in _all_routes(
            route_kind=kind,
            symbol=None,
            exchange=None,
            quote=None,
            include_retained=True,
            payloads=payloads,
        )
    ]
    current_funding = bulk_quotes.load_funding()
    if not current_funding:
        return {}
    exact_legs = venue_funding_history.load()
    windows = ("now", "1d", "7d", "30d")
    lanes: dict[
        tuple[str, str], dict[str, list[tuple[float | None, dict[str, Any]]]]
    ] = {(kind, window): {} for kind in kinds for window in windows}
    window_route_counts = {
        kind: {label: 0 for label in windows[1:]} for kind in kinds
    }
    window_tokens = {
        kind: {label: set() for label in windows[1:]} for kind in kinds
    }

    for route in rows:
        kind = next(
            (
                candidate
                for candidate in kinds
                if funding_radar.kind_matches(
                    str(route.get("route_kind") or ""), candidate
                )
            ),
            None,
        )
        token = str(route.get("token") or "").strip().upper()
        if kind is None or not token:
            continue
        current_value = _live_current_value(route, current_funding)
        _apply_live_current_value(route, current_value)
        route["funding_age_min"] = _live_current_age(route, current_funding)
        realised = {
            label: _window_value(route, label, exact_legs=exact_legs)
            for label in windows[1:]
        }
        # Preserve the exact generation used for ranking so rendering and the
        # selected headline cannot disagree during an atomic archive handoff.
        route["funding_navigation_windows"] = dict(realised)
        if not route.get("radar_historical") and current_value is not None and current_value > 0:
            lanes[(kind, "now")].setdefault(token, []).append(
                (current_value, route)
            )
        for label, value in realised.items():
            if value is None or value <= 0:
                continue
            window_route_counts[kind][label] += 1
            window_tokens[kind][label].add(token)
            lanes[(kind, label)].setdefault(token, []).append((value, route))

    page_limit = max(1, min(10_000, int(limit or 500)))
    route_preview = max(1, min(20, int(preview_limit or 3)))
    pages: dict[tuple[str, str], dict[str, Any]] = {}
    for kind in kinds:
        for window in windows:
            groups: list[dict[str, Any]] = []
            matching_routes = 0
            for token, candidates in lanes[(kind, window)].items():
                if not candidates:
                    continue
                group = _group(token, candidates, window=window)
                routes = list(group.get("routes") or [])
                matching_routes += len(routes)
                group["route_count"] = len(routes)
                group["displayed_route_count"] = min(len(routes), route_preview)
                group["routes"] = routes[:route_preview]
                group["materialized_route_preview"] = True
                groups.append(group)
            rank_field = (
                "best_funding_24h_pct"
                if window == "now"
                else "best_funding_window_pct"
            )
            groups.sort(
                key=lambda group: float(
                    group.get(rank_field) or float("-inf")
                ),
                reverse=True,
            )
            visible = groups[:page_limit]
            pages[(kind, window)] = {
                "ok": bool(visible),
                "mode": "persisted_exact_funding_ranked_before_pagination",
                "window": window,
                "window_value_kind": (
                    "current_rate_projected_24h"
                    if window == "now"
                    else "aggregate_exact_settlements"
                ),
                "window_duration_days": (
                    None
                    if window == "now"
                    else {"1d": 1, "7d": 7, "30d": 30}[window]
                ),
                "now_is_independent": True,
                "exact_symbol_detail": False,
                "groups": visible,
                "rows": [
                    route
                    for group in visible
                    for route in group.get("routes") or []
                ],
                "matching_token_count": len(groups),
                "matching_route_count": matching_routes,
                "returned_token_count": len(visible),
                "returned_route_count": sum(
                    len(group.get("routes") or []) for group in visible
                ),
                "offset": 0,
                "limit": page_limit,
                "largest_value": groups[0].get(rank_field) if groups else None,
                "window_route_counts": dict(window_route_counts[kind]),
                "window_token_counts": {
                    label: len(tokens)
                    for label, tokens in window_tokens[kind].items()
                },
            }
    return pages


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
