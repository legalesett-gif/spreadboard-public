"""Exact account data imported by an operator-side read-only sync.

Exchange credentials deliberately stay off the public server. A local worker
reads the private funding ledger plus quantity-independent venue/accounting
reference prices, strips them down to per-position totals/marks, and writes one
atomic snapshot into the production runtime. Portfolio totals use that snapshot
only when it still matches the saved position inputs.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any


RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))
DEFAULT_PATH = RUNTIME_DIR / "portfolio_funding.json"
# v2 changes marks from full-size liquidation quotes to quantity-independent
# accounting references. Rejecting v1 prevents old exit economics from being
# silently presented as current mark-to-market PnL after this release.
SCHEMA = "spreadboard.portfolio_funding.v2"
DEFAULT_MAX_AGE_SECONDS = 900.0


def position_fingerprint(position: dict[str, Any]) -> str:
    """Stable identity for the fields that bound a private funding query."""

    legs = []
    for side in ("long", "short"):
        legs.append(
            {
                "side": side,
                "venue": str(position.get(f"{side}_venue") or ""),
                "market_type": str(position.get(f"{side}_market_type") or ""),
                "symbol": str(position.get(f"{side}_symbol") or ""),
                "quantity": _canonical_number(position.get(f"{side}_quantity")),
                "entry_price": _canonical_number(position.get(f"{side}_entry_price")),
            }
        )
    payload = {
        "user_id": int(position.get("user_id") or 0),
        "position_id": int(position.get("id") or 0),
        "status": str(position.get("status") or ""),
        "opened_at": str(position.get("opened_at") or ""),
        "closed_at": str(position.get("closed_at") or ""),
        "legs": legs,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return {}
    return payload


def exact_funding(
    position: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    now: float | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, Any]:
    """Return an exact funding total only when source and position still match."""

    if not _futures_legs(position):
        return {
            "known": True,
            "amount_usd": 0.0,
            "status": "not_applicable",
            "source": "not_applicable",
            "event_count": 0,
            "synced_at": None,
        }
    item = ((snapshot or {}).get("positions") or {}).get(
        f"{int(position.get('user_id') or 0)}:{int(position.get('id') or 0)}"
    )
    if not isinstance(item, dict):
        return _legacy_or_unknown(position, status="not_connected")
    if str(item.get("position_fingerprint") or "") != position_fingerprint(position):
        return _legacy_or_unknown(position, status="position_changed")
    if str(item.get("status") or "") != "ok":
        return _legacy_or_unknown(position, status=str(item.get("status") or "sync_error"))
    synced_at = str(item.get("synced_at") or (snapshot or {}).get("generated_at") or "")
    age = _age_seconds(synced_at, now=now)
    limit = (
        float(max_age_seconds)
        if max_age_seconds is not None
        else float(
            os.environ.get(
                "SPREADBOARD_PORTFOLIO_FUNDING_MAX_AGE_SECONDS",
                DEFAULT_MAX_AGE_SECONDS,
            )
        )
    )
    if position.get("status") == "open" and (age is None or age > max(60.0, limit)):
        return _legacy_or_unknown(position, status="stale", synced_at=synced_at)
    try:
        amount = float(item.get("amount_usd") or 0.0)
        event_count = int(item.get("event_count") or 0)
    except (TypeError, ValueError):
        return _legacy_or_unknown(position, status="invalid_snapshot", synced_at=synced_at)
    return {
        "known": True,
        "amount_usd": amount,
        "status": "exact",
        "source": "private_exchange_ledger",
        "event_count": event_count,
        "synced_at": synced_at or None,
        "latest_event_at": item.get("latest_event_at"),
    }


def exact_marks(
    position: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    now: float | None = None,
    max_age_seconds: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Return accounting reference marks only while position/snapshot match."""

    item = ((snapshot or {}).get("positions") or {}).get(
        f"{int(position.get('user_id') or 0)}:{int(position.get('id') or 0)}"
    )
    if not isinstance(item, dict):
        return {}
    if str(item.get("position_fingerprint") or "") != position_fingerprint(position):
        return {}
    synced_at = str(item.get("synced_at") or (snapshot or {}).get("generated_at") or "")
    limit = (
        float(max_age_seconds)
        if max_age_seconds is not None
        else float(
            os.environ.get(
                "SPREADBOARD_PORTFOLIO_MARK_MAX_AGE_SECONDS",
                DEFAULT_MAX_AGE_SECONDS,
            )
        )
    )
    age = _age_seconds(synced_at, now=now)
    if position.get("status") == "open" and (age is None or age > max(60.0, limit)):
        return {}
    marks = item.get("marks") if isinstance(item.get("marks"), dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for side in ("long", "short"):
        mark = marks.get(side) if isinstance(marks.get(side), dict) else None
        if not mark or str(mark.get("status") or "") != "ok":
            continue
        mark_age = _age_seconds(
            str(mark.get("quoted_at") or synced_at),
            now=now,
        )
        if position.get("status") == "open" and (
            mark_age is None or mark_age > max(60.0, limit)
        ):
            continue
        try:
            price = float(mark["price_usd"])
            quantity = float(mark["quantity"])
            expected = float(position.get(f"{side}_quantity") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0 or quantity <= 0 or expected <= 0:
            continue
        if abs(quantity - expected) > max(1e-9, expected * 1e-9):
            continue
        result[side] = {
            **mark,
            "price_usd": price,
            "quantity": quantity,
            "synced_at": synced_at,
        }
    return result


def _legacy_or_unknown(
    position: dict[str, Any], *, status: str, synced_at: str | None = None
) -> dict[str, Any]:
    rows = position.get("funding_cashflows") or []
    if rows:
        return {
            "known": True,
            "amount_usd": sum(float(row.get("amount_usd") or 0.0) for row in rows),
            "status": "legacy_manual",
            "source": "legacy_manual",
            "event_count": len(rows),
            "synced_at": None,
        }
    return {
        "known": False,
        "amount_usd": None,
        "status": status,
        "source": "unavailable",
        "event_count": 0,
        "synced_at": synced_at or None,
    }


def _futures_legs(position: dict[str, Any]) -> list[str]:
    return [
        side
        for side in ("long", "short")
        if str(position.get(f"{side}_market_type") or "").casefold() == "futures"
    ]


def _canonical_number(value: Any) -> str:
    try:
        return format(float(value), ".15g")
    except (TypeError, ValueError):
        return ""


def _age_seconds(value: str, *, now: float | None = None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (now if now is not None else time.time()) - parsed.timestamp())
