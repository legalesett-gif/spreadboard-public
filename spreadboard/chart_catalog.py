"""Public CEX and identity-verified DEX markets for user-selected charts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any

from spreadboard.fast_quotes import NATIVE_FUTURES_VENUES, NATIVE_SPOT_VENUES, VENUE_IDS
from spreadboard import route_taxonomy
from spreadarb.api_discovery.identity import load_watchlist


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_PATH = RUNTIME_DIR / "chart_market_catalog.json"
STABLE_QUOTES = {"USD", "USDC", "USDT"}
DEX_WATCHLIST_PATH = ROOT / "data" / "api_discovery_watchlist.json"
_LOAD_CACHE: dict[str, Any] = {"key": None, "payload": None}
_LOAD_LOCK = threading.Lock()


def refresh(path: Path | str = DEFAULT_PATH, *, workers: int = 4) -> dict[str, Any]:
    path = Path(path)
    previous = load(path)
    previous_generated_at = previous.get("generated_at")
    previous_by_job: dict[str, list[dict[str, Any]]] = {}
    for item in previous.get("markets") or []:
        if not isinstance(item, dict):
            continue
        key = f"{item.get('venue')}|{item.get('market_type')}"
        previous_by_job.setdefault(key, []).append(item)
    jobs = [
        (venue, market_type)
        for venue in VENUE_IDS
        for market_type, supported in (
            ("Spot", venue in NATIVE_SPOT_VENUES),
            ("Futures", venue in NATIVE_FUTURES_VENUES),
        )
        if supported
    ]
    markets: list[dict[str, Any]] = []
    health: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(8, workers))) as pool:
        futures = {pool.submit(_load_job_subprocess, venue, market_type): (venue, market_type) for venue, market_type in jobs}
        for future in as_completed(futures):
            venue, market_type = futures[future]
            key = f"{venue}|{market_type}"
            try:
                rows = future.result()
                markets.extend(rows)
                health[key] = {"status": "ok", "markets": len(rows)}
            except Exception as exc:  # noqa: BLE001 - one venue must not invalidate the catalogue.
                retained = previous_by_job.get(key, [])
                markets.extend(retained)
                health[key] = {
                    "status": "stale_cache" if retained else "unavailable",
                    "markets": len(retained),
                    "error": type(exc).__name__,
                    "catalogued_at": previous_generated_at if retained else None,
                }
    dex_markets = dex_market_entries()
    markets.extend(dex_markets)
    health["OKX DEX|Spot"] = {"status": "ok", "markets": len(dex_markets)}
    markets.sort(key=lambda item: (item["token"], item["venue"], item["market_type"], item["symbol"]))
    payload = {
        "ok": bool(markets),
        "generated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(markets),
        "token_count": len({item["token"] for item in markets}),
        "markets": markets,
        "health": health,
    }
    _atomic_json(path, payload)
    return payload


def _load_job_subprocess(venue: str, market_type: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/chart_catalog_worker.py"), venue, market_type],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=float(os.environ.get("SPREADBOARD_CHART_CATALOG_VENUE_TIMEOUT", "60")),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("catalog_worker_failed")
    payload = json.loads((completed.stdout or "").strip().splitlines()[-1])
    if payload.get("status") != "ok":
        raise RuntimeError(str(payload.get("error") or "catalog_worker_unavailable"))
    return payload.get("markets") or []


def load(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    catalog_path = Path(path)

    def stamp(candidate: Path) -> int | None:
        try:
            return candidate.stat().st_mtime_ns
        except OSError:
            return None

    cache_key = (str(catalog_path.resolve()), stamp(catalog_path), stamp(DEX_WATCHLIST_PATH))
    with _LOAD_LOCK:
        if _LOAD_CACHE["key"] == cache_key and isinstance(_LOAD_CACHE["payload"], dict):
            return _LOAD_CACHE["payload"]
        try:
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"ok": False, "generated_at": None, "count": 0, "token_count": 0, "markets": [], "health": {}}
        if not isinstance(payload, dict):
            payload = {"ok": False, "markets": []}
        # DEX identities are a small, local allowlist and must not wait for the
        # slower multi-venue CEX catalogue job. Merging on read makes a deployed
        # identity immediately usable even when the persisted catalogue predates
        # the code release. Both file stamps are part of the cache key, so this
        # remains immediate without reparsing 22,000 markets on every keystroke.
        dex_markets = dex_market_entries()
        markets = [item for item in payload.get("markets") or [] if isinstance(item, dict)]
        known = {
            (
                str(item.get("token") or ""), str(item.get("venue") or ""),
                str(item.get("market_type") or ""), str(item.get("symbol") or ""),
                str(item.get("dex_chain") or ""), str(item.get("dex_contract") or "").casefold(),
            )
            for item in markets
        }
        for item in dex_markets:
            key = (
                item["token"], item["venue"], item["market_type"], item["symbol"],
                item["dex_chain"], item["dex_contract"].casefold(),
            )
            if key not in known:
                markets.append(item)
                known.add(key)
        markets.sort(key=lambda item: (
            str(item.get("token") or ""), str(item.get("venue") or ""),
            str(item.get("market_type") or ""), str(item.get("symbol") or ""),
        ))
        health = dict(payload.get("health") or {})
        health["OKX DEX|Spot"] = {"status": "ok", "markets": len(dex_markets)}
        result = {
            **payload,
            "ok": bool(markets),
            "count": len(markets),
            "token_count": len({item.get("token") for item in markets if item.get("token")}),
            "markets": markets,
            "health": health,
        }
        _LOAD_CACHE.update({"key": cache_key, "payload": result})
        return result


def custom_route_key(
    token: str,
    long_leg: dict[str, Any],
    short_leg: dict[str, Any],
    *,
    long_multiplier: float = 1.0,
    short_multiplier: float = 1.0,
) -> str:
    payload = {
        "token": str(token).upper(),
        "long": _compact_leg(long_leg),
        "short": _compact_leg(short_leg),
        "long_multiplier": _validated_multiplier(long_multiplier),
        "short_multiplier": _validated_multiplier(short_multiplier),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")
    return f"CUSTOM:{encoded}"


def route_from_key(route_key: str) -> dict[str, Any] | None:
    if not route_key.startswith("CUSTOM:"):
        return None
    try:
        encoded = route_key.split(":", 1)[1]
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        token = str(payload["token"]).upper()
        long_leg = _validated_leg(payload["long"], token=token)
        short_leg = _validated_leg(payload["short"], token=token)
        long_multiplier = _validated_multiplier(payload.get("long_multiplier", 1.0))
        short_multiplier = _validated_multiplier(payload.get("short_multiplier", 1.0))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    route_kind = _route_kind(long_leg, short_leg)
    dex_leg = next((leg for leg in (long_leg, short_leg) if _is_dex_leg(leg)), None)
    result = {
        "route_key": route_key,
        "token": token,
        "route_kind": route_kind,
        "long_venue": long_leg["venue"],
        "long_market_type": long_leg["market_type"],
        "long_market_symbol": long_leg["symbol"],
        "short_venue": short_leg["venue"],
        "short_market_type": short_leg["market_type"],
        "short_market_symbol": short_leg["symbol"],
        "notes": {
            "route_inputs": {
                "long": {"symbol": long_leg["symbol"]},
                "short": {"symbol": short_leg["symbol"]},
            },
            "custom_chart": True,
            "relative_value": {
                "long_multiplier": long_multiplier,
                "short_multiplier": short_multiplier,
            },
        },
        "blockers": ["custom_chart_research_only"],
    }
    if dex_leg is not None:
        result["dex_chain"] = dex_leg["dex_chain"]
        result["dex_contract"] = dex_leg["dex_contract"]
        result["notes"]["identity"] = {
            side: {
                "chain_id": leg.get("dex_chain"),
                "token_address": leg.get("dex_contract"),
                "venue": leg["venue"],
                "market_type": leg["market_type"],
            }
            for side, leg in (("long", long_leg), ("short", short_leg))
            if _is_dex_leg(leg)
        }
    return result


def _load_venue(venue: str, market_type: str) -> list[dict[str, Any]]:
    import ccxt

    exchange_id = VENUE_IDS[venue]
    aliases = {"gateio": ("gateio", "gate"), "gate": ("gate", "gateio")}
    klass = next((getattr(ccxt, item) for item in aliases.get(exchange_id, (exchange_id,)) if hasattr(ccxt, item)), None)
    if klass is None:
        raise RuntimeError("adapter_unavailable")
    client = klass({"enableRateLimit": True, "timeout": 12_000, "options": {"defaultType": "spot" if market_type == "Spot" else "swap"}})
    try:
        loaded = client.load_markets()
        rows = []
        for market in loaded.values():
            if not _catalog_market_supported(market, market_type):
                continue
            is_derivative = bool(market.get("swap"))
            token = str(market.get("base") or "").upper()
            symbol = str(market.get("symbol") or "")
            if not token or not symbol:
                continue
            rows.append({
                "token": token,
                "venue": venue,
                "market_type": market_type,
                "symbol": symbol,
                "market_id": str(market.get("id") or ""),
                "quote": str(market.get("quote") or "").upper(),
                "contract_size": market.get("contractSize") if is_derivative else 1.0,
            })
        return rows
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _catalog_market_supported(market: dict[str, Any], market_type: str) -> bool:
    if market.get("active") is False or str(market.get("quote") or "").upper() not in STABLE_QUOTES:
        return False
    if market_type == "Spot":
        return bool(market.get("spot"))
    if market_type != "Futures" or not market.get("swap"):
        return False
    # The native futures adapters quote stablecoin-settled perpetuals. Inverse
    # contracts such as BTC/USD:BTC need different symbols, sizing, and funding
    # units and must not leak into the same chart path.
    return str(market.get("settle") or "").upper() in STABLE_QUOTES


def dex_market_entries(path: Path | str = DEX_WATCHLIST_PATH) -> list[dict[str, Any]]:
    """Every allowlisted DEX identity the quote path can price exactly."""
    rows: list[dict[str, Any]] = []
    for asset in load_watchlist(Path(path)).values():
        if not asset.dex_enabled:
            continue
        for chain_id, contract in sorted((asset.evm_contracts or {}).items()):
            rows.append({
                "token": asset.token,
                "venue": f"OKX DEX {chain_id}",
                "market_type": "Spot",
                "symbol": asset.token,
                "market_id": str(contract),
                "quote": "USDC",
                "contract_size": 1.0,
                "dex_chain": str(chain_id),
                "dex_contract": str(contract).casefold(),
            })
        if asset.solana_mint:
            rows.append({
                "token": asset.token,
                "venue": "OKX DEX 501",
                "market_type": "Spot",
                "symbol": asset.token,
                "market_id": asset.solana_mint,
                "quote": "USDC",
                "contract_size": 1.0,
                "dex_chain": "501",
                "dex_contract": asset.solana_mint,
            })
    return rows


def _compact_leg(leg: dict[str, Any]) -> dict[str, str]:
    keys = ("venue", "market_type", "symbol", "dex_chain", "dex_contract")
    return {key: str(leg.get(key) or "") for key in keys if leg.get(key)}


def _validated_leg(value: Any, *, token: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("invalid_leg")
    leg = _compact_leg(value)
    if not leg.get("symbol") or leg.get("market_type") not in {"Spot", "Futures"}:
        raise ValueError("invalid_leg")
    # Some production-native adapters (currently Ourbit futures) use direct
    # REST rather than CCXT, so they deliberately have no VENUE_IDS entry.
    # They are still valid chart legs and must not turn a self-contained key
    # into the 20-second legacy-token compatibility path.
    if leg["venue"] in VENUE_IDS or (
        leg["market_type"] == "Futures"
        and leg["venue"] in NATIVE_FUTURES_VENUES
    ) or (
        leg["market_type"] == "Spot"
        and leg["venue"] in NATIVE_SPOT_VENUES
    ):
        return leg
    known = next(
        (
            item for item in dex_market_entries()
            if item["token"] == token
            and item["venue"] == leg["venue"]
            and item["market_type"] == leg["market_type"]
            and item["symbol"] == leg["symbol"]
            and item["dex_chain"] == leg.get("dex_chain")
            and item["dex_contract"].casefold() == str(leg.get("dex_contract") or "").casefold()
        ),
        None,
    )
    if known is None:
        raise ValueError("invalid_leg")
    leg["dex_chain"] = str(known["dex_chain"])
    leg["dex_contract"] = str(known["dex_contract"])
    return leg


def _validated_multiplier(value: Any) -> float:
    multiplier = float(value)
    if not 0 < multiplier <= 10_000:
        raise ValueError("invalid_multiplier")
    return multiplier


def skhx_skhynix_route_key() -> str:
    """Hyperliquid pre-IPO relative-value route normalized to SKHX = 10 x SK Hynix."""
    return custom_route_key(
        "SKHX / SK HYNIX",
        {
            "venue": "Hyperliquid",
            "market_type": "Futures",
            "symbol": "XYZ-SKHX/USDC:USDC",
        },
        {
            "venue": "Hyperliquid",
            "market_type": "Futures",
            "symbol": "XYZ-SKHY/USDC:USDC",
        },
        short_multiplier=10.0,
    )


def _route_kind(long_leg: dict[str, str], short_leg: dict[str, str]) -> str:
    return route_taxonomy.route_kind(
        long_venue=long_leg.get("venue"),
        long_market_type=long_leg.get("market_type"),
        short_venue=short_leg.get("venue"),
        short_market_type=short_leg.get("market_type"),
    )


def _is_dex_leg(leg: dict[str, str]) -> bool:
    # Only on-chain spot legs carry chain/contract identity in custom route
    # payloads. Perpetual DEXes are classified by _route_kind but keep ordinary
    # venue symbols and must not be forced through this contract path.
    return "okx dex" in str(leg.get("venue") or "").casefold()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, separators=(",", ":"))
        temporary = Path(handle.name)
    temporary.replace(path)
