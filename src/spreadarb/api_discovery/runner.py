"""One-shot read-only API discovery runner."""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic
from typing import Any

from spreadarb.api_discovery.attestations import load_executor_attestations
from spreadarb.api_discovery.blacklist_filter import (
    DEFAULT_BLACKLIST_OVERRIDE_PATH,
    filter_blacklisted_rows,
    load_blacklist_overrides,
)
from spreadarb.api_discovery.identity import (
    load_identity_registry,
    load_scanner_tokens,
    load_watchlist,
    merge_tokens,
)
from spreadarb.api_discovery.models import (
    SOURCE_DEX_DISCOVERED,
    SourceResult,
    SourceStatus,
    build_snapshot,
    clean_error,
    utc_now_iso,
)
from spreadarb.api_discovery.sources import (
    DiscoveryContext,
    DiscoverySource,
    default_sources,
)
from spreadarb.api_discovery.storage import append_archive, atomic_write_json
from spreadarb.live.demo_blacklist import blacklisted_tokens
from spreadarb.public_runtime import discovery_max_spread_pct


def run_discovery(
    *,
    db_path: Path | None,
    watchlist_path: Path | None,
    snapshot_path: Path,
    archive_dir: Path,
    timeout_seconds: float | None,
    include_network: bool = True,
    token_limit: int = 20,
    ttl_seconds: int = 300,
    all_platform_tokens: bool = False,
    max_orderbook_candidates: int = 100,
    row_limit: int = 100,
    source_filter: set[str] | None = None,
    identity_registry_path: Path | None = None,
    attestation_path: Path | None = None,
    sources: list[DiscoverySource] | None = None,
    blacklist_filter_enabled: bool = True,
    blacklist_override_path: Path | None = DEFAULT_BLACKLIST_OVERRIDE_PATH,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    started = monotonic()
    previous_snapshot = _load_previous_snapshot(snapshot_path)
    watchlist = load_watchlist(watchlist_path)
    identity_registry = load_identity_registry(identity_registry_path)
    executor_attestations = load_executor_attestations(attestation_path)
    watchlist_tokens = [asset.token for asset in watchlist.values() if asset.cex_enabled]
    scanner_tokens = load_scanner_tokens(db_path, limit=token_limit)
    tokens = merge_tokens(watchlist_tokens, scanner_tokens, limit=token_limit)
    deadline = None if timeout_seconds is None or timeout_seconds <= 0 else monotonic() + timeout_seconds

    source_results: list[SourceResult] = []
    api_rows: list[dict[str, Any]] = []
    dex_rows: list[dict[str, Any]] = []
    reference_quotes = ()
    blacklist: dict[str, Any] = {}
    blacklist_load_error = None
    if blacklist_filter_enabled and db_path is not None:
        try:
            blacklist = blacklisted_tokens(db_path)
        except Exception as exc:
            blacklist_load_error = clean_error(exc)
    blacklist_overrides = (
        load_blacklist_overrides(blacklist_override_path)
        if blacklist_filter_enabled
        else set()
    )
    for source in sources if sources is not None else default_sources(
        include_network=include_network,
        source_filter=source_filter,
    ):
        context = DiscoveryContext(
            tokens=tuple(tokens),
            watchlist=watchlist,
            deadline_monotonic=deadline,
            reference_quotes=reference_quotes,
            all_platform_tokens=all_platform_tokens,
            max_orderbook_candidates=max_orderbook_candidates,
            max_spread_pct=discovery_max_spread_pct(),
            identity_registry=identity_registry,
            executor_attestations=executor_attestations,
        )
        result = source.collect(context)
        source_results.append(result)
        if result.quotes and source.kind == "cex":
            reference_quotes = (*reference_quotes, *result.quotes)
        for row in result.rows:
            if row.get("source_kind") == SOURCE_DEX_DISCOVERED:
                dex_rows.append(row)
            else:
                api_rows.append(row)
        partial = _build_filtered_snapshot(
            api_rows=api_rows,
            dex_rows=dex_rows,
            source_results=source_results,
            blacklist=blacklist,
            blacklist_overrides=blacklist_overrides,
            blacklist_filter_enabled=blacklist_filter_enabled,
            blacklist_load_error=blacklist_load_error,
            row_limit=row_limit,
            ttl_seconds=ttl_seconds,
        )
        partial["source_refresh"]["partial"] = True
        partial["source_refresh"]["sources_completed"] = len(source_results)
        partial = _retain_previous_rows(
            partial,
            previous_snapshot,
            row_limit=row_limit,
        )
        atomic_write_json(snapshot_path, partial)
    if not tokens:
        source_results.append(_empty_token_status(started_at))
    snapshot = _build_filtered_snapshot(
        api_rows=api_rows,
        dex_rows=dex_rows,
        source_results=source_results,
        blacklist=blacklist,
        blacklist_overrides=blacklist_overrides,
        blacklist_filter_enabled=blacklist_filter_enabled,
        blacklist_load_error=blacklist_load_error,
        row_limit=row_limit,
        ttl_seconds=ttl_seconds,
    )
    snapshot["run"] = {
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "elapsed_seconds": round(monotonic() - started, 3),
        "tokens": tokens,
        "token_seed_count": len(tokens),
        "all_platform_tokens": all_platform_tokens,
        "max_orderbook_candidates": max_orderbook_candidates,
        "row_limit": row_limit,
        "source_filter": sorted(source_filter) if source_filter else None,
        "blacklist_filter_enabled": blacklist_filter_enabled,
        "blacklist_override_path": str(blacklist_override_path) if blacklist_override_path else None,
        "watchlist_path": str(watchlist_path) if watchlist_path else None,
        "identity_registry_path": str(identity_registry_path) if identity_registry_path else None,
        "attestation_path": str(attestation_path) if attestation_path else None,
        "db_path": str(db_path) if db_path else None,
        "network_enabled": include_network,
    }
    atomic_write_json(snapshot_path, snapshot)
    archive_path = append_archive(
        archive_dir,
        {
            "event": "api_discovery_run",
            "snapshot_path": str(snapshot_path),
            "snapshot": snapshot,
        },
    )
    snapshot["archive_path"] = str(archive_path)
    return snapshot


def _load_previous_snapshot(snapshot_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _retain_previous_rows(
    partial: dict[str, Any],
    previous: dict[str, Any],
    *,
    row_limit: int,
) -> dict[str, Any]:
    retained = 0
    for bucket in ("api_discovered_rows", "dex_discovered_rows"):
        current_rows = [
            row for row in partial.get(bucket) or [] if isinstance(row, dict)
        ]
        previous_rows = [
            row for row in previous.get(bucket) or [] if isinstance(row, dict)
        ]
        if len(current_rows) >= row_limit:
            partial[bucket] = current_rows[:row_limit]
            continue
        seen = {_row_identity(row) for row in current_rows}
        merged = list(current_rows)
        for row in previous_rows:
            identity = _row_identity(row)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(row)
            retained += 1
            if len(merged) >= row_limit:
                break
        partial[bucket] = merged[:row_limit]
    partial["source_refresh"]["previous_snapshot_rows_retained"] = retained
    return partial


def _row_identity(row: dict[str, Any]) -> tuple[str, ...]:
    route_key = str(row.get("route_key") or "")
    if route_key:
        return ("route_key", route_key)
    return (
        "legs",
        str(row.get("token") or ""),
        str(row.get("long_venue") or ""),
        str(row.get("long_market_type") or ""),
        str(row.get("long_market_symbol") or ""),
        str(row.get("short_venue") or ""),
        str(row.get("short_market_type") or ""),
        str(row.get("short_market_symbol") or ""),
    )


def _build_filtered_snapshot(
    *,
    api_rows: list[dict[str, Any]],
    dex_rows: list[dict[str, Any]],
    source_results: list[SourceResult],
    blacklist: dict[str, Any],
    blacklist_overrides: set[str],
    blacklist_filter_enabled: bool,
    blacklist_load_error: str | None,
    row_limit: int,
    ttl_seconds: int,
) -> dict[str, Any]:
    filtered = filter_blacklisted_rows(
        api_rows=api_rows,
        dex_rows=dex_rows,
        blacklist=blacklist,
        overrides=blacklist_overrides,
        enabled=blacklist_filter_enabled,
        load_error=blacklist_load_error,
    )
    snapshot = build_snapshot(
        api_rows=filtered.api_rows[:row_limit],
        dex_rows=filtered.dex_rows[:row_limit],
        source_statuses=[result.status for result in source_results],
        ttl_seconds=ttl_seconds,
    )
    snapshot["source_refresh"]["blacklist_filter"] = filtered.metadata
    return snapshot


def _empty_token_status(started_at: str) -> SourceResult:
    status = SourceStatus(
        name="token_seed",
        kind="watchlist",
        status="skipped",
        started_at=started_at,
        finished_at=utc_now_iso(),
        elapsed_seconds=0.0,
        blockers=("no_tokens_from_watchlist_or_scanner",),
    )
    return SourceResult(status=status)
