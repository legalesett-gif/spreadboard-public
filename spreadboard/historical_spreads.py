"""Indicative long-window spread history built from aligned public OHLCV closes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from spreadboard.fast_quotes import VENUE_IDS


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
CACHE_DIR = RUNTIME_DIR / "historical_spread_cache"


def load_or_fetch(row: dict[str, Any], *, hours: float, max_points: int = 1200) -> dict[str, Any]:
    """Return full-window indicative history without presenting candles as books."""
    if hours < 4 or any("dex" in str(row.get(f"{side}_venue") or "").casefold() for side in ("long", "short")):
        return {"status": "not_applicable", "rows": []}
    cache_path = _cache_path(str(row.get("route_key") or ""), hours)
    cached = _read_cache(cache_path)
    if cached and time.time() - float(cached.get("cached_at") or 0) <= 300:
        return cached
    timeframe = "1m" if hours <= 24 else "5m" if hours <= 72 else "15m"
    since_ms = int((time.time() - hours * 3600) * 1000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            side: pool.submit(_fetch_leg, row, side, timeframe, since_ms)
            for side in ("long", "short")
        }
        legs = {side: future.result() for side, future in futures.items()}
    if not legs["long"] or not legs["short"]:
        result = {"status": "unavailable", "rows": [], "timeframe": timeframe, "cached_at": time.time()}
        _atomic_json(cache_path, result)
        return result
    rows = _align(legs["long"], legs["short"], timeframe)
    if len(rows) > max_points:
        latest = rows[-1]
        stride = max(1, len(rows) // max_points)
        rows = rows[::stride]
        if rows[-1]["quote_ts_us"] != latest["quote_ts_us"]:
            rows.append(latest)
    result = {
        "status": "ok" if rows else "unavailable",
        "sample_source": "historical_ohlcv_close_proxy",
        "timeframe": timeframe,
        "rows": rows,
        "cached_at": time.time(),
    }
    _atomic_json(cache_path, result)
    return result


def _fetch_leg(row: dict[str, Any], side: str, timeframe: str, since_ms: int) -> list[list[float]]:
    import ccxt

    venue = str(row.get(f"{side}_venue") or "")
    market_type = str(row.get(f"{side}_market_type") or "")
    symbol = _symbol(row, side)
    if venue not in VENUE_IDS or not symbol:
        return []
    exchange_id = VENUE_IDS[venue]
    aliases = {"gateio": ("gateio", "gate"), "gate": ("gate", "gateio")}
    klass = next((getattr(ccxt, item) for item in aliases.get(exchange_id, (exchange_id,)) if hasattr(ccxt, item)), None)
    if klass is None:
        return []
    client = klass({"enableRateLimit": True, "timeout": 15_000, "options": {"defaultType": "spot" if market_type == "Spot" else "swap"}})
    try:
        client.load_markets()
        if not client.has.get("fetchOHLCV"):
            return []
        duration_ms = int(client.parse_timeframe(timeframe) * 1000)
        cursor = since_ms
        output: list[list[float]] = []
        now_ms = int(time.time() * 1000)
        for _ in range(8):
            page = client.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000) or []
            normalized = [item for item in page if len(item) >= 5 and item[0] >= since_ms and item[4] is not None]
            output.extend(normalized)
            if not normalized:
                break
            next_cursor = int(normalized[-1][0]) + duration_ms
            if next_cursor <= cursor or next_cursor >= now_ms or len(page) < 1000:
                break
            cursor = next_cursor
        deduped = {int(item[0]): item for item in output}
        return [deduped[key] for key in sorted(deduped)]
    except Exception:  # noqa: BLE001 - history is an optional supplement to exact live books.
        return []
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _align(long_rows: list[list[float]], short_rows: list[list[float]], timeframe: str) -> list[dict[str, Any]]:
    interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[timeframe]
    long_map = {int(item[0] // interval_ms): float(item[4]) for item in long_rows if float(item[4]) > 0}
    short_map = {int(item[0] // interval_ms): float(item[4]) for item in short_rows if float(item[4]) > 0}
    rows = []
    for bucket in sorted(long_map.keys() & short_map.keys()):
        long_close = long_map[bucket]
        short_close = short_map[bucket]
        rows.append({
            "quote_ts_us": bucket * interval_ms * 1000,
            "long_price": long_close,
            "short_price": short_close,
            "executable_spread_pct": (short_close / long_close - 1.0) * 100.0,
            "depth_weighted_spread_pct": None,
            "exit_spread_pct": (long_close / short_close - 1.0) * 100.0,
            "sample_source": "historical_ohlcv_close_proxy",
            "target_notional_usd": None,
        })
    return rows


def _symbol(row: dict[str, Any], side: str) -> str:
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    leg = inputs.get(side) if isinstance(inputs.get(side), dict) else {}
    return str(leg.get("symbol") or row.get(f"{side}_market_symbol") or row.get(f"{side}_symbol") or "")


def _cache_path(route_key: str, hours: float) -> Path:
    digest = hashlib.sha256(f"{route_key}|{hours:g}".encode()).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, separators=(",", ":"))
        temporary = Path(handle.name)
    temporary.replace(path)
