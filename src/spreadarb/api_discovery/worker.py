"""Grouped API discovery worker for continuous read-only snapshot refreshes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from spreadarb.api_discovery.blacklist_filter import (
    DEFAULT_BLACKLIST_OVERRIDE_PATH,
    filter_blacklisted_rows,
    load_blacklist_overrides,
)
from spreadarb.api_discovery.broad_dex import normalize_broad_dex_payload
from spreadarb.api_discovery.models import (
    SourceStatus,
    as_float,
    build_snapshot,
    clean_error,
    utc_now_iso,
)
from spreadarb.api_discovery.runner import run_discovery
from spreadarb.api_discovery.storage import append_archive, atomic_write_json
from spreadarb.live.demo_blacklist import blacklisted_tokens


RunDiscovery = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class DiscoveryGroup:
    name: str
    sources: set[str]
    all_platform_tokens: bool
    timeout_seconds: float | None
    max_orderbook_candidates: int
    row_limit: int


def default_groups(
    *,
    cex_max_orderbook_candidates: int = 100,
    dex_derivative_max_orderbook_candidates: int = 100,
    dex_spot_timeout_seconds: float | None = 30.0,
    row_limit: int = 100,
) -> list[DiscoveryGroup]:
    return [
        DiscoveryGroup(
            name="all_markets",
            sources={"cex", "disabled", "dex-derivatives", "dex-spot"},
            all_platform_tokens=True,
            timeout_seconds=None,
            max_orderbook_candidates=max(
                cex_max_orderbook_candidates,
                dex_derivative_max_orderbook_candidates,
            ),
            row_limit=row_limit,
        ),
    ]


def run_grouped_discovery(
    *,
    db_path: Path | None,
    watchlist_path: Path | None,
    snapshot_path: Path,
    archive_dir: Path,
    parts_dir: Path,
    identity_registry_path: Path | None = None,
    attestation_path: Path | None = None,
    ttl_seconds: int = 900,
    token_limit: int = 20,
    row_limit: int = 100,
    groups: list[DiscoveryGroup] | None = None,
    broad_dex_payload: dict[str, Any] | None = None,
    run_discovery_func: RunDiscovery = run_discovery,
    blacklist_filter_enabled: bool = True,
    blacklist_override_path: Path | None = DEFAULT_BLACKLIST_OVERRIDE_PATH,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    started = monotonic()
    selected_groups = groups or default_groups(row_limit=row_limit)
    group_snapshots: list[tuple[DiscoveryGroup, dict[str, Any]]] = []
    group_errors: list[SourceStatus] = []
    for group in selected_groups:
        group_snapshot_path = (
            snapshot_path
            if len(selected_groups) == 1
            else parts_dir / f"{group.name}.json"
        )
        try:
            group_snapshot = run_discovery_func(
                db_path=db_path,
                watchlist_path=watchlist_path,
                snapshot_path=group_snapshot_path,
                archive_dir=archive_dir,
                timeout_seconds=group.timeout_seconds,
                include_network=True,
                token_limit=token_limit,
                ttl_seconds=ttl_seconds,
                all_platform_tokens=group.all_platform_tokens,
                max_orderbook_candidates=group.max_orderbook_candidates,
                row_limit=group.row_limit,
                source_filter=set(group.sources),
                identity_registry_path=identity_registry_path,
                attestation_path=attestation_path,
                blacklist_filter_enabled=blacklist_filter_enabled,
                blacklist_override_path=blacklist_override_path,
            )
        except Exception as exc:
            finished = utc_now_iso()
            group_errors.append(
                SourceStatus(
                    name=f"api_discovery_worker:{group.name}",
                    kind="worker",
                    status="failed",
                    started_at=started_at,
                    finished_at=finished,
                    elapsed_seconds=monotonic() - started,
                    errors=(clean_error(exc),),
                    blockers=("group_run_failed",),
                    details={"sources": sorted(group.sources)},
                )
            )
            continue
        group_snapshots.append((group, group_snapshot))

    source_statuses = [
        *[
            _source_status_from_payload(source)
            for _, snapshot in group_snapshots
            for source in (snapshot.get("source_refresh") or {}).get("sources") or []
        ],
        *group_errors,
    ]
    broad_dex_rows, broad_dex_statuses = (
        normalize_broad_dex_payload(broad_dex_payload, row_limit=row_limit)
        if broad_dex_payload is not None
        else ([], [])
    )
    source_statuses.extend(broad_dex_statuses)
    merged_api_rows = _sort_rows(
        _dedupe_rows(
            [
                row
                for _, snapshot in group_snapshots
                for row in snapshot.get("api_discovered_rows") or []
                if isinstance(row, dict)
            ]
        )
    )
    merged_dex_rows = _sort_rows(
        _dedupe_rows(
            [
                row
                for _, snapshot in group_snapshots
                for row in snapshot.get("dex_discovered_rows") or []
                if isinstance(row, dict)
            ]
            + broad_dex_rows
        )
    )
    blacklist: dict[str, Any] = {}
    blacklist_load_error = None
    if blacklist_filter_enabled and db_path is not None:
        try:
            blacklist = blacklisted_tokens(db_path)
        except Exception as exc:
            blacklist_load_error = clean_error(exc)
    filtered = filter_blacklisted_rows(
        api_rows=merged_api_rows,
        dex_rows=merged_dex_rows,
        blacklist=blacklist,
        overrides=load_blacklist_overrides(blacklist_override_path) if blacklist_filter_enabled else set(),
        enabled=blacklist_filter_enabled,
        load_error=blacklist_load_error,
    )
    api_rows = filtered.api_rows[:row_limit]
    dex_rows = filtered.dex_rows[:row_limit]
    snapshot = build_snapshot(
        api_rows=api_rows,
        dex_rows=dex_rows,
        source_statuses=source_statuses,
        ttl_seconds=ttl_seconds,
    )
    snapshot["source_refresh"]["blacklist_filter"] = filtered.metadata
    snapshot["worker"] = {
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "elapsed_seconds": round(monotonic() - started, 3),
        "group_count": len(selected_groups),
        "groups": [_group_summary(group, snapshot) for group, snapshot in group_snapshots],
        "group_failures": len(group_errors),
        "not_probed_venues": _not_probed_venues(source_statuses),
        "broad_dex_spot": {
            "enabled": broad_dex_payload is not None,
            "rows": len(broad_dex_rows),
            "statuses": [status.to_dict() for status in broad_dex_statuses],
        },
        "blacklist_filter_enabled": blacklist_filter_enabled,
        "blacklist_override_path": str(blacklist_override_path) if blacklist_override_path else None,
    }
    atomic_write_json(snapshot_path, snapshot)
    archive_path = append_archive(
        archive_dir,
        {
            "event": "api_discovery_worker_run",
            "snapshot_path": str(snapshot_path),
            "snapshot": snapshot,
        },
    )
    snapshot["archive_path"] = str(archive_path)
    return snapshot


def _not_probed_venues(source_statuses: list[SourceStatus]) -> list[dict[str, str]]:
    """Venues a sweep did NOT actually probe (disabled connectors or skipped
    sources). Surfaced in the snapshot so a "no route found" conclusion is
    explicitly qualified by what was never checked, rather than read as
    exhaustive. Part of API-sweep de-poisoning."""

    return [
        {
            "venue": status.name,
            "kind": status.kind,
            "reason": (status.blockers[0] if status.blockers else status.status),
        }
        for status in source_statuses
        if status.disabled or status.status == "skipped"
    ]


def _source_status_from_payload(payload: dict[str, Any]) -> SourceStatus:
    return SourceStatus(
        name=str(payload.get("name") or "unknown"),
        kind=str(payload.get("kind") or "unknown"),
        status=str(payload.get("status") or "partial"),
        started_at=str(payload.get("started_at") or utc_now_iso()),
        finished_at=str(payload.get("finished_at") or utc_now_iso()),
        elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
        rows=int(payload.get("rows") or 0),
        errors=tuple(str(item) for item in payload.get("errors") or []),
        blockers=tuple(str(item) for item in payload.get("blockers") or []),
        disabled=bool(payload.get("disabled", False)),
        details=dict(payload.get("details") or {}),
    )


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("source_kind") or ""),
            str(row.get("source_name") or ""),
            str(row.get("token") or ""),
            str(row.get("long_venue") or ""),
            str(row.get("long_market_type") or ""),
            str(row.get("short_venue") or ""),
            str(row.get("short_market_type") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: as_float(row.get("depth_weighted_spread_pct"))
        or as_float(row.get("executable_spread_pct"))
        or -999.0,
        reverse=True,
    )


def _group_summary(group: DiscoveryGroup, snapshot: dict[str, Any]) -> dict[str, Any]:
    refresh = snapshot.get("source_refresh") or {}
    return {
        "name": group.name,
        "sources": sorted(group.sources),
        "all_platform_tokens": group.all_platform_tokens,
        "max_orderbook_candidates": group.max_orderbook_candidates,
        "timeout_seconds": group.timeout_seconds,
        "api_discovered_count": refresh.get("api_discovered_count"),
        "dex_discovered_count": refresh.get("dex_discovered_count"),
        "executor_ready_count": refresh.get("executor_ready_count"),
        "status": refresh.get("status"),
    }
