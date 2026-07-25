"""Small read-only order-book math helpers."""

from __future__ import annotations


def depth_weighted_price(levels: list[list[float]], notional_usd: float) -> float | None:
    if not levels or notional_usd <= 0:
        return None
    remaining = notional_usd
    quantity = 0.0
    spent = 0.0
    for price, amount in levels:
        if price <= 0 or amount <= 0:
            continue
        level_notional = price * amount
        take_notional = min(remaining, level_notional)
        take_quantity = take_notional / price
        quantity += take_quantity
        spent += take_quantity * price
        remaining -= take_notional
        if remaining <= 1e-9:
            break
    if remaining > 1e-6 or quantity <= 0:
        return None
    return spent / quantity
