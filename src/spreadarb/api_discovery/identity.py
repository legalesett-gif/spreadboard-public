"""Token and market identity seeds for read-only API discovery."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from spreadarb.api_discovery.models import now_us

IDENTITY_REGISTRY_SCHEMA = "spreadarb.api_discovery.identity_registry.v1"


@dataclass(frozen=True, slots=True)
class WatchAsset:
    symbol: str
    identity_key: str | None = None
    decimals: int = 18
    cex_enabled: bool = True
    dex_enabled: bool = False
    evm_contracts: dict[int, str] | None = None
    solana_mint: str | None = None
    solana_decimals: int | None = None

    @property
    def token(self) -> str:
        return self.symbol.upper()


@dataclass(frozen=True, slots=True)
class AssetIdentity:
    asset_id: str
    symbol: str
    name: str | None = None
    decimals: int | None = None
    evm_contracts: dict[int, str] | None = None
    solana_mint: str | None = None
    solana_decimals: int | None = None

    @property
    def token(self) -> str:
        return self.symbol.upper()


@dataclass(frozen=True, slots=True)
class MarketIdentity:
    venue: str
    market_type: str
    token: str
    asset_id: str
    exchange_symbol: str | None = None
    decimals: int | None = None
    chain_id: int | None = None
    settle_asset: str | None = None
    contract_size: str | None = None
    source: str = "identity_registry"

    @property
    def venue_key(self) -> str:
        return _norm(self.venue)

    @property
    def market_type_key(self) -> str:
        return _norm_market_type(self.market_type)

    @property
    def token_key(self) -> str:
        return self.token.upper()


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    identity_key: str | None
    blockers: tuple[str, ...] = ()
    market_identity: MarketIdentity | None = None


class IdentityRegistry:
    def __init__(
        self,
        *,
        assets: dict[str, AssetIdentity] | None = None,
        market_identities: list[MarketIdentity] | None = None,
        known_ticker_collisions: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.assets = assets or {}
        self.market_identities = market_identities or []
        self.known_ticker_collisions = {
            key.upper(): tuple(value)
            for key, value in (known_ticker_collisions or {}).items()
        }
        self._by_exact_symbol: dict[tuple[str, str, str], MarketIdentity] = {}
        self._by_token: dict[tuple[str, str, str], MarketIdentity] = {}
        for identity in self.market_identities:
            token_key = (identity.venue_key, identity.market_type_key, identity.token_key)
            self._by_token[token_key] = identity
            if identity.exchange_symbol:
                exact_key = (
                    identity.venue_key,
                    identity.market_type_key,
                    _norm_symbol(identity.exchange_symbol),
                )
                self._by_exact_symbol[exact_key] = identity

    @classmethod
    def empty(cls) -> "IdentityRegistry":
        return cls()

    def known_collision_blockers(self, token: str, identities: list[str | None]) -> list[str]:
        symbol = str(token).upper()
        if symbol not in self.known_ticker_collisions:
            return []
        known = sorted({identity for identity in identities if identity})
        if len(known) == 1:
            return []
        return [f"known_ticker_collision:{symbol}"]

    def resolve_market(
        self,
        *,
        venue: str,
        market_type: str,
        token: str,
        symbol: str | None = None,
    ) -> IdentityResolution:
        venue_key = _norm(venue)
        market_type_key = _norm_market_type(market_type)
        if symbol:
            identity = self._by_exact_symbol.get((venue_key, market_type_key, _norm_symbol(symbol)))
            if identity is not None:
                return IdentityResolution(identity.asset_id, market_identity=identity)
        identity = self._by_token.get((venue_key, market_type_key, str(token).upper()))
        if identity is not None:
            return IdentityResolution(identity.asset_id, market_identity=identity)
        blockers = self.known_collision_blockers(token, [])
        return IdentityResolution(None, blockers=tuple(blockers))


def load_watchlist(path: Path | None) -> dict[str, WatchAsset]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw_assets = payload.get("tokens") if isinstance(payload, dict) else payload
    if not isinstance(raw_assets, list):
        return {}
    assets: dict[str, WatchAsset] = {}
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or item.get("token") or "").upper().strip()
        if not symbol:
            continue
        evm_contracts = _parse_evm_contracts(item.get("evm_contracts") or item.get("contracts"))
        asset = WatchAsset(
            symbol=symbol,
            identity_key=str(item.get("identity_key") or "").strip() or None,
            decimals=_int(item.get("decimals"), 18),
            cex_enabled=bool(item.get("cex_enabled", True)),
            dex_enabled=bool(item.get("dex_enabled", False)),
            evm_contracts=evm_contracts or None,
            solana_mint=str(item.get("solana_mint") or "").strip() or None,
            solana_decimals=_int(item.get("solana_decimals"), _int(item.get("decimals"), 18)),
        )
        assets[asset.token] = asset
    return assets


def load_identity_registry(path: Path | None) -> IdentityRegistry:
    if path is None or not path.exists():
        return IdentityRegistry.empty()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return IdentityRegistry.empty()
    if not isinstance(payload, dict):
        return IdentityRegistry.empty()
    assets: dict[str, AssetIdentity] = {}
    market_identities: list[MarketIdentity] = []
    for item in payload.get("assets") or []:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or item.get("identity_key") or "").strip()
        symbol = str(item.get("symbol") or item.get("token") or "").upper().strip()
        if not asset_id or not symbol:
            continue
        asset = AssetIdentity(
            asset_id=asset_id,
            symbol=symbol,
            name=str(item.get("name") or "").strip() or None,
            decimals=_optional_int(item.get("decimals")),
            evm_contracts=_parse_evm_contracts(item.get("evm_contracts") or item.get("contracts")) or None,
            solana_mint=str(item.get("solana_mint") or "").strip() or None,
            solana_decimals=_optional_int(item.get("solana_decimals")),
        )
        assets[asset.asset_id] = asset
        market_identities.extend(_market_identities_for_asset(asset, item))

    collisions: dict[str, tuple[str, ...]] = {}
    for item in payload.get("known_ticker_collisions") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or item.get("token") or "").upper().strip()
        if not symbol:
            continue
        asset_ids = tuple(str(value) for value in item.get("asset_ids") or item.get("identities") or [])
        collisions[symbol] = asset_ids
    return IdentityRegistry(
        assets=assets,
        market_identities=market_identities,
        known_ticker_collisions=collisions,
    )


def load_scanner_tokens(db_path: Path | None, *, lookback_seconds: int = 86_400, limit: int = 20) -> list[str]:
    if db_path is None or not db_path.exists():
        return []
    min_ts = now_us() - int(lookback_seconds * 1_000_000)
    uri = f"file:{db_path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                """
                SELECT token, MAX(open_spread_pct) AS max_spread, MAX(ts_us) AS latest_ts
                FROM scanner_obs
                WHERE ts_us >= ?
                GROUP BY token
                ORDER BY max_spread DESC, latest_ts DESC
                LIMIT ?
                """,
                (min_ts, limit),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row["token"]).upper() for row in rows if row["token"]]


def merge_tokens(*groups: list[str], limit: int = 20) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for token in group:
            normalized = str(token).upper().strip()
            if normalized and normalized not in merged:
                merged.append(normalized)
            if len(merged) >= limit:
                return merged
    return merged


def identity_key_for(token: str, watchlist: dict[str, WatchAsset]) -> str | None:
    asset = watchlist.get(str(token).upper())
    return asset.identity_key if asset else None


def collision_blockers(token: str, identities: list[str | None]) -> list[str]:
    known = sorted({identity for identity in identities if identity})
    if len(known) > 1:
        return [f"identity_collision:{token.upper()}"]
    if not known:
        return ["identity_unverified"]
    return []


def pair_identity_blockers(
    token: str,
    identities: list[str | None],
    registry: IdentityRegistry | None = None,
) -> list[str]:
    blockers = collision_blockers(token, identities)
    if registry is not None:
        blockers.extend(registry.known_collision_blockers(token, identities))
    return list(dict.fromkeys(blockers))


def _market_identities_for_asset(asset: AssetIdentity, item: dict[str, Any]) -> list[MarketIdentity]:
    identities: list[MarketIdentity] = []
    for market_type, key in (("Spot", "cex_spot"), ("Futures", "cex_futures"), ("Futures", "cex_perp")):
        for market in item.get(key) or []:
            if not isinstance(market, dict):
                continue
            venue = str(market.get("venue") or "").strip()
            if not venue:
                continue
            identities.append(
                MarketIdentity(
                    venue=venue,
                    market_type=str(market.get("market_type") or market_type),
                    token=str(market.get("token") or asset.symbol).upper(),
                    asset_id=asset.asset_id,
                    exchange_symbol=str(market.get("symbol") or market.get("exchange_symbol") or "").strip()
                    or None,
                    decimals=_optional_int(market.get("decimals")) or asset.decimals,
                    chain_id=_optional_int(market.get("chain_id")),
                    settle_asset=str(market.get("settle_asset") or "").upper().strip() or None,
                    contract_size=str(market.get("contract_size") or "").strip() or None,
                )
            )
    return identities


def _parse_evm_contracts(value: Any) -> dict[int, str]:
    if not isinstance(value, dict):
        return {}
    contracts: dict[int, str] = {}
    for key, contract in value.items():
        parsed_chain = _int(key, 0)
        text = str(contract or "").strip()
        if parsed_chain > 0 and text:
            contracts[parsed_chain] = text
    return contracts


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _norm_market_type(value: str) -> str:
    text = str(value).strip().lower()
    if text in {"future", "futures", "swap", "perp", "perpetual"}:
        return "futures"
    if text == "spot":
        return "spot"
    return _norm(text)


def _norm_symbol(value: str) -> str:
    return str(value).upper().replace("_", "/").replace("-", "/")
