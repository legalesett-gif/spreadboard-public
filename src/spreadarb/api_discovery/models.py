"""Shared models and state guards for read-only API discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from typing import Any

from spreadarb.live.candidate_state import (
    DISCOVERED_STATE,
    EXECUTOR_READY_STATE,
    IDENTITY_VERIFIED_STATE,
    QUOTE_VERIFIED_STATE,
    ROUTE_FEASIBLE_STATE,
    VALIDATION_STATES,
    is_executor_ready,
    normalize_validation_state,
    state_at_least,
)

API_DISCOVERY_SCHEMA = "spreadarb.api_discovery.snapshot.v1"
SOURCE_API_DISCOVERED = "api_discovered"
SOURCE_DEX_DISCOVERED = "dex_discovered"
DEFAULT_TTL_SECONDS = 300


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_us() -> int:
    return int(utc_now().timestamp() * 1_000_000)


def iso_after(seconds: int | float) -> str:
    return (utc_now() + timedelta(seconds=float(seconds))).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def clean_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:180]}"


def as_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def pct_text(value: object) -> str | None:
    parsed = as_float(value)
    if parsed is None:
        return None
    return f"{parsed:.8f}".rstrip("0").rstrip(".")


def dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


def spread_pct(long_ask: float | None, short_bid: float | None) -> float | None:
    if long_ask is None or short_bid is None or long_ask <= 0:
        return None
    value = (short_bid - long_ask) / long_ask * 100.0
    return value if math.isfinite(value) else None


@dataclass(frozen=True, slots=True)
class MarketQuote:
    token: str
    venue: str
    market_type: str
    bid: float | None
    ask: float | None
    bid_vwap: float | None
    ask_vwap: float | None
    quote_ts_us: int
    source_name: str
    symbol: str | None = None
    quote_asset: str | None = None
    identity_key: str | None = None
    identity_source: str | None = None
    decimals: int | None = None
    chain_id: int | None = None
    token_address: str | None = None
    settle_asset: str | None = None
    contract_size: str | None = None
    funding_rate_pct: float | None = None
    funding_interval_hours: float | None = None
    funding_apr_pct: float | None = None
    next_funding_ts_us: int | None = None
    funding_interval_assumed: bool = False
    volume_24h_usd: float | None = None
    gas_estimate_usd: float | None = None
    ask_gas_estimate_usd: float | None = None
    bid_gas_estimate_usd: float | None = None
    slippage_bps: int | None = None
    price_impact_pct: float | None = None
    ask_price_impact_pct: float | None = None
    bid_price_impact_pct: float | None = None
    quote_notional_usd: float | None = None
    route_plan: tuple[str, ...] = ()
    mev_protection: str | None = None
    transfer_time_seconds: float | None = None
    blockers: tuple[str, ...] = ()
    #: Which path produced this quote: "ticker" for a top-of-book ticker,
    #: "orderbook" for a walked ladder. Without it the snapshot cannot tell the
    #: two apart -- a $50 probe that fills at the first level leaves the VWAP
    #: equal to top of book -- so there was no way to check whether the
    #: depth_unverified blocker, set on 100% of rows, was accurate.
    quote_source: str | None = None


@dataclass(frozen=True, slots=True)
class SourceStatus:
    name: str
    kind: str
    status: str
    started_at: str
    finished_at: str
    elapsed_seconds: float
    rows: int = 0
    errors: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    disabled: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(float(self.elapsed_seconds), 3),
            "rows": self.rows,
            "errors": list(self.errors),
            "blockers": list(self.blockers),
            "disabled": self.disabled,
        }
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True, slots=True)
class SourceResult:
    status: SourceStatus
    rows: tuple[dict[str, Any], ...] = ()
    quotes: tuple[MarketQuote, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    token: str
    long_venue: str
    long_market_type: str
    short_venue: str
    short_market_type: str
    source_kind: str
    source_name: str
    validation_state: str
    quote_ts_us: int
    executable_spread_pct: float | None = None
    depth_weighted_spread_pct: float | None = None
    gas_adjusted_spread_pct: float | None = None
    scanner_open_spread_pct: float | None = None
    funding_spread_apr_pct: float | None = None
    funding_daily_pct: float | None = None
    identity_key: str | None = None
    executor_attestation: dict[str, Any] | None = None
    blockers: tuple[str, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)

    def to_row(self, *, allow_executor_ready: bool = False) -> dict[str, Any]:
        state = normalize_validation_state(self.validation_state)
        blockers = list(self.blockers)
        has_attestation = bool(
            self.executor_attestation and self.executor_attestation.get("status") == "ready"
        )
        if is_executor_ready(state) and (not allow_executor_ready or not has_attestation):
            state = ROUTE_FEASIBLE_STATE
            blockers.append("executor_attestation_missing")
        if state_at_least(state, QUOTE_VERIFIED_STATE):
            if self.executable_spread_pct is None or self.depth_weighted_spread_pct is None:
                state = DISCOVERED_STATE
                blockers.append("quote_or_depth_missing")
        if state_at_least(state, IDENTITY_VERIFIED_STATE) and not self.identity_key:
            state = QUOTE_VERIFIED_STATE
            blockers.append("identity_key_missing")
        if state_at_least(state, ROUTE_FEASIBLE_STATE):
            if not has_attestation:
                blockers.append("route_feasibility_unproven")
                state = IDENTITY_VERIFIED_STATE if self.identity_key else QUOTE_VERIFIED_STATE
            elif not self.identity_key:
                state = QUOTE_VERIFIED_STATE
                blockers.append("identity_required_for_route_feasibility")
        if self.source_kind == SOURCE_DEX_DISCOVERED and "Spot" in {
            self.long_market_type,
            self.short_market_type,
        }:
            if self.gas_adjusted_spread_pct is None:
                blockers.append("gas_estimate_missing")
            if state_at_least(state, IDENTITY_VERIFIED_STATE):
                dex_route_proven = bool(
                    self.executor_attestation
                    and self.executor_attestation.get("dex_spot_executable_route_proven") is True
                    and self.gas_adjusted_spread_pct is not None
                )
                if not dex_route_proven:
                    state = QUOTE_VERIFIED_STATE
                    blockers.append("dex_spot_route_feasibility_unproven")
        if self.identity_key is None:
            blockers.append("identity_unverified")
        blockers = dedupe(blockers)
        row: dict[str, Any] = {
            "token": self.token.upper(),
            "long_venue": self.long_venue,
            "long_market_type": self.long_market_type,
            "short_venue": self.short_venue,
            "short_market_type": self.short_market_type,
            "source_kind": self.source_kind,
            "source_name": self.source_name,
            "validation_state": state,
            "executor_status": "ready" if is_executor_ready(state) else "not_ready",
            "quote_ts_us": self.quote_ts_us,
            "blockers": blockers,
        }
        optional_values = {
            "scanner_open_spread_pct": self.scanner_open_spread_pct,
            "executable_spread_pct": self.executable_spread_pct,
            "depth_weighted_spread_pct": self.depth_weighted_spread_pct,
            "gas_adjusted_spread_pct": self.gas_adjusted_spread_pct,
            "funding_spread_apr_pct": self.funding_spread_apr_pct,
            "funding_daily_pct": self.funding_daily_pct,
            "identity_key": self.identity_key,
        }
        for key, value in optional_values.items():
            if value is None:
                continue
            row[key] = pct_text(value) if key.endswith("_pct") else value
        if self.notes:
            row["notes"] = dict(self.notes)
        if self.executor_attestation:
            row.setdefault("notes", {})["executor_attestation"] = dict(self.executor_attestation)
        return row


def candidate_state_from_checks(
    *,
    has_quote: bool,
    has_identity: bool,
    route_feasible: bool = False,
    executor_ready: bool = False,
) -> str:
    if executor_ready:
        return EXECUTOR_READY_STATE
    if route_feasible:
        return ROUTE_FEASIBLE_STATE
    if has_identity:
        return IDENTITY_VERIFIED_STATE
    if has_quote:
        return QUOTE_VERIFIED_STATE
    return DISCOVERED_STATE


def build_snapshot(
    *,
    api_rows: list[dict[str, Any]],
    dex_rows: list[dict[str, Any]],
    source_statuses: list[SourceStatus],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    updated_at = utc_now_iso()
    rows = [*api_rows, *dex_rows]
    executor_ready_rows = [row for row in rows if is_executor_ready(row.get("validation_state"))]
    return {
        "schema": API_DISCOVERY_SCHEMA,
        "updated_at": updated_at,
        "expires_at": iso_after(ttl_seconds),
        "api_discovered_rows": api_rows,
        "dex_discovered_rows": dex_rows,
        "executor_ready_rows": executor_ready_rows,
        "source_refresh": {
            "status": _overall_status(source_statuses),
            "updated_at": updated_at,
            "api_discovered_count": len(api_rows),
            "dex_discovered_count": len(dex_rows),
            "executor_ready_count": len(executor_ready_rows),
            "states": list(VALIDATION_STATES),
            "sources": [status.to_dict() for status in source_statuses],
        },
    }


def _overall_status(source_statuses: list[SourceStatus]) -> str:
    enabled = [status for status in source_statuses if not status.disabled]
    if not enabled:
        return "skipped"
    if not any(status.status == "ok" for status in enabled):
        return "skipped"
    if any(status.status == "ok" for status in enabled) and any(
        status.status not in {"ok", "skipped"} for status in enabled
    ):
        return "partial"
    if all(status.status in {"ok", "skipped"} for status in enabled):
        return "ok"
    return "partial"
