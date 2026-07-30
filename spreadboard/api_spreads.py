"""Canonical public-API spread rows grouped into token-level market views."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import time
from typing import Any

from spreadboard import board, exchange_links, public_rails, token_metadata

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_API_DISCOVERY_PATH = RUNTIME_DIR / "api_discovery_latest.json"
DEFAULT_MAX_AGE_MIN = 15.0
DEFAULT_LIMIT = 25

# Spot-DEX is outside the current public product. Spot-Spot remains a first-class
# arbitrage lane and must participate in grouping and top-25 ranking.
RETIRED_ROUTE_KINDS = frozenset({"DEX-SPOT"})


@dataclass(frozen=True)
class SpreadTerminalRow:
    token: str
    token_name: str | None
    route_key: str
    route_kind: str
    source_group: str
    source_label: str
    source_name: str | None
    long_venue: str | None
    long_market_type: str | None
    short_venue: str | None
    short_market_type: str | None
    executable_spread_pct: float | None
    depth_weighted_spread_pct: float | None
    displayed_open_spread_pct: float | None
    funding_apr_pct: float | None
    funding_daily_pct: float | None
    funding_spread_pct: float | None
    depth_usd: float | None
    validation_state: str | None
    executor_status: str | None
    status: str
    blockers: list[str]
    next_action: str
    freshness: str
    age_min: float | None
    quote_ts_us: int | None
    href: str
    long_price: float | None = None
    short_price: float | None = None
    long_funding_pct: float | None = None
    short_funding_pct: float | None = None
    funding_24h_pct: float | None = None
    funding_projected_24h_pct: float | None = None
    funding_24h_source: str | None = None
    long_funding_interval_hours: float | None = None
    short_funding_interval_hours: float | None = None
    long_funding_interval_assumed: bool = False
    short_funding_interval_assumed: bool = False
    long_next_funding_ts_us: int | None = None
    short_next_funding_ts_us: int | None = None
    long_volume_24h_usd: float | None = None
    short_volume_24h_usd: float | None = None
    long_bid: float | None = None
    long_ask: float | None = None
    short_bid: float | None = None
    short_ask: float | None = None
    long_deposit_enabled: bool | None = None
    long_withdraw_enabled: bool | None = None
    short_deposit_enabled: bool | None = None
    short_withdraw_enabled: bool | None = None
    long_market_symbol: str | None = None
    short_market_symbol: str | None = None
    long_exchange_url: str | None = None
    short_exchange_url: str | None = None
    dex_chain: str | None = None
    dex_contract: str | None = None
    raw_source_kind: str | None = None
    mirage_guarded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_spreads(
    *,
    api_path: Path | str = DEFAULT_API_DISCOVERY_PATH,
    board_path: Path | str = board.DEFAULT_BOARD_PATH,
    q: str | None = None,
    exchange: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    min_spread_pct: float | None = None,
    min_abs_funding_24h_pct: float | None = None,
    min_abs_funding_apr_pct: float | None = None,
    funding_only: bool = False,
    include_stale: bool = False,
    include_unverified: bool = False,
    max_age_min: float | None = DEFAULT_MAX_AGE_MIN,
    sort_by: str = "edge",
    direction: str = "desc",
    offset: int = 0,
    limit: int | None = DEFAULT_LIMIT,
    now: float | None = None,
) -> dict[str, Any]:
    """Return token-grouped routes sourced only from public exchange APIs.

    Website rows are read solely for hidden reconciliation metrics. They never
    enter the displayed row set.
    """

    current_time = time.time() if now is None else now
    api_path = Path(api_path)
    board_path = Path(board_path)
    metadata = token_metadata.load_token_metadata()
    rails = public_rails.load_public_rails()
    api_rows, api_meta = _load_api_discovery_rows(
        api_path,
        now=current_time,
        metadata=metadata,
        rails=rails,
    )
    board_rows, board_meta = _load_board_rows(board_path, now=current_time)
    all_rows = _dedupe_rows(api_rows)
    # Diagnostic: how DEX-sourced rows classify BEFORE retirement is applied.
    # Futures-DEX is currently empty on the public board while discovery still
    # reports DEX rows, so surface the raw distribution to pinpoint the loss.
    dex_raw_kind_counts = dict(
        sorted(
            Counter(
                row.route_kind
                for row in all_rows
                if row.route_kind.startswith("DEX-")
                or row.raw_source_kind == "dex_discovered_rows"
            ).items()
        )
    )
    all_rows = [row for row in all_rows if row.route_kind not in RETIRED_ROUTE_KINDS]
    held_out = [row for row in all_rows if _is_mirage_guarded(row)]
    # Headline rankings must be executable research leads, not ticker
    # dislocations with unresolved identity, inventory, or transfer rails.
    # Guarded rows remain available through the explicit audit switch.
    ranked_rows = (
        all_rows
        if include_unverified
        else [
            row
            for row in all_rows
            if not _is_mirage_guarded(row)
        ]
    )
    public_universe = (
        ranked_rows
        if include_stale
        else [row for row in ranked_rows if row.freshness == "fresh"]
    )
    route_kind_token_counts = _route_kind_token_counts(public_universe)
    release_lane_token_counts = _release_lane_token_counts(public_universe)
    filtered = _filter_rows(
        ranked_rows,
        q=q,
        exchange=exchange,
        kind=kind,
        source=source,
        min_spread_pct=min_spread_pct,
        min_abs_funding_24h_pct=(
            min_abs_funding_24h_pct
            if min_abs_funding_24h_pct is not None
            else (
                min_abs_funding_apr_pct / 365.0
                if min_abs_funding_apr_pct is not None
                else None
            )
        ),
        funding_only=funding_only,
        include_stale=include_stale,
    )
    normalized_sort = _normalize_sort(sort_by)
    normalized_direction = "asc" if str(direction).casefold() == "asc" else "desc"
    filtered.sort(
        key=lambda row: _sort_value(row, normalized_sort),
        reverse=normalized_direction == "desc",
    )
    groups = _group_rows(filtered)
    for group in groups:
        (group.get("routes") or []).sort(
            key=lambda row: _route_dict_sort_value(row, normalized_sort),
            reverse=normalized_direction == "desc",
        )
    groups.sort(
        key=lambda group: _group_sort_value(group, normalized_sort),
        reverse=normalized_direction == "desc",
    )
    normalized_offset = max(0, int(offset or 0))
    visible_groups = (
        groups[normalized_offset : normalized_offset + max(0, limit)]
        if limit is not None
        else groups[normalized_offset:]
    )
    visible_route_keys = {
        route["route_key"]
        for group in visible_groups
        for route in group.get("routes") or []
    }
    visible = [row for row in filtered if row.route_key in visible_route_keys]
    summary = _summary(
        all_rows,
        filtered,
        visible,
        groups=groups,
        visible_groups=visible_groups,
        offset=normalized_offset,
        limit=limit,
    )
    _ = _reconciliation_summary(all_rows, board_rows, board_meta)

    return {
        "ok": not api_meta.get("error") and api_meta.get("status") == "fresh",
        "mode": "canonical_public_api_spreads",
        "filters": {
            "q": q,
            "exchange": exchange,
            "kind": kind,
            "source": "public_api",
            "min_spread_pct": min_spread_pct,
            "min_abs_funding_24h_pct": min_abs_funding_24h_pct,
            "min_abs_funding_apr_pct": min_abs_funding_apr_pct,
            "funding_only": funding_only,
            "include_stale": include_stale,
            "include_unverified": include_unverified,
            "max_age_min": max_age_min,
            "sort": normalized_sort,
            "direction": normalized_direction,
            "offset": normalized_offset,
            "limit": limit,
        },
        "summary": summary,
        "pagination": {
            "offset": normalized_offset,
            "limit": limit,
            "returned_rows": len(visible_groups),
            "matching_rows": len(groups),
            "has_previous": normalized_offset > 0,
            "has_more": normalized_offset + len(visible_groups) < len(groups),
        },
        "source_health": {
            "canonical_api": {
                **_public_source_health(api_meta),
                "mirage_guarded_count": len(held_out),
                "dex_raw_kind_counts": dex_raw_kind_counts,
                "lane_token_counts": release_lane_token_counts,
                "top_25_ready": {
                    kind: count >= DEFAULT_LIMIT
                    for kind, count in release_lane_token_counts.items()
                },
            },
        },
        "exchange_options": _exchange_options(public_universe),
        "route_kind_counts": dict(
            sorted(Counter(row.route_kind for row in public_universe).items())
        ),
        "route_kind_token_counts": route_kind_token_counts,
        "lane_token_counts": release_lane_token_counts,
        "top_edges": _top_unique_groups(public_universe, metric="edge"),
        "top_funding": _top_unique_groups(public_universe, metric="funding"),
        "groups": visible_groups,
        "rows": [_public_row(row) for row in visible],
    }


def _route_kind_token_counts(
    rows: list[SpreadTerminalRow],
) -> dict[str, int]:
    tokens: dict[str, set[str]] = {}
    for row in rows:
        tokens.setdefault(row.route_kind, set()).add(row.token)
    return {kind: len(values) for kind, values in sorted(tokens.items())}


def _release_lane_token_counts(
    rows: list[SpreadTerminalRow],
) -> dict[str, int]:
    tokens = {
        "FUTURES": set(),
        "FUTURES-SPOT": set(),
        "SPOT": set(),
        "DEX-FUTURES": set(),
    }
    for row in rows:
        if row.route_kind == "FUTURES":
            tokens["FUTURES"].add(row.token)
        elif row.route_kind in {"FUTURES-SPOT", "SPOT-FUTURES"}:
            tokens["FUTURES-SPOT"].add(row.token)
        elif row.route_kind == "SPOT":
            tokens["SPOT"].add(row.token)
        elif row.route_kind in {"DEX-FUTURES", "FUTURES-DEX"}:
            tokens["DEX-FUTURES"].add(row.token)
    return {kind: len(values) for kind, values in tokens.items()}


def _load_api_discovery_rows(
    path: Path,
    *,
    now: float,
    metadata: dict[str, dict[str, Any]] | None = None,
    rails: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[SpreadTerminalRow], dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [], {
            "status": "missing",
            "path": str(path),
            "error": str(exc),
            "row_count": 0,
        }
    except json.JSONDecodeError as exc:
        return [], {
            "status": "error",
            "path": str(path),
            "error": str(exc),
            "row_count": 0,
        }

    rows: list[SpreadTerminalRow] = []
    for bucket in ("api_discovered_rows", "dex_discovered_rows"):
        for raw in payload.get(bucket) or []:
            if isinstance(raw, dict):
                rows.append(
                    _row_from_api(
                        raw,
                        bucket=bucket,
                        now=now,
                        metadata=metadata or {},
                        rails=rails or {},
                    )
                )
    updated_at = _str_or_none(payload.get("updated_at"))
    age = _iso_age_min(updated_at, now=now)
    return rows, {
        "status": _freshness(age, DEFAULT_MAX_AGE_MIN),
        "path": str(path),
        "updated_at": updated_at,
        "age_min": age,
        "row_count": len(rows),
        "api_discovered_count": len(payload.get("api_discovered_rows") or []),
        "dex_discovered_count": len(payload.get("dex_discovered_rows") or []),
        "executor_ready_count": len(payload.get("executor_ready_rows") or []),
        "expires_at": payload.get("expires_at"),
        "worker_status": ((payload.get("source_refresh") or {}).get("status")),
        "dex_spot_source": _dex_spot_source_status(payload),
        "fast_quote_refresh": payload.get("fast_quote_refresh"),
    }


def _dex_spot_source_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Report whether the OKX DEX spot quote source actually ran.

    Futures-DEX renders empty whenever this source is skipped. The rows counted
    by `dex_discovered_count` are Hyperliquid/Aster perpetuals, which classify as
    Futures-Futures, so that number is not evidence that DEX quoting works.
    """

    sources = ((payload.get("source_refresh") or {}).get("sources")) or []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("name") == "okx_dex_quote":
            return {
                "name": "okx_dex_quote",
                "status": source.get("status"),
                "rows": source.get("rows"),
                "blockers": list(source.get("blockers") or []),
                "errors": [
                    str(error)[:240]
                    for error in (source.get("errors") or [])[:5]
                ],
                "details": {
                    key: value
                    for key, value in dict(source.get("details") or {}).items()
                    if key in {"provider", "quote_count"}
                },
            }
    return {"name": "okx_dex_quote", "status": "absent", "rows": 0, "blockers": []}


def _public_source_health(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        key: meta.get(key)
        for key in (
            "status",
            "updated_at",
            "age_min",
            "row_count",
            "api_discovered_count",
            "dex_discovered_count",
            "dex_spot_source",
            "expires_at",
            "fast_quote_refresh",
        )
        if meta.get(key) is not None
    }


def _row_from_api(
    raw: dict[str, Any],
    *,
    bucket: str,
    now: float,
    metadata: dict[str, dict[str, Any]] | None = None,
    rails: dict[str, dict[str, Any]] | None = None,
) -> SpreadTerminalRow:
    token = str(raw.get("token") or "").upper().strip()
    long_venue = _str_or_none(raw.get("long_venue"))
    short_venue = _str_or_none(raw.get("short_venue"))
    long_market_type = _str_or_none(raw.get("long_market_type"))
    short_market_type = _str_or_none(raw.get("short_market_type"))
    source_kind = _str_or_none(raw.get("source_kind"))
    route_kind = _route_kind(
        long_venue=long_venue,
        long_market_type=long_market_type,
        short_venue=short_venue,
        short_market_type=short_market_type,
        source_kind=source_kind,
    )
    quote_ts_us = _int_or_none(raw.get("quote_ts_us"))
    age = _age_min(now, quote_ts_us)
    source_group = "public_api"
    executor_status = _str_or_none(raw.get("executor_status"))
    validation = _str_or_none(raw.get("validation_state"))
    blockers = _string_list(raw.get("blockers"))
    funding_apr = _float_or_none(
        raw.get("funding_spread_apr_pct")
        if raw.get("funding_spread_apr_pct") is not None
        else raw.get("funding_apr_pct")
    )
    funding_daily = _float_or_none(raw.get("funding_daily_pct"))
    funding_24h = _float_or_none(raw.get("funding_24h_pct"))
    funding_projected_24h = _float_or_none(
        raw.get("funding_projected_24h_pct"),
        funding_daily,
        funding_apr / 365.0 if funding_apr is not None else None,
    )
    route_key = "|".join(
        [token or "?", long_venue or "?", long_market_type or "?", short_venue or "?", short_market_type or "?"]
    )
    notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
    route_inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    identities = notes.get("identity") if isinstance(notes.get("identity"), dict) else {}
    long_identity = identities.get("long") if isinstance(identities.get("long"), dict) else {}
    short_identity = identities.get("short") if isinstance(identities.get("short"), dict) else {}
    funding = notes.get("funding") if isinstance(notes.get("funding"), dict) else {}
    long_funding = funding.get("long") if isinstance(funding.get("long"), dict) else {}
    short_funding = funding.get("short") if isinstance(funding.get("short"), dict) else {}
    long_market_symbol = _str_or_none(
        (route_inputs.get("long") or {}).get("symbol")
        if isinstance(route_inputs.get("long"), dict)
        else None
    )
    short_market_symbol = _str_or_none(
        (route_inputs.get("short") or {}).get("symbol")
        if isinstance(route_inputs.get("short"), dict)
        else None
    )
    dex_identity = (
        long_identity
        if "dex" in str(long_venue or "").casefold()
        else short_identity
        if "dex" in str(short_venue or "").casefold()
        else {}
    )
    long_rails = public_rails.rail_state(rails or {}, long_venue, token)
    short_rails = public_rails.rail_state(rails or {}, short_venue, token)
    blockers.extend(
        _route_mirage_reasons(
            raw=raw,
            long_market_type=long_market_type,
            short_market_type=short_market_type,
            long_rails=long_rails,
            short_rails=short_rails,
        )
    )
    return SpreadTerminalRow(
        token=token,
        token_name=token_metadata.token_name(token, metadata or {}),
        route_key=route_key,
        route_kind=route_kind,
        source_group=source_group,
        source_label="Public API",
        source_name=_str_or_none(raw.get("source_name")),
        long_venue=long_venue,
        long_market_type=long_market_type,
        short_venue=short_venue,
        short_market_type=short_market_type,
        executable_spread_pct=_float_or_none(raw.get("executable_spread_pct")),
        depth_weighted_spread_pct=_float_or_none(raw.get("depth_weighted_spread_pct")),
        displayed_open_spread_pct=(
            _float_or_none(raw.get("executable_spread_pct"))
            if _float_or_none(raw.get("executable_spread_pct")) is not None
            else _float_or_none(raw.get("depth_weighted_spread_pct"))
        ),
        funding_apr_pct=funding_apr,
        funding_daily_pct=(
            funding_daily
            if funding_daily is not None
            else (funding_apr / 365.0 if funding_apr is not None else None)
        ),
        funding_spread_pct=funding_daily,
        depth_usd=_depth_from_api(raw),
        validation_state=validation,
        executor_status=executor_status,
        status="live" if _freshness(age, DEFAULT_MAX_AGE_MIN) == "fresh" else "unavailable",
        blockers=blockers,
        next_action="Open route details",
        freshness=_freshness(age, DEFAULT_MAX_AGE_MIN),
        age_min=age,
        quote_ts_us=quote_ts_us,
        href=f"/token/{token}" if token else "/markets",
        long_price=_float_or_none(
            raw.get("long_price"),
            raw.get("long_ask_vwap"),
            raw.get("long_ask"),
            raw.get("long_mark"),
            _nested_float(route_inputs, "long", "ask_vwap"),
            _nested_float(route_inputs, "long", "ask"),
        ),
        short_price=_float_or_none(
            raw.get("short_price"),
            raw.get("short_bid_vwap"),
            raw.get("short_bid"),
            raw.get("short_mark"),
            _nested_float(route_inputs, "short", "bid_vwap"),
            _nested_float(route_inputs, "short", "bid"),
        ),
        long_funding_pct=_float_or_none(
            raw.get("long_funding_pct"),
            raw.get("long_funding"),
            long_funding.get("current_funding_pct"),
            long_funding.get("rate_pct"),
        ),
        short_funding_pct=_float_or_none(
            raw.get("short_funding_pct"),
            raw.get("short_funding"),
            short_funding.get("current_funding_pct"),
            short_funding.get("rate_pct"),
        ),
        funding_24h_pct=funding_24h,
        funding_projected_24h_pct=funding_projected_24h,
        funding_24h_source=_str_or_none(raw.get("funding_24h_source")),
        long_funding_interval_hours=_float_or_none(
            long_funding.get("funding_interval_hours"),
            long_funding.get("interval_hours"),
        ),
        short_funding_interval_hours=_float_or_none(
            short_funding.get("funding_interval_hours"),
            short_funding.get("interval_hours"),
        ),
        long_funding_interval_assumed=bool(
            long_funding.get(
                "funding_interval_assumed",
                long_funding.get("interval_assumed", False),
            )
        ),
        short_funding_interval_assumed=bool(
            short_funding.get(
                "funding_interval_assumed",
                short_funding.get("interval_assumed", False),
            )
        ),
        long_next_funding_ts_us=_int_or_none(long_funding.get("next_funding_ts_us")),
        short_next_funding_ts_us=_int_or_none(short_funding.get("next_funding_ts_us")),
        long_volume_24h_usd=_nested_float(route_inputs, "long", "volume_24h_usd"),
        short_volume_24h_usd=_nested_float(route_inputs, "short", "volume_24h_usd"),
        long_bid=_nested_float(route_inputs, "long", "bid"),
        long_ask=_nested_float(route_inputs, "long", "ask"),
        short_bid=_nested_float(route_inputs, "short", "bid"),
        short_ask=_nested_float(route_inputs, "short", "ask"),
        long_deposit_enabled=_bool_or_none(long_rails.get("deposit")),
        long_withdraw_enabled=_bool_or_none(long_rails.get("withdraw")),
        short_deposit_enabled=_bool_or_none(short_rails.get("deposit")),
        short_withdraw_enabled=_bool_or_none(short_rails.get("withdraw")),
        long_market_symbol=long_market_symbol,
        short_market_symbol=short_market_symbol,
        long_exchange_url=exchange_links.exchange_market_url(
            venue=long_venue,
            market_type=long_market_type,
            market_symbol=long_market_symbol,
            token=token,
        ),
        short_exchange_url=exchange_links.exchange_market_url(
            venue=short_venue,
            market_type=short_market_type,
            market_symbol=short_market_symbol,
            token=token,
        ),
        dex_chain=_str_or_none(dex_identity.get("chain_id")),
        dex_contract=_str_or_none(dex_identity.get("token_address")),
        raw_source_kind=source_kind or bucket,
        mirage_guarded=any(str(item).startswith("mirage_guard:") for item in blockers),
    )


def _load_board_rows(path: Path, *, now: float) -> tuple[list[SpreadTerminalRow], dict[str, Any]]:
    snapshot = board.load_board(path, include_stale=True, max_age_min=None, limit=None, now=now)
    rows = [_row_from_board(row) for row in snapshot.rows]
    return rows, {
        "status": "error" if snapshot.error else _freshness(snapshot.age_min, DEFAULT_MAX_AGE_MIN),
        "path": snapshot.source_path,
        "error": snapshot.error,
        "age_min": snapshot.age_min,
        "row_count": len(rows),
        "fresh_count": len([row for row in rows if row.freshness == "fresh"]),
        "stale_count": len([row for row in rows if row.freshness == "stale"]),
    }


def _row_from_board(row: board.BoardRow) -> SpreadTerminalRow:
    source_group = "website_verified"
    blockers = list(row.blockers or [])
    freshness = _freshness(row.age_min, DEFAULT_MAX_AGE_MIN)
    status = _status_for_row(
        freshness=freshness,
        executor_status=None,
        blockers=blockers,
        validation_state=row.strategy_verdict,
    )
    return SpreadTerminalRow(
        token=row.symbol,
        token_name=None,
        route_key=row.route_key,
        route_kind=row.kind,
        source_group=source_group,
        source_label="Website verified",
        source_name=row.source_tab or "legacy_public_verification",
        long_venue=row.long_venue,
        long_market_type=row.long_market_type,
        short_venue=row.short_venue,
        short_market_type=row.short_market_type,
        executable_spread_pct=row.spread_pct,
        depth_weighted_spread_pct=row.depth_weighted_spread_pct,
        displayed_open_spread_pct=row.displayed_open_spread_pct,
        funding_apr_pct=row.funding_apr_pct,
        funding_daily_pct=(
            row.funding_apr_pct / 365.0 if row.funding_apr_pct is not None else None
        ),
        funding_spread_pct=row.funding_spread_pct,
        depth_usd=row.depth_usd,
        validation_state=row.strategy_verdict,
        executor_status=None,
        status=status,
        blockers=blockers,
        next_action=row.next_action or _next_action(status, blockers, source_group=source_group),
        freshness=freshness,
        age_min=row.age_min,
        quote_ts_us=row.ingested_at_us,
        href=f"/pair/{board.route_key_url(row.route_key)}",
        long_price=row.long_price,
        short_price=row.short_price,
        long_funding_pct=row.long_funding_pct,
        short_funding_pct=row.short_funding_pct,
        long_deposit_enabled=row.long_deposit_enabled,
        long_withdraw_enabled=row.long_withdraw_enabled,
        short_deposit_enabled=row.short_deposit_enabled,
        short_withdraw_enabled=row.short_withdraw_enabled,
        long_market_symbol=None,
        short_market_symbol=None,
        long_exchange_url=exchange_links.exchange_market_url(
            venue=row.long_venue,
            market_type=row.long_market_type,
            market_symbol=None,
            token=row.symbol,
        ),
        short_exchange_url=exchange_links.exchange_market_url(
            venue=row.short_venue,
            market_type=row.short_market_type,
            market_symbol=None,
            token=row.symbol,
        ),
        raw_source_kind="website_verified",
    )


def _filter_rows(rows: list[SpreadTerminalRow], **filters: Any) -> list[SpreadTerminalRow]:
    q = str(filters.get("q") or "").upper().strip()
    exchange = str(filters.get("exchange") or "").casefold().strip()
    kind = _normalize_kind_filter(filters.get("kind"))
    min_spread = filters.get("min_spread_pct")
    min_funding = filters.get("min_abs_funding_24h_pct")
    funding_only = bool(filters.get("funding_only"))
    include_stale = bool(filters.get("include_stale"))

    output: list[SpreadTerminalRow] = []
    for row in rows:
        if q and q not in f"{row.token} {row.token_name or ''}".upper():
            continue
        if exchange and exchange not in " ".join(filter(None, [row.long_venue, row.short_venue])).casefold():
            continue
        if kind:
            if kind == "FUTURES-SPOT-PAIR":
                if row.route_kind not in {"FUTURES-SPOT", "SPOT-FUTURES"}:
                    continue
            elif row.route_kind != kind:
                continue
        if not include_stale and row.freshness == "stale":
            continue
        spread = _entrance_spread(row)
        if min_spread is not None and spread < float(min_spread):
            continue
        effective_funding = _effective_funding_24h(row)
        if funding_only and effective_funding is None:
            continue
        if (
            min_funding is not None
            and abs(effective_funding or 0.0) < float(min_funding)
        ):
            continue
        output.append(row)
    return output


def _route_mirage_reasons(
    *,
    raw: dict[str, Any],
    long_market_type: str | None,
    short_market_type: str | None,
    long_rails: dict[str, Any],
    short_rails: dict[str, Any],
) -> list[str]:
    spread = max(
        abs(_float_or_none(raw.get("executable_spread_pct")) or 0.0),
        abs(_float_or_none(raw.get("depth_weighted_spread_pct")) or 0.0),
    )
    reasons: list[str] = []
    if short_market_type == "Spot" and long_market_type != "Spot":
        reasons.append("condition:spot_sell_inventory_required")
    if spread < 1.0:
        return reasons
    raw_blockers = {str(item) for item in raw.get("blockers") or []}
    identity_unverified = (
        "identity_unverified" in raw_blockers
        or "cex_identity_unverified" in raw_blockers
        or any(item.startswith("identity_collision:") for item in raw_blockers)
    )
    raw_source = str(raw.get("source_kind") or raw.get("raw_source_kind") or "")
    identity_threshold = 5.0 if "dex" in raw_source.casefold() else 25.0
    if spread >= identity_threshold and identity_unverified:
        reasons.append("mirage_guard:high_dislocation_identity_unverified")
    if long_market_type == "Spot" and short_market_type == "Spot":
        compatibility = public_rails.transfer_compatibility(long_rails, short_rails)
        status = str(compatibility.get("status") or "unknown")
        if status == "incompatible":
            reasons.append(
                "mirage_guard:spot_transfer_incompatible"
            )
        elif status != "compatible":
            reasons.append(f"condition:spot_transfer_{status}")
    return reasons


def _is_mirage_guarded(row: SpreadTerminalRow) -> bool:
    return any(str(item).startswith("mirage_guard:") for item in row.blockers)


def _normalize_kind_filter(value: Any) -> str:
    kind = str(value or "").upper().strip()
    return {
        "FUTURES-FUTURES": "FUTURES",
        "FUTURES-SPOT": "FUTURES-SPOT-PAIR",
        "SPOT-FUTURES": "FUTURES-SPOT-PAIR",
        "SPOT-SPOT": "SPOT",
        "FUTURES-DEX": "DEX-FUTURES",
        "SPOT-DEX": "DEX-SPOT",
    }.get(kind, kind)


def _summary(
    all_rows: list[SpreadTerminalRow],
    filtered: list[SpreadTerminalRow],
    visible: list[SpreadTerminalRow],
    *,
    groups: list[dict[str, Any]],
    visible_groups: list[dict[str, Any]],
    offset: int,
    limit: int | None,
) -> dict[str, Any]:
    return {
        "total_rows": len(all_rows),
        "total_tokens": len({row.token for row in all_rows}),
        "visible_rows": len(filtered),
        "matching_rows": len(filtered),
        "returned_rows": len(visible),
        "matching_tokens": len(groups),
        "returned_tokens": len(visible_groups),
        "offset": offset,
        "limit": limit,
        "fresh_rows": len([row for row in filtered if row.freshness == "fresh"]),
        "stale_rows": len([row for row in filtered if row.freshness == "stale"]),
        "api_rows": len(all_rows),
        "dex_rows": len([row for row in all_rows if row.raw_source_kind == "dex_discovered"]),
        "funding_rows": len([row for row in filtered if _effective_funding_24h(row) is not None]),
        "max_executable_spread_pct": max(
            (_float_or_none(row.executable_spread_pct) or 0.0 for row in filtered),
            default=None,
        ),
        "max_depth_weighted_spread_pct": max(
            (_entrance_spread(row) for row in filtered),
            default=None,
        ),
        "max_abs_funding_apr_pct": max(
            (abs(_float_or_none(row.funding_apr_pct) or 0.0) for row in filtered),
            default=None,
        ),
        "max_abs_funding_24h_pct": max(
            (abs(_effective_funding_24h(row) or 0.0) for row in filtered),
            default=None,
        ),
    }


def _top_unique_groups(rows: list[SpreadTerminalRow], *, metric: str) -> list[dict[str, Any]]:
    groups = _group_rows(
        [
            row
            for row in rows
            if row.freshness == "fresh"
            and (metric != "funding" or _effective_funding_24h(row) is not None)
        ]
    )
    if metric == "edge":
        groups = [
            group
            for group in groups
            if (_float_or_none(group.get("best_edge_pct")) or 0.0) > 0
        ]
    groups.sort(
        key=lambda group: _float_or_none(
            group.get("best_funding_24h_pct")
            if metric == "funding"
            else group.get("best_edge_pct")
        )
        or 0.0,
        reverse=True,
    )
    return [
        {key: value for key, value in group.items() if key != "routes"}
        for group in groups[:8]
    ]


def _group_rows(rows: list[SpreadTerminalRow]) -> list[dict[str, Any]]:
    grouped: dict[str, list[SpreadTerminalRow]] = {}
    for row in rows:
        grouped.setdefault(row.token, []).append(row)
    output: list[dict[str, Any]] = []
    for token, token_rows in grouped.items():
        # The headline matches the board convention: buy at the current ask and
        # sell at the current bid. Matched-size VWAP remains execution context.
        token_rows.sort(
            key=lambda row: (
                _entrance_spread(row),
                _float_or_none(row.executable_spread_pct) or -999999.0,
                _float_or_none(row.depth_usd) or 0.0,
                -(row.age_min or 999999.0),
            ),
            reverse=True,
        )
        routes = [_public_row(row) for row in token_rows]
        best = max(
            token_rows,
            key=_entrance_spread,
        )
        funding_rows = [
            row for row in token_rows if _effective_funding_24h(row) is not None
        ]
        best_funding = max(
            funding_rows,
            key=lambda row: abs(_effective_funding_24h(row) or 0.0),
            default=None,
        )
        output.append(
            {
                "token": token,
                "token_name": best.token_name,
                "href": best.href,
                "route_count": len(token_rows),
                "venues": sorted(
                    {
                        venue
                        for row in token_rows
                        for venue in (row.long_venue, row.short_venue)
                        if venue
                    }
                ),
                "route_kinds": sorted({row.route_kind for row in token_rows}),
                "best_route": _public_row(best),
                "best_edge_pct": _entrance_spread(best),
                "best_funding_route": (
                    _public_row(best_funding) if best_funding is not None else None
                ),
                "best_funding_apr_pct": (
                    best_funding.funding_apr_pct if best_funding is not None else None
                ),
                "best_funding_24h_pct": (
                    _effective_funding_24h(best_funding)
                    if best_funding is not None
                    else None
                ),
                "best_funding_24h_basis": (
                    "settled_public_events"
                    if best_funding is not None and best_funding.funding_24h_pct is not None
                    else "projected_current_rate"
                    if best_funding is not None
                    else None
                ),
                "age_min": min(
                    (row.age_min for row in token_rows if row.age_min is not None),
                    default=None,
                ),
                "routes": routes,
            }
        )
    return output


def _group_sort_value(group: dict[str, Any], sort_by: str) -> Any:
    routes = group.get("routes") or []
    if not routes:
        return -999999.0
    if sort_by == "token":
        return str(group.get("token") or "")
    if sort_by in {"funding", "funding_abs"}:
        value = _float_or_none(group.get("best_funding_24h_pct"))
        if sort_by == "funding":
            return value if value is not None else -999999.0
        return abs(value or 0.0)
    if sort_by == "age":
        return _float_or_none(group.get("age_min")) or 999999999.0
    if sort_by == "depth":
        return max((_float_or_none(row.get("depth_usd")) or 0.0 for row in routes), default=0.0)
    if sort_by == "edge":
        return _float_or_none(group.get("best_edge_pct")) or 0.0
    return _row_sort_key_dict(group.get("best_route") or {})


def _row_sort_key_dict(row: dict[str, Any]) -> tuple[float, float, float, float]:
    spread = max(0.0, _entrance_spread_dict(row))
    funding = max(0.0, _float_or_none(row.get("funding_apr_pct")) or 0.0) / 10.0
    depth = min(_float_or_none(row.get("depth_usd")) or 0.0, 250_000.0) / 10_000.0
    return (spread + funding + depth, funding, spread, -(_float_or_none(row.get("age_min")) or 999999.0))


def _route_dict_sort_value(row: dict[str, Any], sort_by: str) -> Any:
    if sort_by == "edge":
        return _entrance_spread_dict(row)
    if sort_by == "funding":
        funding = _effective_funding_24h_dict(row)
        return funding if funding is not None else -999999.0
    if sort_by == "funding_abs":
        funding = _effective_funding_24h_dict(row)
        return abs(funding) if funding is not None else -999999.0
    if sort_by == "depth":
        return _float_or_none(row.get("depth_usd")) or 0.0
    if sort_by == "age":
        return _float_or_none(row.get("age_min")) or 999999999.0
    if sort_by == "token":
        return str(row.get("token") or "")
    return _row_sort_key_dict(row)


def _dedupe_rows(rows: list[SpreadTerminalRow]) -> list[SpreadTerminalRow]:
    selected: dict[str, SpreadTerminalRow] = {}
    for row in rows:
        current = selected.get(row.route_key)
        if current is None or (row.quote_ts_us or 0) > (current.quote_ts_us or 0):
            selected[row.route_key] = row
    return list(selected.values())


def _row_sort_key(row: SpreadTerminalRow) -> tuple[float, float, float, float]:
    fresh_bonus = 10_000.0 if row.freshness == "fresh" else 0.0
    spread = max(0.0, _entrance_spread(row))
    funding = max(0.0, _float_or_none(row.funding_apr_pct) or 0.0) / 10.0
    depth = min(_float_or_none(row.depth_usd) or 0.0, 250_000.0) / 10_000.0
    return (fresh_bonus + spread + funding + depth, funding, spread, -(row.age_min or 999999.0))


def _public_row(row: SpreadTerminalRow) -> dict[str, Any]:
    payload = row.to_dict()
    payload["conditions"] = [
        item.removeprefix("mirage_guard:").removeprefix("condition:")
        for item in row.blockers
        if item.startswith("condition:")
        or item.removeprefix("mirage_guard:") in {"spot_sell_inventory_required"}
    ]
    for key in (
        "blockers",
        "executor_status",
        "next_action",
        "raw_source_kind",
        "source_group",
        "source_label",
        "source_name",
        "status",
        "validation_state",
    ):
        payload.pop(key, None)
    return payload


def _normalize_sort(value: str | None) -> str:
    normalized = str(value or "edge").casefold().strip()
    return normalized if normalized in {"rank", "edge", "funding", "funding_abs", "depth", "age", "token"} else "rank"


def _sort_value(row: SpreadTerminalRow, sort_by: str) -> Any:
    if sort_by == "edge":
        return _entrance_spread(row)
    if sort_by == "funding":
        funding = _effective_funding_24h(row)
        return funding if funding is not None else -999999.0
    if sort_by == "funding_abs":
        funding = _effective_funding_24h(row)
        return abs(funding) if funding is not None else -999999.0
    if sort_by == "depth":
        return _float_or_none(row.depth_usd) or 0.0
    if sort_by == "age":
        return row.age_min if row.age_min is not None else 999999999.0
    if sort_by == "token":
        return row.token
    return _row_sort_key(row)


def _entrance_spread(row: SpreadTerminalRow) -> float:
    open_spread = _float_or_none(row.displayed_open_spread_pct)
    if open_spread is not None:
        return open_spread
    executable = _float_or_none(row.executable_spread_pct)
    if executable is not None:
        return executable
    depth_spread = _float_or_none(row.depth_weighted_spread_pct)
    if depth_spread is not None:
        return depth_spread
    return -999999.0


def _entrance_spread_dict(row: dict[str, Any]) -> float:
    open_spread = _float_or_none(row.get("displayed_open_spread_pct"))
    if open_spread is not None:
        return open_spread
    executable = _float_or_none(row.get("executable_spread_pct"))
    if executable is not None:
        return executable
    depth_spread = _float_or_none(row.get("depth_weighted_spread_pct"))
    if depth_spread is not None:
        return depth_spread
    return -999999.0


def _effective_funding_24h(row: SpreadTerminalRow) -> float | None:
    settled = _float_or_none(row.funding_24h_pct)
    return settled if settled is not None else _float_or_none(row.funding_projected_24h_pct)


def _effective_funding_24h_dict(row: dict[str, Any]) -> float | None:
    settled = _float_or_none(row.get("funding_24h_pct"))
    return settled if settled is not None else _float_or_none(row.get("funding_projected_24h_pct"))


def _exchange_counts(rows: list[SpreadTerminalRow]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for venue in {row.long_venue, row.short_venue}:
            if venue:
                counts[venue] += 1
    return dict(sorted(counts.items()))


def _exchange_options(rows: list[SpreadTerminalRow]) -> list[str]:
    venues = set()
    for row in rows:
        if row.long_venue:
            venues.add(row.long_venue)
        if row.short_venue:
            venues.add(row.short_venue)
    return sorted(venues)


def _route_kind(
    *,
    long_venue: str | None,
    long_market_type: str | None,
    short_venue: str | None,
    short_market_type: str | None,
    source_kind: str | None,
) -> str:
    venues = f"{long_venue or ''} {short_venue or ''}".casefold()
    market_types = {
        str(long_market_type or "").casefold(),
        str(short_market_type or "").casefold(),
    }
    is_dex = "dex" in market_types or any(
        token in venues for token in ("jupiter", "zerox", "0x", "okx dex")
    )
    if is_dex:
        if "futures" in market_types:
            return "DEX-FUTURES"
        return "DEX-SPOT"
    if long_market_type == "Futures" and short_market_type == "Futures":
        return "FUTURES"
    if long_market_type == "Spot" and short_market_type == "Futures":
        return "SPOT-FUTURES"
    if long_market_type == "Futures" and short_market_type == "Spot":
        return "FUTURES-SPOT"
    if long_market_type == "Spot" and short_market_type == "Spot":
        return "SPOT"
    return "UNKNOWN"


def _reconciliation_summary(
    api_rows: list[SpreadTerminalRow],
    website_rows: list[SpreadTerminalRow],
    website_meta: dict[str, Any],
) -> dict[str, Any]:
    """Compare sources without allowing website data into product rows."""

    api_routes = {_route_fingerprint(row): row for row in api_rows}
    website_routes = {_route_fingerprint(row): row for row in website_rows}
    matched = sorted(set(api_routes) & set(website_routes))
    drifts: list[float] = []
    for key in matched:
        api_spread = _float_or_none(api_routes[key].executable_spread_pct)
        website_spread = _float_or_none(website_routes[key].executable_spread_pct)
        if api_spread is not None and website_spread is not None:
            drifts.append(abs(api_spread - website_spread))
    api_tokens = {row.token for row in api_rows}
    website_tokens = {row.token for row in website_rows}
    return {
        "status": "checked" if website_rows else "unavailable",
        "website_age_min": website_meta.get("age_min"),
        "api_route_count": len(api_routes),
        "website_route_count": len(website_routes),
        "matched_route_count": len(matched),
        "matched_token_count": len(api_tokens & website_tokens),
        "api_only_token_count": len(api_tokens - website_tokens),
        "website_only_token_count": len(website_tokens - api_tokens),
        "median_spread_drift_pct_points": _median(drifts),
        "display_source": "public_api_only",
    }


def _route_fingerprint(row: SpreadTerminalRow) -> tuple[str, str, str, str, str]:
    return (
        row.token,
        str(row.long_venue or "").casefold(),
        str(row.long_market_type or "").casefold(),
        str(row.short_venue or "").casefold(),
        str(row.short_market_type or "").casefold(),
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _status_for_row(
    *,
    freshness: str,
    executor_status: str | None,
    blockers: list[str],
    validation_state: str | None,
) -> str:
    if freshness == "stale":
        return "stale"
    if executor_status in {"ready", "executor_ready"}:
        return "executor_ready"
    if blockers:
        return "setup_needed"
    if validation_state in {"quote_verified", "ok", "matched_board"}:
        return "watch_only"
    return "research"


def _next_action(status: str, blockers: list[str], *, source_group: str) -> str:
    if status == "stale":
        return "refresh source before ranking this route"
    if source_group == "api_discovery":
        return "verify identity, funding, private state, and executor coverage"
    if source_group == "dex_discovery":
        return "prove exact chain/contract and DEX route feasibility"
    if blockers:
        return "inspect blockers before any trade decision"
    return "open route detail and compare funding/depth"


def _depth_from_api(raw: dict[str, Any]) -> float | None:
    notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
    screen = notes.get("screen") if isinstance(notes.get("screen"), dict) else {}
    return _float_or_none(screen.get("liquidity_usd"))


def _nested_float(value: Any, section: str, key: str) -> float | None:
    section_value = value.get(section) if isinstance(value, dict) else None
    if not isinstance(section_value, dict):
        return None
    return _float_or_none(section_value.get(key))


def _freshness(age_min: float | None, max_age_min: float | None) -> str:
    if age_min is None:
        return "unknown"
    if max_age_min is not None and age_min > max_age_min:
        return "stale"
    return "fresh"


def _age_min(now: float, ts_us: int | None) -> float | None:
    if ts_us is None:
        return None
    return max(0.0, (now - (ts_us / 1_000_000.0)) / 60.0)


def _iso_age_min(value: str | None, *, now: float) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed.timestamp()) / 60.0)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _float_or_none(*values: Any) -> float | None:
    for value in values:
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
