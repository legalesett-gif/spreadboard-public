"""Public deposit/withdraw rail metadata for spot legs."""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any

import ccxt

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_CACHE_PATH = RUNTIME_DIR / "public_transfer_rails.json"
DEFAULT_TTL_SECONDS = 10 * 60

VENUE_IDS = {
    "Binance": "binance",
    "Bybit": "bybit",
    "Bitget": "bitget",
    "OKX": "okx",
    "Gate": "gateio",
    "Mexc": "mexc",
    "Kucoin": "kucoin",
    "Bingx": "bingx",
    "Coinbase": "coinbaseexchange",
    "Kraken": "kraken",
}


def load_public_rails(path: Path | str = DEFAULT_CACHE_PATH) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rails = payload.get("rails") if isinstance(payload, dict) else {}
    return rails if isinstance(rails, dict) else {}


def rail_state(
    rails: dict[str, dict[str, Any]],
    venue: str | None,
    token: str,
) -> dict[str, Any]:
    venue_rows = rails.get(str(venue or ""))
    if not isinstance(venue_rows, dict):
        return {}
    state = venue_rows.get(str(token).upper())
    return state if isinstance(state, dict) else {}


def transfer_compatibility(
    long_state: dict[str, Any],
    short_state: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate whether a spot asset can move from buy venue to sell venue."""

    long_networks = _usable_networks(long_state, "withdraw")
    short_networks = _usable_networks(short_state, "deposit")
    if not long_networks or not short_networks:
        return {"status": "unknown", "shared_networks": []}

    shared: list[str] = []
    for network in sorted(set(long_networks) & set(short_networks)):
        long_contract = long_networks[network]
        short_contract = short_networks[network]
        if long_contract and short_contract and long_contract.casefold() != short_contract.casefold():
            continue
        shared.append(network)
    return {
        "status": "compatible" if shared else "incompatible",
        "shared_networks": shared,
    }


def _usable_networks(state: dict[str, Any], direction: str) -> dict[str, str | None]:
    networks = state.get("networks") if isinstance(state, dict) else None
    if not isinstance(networks, list):
        return {}
    output: dict[str, str | None] = {}
    for item in networks:
        if not isinstance(item, dict) or item.get(direction) is not True:
            continue
        network = _normalize_network(item.get("network"))
        if network:
            output[network] = str(item.get("contract") or "").strip() or None
    return output


def _normalize_network(value: Any) -> str:
    text = "".join(ch for ch in str(value or "").casefold() if ch.isalnum())
    return {
        "eth": "ethereum",
        "erc20": "ethereum",
        "ethereum": "ethereum",
        "bep20": "bsc",
        "bep20bsc": "bsc",
        "binancesmartchain": "bsc",
        "trx": "tron",
        "trc20": "tron",
    }.get(text, text)


def refresh_public_rails(
    snapshot: dict[str, Any],
    *,
    path: Path | str = DEFAULT_CACHE_PATH,
    force: bool = False,
    now: float | None = None,
    max_workers: int = 8,
) -> dict[str, Any]:
    current_time = time.time() if now is None else now
    path = Path(path)
    current = _load_payload(path)
    tokens_by_venue = _tokens_by_venue(snapshot)
    if (
        not force
        and _payload_is_fresh(current, current_time)
        and _request_is_covered(current, tokens_by_venue)
    ):
        return current

    rails: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    worker_count = max(1, min(max_workers, len(tokens_by_venue) or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(_fetch_venue_rails, venue, tokens): venue
            for venue, tokens in tokens_by_venue.items()
        }
        for future in concurrent.futures.as_completed(futures):
            venue = futures[future]
            try:
                rails[venue] = future.result()
            except Exception as exc:  # noqa: BLE001 - partial venue coverage is expected.
                errors[venue] = f"{type(exc).__name__}: {str(exc)[:160]}"

    payload = {
        "schema": "spreadboard.public_transfer_rails.v1",
        "updated_at": datetime.fromtimestamp(current_time, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": "public exchange currency APIs",
        "requested": {
            venue: sorted(tokens)
            for venue, tokens in sorted(tokens_by_venue.items())
        },
        "rails": rails,
        "errors": errors,
    }
    _atomic_write(path, payload)
    return payload


def _tokens_by_venue(snapshot: dict[str, Any]) -> dict[str, set[str]]:
    tokens_by_venue: dict[str, set[str]] = {}
    for bucket in ("api_discovered_rows", "dex_discovered_rows"):
        for row in snapshot.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            token = str(row.get("token") or "").upper()
            if not token:
                continue
            for side in ("long", "short"):
                if str(row.get(f"{side}_market_type") or "") != "Spot":
                    continue
                venue = str(row.get(f"{side}_venue") or "")
                if venue in VENUE_IDS:
                    tokens_by_venue.setdefault(venue, set()).add(token)
    return tokens_by_venue


def _fetch_venue_rails(venue: str, tokens: set[str]) -> dict[str, Any]:
    exchange_id = VENUE_IDS[venue]
    if exchange_id == "gateio" and not hasattr(ccxt, exchange_id):
        exchange_id = "gate"
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class(
        {
            "enableRateLimit": True,
            "timeout": 8_000,
            "options": {"defaultType": "spot"},
        }
    )
    if not exchange.has.get("fetchCurrencies"):
        return {}
    currencies = exchange.fetch_currencies()
    output: dict[str, Any] = {}
    for token in tokens:
        currency = currencies.get(token) if isinstance(currencies, dict) else None
        if not isinstance(currency, dict):
            continue
        networks = currency.get("networks") if isinstance(currency.get("networks"), dict) else {}
        deposit = _bool_or_none(currency.get("deposit"))
        withdraw = _bool_or_none(currency.get("withdraw"))
        if deposit is None and networks:
            deposit = _any_network_state(networks, "deposit")
        if withdraw is None and networks:
            withdraw = _any_network_state(networks, "withdraw")
        output[token] = {
            "name": str(currency.get("name") or "").strip() or None,
            "currency_id": str(currency.get("id") or "").strip() or None,
            "deposit": deposit,
            "withdraw": withdraw,
            "network_count": len(networks),
            "networks": [
                {
                    "network": str(network_name),
                    "deposit": _bool_or_none(network.get("deposit")),
                    "withdraw": _bool_or_none(network.get("withdraw")),
                    "contract": str(
                        network.get("contract")
                        or network.get("address")
                        or ""
                    )
                    or None,
                }
                for network_name, network in sorted(networks.items())
                if isinstance(network, dict)
            ],
        }
    return output


def _any_network_state(networks: dict[str, Any], key: str) -> bool | None:
    values = [
        _bool_or_none(value.get(key))
        for value in networks.values()
        if isinstance(value, dict)
    ]
    known = [value for value in values if value is not None]
    if not known:
        return None
    return any(known)


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _payload_is_fresh(payload: dict[str, Any], now: float) -> bool:
    value = str(payload.get("updated_at") or "")
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - parsed.timestamp() <= DEFAULT_TTL_SECONDS


def _request_is_covered(
    payload: dict[str, Any],
    tokens_by_venue: dict[str, set[str]],
) -> bool:
    requested = payload.get("requested")
    if not isinstance(requested, dict):
        return False
    for venue, tokens in tokens_by_venue.items():
        cached = requested.get(venue)
        if not isinstance(cached, list) or not tokens.issubset(
            {str(token).upper() for token in cached}
        ):
            return False
    return True


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
