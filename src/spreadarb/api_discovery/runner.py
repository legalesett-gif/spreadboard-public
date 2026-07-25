"""One-shot read-only API discovery runner."""

from __future__ import annotations

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
    if not tokens:
        source_results.append(_empty_token_status(started_at))
    source_statuses = [result.status for result in source_results]
    blacklist: dict[str, Any] = {}
    blacklist_load_error = None
    if blacklist_filter_enabled and db_path is not None:
        try:
            blacklist = blacklisted_tokens(db_path)
        except Exception as exc:
            blacklist_load_error = clean_error(exc)
    filtered = filter_blacklisted_rows(
        api_rows=api_rows,
        dex_rows=dex_rows,
        blacklist=blacklist,
        overrides=load_blacklist_overrides(blacklist_override_path) if blacklist_filter_enabled else set(),
        enabled=blacklist_filter_enabled,
        load_error=blacklist_load_error,
    )
    snapshot = build_snapshot(
        api_rows=filtered.api_rows[:row_limit],
        dex_rows=filtered.dex_rows[:row_limit],
        source_statuses=source_statuses,
        ttl_seconds=ttl_seconds,
    )
    snapshot["source_refresh"]["blacklist_filter"] = filtered.metadata
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
