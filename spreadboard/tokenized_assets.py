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
KNOWN_TOKENIZED = {
    "ANTHROPIC",
    "OPENAI",
    "QNTX",
    "TSLL",
    "SOXL",
    "SKHX",
    "SKHY",
}
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
        route.get("asset_class") == "tokenized"
        or route.get("long_asset_class") == "tokenized"
        or route.get("short_asset_class") == "tokenized"
        or token.endswith("STOCK")
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
    # A venue can list several instruments for one label. Evidence for one
    # perpetual must not certify a spot wrapper or another builder's contract.
    market_evidence = entry.get("markets") or []
    for side in ("long", "short"):
        identity = (
            str(route.get(f"{side}_venue") or ""),
            str(route.get(f"{side}_market_type") or ""),
            str(route.get(f"{side}_market_symbol") or ""),
        )
        matches = [
            market for market in market_evidence
            if identity == (market["venue"], market["market_type"], market["market_symbol"])
        ]
        if not all(identity) or len(matches) != 1:
            missing.append(f"{side}_market_mapping")
        elif any(not matches[0].get(key) for key in (
            "underlying_symbol", "instrument_type", "oracle_source",
            "trading_hours", "source_url",
        )) or matches[0]["underlying_symbol"] != entry["underlying_symbol"]:
            missing.append(f"{side}_market_evidence")
    return {
        "asset_class": "tokenized",
        "status": "verified" if not missing else "blocked",
        **{key: value for key, value in entry.items() if key != "markets"},
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
        "markets": _normalize_markets(value.get("markets")),
    }


def _normalize_markets(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    markets = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Reuse field validation, without recursively accepting nested markets.
        evidence = _normalize_entry("market", {**item, "markets": None})
        if evidence is None:
            continue
        evidence.pop("markets")
        evidence.pop("symbol")
        evidence.pop("venues")
        evidence.update({
            "venue": str(item.get("venue") or "").strip(),
            "market_type": str(item.get("market_type") or "").strip(),
            "market_symbol": str(item.get("market_symbol") or "").strip(),
        })
        markets.append(evidence)
    return markets


def exact_stock_pair(row: Any) -> tuple[str, ...] | None:
    """Identity for duplicate stock labels, never ticker-based equivalence."""
    get = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
    guard = get("tokenized_guard") or {}
    if get("asset_class") != "tokenized" and guard.get("asset_class") != "tokenized":
        return None
    identity = tuple(str(get(f"{side}_{field}") or "") for side in ("long", "short")
                     for field in ("venue", "market_type", "market_symbol"))
    if not all(identity) or identity[1] not in {"Spot", "Futures"} or identity[4] not in {"Spot", "Futures"}:
        return None
    return identity


def claim_stock_pair(seen: set[tuple[str, ...]], row: Any) -> bool:
    identity = exact_stock_pair(row)
    if identity is None:
        return True
    if identity in seen:
        return False
    seen.add(identity)
    return True


def unique_stock_rows(rows: list[Any]) -> list[Any]:
    """Keep fresh exact stock routes once; preserve filtered alias lookups."""
    winners: dict[tuple[str, ...], Any] = {}
    duplicate = False

    def rank(row: Any) -> tuple[Any, ...]:
        get = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
        token = str(get("token") or "")
        return (bool(get("mirage_guarded")), get("depth_weighted_spread_pct") is None,
                -(get("quote_ts_us") or 0), len(token), token)

    for row in rows:
        identity = exact_stock_pair(row)
        if identity is None:
            continue
        previous = winners.get(identity)
        if previous is None:
            winners[identity] = row
        else:
            duplicate = True
            if rank(row) < rank(previous):
                winners[identity] = row
    if not duplicate:
        return rows
    return [row for row in rows if (identity := exact_stock_pair(row)) is None or winners[identity] is row]
