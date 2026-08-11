"""Deterministic, explainable route research score with no model API calls."""

from __future__ import annotations

import math
from typing import Any


def evaluate(
    route: dict[str, Any] | None,
    *,
    windows: dict[str, Any] | None = None,
    historical: bool = False,
) -> dict[str, Any]:
    """Score evidence quality and route characteristics from 0 to 100."""
    if not isinstance(route, dict) or not route:
        return {
            "score": None,
            "confidence": 0,
            "label": "Insufficient data",
            "components": {},
            "planning_buffer_pct": None,
            "planning_buffer_label": "Unavailable",
            "reasons": ["No live or retained route is available for this token."],
            "method": "deterministic_public_market_evidence",
        }

    windows = windows or {}
    daily = _first_number(
        route.get("funding_24h_pct"),
        route.get("funding_projected_24h_pct"),
        route.get("funding_daily_pct"),
        windows.get("1d"),
    )
    carry = _clamp(max(0.0, daily or 0.0) * 50.0, 0.0, 25.0)

    persistence = 0.0
    persistence_detail = []
    for label, maximum, days in (("1d", 7.0, 1), ("7d", 10.0, 7), ("30d", 8.0, 30)):
        value = _number(windows.get(label))
        if value is None:
            persistence_detail.append(f"{label} unavailable")
            continue
        per_day = value / days
        if per_day > 0:
            persistence += maximum * _clamp(per_day / 0.25, 0.2, 1.0)
            persistence_detail.append(f"{label} positive")
        else:
            persistence_detail.append(f"{label} non-positive")

    depth = _number(route.get("depth_usd"))
    volumes = [
        value
        for value in (
            _number(route.get("long_volume_24h_usd")),
            _number(route.get("short_volume_24h_usd")),
            _number(route.get("metadata_volume_24h_usd")),
        )
        if value is not None and value > 0
    ]
    min_volume = min(volumes) if volumes else None
    if depth is not None and depth > 0:
        liquidity = 20.0 * _log_scale(depth, low=50.0, high=250_000.0)
        liquidity_basis = f"matched depth ${depth:,.0f}"
    elif min_volume is not None:
        liquidity = 14.0 * _log_scale(min_volume, low=10_000.0, high=50_000_000.0)
        liquidity_basis = f"minimum leg volume ${min_volume:,.0f}"
    else:
        liquidity = 0.0
        liquidity_basis = "liquidity unavailable"

    freshness = str(route.get("freshness") or "").casefold()
    age = _number(route.get("age_min"))
    executable = _first_number(
        route.get("depth_weighted_spread_pct"),
        route.get("executable_spread_pct"),
        route.get("displayed_open_spread_pct"),
    )
    execution = 0.0
    if not historical and freshness in {"fresh", "live"}:
        execution += 6.0
    elif not historical and (age is None or age <= 10):
        execution += 4.0
    if executable is not None:
        execution += 5.0
    if route.get("long_market_symbol") and route.get("short_market_symbol"):
        execution += 4.0

    blockers = [str(item).casefold() for item in route.get("blockers") or []]
    unresolved_identity = bool(route.get("mirage_guarded") or route.get("identity_unresolved"))
    integrity = 0.0
    if not unresolved_identity:
        integrity += 6.0
    if not any("deposit" in item or "withdraw" in item or "deliver" in item for item in blockers):
        integrity += 5.0
    if not blockers:
        integrity += 4.0

    components = {
        "carry": _component(carry, 25, "Current or latest 24h net carry"),
        "persistence": _component(persistence, 25, ", ".join(persistence_detail)),
        "liquidity": _component(liquidity, 20, liquidity_basis),
        "execution": _component(execution, 15, "Freshness, executable spread and exact symbols"),
        "integrity": _component(integrity, 15, "Identity, transfer warnings and blockers"),
    }
    score = round(sum(item["value"] for item in components.values()), 1)

    evidence = 20.0 if daily is not None else 0.0
    evidence += sum(15.0 for label in ("1d", "7d", "30d") if _number(windows.get(label)) is not None)
    evidence += 15.0 if depth is not None or min_volume is not None else 0.0
    evidence += 10.0 if executable is not None else 0.0
    evidence += 10.0 if not unresolved_identity else 0.0
    confidence = int(round(_clamp(evidence, 0.0, 100.0)))

    spread = abs(executable or 0.0)
    buffer = 35.0 + min(20.0, spread * 2.0)
    buffer += 10.0 if depth is None or depth < 500 else 0.0
    buffer += 10.0 if _number(windows.get("7d")) is None else 0.0
    buffer += 15.0 if unresolved_identity else 0.0
    buffer += 10.0 if historical else 0.0
    buffer = round(_clamp(buffer, 35.0, 100.0), 0)

    reasons = []
    if daily is not None:
        reasons.append(f"Net carry currently {daily:+.3f}% per 24h.")
    if _number(windows.get("7d")) is None:
        reasons.append("Seven-day settled history is not complete yet.")
    if unresolved_identity:
        reasons.append("Token identity is unresolved; verify the exact contract before considering transfer.")
    if historical:
        reasons.append("This is a retained radar route, not a current executable row.")
    if not reasons:
        reasons.append("Current route evidence is complete; inspect the exact pair and component breakdown.")

    return {
        "score": score,
        "confidence": confidence,
        "label": _label(score, confidence),
        "components": components,
        "planning_buffer_pct": buffer,
        "planning_buffer_label": f"Model stress reserve: {buffer:.0f}% of futures notional per leg",
        "reasons": reasons[:4],
        "method": "deterministic_public_market_evidence",
        "disclaimer": "Research ranking and stress-planning assumption only; not personalized investment or liquidation advice.",
    }


def _component(value: float, maximum: float, detail: str) -> dict[str, Any]:
    return {"value": round(_clamp(value, 0.0, maximum), 1), "max": maximum, "detail": detail}


def _label(score: float, confidence: int) -> str:
    if confidence < 45:
        return "Low-confidence radar"
    if score >= 75:
        return "Strong research candidate"
    if score >= 55:
        return "Worth deeper review"
    if score >= 35:
        return "Speculative / incomplete"
    return "Weak current evidence"


def _log_scale(value: float, *, low: float, high: float) -> float:
    if value <= low:
        return 0.0
    return _clamp(math.log(value / low) / math.log(high / low), 0.0, 1.0)


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))
