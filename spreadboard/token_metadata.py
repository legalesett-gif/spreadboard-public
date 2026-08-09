"""Public token metadata used by SpreadBoard.

The page render path is disk-only. A background refresh fetches CoinGecko's
public coin list and market snapshot, then writes a compact cache. No page
request reaches a third-party metadata service.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_CACHE_PATH = RUNTIME_DIR / "token_metadata.json"
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/list"
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
DEFAULT_TTL_SECONDS = 24 * 60 * 60

PREFERRED_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "LINK": "chainlink",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "TRX": "tron",
    "TON": "the-open-network",
    "USDT": "tether",
    "USDC": "usd-coin",
    "BDX": "beldex",
    "HOUSE": "housecoin-2",
    "ACE": "endurance",
    "AFC": "arsenal-fan-token",
    "AIGENSYN": "gensyn",
    "AURORA": "aurora-near",
    "BP": "backpack",
    "ERA": "caldera",
    "MNT": "mantle",
    "O": "o1-exchange",
    "OPENX": "openxai",
    "SLEEPLESSAI": "sleepless-ai",
    "TBC": "turingbitchain",
}

DISPLAY_NAME_OVERRIDES = {
    "AIGENSYN": "Gensyn",
    "SLEEPLESSAI": "Sleepless AI",
    "TSLL": "Direxion Daily TSLA Bull 2X ETF",
    "TSTBSC": "Test Token (BSC)",
}


def load_token_metadata(path: Path | str = DEFAULT_CACHE_PATH) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw_tokens = payload.get("tokens") if isinstance(payload, dict) else {}
    if not isinstance(raw_tokens, dict):
        return {}
    return {
        str(symbol).upper(): dict(value)
        for symbol, value in raw_tokens.items()
        if isinstance(value, dict)
    }


def token_name(
    symbol: str,
    metadata: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    normalized = str(symbol or "").upper()
    if normalized in DISPLAY_NAME_OVERRIDES:
        return DISPLAY_NAME_OVERRIDES[normalized]
    entry = (metadata or load_token_metadata()).get(normalized)
    name = str((entry or {}).get("name") or "").strip()
    return name or None


def refresh_token_metadata(
    symbols: list[str] | set[str] | tuple[str, ...],
    *,
    path: Path | str = DEFAULT_CACHE_PATH,
    force: bool = False,
    timeout_seconds: float = 15.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Refresh names for the requested symbols from a public, read-only API."""

    current_time = time.time() if now is None else now
    path = Path(path)
    wanted = sorted({str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()})
    current_payload = _load_payload(path)
    current_tokens = current_payload.get("tokens") if isinstance(current_payload.get("tokens"), dict) else {}
    age_seconds = _payload_age_seconds(current_payload, current_time)
    if (
        not force
        and age_seconds is not None
        and age_seconds <= DEFAULT_TTL_SECONDS
        and all(symbol in current_tokens for symbol in wanted)
    ):
        return current_payload

    params = urlencode({"include_platform": "false"})
    request = Request(
        f"{COINGECKO_URL}?{params}",
        headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - public metadata API.
        coins = json.loads(response.read().decode("utf-8"))
    if not isinstance(coins, list):
        raise ValueError("CoinGecko coin list was not a list")

    candidates: dict[str, list[dict[str, str]]] = defaultdict(list)
    for coin in coins:
        if not isinstance(coin, dict):
            continue
        symbol = str(coin.get("symbol") or "").upper()
        if symbol not in wanted:
            continue
        candidates[symbol].append(
            {
                "id": str(coin.get("id") or ""),
                "name": str(coin.get("name") or "").strip(),
            }
        )

    timestamp = _iso_timestamp(current_time)
    tokens: dict[str, dict[str, Any]] = {}
    for symbol in wanted:
        resolved = _resolve_candidate(symbol, candidates.get(symbol) or [])
        previous = current_tokens.get(symbol) if isinstance(current_tokens.get(symbol), dict) else {}
        if resolved is None:
            tokens[symbol] = {
                "name": None,
                "status": "unresolved",
                "candidate_count": len(candidates.get(symbol) or []),
                "first_seen_at": previous.get("first_seen_at") or timestamp,
                "listing_age_source": "scanner_first_seen",
            }
            continue
        tokens[symbol] = {
            "name": resolved["name"],
            "coin_id": resolved["id"],
            "status": resolved["status"],
            "candidate_count": len(candidates.get(symbol) or []),
            "first_seen_at": previous.get("first_seen_at") or timestamp,
            "listing_age_source": "scanner_first_seen",
        }
        for key in (
            "market_cap_usd",
            "fdv_usd",
            "market_volume_24h_usd",
            "market_data_updated_at",
        ):
            if previous.get(key) is not None:
                tokens[symbol][key] = previous[key]

    # Market metrics are best-effort enrichment. A rate limit or transient
    # CoinGecko error must not discard the already-resolved identity cache.
    try:
        market_metrics = _fetch_market_metrics(
            {entry["coin_id"] for entry in tokens.values() if entry.get("coin_id")},
            timeout_seconds=timeout_seconds,
        )
    except Exception:  # noqa: BLE001 - names remain useful without a market snapshot.
        market_metrics = {}
    for entry in tokens.values():
        metrics = market_metrics.get(str(entry.get("coin_id") or ""))
        if not metrics:
            continue
        entry.update(metrics)
        entry["market_data_updated_at"] = timestamp

    payload = {
        "schema": "spreadboard.token_metadata.v2",
        "updated_at": timestamp,
        "source": "CoinGecko public coins list and markets snapshot",
        "tokens": tokens,
    }
    _atomic_write(path, payload)
    return payload


def _resolve_candidate(symbol: str, candidates: list[dict[str, str]]) -> dict[str, str] | None:
    named = [item for item in candidates if item.get("id") and item.get("name")]
    if not named:
        return None
    if len(named) == 1:
        return {**named[0], "status": "unique_symbol"}

    preferred_id = PREFERRED_IDS.get(symbol)
    if preferred_id:
        match = next((item for item in named if item["id"] == preferred_id), None)
        if match:
            return {**match, "status": "known_asset"}

    common_names = {item["name"].casefold() for item in named}
    if len(common_names) == 1:
        return {**named[0], "status": "common_name"}

    normalized = symbol.casefold().replace("_", "-")
    exact_id = next((item for item in named if item["id"].casefold() == normalized), None)
    if exact_id:
        return {**exact_id, "status": "exact_id"}

    exact_name = [
        item
        for item in named
        if item["name"].casefold().replace(" ", "") == symbol.casefold().replace(" ", "")
    ]
    if len(exact_name) == 1:
        return {**exact_name[0], "status": "exact_name"}
    return None


def _fetch_market_metrics(
    coin_ids: set[str],
    *,
    timeout_seconds: float,
) -> dict[str, dict[str, float | None]]:
    output: dict[str, dict[str, float | None]] = {}
    ordered = sorted({str(value) for value in coin_ids if value})
    # CoinGecko accepts up to 250 ids per page. Smaller chunks keep URLs within
    # conservative proxy limits and make the cache refresh easy to retry.
    for start in range(0, len(ordered), 200):
        params = urlencode(
            {
                "vs_currency": "usd",
                "ids": ",".join(ordered[start : start + 200]),
                "order": "market_cap_desc",
                "per_page": "200",
                "page": "1",
                "sparkline": "false",
            }
        )
        request = Request(
            f"{COINGECKO_MARKETS_URL}?{params}",
            headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"},
        )
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - public metadata API.
            rows = json.loads(response.read().decode("utf-8"))
        if not isinstance(rows, list):
            raise ValueError("CoinGecko markets response was not a list")
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            output[str(row["id"])] = {
                "market_cap_usd": _number_or_none(row.get("market_cap")),
                "fdv_usd": _number_or_none(row.get("fully_diluted_valuation")),
                "market_volume_24h_usd": _number_or_none(row.get("total_volume")),
            }
    return output


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _iso_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _payload_age_seconds(payload: dict[str, Any], now: float) -> float | None:
    value = str(payload.get("updated_at") or "")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, now - parsed.timestamp())


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
