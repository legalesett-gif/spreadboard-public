"""Broad public CEX market catalogue for user-selected spread charts."""

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
from typing import Any

from spreadboard.fast_quotes import NATIVE_FUTURES_VENUES, NATIVE_SPOT_VENUES, VENUE_IDS


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_PATH = RUNTIME_DIR / "chart_market_catalog.json"
STABLE_QUOTES = {"USD", "USDC", "USDT"}


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
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "generated_at": None, "count": 0, "token_count": 0, "markets": [], "health": {}}
    return payload if isinstance(payload, dict) else {"ok": False, "markets": []}


def custom_route_key(token: str, long_leg: dict[str, Any], short_leg: dict[str, Any]) -> str:
    payload = {
        "token": str(token).upper(),
        "long": _compact_leg(long_leg),
        "short": _compact_leg(short_leg),
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
        long_leg = _validated_leg(payload["long"])
        short_leg = _validated_leg(payload["short"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    route_kind = _route_kind(long_leg, short_leg)
    return {
        "route_key": route_key,
        "token": token,
        "route_kind": route_kind,
        "long_venue": long_leg["venue"],
        "long_market_type": long_leg["market_type"],
        "long_market_symbol": long_leg["symbol"],
        "short_venue": short_leg["venue"],
        "short_market_type": short_leg["market_type"],
        "short_market_symbol": short_leg["symbol"],
        "notes": {"route_inputs": {"long": {"symbol": long_leg["symbol"]}, "short": {"symbol": short_leg["symbol"]}}, "custom_chart": True},
        "blockers": ["custom_chart_research_only"],
    }


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


def _compact_leg(leg: dict[str, Any]) -> dict[str, str]:
    return {key: str(leg.get(key) or "") for key in ("venue", "market_type", "symbol")}


def _validated_leg(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("invalid_leg")
    leg = _compact_leg(value)
    if leg["venue"] not in VENUE_IDS or leg["market_type"] not in {"Spot", "Futures"} or not leg["symbol"]:
        raise ValueError("invalid_leg")
    return leg


def _route_kind(long_leg: dict[str, str], short_leg: dict[str, str]) -> str:
    types = {long_leg["market_type"], short_leg["market_type"]}
    if types == {"Futures"}:
        return "FUTURES"
    if types == {"Spot"}:
        return "SPOT"
    return "FUTURES-SPOT"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, separators=(",", ":"))
        temporary = Path(handle.name)
    temporary.replace(path)
