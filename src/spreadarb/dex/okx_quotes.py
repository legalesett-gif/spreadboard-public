"""Read-only OKX DEX quote client with no transaction-building functions."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from spreadarb.public_runtime import keychain

OKX_DEX_BASE_URL = "https://web3.okx.com"
OKX_DEX_QUOTE_PATH = "/api/v6/dex/aggregator/quote"
OKX_DEX_TOKENS_PATH = "/api/v6/dex/aggregator/all-tokens"
SOLANA_USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
ETHEREUM_USDT_ADDRESS = "0xdac17f958d2ee523a2206206994597c13d831ec7"
ERC20_DECIMALS_SELECTOR = "0x313ce567"
CHAIN_ALIASES = {
    "eth": "1",
    "ethereum": "1",
    "base": "8453",
    "bsc": "56",
    "bnb": "56",
    "arbitrum": "42161",
    "arb": "42161",
    "polygon": "137",
    "matic": "137",
    "sol": "501",
    "solana": "501",
}
CHAIN_NAMES = {
    "1": "ethereum",
    "8453": "base",
    "56": "bsc",
    "42161": "arbitrum",
    "137": "polygon",
    "501": "solana",
}
STABLE_BY_CHAIN = {
    "1": (ETHEREUM_USDT_ADDRESS, 6, "USDT"),
    "8453": ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6, "USDC"),
    "42161": ("0xaf88d065e77c8cc2239327c5ed3a432268e5831", 6, "USDC"),
    "137": ("0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", 6, "USDC"),
    "56": ("0x55d398326f99059ff775485246999027b3197955", 18, "USDT"),
    "501": (SOLANA_USDT_MINT, 6, "USDT"),
}
PUBLIC_RPC_BY_CHAIN = {
    "8453": (
        "https://base-rpc.publicnode.com",
        "https://developer-access-mainnet.base.org",
    ),
    "1": (
        "https://ethereum-rpc.publicnode.com",
        "https://eth.llamarpc.com",
    ),
    "42161": (
        "https://arbitrum-one-rpc.publicnode.com",
        "https://arb1.arbitrum.io/rpc",
    ),
    "137": (
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon-rpc.com",
    ),
    "56": (
        "https://bsc-rpc.publicnode.com",
        "https://bsc-dataseed.binance.org",
    ),
}

HttpGet = Callable[[str, dict[str, str]], dict[str, Any]]

OKX_DEX_RATE_STATE_PATH = os.environ.get(
    "SPREADBOARD_OKX_DEX_RATE_STATE_PATH",
    "/tmp/spreadboard-okx-dex-rate.state",
)
OKX_DEX_PRIORITY_STATE_PATH = os.environ.get(
    "SPREADBOARD_OKX_DEX_PRIORITY_STATE_PATH",
    "/tmp/spreadboard-okx-dex-priority.state",
)
OKX_DEX_MIN_REQUEST_INTERVAL_SECONDS = float(
    os.environ.get("SPREADBOARD_OKX_DEX_MIN_INTERVAL_S", "1.15")
)
OKX_DEX_RATE_LIMIT_RETRIES = 2


@dataclass(frozen=True, slots=True)
class OkxDexCredentials:
    api_key: str
    secret: str
    passphrase: str
    project_id: str | None = None


def load_okx_dex_credentials() -> OkxDexCredentials | None:
    api_key = keychain("SPREADARB/okx_dex/api_key") or keychain("SPREADARB/okx/api_key")
    secret = keychain("SPREADARB/okx_dex/secret") or keychain("SPREADARB/okx/secret")
    passphrase = keychain("SPREADARB/okx_dex/passphrase") or keychain(
        "SPREADARB/okx/passphrase"
    )
    project_id = keychain("SPREADARB/okx_dex/project_id") or keychain(
        "SPREADARB/okx_web3/project_id"
    )
    if not (api_key and secret and passphrase):
        return None
    return OkxDexCredentials(
        api_key=api_key,
        secret=secret,
        passphrase=passphrase,
        project_id=project_id,
    )


def chain_index(chain: str | None) -> str | None:
    if not chain:
        return None
    normalized = chain.strip().casefold()
    if normalized in CHAIN_NAMES:
        return normalized
    return CHAIN_ALIASES.get(normalized)


def quote_usdc_to_token(
    *,
    chain: str,
    token_address: str,
    notional_usd: Decimal,
    slippage_percent: str = "0.5",
    credentials: OkxDexCredentials | None = None,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    """Return a buy quote summary without an approval or transaction payload."""

    index = chain_index(chain)
    if index is None:
        return {"status": "blocked", "blockers": [f"unsupported_chain:{chain}"]}
    stable = STABLE_BY_CHAIN.get(index)
    if stable is None:
        return {"status": "blocked", "blockers": [f"stable_address_missing:{index}"]}
    credentials = credentials or load_okx_dex_credentials()
    if credentials is None:
        return {"status": "blocked", "blockers": ["missing_okx_dex_credentials"]}

    stable_address, stable_decimals, stable_symbol = stable
    normalized_token = _token_address(index, token_address)
    amount_units = int(Decimal(notional_usd) * (Decimal(10) ** stable_decimals))
    raw = _signed_get(
        params={
            "chainIndex": index,
            "amount": str(amount_units),
            "swapMode": "exactIn",
            "fromTokenAddress": stable_address,
            "toTokenAddress": normalized_token,
            "slippagePercent": slippage_percent,
        },
        credentials=credentials,
        http_get=http_get,
    )
    quote = _quote_payload(raw)
    if isinstance(quote, dict) and quote.get("status") == "blocked":
        return quote

    to_amount = _decimal_or_none(quote.get("toTokenAmount"))
    to_token = quote.get("toToken") if isinstance(quote.get("toToken"), dict) else {}
    decimals = int(to_token.get("decimal")) if str(to_token.get("decimal") or "").isdigit() else None
    out_qty = to_amount / (Decimal(10) ** decimals) if to_amount is not None and decimals is not None else None
    price = Decimal(notional_usd) / out_qty if out_qty is not None and out_qty > 0 else None
    return {
        "status": "ok",
        "chain_index": index,
        "from_token": stable_address,
        "from_token_symbol": stable_symbol,
        "to_token": normalized_token,
        "notional_usd": str(notional_usd),
        "out_qty": str(out_qty) if out_qty is not None else None,
        "to_token_decimals": decimals,
        "dex_buy_price_usd": str(price) if price is not None else None,
        "trade_fee_usd": quote.get("tradeFee"),
        "estimate_gas_fee": quote.get("estimateGasFee"),
        "slippage_bps": _slippage_bps(slippage_percent),
        "price_impact_pct": _first_present(
            quote,
            "priceImpactPercentage",
            "priceImpactPercent",
        ),
        # A read-only quote does not use OKX's whitelisted Broadcast API, which
        # is the only point at which MEV protection can be requested.
        "mev_protection": "not_enabled_quote_only",
        "router": quote.get("router"),
        "raw_code": raw.get("code"),
    }


def quote_token_to_usdc(
    *,
    chain: str,
    token_address: str,
    token_quantity: Decimal,
    token_decimals: int | None = None,
    slippage_percent: str = "0.5",
    credentials: OkxDexCredentials | None = None,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    """Return a sell quote summary without an approval or transaction payload."""

    index = chain_index(chain)
    if index is None:
        return {"status": "blocked", "blockers": [f"unsupported_chain:{chain}"]}
    stable = STABLE_BY_CHAIN.get(index)
    if stable is None:
        return {"status": "blocked", "blockers": [f"stable_address_missing:{index}"]}
    decimals = token_decimals
    if decimals is None:
        decimals = erc20_decimals(chain=chain, token_address=token_address)
    if decimals is None:
        return {"status": "blocked", "blockers": ["token_decimals_unresolved"]}
    credentials = credentials or load_okx_dex_credentials()
    if credentials is None:
        return {"status": "blocked", "blockers": ["missing_okx_dex_credentials"]}

    stable_address, stable_decimals, stable_symbol = stable
    normalized_token = _token_address(index, token_address)
    amount_units = int(Decimal(token_quantity) * (Decimal(10) ** decimals))
    raw = _signed_get(
        params={
            "chainIndex": index,
            "amount": str(amount_units),
            "swapMode": "exactIn",
            "fromTokenAddress": normalized_token,
            "toTokenAddress": stable_address,
            "slippagePercent": slippage_percent,
        },
        credentials=credentials,
        http_get=http_get,
    )
    quote = _quote_payload(raw)
    if isinstance(quote, dict) and quote.get("status") == "blocked":
        return quote

    to_amount = _decimal_or_none(quote.get("toTokenAmount"))
    out_qty = to_amount / (Decimal(10) ** stable_decimals) if to_amount is not None else None
    price = out_qty / Decimal(token_quantity) if out_qty is not None and token_quantity > 0 else None
    return {
        "status": "ok",
        "chain_index": index,
        "from_token": normalized_token,
        "from_token_quantity": str(token_quantity),
        "from_token_decimals": decimals,
        "to_token": stable_address,
        "to_token_symbol": stable_symbol,
        "out_qty": str(out_qty) if out_qty is not None else None,
        "dex_sell_price_usd": str(price) if price is not None else None,
        "trade_fee_usd": quote.get("tradeFee"),
        "estimate_gas_fee": quote.get("estimateGasFee"),
        "slippage_bps": _slippage_bps(slippage_percent),
        "price_impact_pct": _first_present(
            quote,
            "priceImpactPercentage",
            "priceImpactPercent",
        ),
        "mev_protection": "not_enabled_quote_only",
        "router": quote.get("router"),
        "raw_code": raw.get("code"),
    }


def list_tokens(
    *,
    chain: str,
    credentials: OkxDexCredentials | None = None,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    """Return OKX's signed token catalogue without transaction data."""

    index = chain_index(chain)
    if index is None:
        return {"status": "blocked", "blockers": [f"unsupported_chain:{chain}"]}
    credentials = credentials or load_okx_dex_credentials()
    if credentials is None:
        return {"status": "blocked", "blockers": ["missing_okx_dex_credentials"]}
    raw = _signed_get(
        params={"chainIndex": index},
        credentials=credentials,
        http_get=http_get,
        path=OKX_DEX_TOKENS_PATH,
    )
    if str(raw.get("code")) != "0":
        code = str(raw.get("code") or "")
        blocker = (
            "okx_dex_geo_blocked"
            if code == "53015"
            else f"okx_dex_tokens:{raw.get('msg') or code or 'unknown'}"
        )
        return {"status": "blocked", "blockers": [blocker]}
    tokens = []
    for item in raw.get("data") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("tokenSymbol") or "").upper().strip()
        address = str(item.get("tokenContractAddress") or "").strip()
        decimals = item.get("decimals")
        if not symbol or not address or not str(decimals or "").isdigit():
            continue
        tokens.append(
            {
                "symbol": symbol,
                "name": str(item.get("tokenName") or "").strip() or None,
                "address": address,
                "decimals": int(decimals),
                "chain_index": index,
            }
        )
    return {"status": "ok", "chain_index": index, "tokens": tokens}


def erc20_decimals(*, chain: str, token_address: str) -> int | None:
    index = chain_index(chain)
    if index is None:
        return None
    for rpc_url in PUBLIC_RPC_BY_CHAIN.get(index, ()):
        result = _eth_call(
            rpc_url=rpc_url,
            token_address=token_address,
            data=ERC20_DECIMALS_SELECTOR,
        )
        if isinstance(result, str) and result.startswith("0x"):
            try:
                return int(result, 16)
            except ValueError:
                continue
    return None


def _signed_get(
    *,
    params: dict[str, str],
    credentials: OkxDexCredentials,
    http_get: HttpGet | None,
    path: str = OKX_DEX_QUOTE_PATH,
) -> dict[str, Any]:
    query_string = urlencode(params)
    query = f"?{query_string}"
    getter = http_get or _http_get
    attempts = 1 if http_get is not None else OKX_DEX_RATE_LIMIT_RETRIES + 1
    result: dict[str, Any] = {}
    for _attempt in range(attempts):
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        prehash = f"{timestamp}GET{path}{query}"
        signature = base64.b64encode(
            hmac.new(
                credentials.secret.encode("utf-8"),
                prehash.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": credentials.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": credentials.passphrase,
        }
        if credentials.project_id:
            headers["OK-ACCESS-PROJECT"] = credentials.project_id
        result = getter(f"{OKX_DEX_BASE_URL}{path}{query}", headers)
        if not _is_rate_limited(result):
            break
    return result


def _http_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    _wait_for_shared_request_slot()
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 SpreadBoard/1.0", **headers},
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"code": str(exc.code), "msg": str(exc.reason)}
        if isinstance(payload, dict):
            payload.setdefault("http_status", exc.code)
            return payload
        return {"code": str(exc.code), "msg": str(exc.reason)}
    except URLError as exc:
        return {"code": "url_error", "msg": str(exc.reason)[:240]}
    if not isinstance(payload, dict):
        raise RuntimeError("OKX DEX returned non-object JSON")
    return payload


def _wait_for_shared_request_slot() -> None:
    """Coordinate OKX Web3 requests across the web and discovery processes."""

    _wait_for_priority_window()
    state_path = OKX_DEX_RATE_STATE_PATH
    with open(state_path, "a+", encoding="ascii") as state:
        fcntl.flock(state.fileno(), fcntl.LOCK_EX)
        try:
            state.seek(0)
            try:
                last_started = float(state.read().strip() or "0")
            except ValueError:
                last_started = 0.0
            remaining = OKX_DEX_MIN_REQUEST_INTERVAL_SECONDS - (
                time.monotonic() - last_started
            )
            if remaining > 0:
                time.sleep(remaining)
            state.seek(0)
            state.truncate()
            state.write(str(time.monotonic()))
            state.flush()
        finally:
            fcntl.flock(state.fileno(), fcntl.LOCK_UN)


def _wait_for_priority_window() -> None:
    """Let the bounded current-board rotation precede broad DEX discovery.

    Only discovery workers opt into this background gate. Interactive chart
    samples keep working, and every caller still shares the provider's normal
    request-rate lock below. A timestamp makes a worker crash self-healing.
    """

    if os.environ.get("SPREADBOARD_OKX_DEX_BACKGROUND", "").strip().casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    path = Path(OKX_DEX_PRIORITY_STATE_PATH)
    stale_after = max(
        60.0,
        float(os.environ.get("SPREADBOARD_OKX_DEX_PRIORITY_STALE_SECONDS", "300")),
    )
    while path.exists():
        try:
            if time.time() - path.stat().st_mtime > stale_after:
                return
        except OSError:
            return
        time.sleep(0.25)


def _is_rate_limited(payload: dict[str, Any]) -> bool:
    message = f"{payload.get('code', '')} {payload.get('msg', '')}".casefold()
    return "too many requests" in message or "rate limit" in message


def _quote_payload(raw: dict[str, Any]) -> dict[str, Any]:
    if str(raw.get("code")) != "0":
        code = str(raw.get("code") or "")
        blocker = "okx_dex_geo_blocked" if code == "53015" else f"okx_dex_quote:{raw.get('msg') or code or 'unknown'}"
        return {"status": "blocked", "blockers": [blocker], "raw": raw}
    data = raw.get("data")
    quote = data[0] if isinstance(data, list) and data else None
    if not isinstance(quote, dict):
        return {"status": "blocked", "blockers": ["okx_dex_quote_empty"], "raw": raw}
    return quote


def _slippage_bps(value: str) -> int | None:
    try:
        return int(Decimal(value) * Decimal("100"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _token_address(index: str, token_address: str) -> str:
    return token_address if index == "501" else token_address.lower()


def _eth_call(*, rpc_url: str, token_address: str, data: str) -> Any:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": token_address.lower(), "data": data}, "latest"],
        }
    ).encode("utf-8")
    request = Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 SpreadBoard/1.0"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    return payload.get("result") if isinstance(payload, dict) else None


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
