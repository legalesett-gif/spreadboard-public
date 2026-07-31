"""Build a runtime market-identity registry from exact public contract evidence."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


NETWORK_CHAIN_IDS = {
    "ETH": 1,
    "ERC20": 1,
    "ETHEREUM": 1,
    "BEP20": 56,
    "BSC": 56,
    "BASE": 8453,
    "ARBITRUM": 42161,
    "ARBITRUMONE": 42161,
    "OPTIMISM": 10,
    "POLYGON": 137,
    "MATIC": 137,
    "AVAXC": 43114,
    "AVALANCHEC": 43114,
    "SOL": 501,
    "SOLANA": 501,
}


def build_verified_identity_registry(
    *,
    static_registry_path: Path,
    watchlist_path: Path,
    rails_path: Path,
    snapshot_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Merge exact public contract matches into the static registry.

    A venue market is promoted only when the venue's public currency metadata
    reports a contract or mint that uniquely matches a configured asset. The
    market symbol is taken from that same venue's current discovery payload.
    """

    static = _load_dict(static_registry_path)
    watchlist = _load_dict(watchlist_path)
    rails = _load_dict(rails_path)
    snapshot = _load_dict(snapshot_path)
    assets = [deepcopy(item) for item in static.get("assets") or [] if isinstance(item, dict)]
    assets_by_id = {
        str(item.get("asset_id") or item.get("identity_key") or ""): item
        for item in assets
        if item.get("asset_id") or item.get("identity_key")
    }
    contract_assets = _unique_contract_assets(watchlist)
    exact_markets = _snapshot_markets(snapshot)
    evidence: list[dict[str, Any]] = []

    venue_rails = rails.get("rails") if isinstance(rails.get("rails"), dict) else {}
    for venue, tokens in venue_rails.items():
        if not isinstance(tokens, dict):
            continue
        for token, state in tokens.items():
            matches = _rail_asset_matches(state, contract_assets)
            if len(matches) != 1:
                continue
            asset_id, chain_id, contract = matches[0]
            configured = contract_assets[(chain_id, _norm_contract(chain_id, contract))]
            asset = assets_by_id.get(asset_id)
            if asset is None:
                asset = _asset_from_watchlist(configured)
                assets.append(asset)
                assets_by_id[asset_id] = asset
            added = 0
            for market_type, symbol in sorted(
                exact_markets.get((str(venue), str(token).upper()), set())
            ):
                key = "cex_spot" if market_type == "Spot" else "cex_futures"
                markets = asset.setdefault(key, [])
                market = {
                    "venue": str(venue),
                    "symbol": symbol,
                    "source": "public_contract_match",
                    "chain_id": chain_id,
                }
                if market_type == "Futures":
                    market["settle_asset"] = _settle_asset(symbol)
                if not _has_market(markets, venue=str(venue), symbol=symbol):
                    markets.append(market)
                    added += 1
            if added:
                evidence.append(
                    {
                        "venue": str(venue),
                        "token": str(token).upper(),
                        "asset_id": asset_id,
                        "chain_id": chain_id,
                        "contract": contract,
                        "markets_added": added,
                    }
                )

    payload = {
        "schema": static.get("schema") or "spreadarb.api_discovery.identity_registry.v1",
        "updated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "assets": assets,
        "known_ticker_collisions": static.get("known_ticker_collisions") or [],
        "generation": {
            "source": "exact public venue contract matches plus static registry",
            "verified_matches": len(evidence),
            "markets_added": sum(item["markets_added"] for item in evidence),
            "evidence": evidence,
        },
    }
    _atomic_write(output_path, payload)
    return payload


def _unique_contract_assets(watchlist: dict[str, Any]) -> dict[tuple[int, str], dict[str, Any]]:
    candidates: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in watchlist.get("tokens") or []:
        if not isinstance(item, dict):
            continue
        for chain, contract in (item.get("evm_contracts") or {}).items():
            try:
                chain_id = int(chain)
            except (TypeError, ValueError):
                continue
            key = (chain_id, _norm_contract(chain_id, contract))
            if key[1]:
                candidates.setdefault(key, []).append(item)
        mint = str(item.get("solana_mint") or "").strip()
        if mint:
            candidates.setdefault((501, mint), []).append(item)
    return {key: values[0] for key, values in candidates.items() if len(values) == 1}


def _rail_asset_matches(
    state: Any,
    contract_assets: dict[tuple[int, str], dict[str, Any]],
) -> list[tuple[str, int, str]]:
    if not isinstance(state, dict):
        return []
    matches: dict[tuple[str, int, str], None] = {}
    for network in state.get("networks") or []:
        if not isinstance(network, dict):
            continue
        chain_id = NETWORK_CHAIN_IDS.get(_norm_network(network.get("network")))
        contract = str(network.get("contract") or "").strip()
        if chain_id is None or not contract:
            continue
        asset = contract_assets.get((chain_id, _norm_contract(chain_id, contract)))
        if asset is None:
            continue
        asset_id = str(asset.get("identity_key") or "").strip()
        if asset_id:
            matches[(asset_id, chain_id, contract)] = None
    return list(matches)


def _snapshot_markets(snapshot: dict[str, Any]) -> dict[tuple[str, str], set[tuple[str, str]]]:
    output: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for bucket in ("api_discovered_rows", "dex_discovered_rows"):
        for row in snapshot.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            token = str(row.get("token") or "").upper()
            route_inputs = (row.get("notes") or {}).get("route_inputs") or {}
            for side in ("long", "short"):
                venue = str(row.get(f"{side}_venue") or "")
                market_type = str(row.get(f"{side}_market_type") or "")
                leg = route_inputs.get(side) if isinstance(route_inputs, dict) else {}
                symbol = str((leg or {}).get("symbol") or "").strip()
                if venue and token and symbol and market_type in {"Spot", "Futures"}:
                    output.setdefault((venue, token), set()).add((market_type, symbol))
    return output


def _asset_from_watchlist(item: dict[str, Any]) -> dict[str, Any]:
    asset = {
        "asset_id": str(item.get("identity_key")),
        "symbol": str(item.get("symbol") or item.get("token") or "").upper(),
        "decimals": item.get("decimals", 18),
    }
    if item.get("evm_contracts"):
        asset["evm_contracts"] = deepcopy(item["evm_contracts"])
    if item.get("solana_mint"):
        asset["solana_mint"] = item["solana_mint"]
        asset["solana_decimals"] = item.get("solana_decimals", item.get("decimals", 18))
    return asset


def _has_market(markets: Any, *, venue: str, symbol: str) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("venue")) == venue
        and str(item.get("symbol") or item.get("exchange_symbol")) == symbol
        for item in markets or []
    )


def _settle_asset(symbol: str) -> str | None:
    return symbol.rsplit(":", 1)[1].upper() if ":" in symbol else None


def _norm_network(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _norm_contract(chain_id: int, value: Any) -> str:
    text = str(value or "").strip()
    return text if chain_id == 501 else text.casefold()


def _load_dict(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
