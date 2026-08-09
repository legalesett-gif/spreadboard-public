"""Public attestation that execution is outside the SpreadBoard trust boundary."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse


FORBIDDEN_IN_SPREADBOARD = (
    "exchange API credentials",
    "private balances or positions",
    "order placement or cancellation",
    "borrow, repay, convert or transfer mutations",
    "wallet approvals, signatures or broadcasts",
)

REQUIRED_SEPARATE_CONTROLS = (
    "separate origin, deployment and database",
    "hardware-backed or dedicated secret store",
    "per-route authorization with size and expiry",
    "fresh private-account and public-book preflight",
    "idempotent two-leg state machine and kill switch",
    "append-only audit trail and independent reconciliation",
)


def status() -> dict[str, Any]:
    research_origin = _origin(os.environ.get("SPREADBOARD_PUBLIC_URL", ""))
    executor_origin = _origin(os.environ.get("SPREADBOARD_EXECUTOR_URL", ""))
    separate = bool(
        executor_origin
        and executor_origin.startswith("https://")
        and executor_origin != research_origin
    )
    return {
        "mode": "separate_product_boundary",
        "research_product": "read_only",
        "executor_origin": executor_origin if separate else None,
        "separate_origin_verified": separate,
        # This application intentionally has no handoff or order endpoint.
        "handoff_enabled": False,
        "exchange_credentials_loaded": False,
        "order_capabilities": [],
        "forbidden_in_spreadboard": list(FORBIDDEN_IN_SPREADBOARD),
        "required_separate_controls": list(REQUIRED_SEPARATE_CONTROLS),
        "verdict": "separate_executor_required" if not separate else "separate_origin_reserved",
    }


def _origin(value: str) -> str | None:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"
