"""Evidence gates for tokenized-equity and tokenized-fund market lanes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_REGISTRY_PATH = RUNTIME_DIR / "tokenized_asset_registry.json"
_CACHE: dict[str, Any] = {"stamp": None, "assets": {}}

# Names that are clearly venue-wrapped equities/funds. This only classifies the
# lane; it does not prove that two venues expose the same legal instrument.
KNOWN_TOKENIZED = {"QNTX", "TSLL", "SOXL", "SKHX", "SKHY"}
NAME_MARKERS = (" ETF", "STOCK", "TOKENIZED EQUITY", "TOKENIZED STOCK")


def load_registry(path: Path | str = DEFAULT_REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    path = Path(path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _CACHE["stamp"] == stamp:
        return dict(_CACHE["assets"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = payload.get("assets") if isinstance(payload, dict) else {}
    assets: dict[str, dict[str, Any]] = {}
    for symbol, value in raw.items() if isinstance(raw, dict) else []:
        normalized = _normalize_entry(symbol, value)
        if normalized is not None:
            assets[normalized["symbol"]] = normalized
    _CACHE.update({"stamp": stamp, "assets": assets})
    return dict(assets)


def classify(route: dict[str, Any], *, path: Path | str = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    token = str(route.get("token") or route.get("symbol") or "").upper().strip()
    token_name = str(route.get("token_name") or "").upper()
    markets = " ".join(
        str(route.get(key) or "").upper()
        for key in ("long_market_symbol", "short_market_symbol")
    )
    inferred = (
        token.endswith("STOCK")
        or token in KNOWN_TOKENIZED
        or any(marker in token_name for marker in NAME_MARKERS)
        or bool(re.search(r"(?:^|[/:-])(XYZ|CASH|KM|MKTS):", markets))
    )
    if not inferred:
        return {"asset_class": "crypto", "status": "not_applicable", "reasons": []}

    registry = load_registry(path)
    entry = registry.get(token)
    if entry is None:
        return {
            "asset_class": "tokenized",
            "status": "blocked",
            "underlying_symbol": token.removesuffix("STOCK") or None,
            "instrument_type": None,
            "oracle_source": None,
            "trading_hours": None,
            "source_url": None,
            "reasons": ["tokenized_registry_missing", "oracle_unresolved", "trading_hours_unresolved"],
            "execution_policy": "research_only",
        }

    missing = [
        key
        for key in ("underlying_symbol", "instrument_type", "oracle_source", "trading_hours", "source_url")
        if not entry.get(key)
    ]
    route_venues = {
        str(route.get("long_venue") or ""),
        str(route.get("short_venue") or ""),
    }
    registry_venues = set(entry.get("venues") or [])
    if registry_venues and not route_venues.issubset(registry_venues):
        missing.append("venue_instrument_mapping")
    return {
        "asset_class": "tokenized",
        "status": "verified" if not missing else "blocked",
        **entry,
        "reasons": [f"{key}_unresolved" for key in missing],
        "execution_policy": "research_only",
    }


def _normalize_entry(symbol: Any, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    token = "".join(char for char in str(symbol or "").upper() if char.isalnum() or char in "_-")[:24]
    if not token:
        return None
    source_url = str(value.get("source_url") or "").strip()[:500]
    if source_url and urlparse(source_url).scheme != "https":
        source_url = ""
    instrument_type = str(value.get("instrument_type") or "").casefold().strip()
    if instrument_type not in {"tokenized_equity", "tokenized_fund", "equity_perpetual"}:
        instrument_type = ""
    return {
        "symbol": token,
        "underlying_symbol": str(value.get("underlying_symbol") or "").upper().strip()[:24],
        "instrument_type": instrument_type,
        "issuer_or_market": " ".join(str(value.get("issuer_or_market") or "").split())[:120],
        "oracle_source": " ".join(str(value.get("oracle_source") or "").split())[:160],
        "trading_hours": " ".join(str(value.get("trading_hours") or "").split())[:160],
        "corporate_action_policy": " ".join(str(value.get("corporate_action_policy") or "").split())[:300],
        "venues": [" ".join(str(item).split())[:80] for item in value.get("venues") or [] if str(item).strip()][:30],
        "source_url": source_url or None,
    }
