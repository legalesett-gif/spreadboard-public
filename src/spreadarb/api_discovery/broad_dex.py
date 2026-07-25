"""Normalize broad DEX spot research scans into API discovery snapshots."""

from __future__ import annotations

from typing import Any

from spreadarb.api_discovery.models import (
    QUOTE_VERIFIED_STATE,
    SOURCE_DEX_DISCOVERED,
    SourceStatus,
    as_float,
    utc_now_iso,
)

BROAD_DEX_SCAN_SOURCE_NAME = "broad_dex_spot_scan"


def normalize_broad_dex_payload(
    payload: dict[str, Any] | None,
    *,
    row_limit: int = 100,
    min_spread_pct: float = 1.0,
    max_spread_pct: float = 90.0,
) -> tuple[list[dict[str, Any]], list[SourceStatus]]:
    """Return safe research rows plus source statuses from broad DEX scan output.

    Broad scans are intentionally symbol-first. They may reveal useful research
    candidates, but they must never self-promote into route-feasible or
    executor-ready candidates.
    """

    if not isinstance(payload, dict):
        now = utc_now_iso()
        return [], [
            SourceStatus(
                name=BROAD_DEX_SCAN_SOURCE_NAME,
                kind="dex_spot",
                status="skipped",
                started_at=now,
                finished_at=now,
                elapsed_seconds=0.0,
                blockers=("broad_scan_missing",),
            )
        ]

    rows: list[dict[str, Any]] = []
    statuses: list[SourceStatus] = []
    updated_at = str(payload.get("updated_at") or utc_now_iso())
    elapsed = float(payload.get("elapsed_seconds") or 0.0)
    scans = payload.get("scans") if isinstance(payload.get("scans"), list) else []
    if not scans:
        statuses.append(
            SourceStatus(
                name=BROAD_DEX_SCAN_SOURCE_NAME,
                kind="dex_spot",
                status="skipped",
                started_at=updated_at,
                finished_at=updated_at,
                elapsed_seconds=elapsed,
                blockers=("broad_scan_no_sources",),
            )
        )
        return [], statuses

    for scan in scans:
        if not isinstance(scan, dict):
            continue
        source = str(scan.get("source") or "unknown")
        scan_rows = [
            row
            for row in (_research_row(item) for item in scan.get("rows") or [] if isinstance(item, dict))
            if row is not None
            and _spread_in_range(
                row.get("executable_spread_pct"),
                min_spread_pct=min_spread_pct,
                max_spread_pct=max_spread_pct,
            )
        ]
        rows.extend(scan_rows)
        statuses.append(
            SourceStatus(
                name=f"broad_{source}_quote",
                kind="dex_spot",
                status=str(scan.get("status") or "partial"),
                started_at=updated_at,
                finished_at=updated_at,
                elapsed_seconds=elapsed,
                rows=len(scan_rows),
                errors=tuple(str(item) for item in (scan.get("errors") or [])[:12]),
                blockers=tuple(_scan_blockers(scan)),
                details=_scan_details(scan),
            )
        )

    rows.sort(
        key=lambda row: as_float(row.get("depth_weighted_spread_pct"))
        or as_float(row.get("executable_spread_pct"))
        or -999.0,
        reverse=True,
    )
    return rows[:row_limit], statuses


def _research_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("source_kind") not in (None, SOURCE_DEX_DISCOVERED):
        return None
    normalized = dict(row)
    normalized["source_kind"] = SOURCE_DEX_DISCOVERED
    normalized["validation_state"] = QUOTE_VERIFIED_STATE
    normalized["executor_status"] = "not_ready"
    blockers = [str(item) for item in normalized.get("blockers") or [] if str(item)]
    for blocker in (
        "broad_dex_research",
        "symbol_match_only",
        "identity_unverified",
        "route_feasibility_unproven",
        "executor_attestation_missing",
        "gas_estimate_missing",
        "dex_spot_route_feasibility_unproven",
    ):
        if blocker not in blockers:
            blockers.append(blocker)
    normalized["blockers"] = blockers
    normalized.pop("identity_key", None)
    notes = dict(normalized.get("notes") or {})
    notes["broad_dex_research"] = True
    normalized["notes"] = notes
    return normalized


def _spread_in_range(value: Any, *, min_spread_pct: float, max_spread_pct: float) -> bool:
    spread = as_float(value)
    return spread is not None and min_spread_pct <= spread <= max_spread_pct


def _scan_blockers(scan: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    status = str(scan.get("status") or "")
    if status not in {"ok", "partial"}:
        blockers.append("broad_scan_failed")
    if int(scan.get("research_row_count_ge_1pct_lte_90pct") or 0) == 0:
        blockers.append("no_research_rows_in_range")
    if int(scan.get("quote_error_tokens") or 0) > 0:
        blockers.append("partial_quote_errors")
    return blockers


def _scan_details(scan: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "verified_tokens",
        "evm_tokens",
        "unique_symbols",
        "duplicate_symbol_groups",
        "crosslisted_unique_symbols_before_filters",
        "candidate_tokens_after_filters",
        "quote_attempted_tokens",
        "quote_success_tokens",
        "quote_error_tokens",
        "row_count",
        "positive_row_count",
        "research_row_count_ge_1pct_lte_90pct",
        "token_list_url",
    )
    return {key: scan[key] for key in keys if key in scan}
