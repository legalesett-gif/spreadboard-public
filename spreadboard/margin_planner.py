"""Transparent, account-input margin stress planning.

No exchange credential or private balance is read here.  The member supplies
the exact maintenance tier, leverage and collateral visible in their venue
account; SpreadBoard combines those values with a public route stress move.
"""

from __future__ import annotations

import math
from typing import Any


def calculate(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("margin_inputs_required")
    mode = str(payload.get("account_mode") or "").strip().casefold()
    if mode not in {"isolated", "cross"}:
        raise ValueError("account_mode_must_be_isolated_or_cross")

    notional = _required(payload, "position_notional_usd", minimum=0.01, maximum=100_000_000)
    leverage = _required(payload, "leverage", minimum=1.0, maximum=125.0)
    maintenance_pct = _required(
        payload, "maintenance_margin_pct", minimum=0.000001, maximum=50.0
    )
    stress_pct = _required(payload, "stress_move_pct", minimum=0.0, maximum=100.0)
    entry_fee_pct = _optional(payload, "entry_fee_pct", maximum=10.0)
    exit_fee_pct = _optional(payload, "exit_fee_pct", maximum=10.0)
    slippage_pct = _optional(payload, "exit_slippage_pct", maximum=25.0)
    adverse_funding_pct = _optional(payload, "adverse_funding_pct", maximum=25.0)
    borrow_cost_pct = _optional(payload, "borrow_cost_pct", maximum=25.0)
    fixed_route_cost_usd = _optional(
        payload, "gas_transfer_cost_usd", maximum=1_000_000.0
    )
    expected_gross_pct = (
        None
        if payload.get("expected_gross_edge_pct") in (None, "")
        else _number(
            payload.get("expected_gross_edge_pct"),
            key="expected_gross_edge_pct",
            minimum=-100.0,
            maximum=100.0,
        )
    )

    if mode == "isolated":
        available = _required(
            payload, "allocated_collateral_usd", minimum=0.0, maximum=100_000_000
        )
        account_equity = None
        other_reserve = 0.0
    else:
        account_equity = _required(
            payload, "account_equity_usd", minimum=0.0, maximum=100_000_000
        )
        other_reserve = _optional(
            payload, "other_positions_reserve_usd", maximum=100_000_000
        ) + _optional(payload, "cash_reserve_usd", maximum=100_000_000)
        available = max(0.0, account_equity - other_reserve)

    initial_margin = notional / leverage
    maintenance_margin = notional * maintenance_pct / 100.0
    stress_loss = notional * stress_pct / 100.0
    cost_pct = entry_fee_pct + exit_fee_pct + slippage_pct + adverse_funding_pct + borrow_cost_pct
    stress_costs = notional * cost_pct / 100.0 + fixed_route_cost_usd
    survival_requirement = maintenance_margin + stress_loss + stress_costs
    recommended_collateral = max(initial_margin, survival_requirement)
    stress_headroom = available - survival_requirement
    opening_headroom = available - initial_margin
    effective_leverage = notional / available if available > 0 else None
    loss_capacity_pct = max(0.0, (available - maintenance_margin - stress_costs) / notional * 100.0)

    if available + 1e-9 < initial_margin:
        verdict = "cannot_open_with_inputs"
    elif available + 1e-9 < survival_requirement:
        verdict = "stress_shortfall"
    elif stress_headroom < notional * 0.10:
        verdict = "thin_stress_headroom"
    else:
        verdict = "within_entered_stress"

    return {
        "ok": True,
        "account_mode": mode,
        "position_notional_usd": _money(notional),
        "available_collateral_usd": _money(available),
        "account_equity_usd": _money(account_equity) if account_equity is not None else None,
        "other_positions_and_cash_reserve_usd": _money(other_reserve),
        "initial_margin_usd": _money(initial_margin),
        "maintenance_margin_usd": _money(maintenance_margin),
        "public_stress_loss_usd": _money(stress_loss),
        "entered_costs_usd": _money(stress_costs),
        "entered_costs_pct": round(cost_pct, 6),
        "fixed_route_cost_usd": _money(fixed_route_cost_usd),
        "expected_gross_edge_pct": round(expected_gross_pct, 6) if expected_gross_pct is not None else None,
        "expected_net_edge_usd": (
            _money(notional * expected_gross_pct / 100.0 - stress_costs)
            if expected_gross_pct is not None
            else None
        ),
        "expected_net_edge_pct": (
            round(expected_gross_pct - cost_pct - fixed_route_cost_usd / notional * 100.0, 6)
            if expected_gross_pct is not None
            else None
        ),
        "survival_requirement_usd": _money(survival_requirement),
        "recommended_collateral_usd": _money(recommended_collateral),
        "opening_headroom_usd": _money(opening_headroom),
        "stress_headroom_usd": _money(stress_headroom),
        "effective_leverage": round(effective_leverage, 4) if effective_leverage is not None else None,
        "loss_capacity_before_maintenance_pct": round(loss_capacity_pct, 4),
        "verdict": verdict,
        "inputs": {
            "leverage": leverage,
            "maintenance_margin_pct": maintenance_pct,
            "stress_move_pct": stress_pct,
            "entry_fee_pct": entry_fee_pct,
            "exit_fee_pct": exit_fee_pct,
            "exit_slippage_pct": slippage_pct,
            "adverse_funding_pct": adverse_funding_pct,
            "borrow_cost_pct": borrow_cost_pct,
        },
        "limitations": [
            "The stress move is historical public evidence, not a maximum possible move.",
            "The venue maintenance tier, leverage, equity and other-position reserve must match the member's live account.",
            "Loss capacity is not an exchange liquidation price; mark rules, tier changes, fees and cross-position PnL can change liquidation.",
            "Collateral on another venue or in a spot wallet cannot protect this futures account.",
        ],
        "method": "account_input_margin_stress_v1",
    }


def _required(payload: dict[str, Any], key: str, *, minimum: float, maximum: float) -> float:
    if payload.get(key) in (None, ""):
        raise ValueError(f"{key}_required")
    return _number(payload.get(key), key=key, minimum=minimum, maximum=maximum)


def _optional(payload: dict[str, Any], key: str, *, maximum: float) -> float:
    if payload.get(key) in (None, ""):
        return 0.0
    return _number(payload.get(key), key=key, minimum=0.0, maximum=maximum)


def _number(value: Any, *, key: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key}_must_be_numeric") from None
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError(f"{key}_out_of_range")
    return number


def _money(value: float) -> float:
    return round(float(value), 2)
