"""Read-only executor/preflight attestations for API discovery rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

ATTESTATION_SCHEMA = "spreadarb.api_discovery.executor_attestations.v1"

READY_STATUSES = {"ready", "ok", "pass", "passed"}
SUPPORTED_EXECUTORS = {"live_auto_entry_spot_isolated_margin_v1"}
BASE_REQUIRED_CHECKS = {
    "fresh_quote_depth",
    "identity_verified",
    "balances_checked",
    "order_size_limits_checked",
    "market_precision_checked",
    "paired_order_feasibility_checked",
}
SPOT_MARGIN_REQUIRED_CHECKS = {
    "account_mode_checked",
    "margin_or_borrow_checked",
}
FUTURES_REQUIRED_CHECKS = {
    "account_mode_checked",
    "contract_size_checked",
    "funding_checked",
    "margin_checked",
    "settle_asset_checked",
}


@dataclass(frozen=True, slots=True)
class ExecutorAttestation:
    route_key: str
    status: str
    executor: str
    checked_at: str | None = None
    expires_at: str | None = None
    identity_key: str | None = None
    checks: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    details: dict[str, Any] | None = None

    @property
    def is_ready_status(self) -> bool:
        return self.status.lower() in READY_STATUSES

    @property
    def is_supported_executor(self) -> bool:
        return self.executor in SUPPORTED_EXECUTORS

    @property
    def is_fresh(self) -> bool:
        if not self.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return expires > datetime.now(tz=timezone.utc)

    def required_checks(self, *, long_market_type: str, short_market_type: str) -> set[str]:
        required = set(BASE_REQUIRED_CHECKS)
        if long_market_type == "Spot" and short_market_type == "Spot":
            required.update(SPOT_MARGIN_REQUIRED_CHECKS)
        if long_market_type == "Futures" or short_market_type == "Futures":
            required.update(FUTURES_REQUIRED_CHECKS)
        return required

    def validation_blockers(
        self,
        *,
        identity_key: str | None,
        long_market_type: str,
        short_market_type: str,
    ) -> list[str]:
        blockers = list(self.blockers)
        if not self.is_supported_executor:
            blockers.append("executor_not_supported")
        if not self.is_ready_status:
            blockers.append("executor_attestation_not_ready")
        if not self.is_fresh:
            blockers.append("executor_attestation_stale")
        if not identity_key:
            blockers.append("identity_unverified")
        elif self.identity_key and self.identity_key != identity_key:
            blockers.append("executor_attestation_identity_mismatch")
        missing_checks = self.required_checks(
            long_market_type=long_market_type,
            short_market_type=short_market_type,
        ) - set(self.checks)
        for check in sorted(missing_checks):
            blockers.append(f"executor_attestation_missing_check:{check}")
        return list(dict.fromkeys(blockers))

    def is_positive(
        self,
        *,
        identity_key: str | None,
        long_market_type: str,
        short_market_type: str,
    ) -> bool:
        return not self.validation_blockers(
            identity_key=identity_key,
            long_market_type=long_market_type,
            short_market_type=short_market_type,
        )

    def to_note(self) -> dict[str, Any]:
        note: dict[str, Any] = {
            "route_key": self.route_key,
            "status": self.status,
            "executor": self.executor,
            "checks": list(self.checks),
        }
        if self.checked_at:
            note["checked_at"] = self.checked_at
        if self.expires_at:
            note["expires_at"] = self.expires_at
        if self.identity_key:
            note["identity_key"] = self.identity_key
        if self.details:
            note["details"] = self.details
        return note


class ExecutorAttestationRegistry:
    def __init__(self, attestations: list[ExecutorAttestation] | None = None) -> None:
        self._by_route = {attestation.route_key: attestation for attestation in attestations or []}

    @classmethod
    def empty(cls) -> "ExecutorAttestationRegistry":
        return cls()

    def get(self, route_key: str) -> ExecutorAttestation | None:
        return self._by_route.get(route_key)


def route_key(
    token: str,
    long_venue: str,
    long_market_type: str,
    short_venue: str,
    short_market_type: str,
) -> str:
    return "|".join(
        (
            str(token).upper(),
            str(long_venue),
            str(long_market_type),
            str(short_venue),
            str(short_market_type),
        )
    )


def load_executor_attestations(path: Path | None) -> ExecutorAttestationRegistry:
    if path is None or not path.exists():
        return ExecutorAttestationRegistry.empty()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ExecutorAttestationRegistry.empty()
    raw_rows = payload.get("attestations") if isinstance(payload, dict) else payload
    if not isinstance(raw_rows, list):
        return ExecutorAttestationRegistry.empty()
    attestations: list[ExecutorAttestation] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        route = str(item.get("route_key") or "").strip()
        if not route:
            route = route_key(
                str(item.get("token") or ""),
                str(item.get("long_venue") or ""),
                str(item.get("long_market_type") or ""),
                str(item.get("short_venue") or ""),
                str(item.get("short_market_type") or ""),
            )
        if not route.strip("|"):
            continue
        checks = _checks_from_payload(item.get("checks"))
        attestations.append(
            ExecutorAttestation(
                route_key=route,
                status=str(item.get("status") or "unknown"),
                executor=str(item.get("executor") or ""),
                checked_at=str(item.get("checked_at") or item.get("created_at") or "").strip() or None,
                expires_at=str(item.get("expires_at") or "").strip() or None,
                identity_key=str(item.get("identity_key") or "").strip() or None,
                checks=tuple(checks),
                blockers=tuple(str(value) for value in item.get("blockers") or []),
                details=dict(item.get("details") or {}),
            )
        )
    return ExecutorAttestationRegistry(attestations)


def _checks_from_payload(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key, enabled in value.items() if enabled]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []
