"""Read-only Velora (ParaSwap) quotes with no transaction-building surface."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://api.paraswap.io/prices"
API_VERSION = "6.2"

# SpreadBoard compares USDT markets. Keep the on-chain reference in USDT too;
# chains without a verified USDT address are deliberately unsupported here.
USDT_BY_CHAIN = {
    "1": ("0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
    "56": ("0x55d398326f99059ff775485246999027b3197955", 18),
}

HttpGet = Callable[[str, Mapping[str, str], float], Any]


def quote_usdt_to_token(
    *,
    chain: str,
    token_address: str,
    token_decimals: int,
    notional_usdt: Decimal,
    http_get: HttpGet | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Return an exact-input USDT buy quote, without calldata or execution."""

    stable = USDT_BY_CHAIN.get(str(chain))
    if stable is None:
        return {"status": "blocked", "blockers": [f"velora_chain_unsupported:{chain}"]}
    usdt_address, usdt_decimals = stable
    amount = int(notional_usdt * (Decimal(10) ** usdt_decimals))
    route = _price(
        chain=str(chain),
        src_token=usdt_address,
        src_decimals=usdt_decimals,
        dest_token=token_address,
        dest_decimals=token_decimals,
        amount=amount,
        http_get=http_get,
        timeout=timeout,
    )
    if route.get("status") == "blocked":
        return route
    out_units = _decimal(route.get("destAmount"))
    out_qty = out_units / (Decimal(10) ** token_decimals) if out_units is not None else None
    price = notional_usdt / out_qty if out_qty is not None and out_qty > 0 else None
    if price is None:
        return {"status": "blocked", "blockers": ["velora_buy_quantity_unavailable"]}
    return {
        "status": "ok",
        "chain_index": str(chain),
        "from_token": usdt_address,
        "from_token_symbol": "USDT",
        "to_token": token_address,
        "out_qty": str(out_qty),
        "to_token_decimals": token_decimals,
        "dex_buy_price_usd": str(price),
        "gas_estimate_usd": route.get("gasCostUSD"),
        "price_impact_pct": _price_impact_pct(route),
        "slippage_bps": None,
        "mev_protection": "provider_quote_only",
        "route_plan": _route_plan(route),
    }


def quote_token_to_usdt(
    *,
    chain: str,
    token_address: str,
    token_decimals: int,
    token_quantity: Decimal,
    http_get: HttpGet | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Return an exact-input token sell quote, without calldata or execution."""

    stable = USDT_BY_CHAIN.get(str(chain))
    if stable is None:
        return {"status": "blocked", "blockers": [f"velora_chain_unsupported:{chain}"]}
    usdt_address, usdt_decimals = stable
    amount = int(token_quantity * (Decimal(10) ** token_decimals))
    route = _price(
        chain=str(chain),
        src_token=token_address,
        src_decimals=token_decimals,
        dest_token=usdt_address,
        dest_decimals=usdt_decimals,
        amount=amount,
        http_get=http_get,
        timeout=timeout,
    )
    if route.get("status") == "blocked":
        return route
    out_units = _decimal(route.get("destAmount"))
    out_usdt = out_units / (Decimal(10) ** usdt_decimals) if out_units is not None else None
    price = out_usdt / token_quantity if out_usdt is not None and token_quantity > 0 else None
    if price is None:
        return {"status": "blocked", "blockers": ["velora_sell_quantity_unavailable"]}
    return {
        "status": "ok",
        "chain_index": str(chain),
        "from_token": token_address,
        "from_token_quantity": str(token_quantity),
        "from_token_decimals": token_decimals,
        "to_token": usdt_address,
        "to_token_symbol": "USDT",
        "out_qty": str(out_usdt),
        "dex_sell_price_usd": str(price),
        "gas_estimate_usd": route.get("gasCostUSD"),
        "price_impact_pct": _price_impact_pct(route),
        "slippage_bps": None,
        "mev_protection": "provider_quote_only",
        "route_plan": _route_plan(route),
    }


def _price(
    *,
    chain: str,
    src_token: str,
    src_decimals: int,
    dest_token: str,
    dest_decimals: int,
    amount: int,
    http_get: HttpGet | None,
    timeout: float,
) -> dict[str, Any]:
    params = {
        "srcToken": src_token,
        "srcDecimals": str(src_decimals),
        "destToken": dest_token,
        "destDecimals": str(dest_decimals),
        "amount": str(amount),
        "side": "SELL",
        "network": chain,
        "version": API_VERSION,
    }
    url = f"{BASE_URL}?{urlencode(params)}"
    try:
        payload = (
            http_get(url, {"User-Agent": "SpreadBoard/1.0 read-only research"}, timeout)
            if http_get is not None
            else _fetch_json(url, timeout=timeout)
        )
    except HTTPError as exc:
        return {"status": "blocked", "blockers": [f"velora_http_{exc.code}"]}
    except (TimeoutError, URLError, OSError) as exc:
        return {"status": "blocked", "blockers": [f"velora_transport:{type(exc).__name__}"]}
    if not isinstance(payload, dict):
        return {"status": "blocked", "blockers": ["velora_invalid_payload"]}
    route = payload.get("priceRoute")
    if not isinstance(route, dict):
        error = str(payload.get("error") or payload.get("message") or "quote_empty")[:120]
        return {"status": "blocked", "blockers": [f"velora_quote:{error}"]}
    return route


def _fetch_json(url: str, *, timeout: float) -> Any:
    request = Request(url, headers={"User-Agent": "SpreadBoard/1.0 read-only research"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _price_impact_pct(route: Mapping[str, Any]) -> str | None:
    src = _decimal(route.get("srcUSD"))
    dest = _decimal(route.get("destUSD"))
    if src is None or dest is None or src <= 0:
        return None
    return str((dest / src - Decimal(1)) * Decimal(100))


def _route_plan(route: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for best in route.get("bestRoute") or []:
        if not isinstance(best, dict):
            continue
        for swap in best.get("swaps") or []:
            if not isinstance(swap, dict):
                continue
            for item in swap.get("swapExchanges") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("exchange") or "").strip()
                if name and name not in names:
                    names.append(name)
    return names
