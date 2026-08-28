"""Fast arbitrary market queries over the last complete materialized universe.

The materialized navigation generation contains every canonical route, while
the principal page payloads cover the common tabs.  A search or filter that did
not exactly match one of those tabs used to fall back to ``load_spreads`` and
make the HTTP request parse and group the full discovery snapshot.  This module
keeps one atomically refreshed live overlay for the complete route index and
projects arbitrary filters from it without network I/O or discovery parsing.
"""

from __future__ import annotations

import copy
import statistics
import threading
import time
from collections import Counter
from collections.abc import Iterable
from typing import Any

from spreadboard import api_spreads

DEFAULT_REFRESH_SECONDS = 10.0


class LiveRouteUniverse:
    """Process-local, atomically replaced economics for materialized routes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._template: dict[str, Any] | None = None
        self._headlines: dict[str, Any] = {}
        self._updates: dict[str, tuple[Any, ...]] = {}
        self._installed_generation = 0
        self._refreshed_at = 0.0
        self._refresh_seconds: float | None = None
        self._last_error: str | None = None

    def install(
        self,
        rows: dict[str, dict[str, Any]],
        *,
        template: dict[str, Any] | None = None,
    ) -> None:
        """Install a complete immutable structural generation by reference."""

        with self._lock:
            retained_updates = {
                key: value for key, value in self._updates.items() if key in rows
            }
            self._rows = rows
            self._template = template
            self._updates = retained_updates
            self._installed_generation += 1
            if not retained_updates:
                self._refreshed_at = 0.0
                self._refresh_seconds = None
                self._headlines = {}
            self._last_error = None

    def refresh(self) -> dict[str, Any]:
        """Build one complete live update map and swap it in atomically."""

        with self._refresh_lock:
            with self._lock:
                rows = self._rows
                generation = self._installed_generation
                previous_updates = self._updates
            if not rows:
                return self.status()
            started = time.monotonic()
            try:
                observed_updates = api_spreads.live_route_updates_for(
                    list(rows.values()), include_basis=True
                )
                updates = _merge_live_updates(
                    previous_updates,
                    observed_updates,
                    route_keys=set(rows),
                    now=time.time(),
                )
                headlines = _build_headlines(
                    tuple(rows.values()), updates, now=time.time()
                )
            except Exception as exc:  # noqa: BLE001 - retain the previous good map.
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                return self.status()
            elapsed = time.monotonic() - started
            with self._lock:
                # A new materialized generation may have arrived during the read.
                if generation == self._installed_generation and rows is self._rows:
                    self._updates = updates
                    self._headlines = headlines
                    self._refreshed_at = time.time()
                    self._refresh_seconds = elapsed
                    self._last_error = None
        return self.status()

    def snapshot(
        self,
    ) -> tuple[tuple[dict[str, Any], ...], dict[str, tuple[Any, ...]], dict[str, Any]]:
        with self._lock:
            rows = tuple(self._rows.values())
            updates = self._updates
            status = self._status_unlocked()
        return rows, updates, status

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_unlocked()

    def template(self) -> dict[str, Any] | None:
        with self._lock:
            return self._template

    def headlines(self) -> dict[str, Any]:
        with self._lock:
            return self._headlines

    def update_snapshot(
        self,
    ) -> tuple[dict[str, tuple[Any, ...]], dict[str, Any]]:
        """Expose the immutable live map without copying all structural rows."""

        with self._lock:
            return self._updates, self._status_unlocked()

    def target_rows(
        self,
        *,
        route_keys: Iterable[str] = (),
        tokens: Iterable[str] = (),
        route_kinds: Iterable[str] = (),
        all_rows: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return current copies only for subscriber-tracked routes/assets.

        Alerts, saved charts and account surfaces should never rebuild or copy
        the complete route universe just to inspect a handful of targets.  The
        structural map and live update map are swapped atomically, so this is
        one bounded dictionary/filter pass with no discovery parsing or public
        API I/O.  Both current and cooled structural rows are returned; callers
        decide whether stale data is useful for their metric.
        """

        wanted_keys = {str(value) for value in route_keys if str(value)}
        wanted_tokens = {
            str(value).strip().upper() for value in tokens if str(value).strip()
        }
        wanted_kinds = {
            str(value).strip().upper() for value in route_kinds if str(value).strip()
        }
        with self._lock:
            rows = self._rows
            updates = self._updates
            status = self._status_unlocked()
        if not rows or not status.get("ready"):
            return [], status
        now = time.time()
        selected = [
            _overlay(row, updates.get(key), now=now)
            for key, row in rows.items()
            if all_rows
            or key in wanted_keys
            or str(row.get("token") or "").upper() in wanted_tokens
            or str(row.get("route_kind") or "").upper() in wanted_kinds
        ]
        return selected, status

    def _status_unlocked(self) -> dict[str, Any]:
        age = max(0.0, time.time() - self._refreshed_at) if self._refreshed_at else None
        return {
            "ready": bool(self._rows and self._refreshed_at),
            "route_count": len(self._rows),
            "updated_route_count": len(self._updates),
            "age_seconds": age,
            "refresh_seconds": self._refresh_seconds,
            "generation": self._installed_generation,
            "last_error": self._last_error,
        }


LIVE_UNIVERSE = LiveRouteUniverse()


def _merge_live_updates(
    previous: dict[str, tuple[Any, ...]],
    observed: dict[str, tuple[Any, ...]],
    *,
    route_keys: set[str],
    now: float,
) -> dict[str, tuple[Any, ...]]:
    """Retain a still-current quote across a partial live-book pass.

    ``live_route_updates_for`` deliberately emits a funding-only tuple when a
    route is missing either exact book in the current read. Replacing the
    complete map with that tuple immediately erased a quote that could still
    have 60-80 seconds left inside the strict freshness boundary. The reader
    would alternate between thousands of routes and an empty farm as venue
    sweeps crossed each other.

    Keep only the previous *price* and its original timestamp/basis while it is
    still current; take funding from the new observation, including ``None``.
    Nothing is re-timestamped or extended, so the ordinary 90-second gate still
    removes the route exactly on schedule.
    """

    merged: dict[str, tuple[Any, ...]] = {}
    for key in route_keys:
        current = observed.get(key)
        prior = previous.get(key)
        if current is not None and len(current) >= 3:
            current_spread = current[0]
            current_timestamp = current[2]
            if current_spread is not None and current_timestamp is not None:
                merged[key] = current
                continue
        if prior is not None and len(prior) >= 3:
            prior_spread = prior[0]
            prior_timestamp = prior[2]
            try:
                prior_age = now - float(prior_timestamp) / 1_000_000.0
            except (TypeError, ValueError, OverflowError):
                prior_age = float("inf")
            if (
                prior_spread is not None
                and -1.0 <= prior_age <= api_spreads.LIVE_BOOK_MAX_AGE_SECONDS
            ):
                current_funding = current[1] if current is not None and len(current) > 1 else None
                prior_basis = prior[3] if len(prior) > 3 else None
                merged[key] = (
                    prior_spread,
                    current_funding,
                    prior_timestamp,
                    prior_basis,
                )
                continue
        if current is not None:
            merged[key] = current
    return merged


class Worker(threading.Thread):
    """Continuously refresh the complete live overlay outside HTTP requests."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        interval_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        super().__init__(name="materialized-live-route-universe", daemon=True)
        self.stop_event = stop_event
        self.interval_seconds = max(2.0, float(interval_seconds))

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            LIVE_UNIVERSE.refresh()
            remaining = max(0.0, self.interval_seconds - (time.monotonic() - started))
            self.stop_event.wait(remaining)


def project(
    query: dict[str, list[str]],
    *,
    template: dict[str, Any],
    limit: int,
    offset: int,
    require_deliverable: bool = True,
) -> dict[str, Any] | None:
    """Return the exact query projection from the continuously live universe."""

    structural, updates, live_status = LIVE_UNIVERSE.snapshot()
    headlines = LIVE_UNIVERSE.headlines()
    if not structural or not live_status.get("ready"):
        return None
    filters = _filters(query, limit=limit, offset=offset)
    now = time.time()
    candidates = [
        _overlay(row, updates.get(str(row.get("route_key") or "")), now=now)
        for row in structural
        if _matches_structural(row, filters)
    ]
    dynamically_matching = [
        row
        for row in candidates
        if _matches_dynamic(row, filters)
    ]
    outliers = _unverifiable_price_outliers(dynamically_matching)
    filtered = [
        row
        for row in dynamically_matching
        if _presentable(
            row,
            filters,
            require_deliverable=require_deliverable,
            unverifiable_outliers=outliers,
        )
    ]
    sort_by = str(filters["sort"])
    reverse = filters["direction"] == "desc"
    filtered.sort(key=lambda row: api_spreads._route_dict_sort_value(row, sort_by), reverse=reverse)
    groups = _group_rows(filtered, evidence=str(filters.get("evidence") or "all"))
    for group in groups:
        (group.get("routes") or []).sort(
            key=lambda row: api_spreads._route_dict_sort_value(row, sort_by),
            reverse=reverse,
        )
    groups.sort(key=lambda group: api_spreads._group_sort_value(group, sort_by), reverse=reverse)
    visible_groups = groups[offset : offset + limit]
    api_spreads.attach_funding_history(visible_groups)
    visible_keys = {
        str(route.get("route_key") or "")
        for group in visible_groups
        for route in group.get("routes") or []
    }
    visible_rows = [
        row for row in filtered if str(row.get("route_key") or "") in visible_keys
    ]
    payload = {
        "ok": bool(template.get("ok", True)),
        "mode": "materialized_live_query_projection",
        "filters": filters,
        "summary": _summary(
            structural,
            filtered,
            visible_rows,
            groups=groups,
            visible_groups=visible_groups,
            offset=offset,
            limit=limit,
        ),
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returned_rows": len(visible_groups),
            "matching_rows": len(groups),
            "has_previous": offset > 0,
            "has_more": offset + len(visible_groups) < len(groups),
        },
        "source_health": copy.deepcopy(template.get("source_health") or {}),
        "exchange_options": list(
            headlines.get("exchange_options")
            or template.get("exchange_options")
            or []
        ),
        "route_kind_counts": dict(
            headlines.get("route_kind_counts")
            or template.get("route_kind_counts")
            or {}
        ),
        "asset_class_counts": dict(
            headlines.get("asset_class_counts")
            or template.get("asset_class_counts")
            or {}
        ),
        "route_kind_token_counts": dict(
            headlines.get("route_kind_token_counts")
            or template.get("route_kind_token_counts")
            or {}
        ),
        "lane_token_counts": dict(
            headlines.get("lane_token_counts")
            or template.get("lane_token_counts")
            or {}
        ),
        "top_edges": copy.deepcopy(
            headlines.get("top_edges") or template.get("top_edges") or []
        ),
        "top_funding": copy.deepcopy(
            headlines.get("top_funding") or template.get("top_funding") or []
        ),
        "groups": visible_groups,
        "rows": visible_rows,
        "materialized_live_universe": live_status,
    }
    canonical = ((payload.get("source_health") or {}).get("canonical_api") or {})
    if isinstance(canonical, dict):
        canonical["materialized_live_projection"] = live_status
    return payload


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    return str(values[0]) if values else None


def _number(query: dict[str, list[str]], key: str) -> float | None:
    try:
        value = _first(query, key)
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _truthy(query: dict[str, list[str]], key: str) -> bool:
    return str(_first(query, key) or "").casefold() in {"1", "true", "yes", "on"}


def _filters(
    query: dict[str, list[str]], *, limit: int, offset: int
) -> dict[str, Any]:
    min_funding_24h = _number(query, "min_abs_funding_24h_pct")
    min_funding_apr = _number(query, "min_abs_funding_apr_pct")
    if min_funding_24h is None and min_funding_apr is not None:
        min_funding_24h = min_funding_apr / 365.0
    sort_by = api_spreads._normalize_sort(_first(query, "sort") or "edge")
    return {
        "q": _first(query, "q"),
        "exchange": _first(query, "exchange"),
        "kind": _first(query, "kind"),
        "source": "public_api",
        "min_spread_pct": _number(query, "min_spread_pct"),
        "min_abs_funding_24h_pct": min_funding_24h,
        "min_abs_funding_apr_pct": min_funding_apr,
        "quote": _first(query, "quote"),
        "min_volume_24h_usd": _number(query, "min_volume_24h_usd"),
        "min_market_cap_usd": _number(query, "min_market_cap_usd"),
        "max_market_cap_usd": _number(query, "max_market_cap_usd"),
        "min_fdv_usd": _number(query, "min_fdv_usd"),
        "max_fdv_usd": _number(query, "max_fdv_usd"),
        "max_listing_age_days": _number(query, "max_listing_age_days"),
        "persistence": _first(query, "persistence"),
        "asset_class": _first(query, "asset_class"),
        "funding_only": _truthy(query, "funding_only"),
        "evidence": str(_first(query, "evidence") or "all").casefold(),
        "include_stale": _truthy(query, "include_stale"),
        # Markets has one admissible list: both matched and indicative rows are
        # visible after the shared evidence classifier rejects known-bad data.
        # This internal switch is retained only for funding/legacy callers.
        "include_unverified": (
            True
            if not _truthy(query, "funding_only")
            else _truthy(query, "include_unverified")
        ),
        "max_age_min": _number(query, "max_age_min"),
        "sort": sort_by,
        "direction": "asc" if str(_first(query, "direction") or "desc").casefold() == "asc" else "desc",
        "offset": offset,
        "limit": limit,
    }


def _route_volume(row: dict[str, Any]) -> float | None:
    values = [
        value
        for value in (
            api_spreads._float_or_none(row.get("long_volume_24h_usd")),
            api_spreads._float_or_none(row.get("short_volume_24h_usd")),
        )
        if value is not None and value >= 0
    ]
    return min(values) if len(values) == 2 else None


def _matches_structural(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    q = str(filters.get("q") or "").upper().strip()
    if q and q not in f"{row.get('token') or ''} {row.get('token_name') or ''}".upper():
        return False
    exchange = str(filters.get("exchange") or "").casefold().strip()
    if exchange and exchange not in " ".join(
        str(row.get(key) or "") for key in ("long_venue", "short_venue")
    ).casefold():
        return False
    kind = api_spreads._normalize_kind_filter(filters.get("kind"))
    row_kind = str(row.get("route_kind") or "")
    if kind == "FUTURES-SPOT-PAIR" and row_kind not in {"FUTURES-SPOT", "SPOT-FUTURES"}:
        return False
    if kind and kind != "FUTURES-SPOT-PAIR" and row_kind != kind:
        return False
    asset_class = str(filters.get("asset_class") or "").casefold().strip()
    if asset_class and str(row.get("asset_class") or "").casefold() != asset_class:
        return False
    quote = str(filters.get("quote") or "").upper().strip()
    if quote and quote not in {
        str(row.get("long_quote") or "").upper(),
        str(row.get("short_quote") or "").upper(),
    }:
        return False
    route_volume = _route_volume(row)
    checks = (
        ("min_volume_24h_usd", route_volume, "min"),
        ("min_market_cap_usd", api_spreads._float_or_none(row.get("market_cap_usd")), "min"),
        ("max_market_cap_usd", api_spreads._float_or_none(row.get("market_cap_usd")), "max"),
        ("min_fdv_usd", api_spreads._float_or_none(row.get("fdv_usd")), "min"),
        ("max_fdv_usd", api_spreads._float_or_none(row.get("fdv_usd")), "max"),
        ("max_listing_age_days", api_spreads._float_or_none(row.get("listing_age_days")), "max"),
    )
    for name, value, direction in checks:
        threshold = filters.get(name)
        if threshold is None:
            continue
        if value is None or (direction == "min" and value < threshold) or (
            direction == "max" and value > threshold
        ):
            return False
    return True


def _overlay(
    source: dict[str, Any], update: tuple[Any, ...] | None, *, now: float
) -> dict[str, Any]:
    row = dict(source)
    if update is not None:
        spread, funding, quote_ts_us, basis = update
        if spread is not None and quote_ts_us is not None:
            row["quote_ts_us"] = quote_ts_us
            if basis in {"matched_vwap", "retained_matched_vwap"}:
                row["depth_weighted_spread_pct"] = spread
                row["depth_unverified"] = False
                row["matched_size_notional_usd"] = api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD
                row["depth_usd"] = api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD
            elif basis == "top_book":
                row["executable_spread_pct"] = spread
                row["displayed_open_spread_pct"] = spread
                row["depth_weighted_spread_pct"] = None
                row["depth_unverified"] = True
                row["matched_size_notional_usd"] = None
        row["funding_daily_pct"] = funding
        row["funding_projected_24h_pct"] = funding
        row["funding_apr_pct"] = funding * 365.0 if funding is not None else None
    age = api_spreads.quote_age_min(row, now=now)
    row["age_min"] = age
    row["freshness"] = (
        "fresh"
        if age is not None and 0 <= age <= api_spreads.DEFAULT_MAX_AGE_MIN
        else "stale"
    )
    row["spread_quote_current"] = api_spreads.spread_quote_current(row, now=now)
    return row


def _matches_dynamic(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    if not filters.get("include_stale") and row.get("freshness") == "stale":
        return False
    persistence = str(filters.get("persistence") or "").casefold().strip()
    if persistence:
        windows = api_spreads.venue_funding_history.route_windows(row)
        values = [float(value) for value in windows.values() if value is not None]
        state = (
            "insufficient"
            if len(values) < 2
            else "persistent"
            if all(value > 0 for value in values)
            else "reversing"
            if all(value <= 0 for value in values)
            else "mixed"
        )
        if state != persistence:
            return False
    spread = api_spreads._entrance_spread_dict(row)
    if (
        not filters.get("funding_only")
        and filters.get("min_spread_pct") is not None
        and spread < float(filters["min_spread_pct"])
    ):
        return False
    funding = api_spreads._effective_funding_24h_dict(row)
    if filters.get("funding_only") and not (funding is not None and funding > 0):
        return False
    threshold = filters.get("min_abs_funding_24h_pct")
    return threshold is None or abs(funding or 0.0) >= float(threshold)


def _presentable(
    row: dict[str, Any],
    filters: dict[str, Any],
    *,
    require_deliverable: bool,
    unverifiable_outliers: set[str],
) -> bool:
    funding_only = bool(filters.get("funding_only"))
    evidence = str(filters.get("evidence") or "all")
    if not funding_only:
        state = api_spreads.spread_evidence_state(row)
        if evidence == "research":
            return state == "research"
        if evidence == "verified":
            return state == "verified"
        if state not in {"verified", "research"}:
            return False
        # The evidence classifier is the single visibility boundary for the
        # unified Markets list. The retired include_unverified branch below
        # must not silently recreate two lists inside the warm projection.
        return str(row.get("route_key") or "") not in unverifiable_outliers
    if filters.get("include_unverified"):
        return True
    if (
        row.get("identity_mismatch")
        or row.get("thin_book")
        or str(row.get("route_key") or "") in unverifiable_outliers
    ):
        return False
    token = str(row.get("token") or "").upper()
    if api_spreads.LEVERAGED_TOKEN_PATTERN.match(token) and row.get("long_venue") != row.get("short_venue"):
        return False
    for side in ("long", "short"):
        if str(row.get(f"{side}_market_type") or "") != "Futures":
            continue
        symbol = str(row.get(f"{side}_market_symbol") or "")
        if api_spreads.DATED_CONTRACT_PATTERN.search(symbol):
            return False
        _, separator, settle = symbol.partition(":")
        if separator and settle and settle.upper() not in api_spreads.LINEAR_SETTLE_ASSETS:
            return False
    spread = api_spreads._entrance_spread_dict(row)
    ticker_derived = any(
        api_spreads._float_or_none(row.get(f"{side}_bid")) is not None
        and api_spreads._float_or_none(row.get(f"{side}_bid"))
        == api_spreads._float_or_none(row.get(f"{side}_ask"))
        for side in ("long", "short")
    )
    if ticker_derived and abs(spread) > api_spreads.TICKER_PRICE_TRUST_LIMIT_PCT:
        return False
    funding = api_spreads._effective_funding_24h_dict(row)
    if not ((api_spreads._float_or_none(row.get("executable_spread_pct")) or 0.0) > 0 or (funding or 0.0) > 0):
        return False
    return not (
        require_deliverable
        and str(row.get("route_kind") or "") in api_spreads.TRANSFER_ROUTE_KINDS
        and row.get("deliverable") is False
    )


def _unverifiable_price_outliers(rows: list[dict[str, Any]]) -> set[str]:
    trusted: dict[str, list[float]] = {}
    for row in rows:
        for side in ("long", "short"):
            price = api_spreads._float_or_none(row.get(f"{side}_price"))
            volume = api_spreads._float_or_none(row.get(f"{side}_volume_24h_usd"))
            if price and price > 0 and volume and volume >= api_spreads.MIN_LEG_VOLUME_24H_USD:
                trusted.setdefault(str(row.get("token") or ""), []).append(price)
    reference = {
        token: statistics.median(prices)
        for token, prices in trusted.items()
        if len(prices) >= 3
    }
    flagged: set[str] = set()
    for row in rows:
        anchor = reference.get(str(row.get("token") or ""))
        if not anchor or anchor <= 0:
            continue
        for side in ("long", "short"):
            price = api_spreads._float_or_none(row.get(f"{side}_price"))
            volume = api_spreads._float_or_none(row.get(f"{side}_volume_24h_usd"))
            if not price or price <= 0 or (volume and volume >= api_spreads.MIN_LEG_VOLUME_24H_USD):
                continue
            if abs(price / anchor - 1.0) > api_spreads.PRICE_CONSENSUS_DEVIATION:
                flagged.add(str(row.get("route_key") or ""))
                break
    return flagged


def _current_rankable(row: dict[str, Any]) -> bool:
    guard = row.get("tokenized_guard") or {}
    return (
        row.get("deliverable") is not False
        and not row.get("mirage_guarded")
        and not row.get("identity_mismatch")
        and not row.get("thin_book")
        and row.get("freshness") == "fresh"
        and api_spreads.spread_quote_current(row)
        and api_spreads.matched_probe_verified(row)
        and (not isinstance(guard, dict) or guard.get("rankable") is not False)
    )


def _group_rows(
    rows: list[dict[str, Any]], *, evidence: str = "all"
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("token") or ""), []).append(row)
    output: list[dict[str, Any]] = []
    for token, token_rows in grouped.items():
        token_rows.sort(
            key=lambda row: (
                api_spreads._entrance_spread_dict(row),
                api_spreads._float_or_none(row.get("executable_spread_pct")) or -999999.0,
                api_spreads._float_or_none(row.get("depth_usd")) or 0.0,
                -(api_spreads._float_or_none(row.get("age_min")) or 999999.0),
            ),
            reverse=True,
        )
        research_view = evidence == "research"
        combined_view = evidence == "all"
        tradeable = [row for row in token_rows if _current_rankable(row)]
        quotable = [
            row
            for row in token_rows
            if row.get("spread_quote_current")
            and (
                api_spreads._float_or_none(row.get("depth_weighted_spread_pct")) is not None
                or api_spreads._float_or_none(row.get("executable_spread_pct")) is not None
            )
        ]
        best = max(
            (quotable if research_view or combined_view else tradeable)
            or quotable
            or token_rows,
            key=api_spreads._entrance_spread_dict,
        )
        funding_rows = [
            row
            for row in token_rows
            if api_spreads._effective_funding_24h_dict(row) is not None
            and not row.get("mirage_guarded")
        ]
        best_funding = max(
            funding_rows,
            key=lambda row: api_spreads._effective_funding_24h_dict(row) or 0.0,
            default=None,
        )
        daily = (
            api_spreads._effective_funding_24h_dict(best_funding)
            if best_funding is not None
            else None
        )
        output.append(
            {
                "token": token,
                "token_name": best.get("token_name"),
                "href": best.get("href"),
                "route_count": len(token_rows),
                "venues": sorted(
                    {
                        str(venue)
                        for row in token_rows
                        for venue in (row.get("long_venue"), row.get("short_venue"))
                        if venue
                    }
                ),
                "route_kinds": sorted({str(row.get("route_kind") or "") for row in token_rows}),
                "best_route": best,
                "best_edge_pct": (
                    api_spreads._entrance_spread_dict(best)
                    if tradeable or ((research_view or combined_view) and quotable)
                    else None
                ),
                "best_funding_route": best_funding,
                "best_funding_apr_pct": daily * 365.0 if daily is not None else None,
                "best_funding_24h_pct": daily,
                "best_funding_24h_basis": (
                    "projected_current_rate" if best_funding is not None else None
                ),
                "age_min": min(
                    (
                        float(age)
                        for row in token_rows
                        if (age := api_spreads._float_or_none(row.get("age_min"))) is not None
                    ),
                    default=None,
                ),
                "routes": token_rows,
            }
        )
    return output


def _build_headlines(
    structural: tuple[dict[str, Any], ...],
    updates: dict[str, tuple[Any, ...]],
    *,
    now: float,
) -> dict[str, Any]:
    """Precompute market-wide controls and tapes during the live refresh."""

    filters = _filters({}, limit=500, offset=0)
    current = [
        _overlay(row, updates.get(str(row.get("route_key") or "")), now=now)
        for row in structural
    ]
    fresh = [row for row in current if row.get("freshness") == "fresh"]
    dynamically_matching = [row for row in fresh if _matches_dynamic(row, filters)]
    outliers = _unverifiable_price_outliers(dynamically_matching)
    presentable = [
        row
        for row in dynamically_matching
        if _presentable(
            row,
            filters,
            require_deliverable=True,
            unverifiable_outliers=outliers,
        )
    ]
    groups = _group_rows(presentable)
    edge_groups = sorted(
        (
            group
            for group in groups
            if api_spreads._float_or_none(group.get("best_edge_pct")) is not None
        ),
        key=lambda group: api_spreads._group_sort_value(group, "edge"),
        reverse=True,
    )
    funding_groups = sorted(
        (
            group
            for group in groups
            if api_spreads._float_or_none(group.get("best_funding_24h_pct")) is not None
        ),
        key=lambda group: api_spreads._group_sort_value(group, "funding"),
        reverse=True,
    )
    route_kind_tokens: dict[str, set[str]] = {}
    lane_tokens: dict[str, set[str]] = {
        "FUTURES": set(),
        "FUTURES-SPOT": set(),
        "SPOT": set(),
        "DEX-FUTURES": set(),
        "DEX-SPOT": set(),
    }
    for row in fresh:
        kind = str(row.get("route_kind") or "")
        token = str(row.get("token") or "")
        route_kind_tokens.setdefault(kind, set()).add(token)
        if not _current_rankable(row):
            continue
        lane = (
            "FUTURES-SPOT"
            if kind in {"FUTURES-SPOT", "SPOT-FUTURES"}
            else "DEX-FUTURES"
            if kind in {"DEX-FUTURES", "FUTURES-DEX"}
            else kind
        )
        if lane in lane_tokens:
            lane_tokens[lane].add(token)
    return {
        "exchange_options": sorted(
            {
                str(venue)
                for row in fresh
                for venue in (row.get("long_venue"), row.get("short_venue"))
                if venue
            }
        ),
        "route_kind_counts": dict(
            sorted(Counter(str(row.get("route_kind") or "") for row in fresh).items())
        ),
        "asset_class_counts": dict(
            Counter(str(row.get("asset_class") or "crypto") for row in fresh)
        ),
        "route_kind_token_counts": {
            kind: len(tokens) for kind, tokens in sorted(route_kind_tokens.items())
        },
        "lane_token_counts": {
            kind: len(tokens) for kind, tokens in lane_tokens.items()
        },
        "top_edges": [
            {key: value for key, value in group.items() if key != "routes"}
            for group in edge_groups[:8]
        ],
        "top_funding": [
            {key: value for key, value in group.items() if key != "routes"}
            for group in funding_groups[:8]
        ],
    }


def _summary(
    all_rows: Iterable[dict[str, Any]],
    filtered: list[dict[str, Any]],
    visible: list[dict[str, Any]],
    *,
    groups: list[dict[str, Any]],
    visible_groups: list[dict[str, Any]],
    offset: int,
    limit: int,
) -> dict[str, Any]:
    all_rows = tuple(all_rows)
    ranked_spreads = [
        api_spreads._entrance_spread_dict(row)
        for row in filtered
        if _current_rankable(row)
    ]
    funding = [
        api_spreads._effective_funding_24h_dict(row)
        for row in filtered
        if api_spreads._effective_funding_24h_dict(row) is not None
    ]
    return {
        "total_rows": len(all_rows),
        "total_tokens": len({str(row.get("token") or "") for row in all_rows}),
        "visible_rows": len(filtered),
        "matching_rows": len(filtered),
        "returned_rows": len(visible),
        "matching_tokens": len(groups),
        "returned_tokens": len(visible_groups),
        "offset": offset,
        "limit": limit,
        "fresh_rows": sum(row.get("freshness") == "fresh" for row in filtered),
        "stale_rows": sum(row.get("freshness") == "stale" for row in filtered),
        "api_rows": len(all_rows),
        "dex_rows": sum(str(row.get("route_kind") or "").startswith("DEX-") for row in all_rows),
        "funding_rows": len(funding),
        "max_executable_spread_pct": max(ranked_spreads, default=None),
        "undeliverable_rows": sum(row.get("deliverable") is False for row in filtered),
        "identity_mismatch_rows": sum(bool(row.get("identity_mismatch")) for row in filtered),
        "thin_book_rows": sum(bool(row.get("thin_book")) for row in filtered),
        "max_depth_weighted_spread_pct": max(ranked_spreads, default=None),
        "max_abs_funding_apr_pct": max((abs(value * 365.0) for value in funding), default=None),
        "max_abs_funding_24h_pct": max((abs(value) for value in funding), default=None),
        "max_funding_24h_pct": max(funding, default=None),
    }
