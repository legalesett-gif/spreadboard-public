"""Indicative long-window spread history built from aligned public OHLCV closes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

from spreadboard.fast_quotes import VENUE_IDS


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
CACHE_DIR = RUNTIME_DIR / "historical_spread_cache"


#: Backfills already running, so N readers of one cold chart cause one fetch.
_WARMING: dict[str, float] = {}
_WARMING_LOCK = threading.Lock()

#: How long a completed backfill is trusted before it is fetched again.
CACHE_SECONDS = 300.0

#: How long a warm-up may run before another reader is allowed to retry it.
WARMING_TIMEOUT_SECONDS = 90.0


def _is_dex(row: dict[str, Any]) -> bool:
    """OKX DEX publishes no candles, so a DEX leg can never be backfilled."""
    return any(
        "dex" in str(row.get(f"{side}_venue") or "").casefold() for side in ("long", "short")
    )


def timeframe_for(hours: float) -> str:
    """The finest candle that still covers the window in one or two pages.

    A 1h window on 1m candles is 60 points -- which is the whole reason the
    short windows were empty. They were refused outright rather than being
    given the timeframe that fits them.
    """
    if hours <= 24:
        return "1m"
    if hours <= 72:
        return "5m"
    return "15m"


def load_or_fetch(
    row: dict[str, Any],
    *,
    hours: float,
    max_points: int = 1200,
    blocking: bool = True,
) -> dict[str, Any]:
    """Return full-window indicative history without presenting candles as books.

    The window used to have to be at least four hours long, which meant the 1H
    default -- the first chart anybody opens -- was never backfilled at all. The
    exact-book recorder rotates over 200k routes and lands roughly one sample
    per route per 45 minutes, so with no backfill a fresh chart held a single
    point and drew no line.

    Fetching both legs takes several seconds, so ``blocking=False`` starts the
    fetch in the background and reports ``warming``; the caller polls until the
    cache lands rather than holding the page open.
    """
    if _is_dex(row):
        return {"status": "not_applicable", "rows": []}
    route_key = str(row.get("route_key") or "")
    cache_path = _cache_path(route_key, hours)
    cached = _read_cache(cache_path)
    if cached and time.time() - float(cached.get("cached_at") or 0) <= CACHE_SECONDS:
        return _with_sampled_rows(cached, max_points=max_points)
    if not blocking:
        if _claim_warming(cache_path, row, hours):
            return {"status": "warming", "rows": [], "timeframe": timeframe_for(hours)}
        # Someone else is already fetching it. Serve the stale copy meanwhile so
        # the chart shows the shape of the window instead of going blank.
        if cached:
            result = _with_sampled_rows(cached, max_points=max_points)
            result["stale"] = True
            return result
        return {"status": "warming", "rows": [], "timeframe": timeframe_for(hours)}
    return _fetch_and_cache(row, hours=hours, max_points=max_points, cache_path=cache_path)


def _claim_warming(cache_path: Path, row: dict[str, Any], hours: float) -> bool:
    """Start a background backfill unless one is already in flight."""
    key = str(cache_path)
    now = time.time()
    with _WARMING_LOCK:
        started = _WARMING.get(key)
        if started is not None and now - started < WARMING_TIMEOUT_SECONDS:
            return False
        _WARMING[key] = now

    def run() -> None:
        try:
            _fetch_and_cache(row, hours=hours, max_points=1, cache_path=cache_path)
        except Exception:  # noqa: BLE001 - history is a supplement to live books.
            pass
        finally:
            with _WARMING_LOCK:
                _WARMING.pop(key, None)

    threading.Thread(target=run, name="historical-spread-warm", daemon=True).start()
    return True


def _fetch_and_cache(
    row: dict[str, Any],
    *,
    hours: float,
    max_points: int,
    cache_path: Path,
) -> dict[str, Any]:
    timeframe = timeframe_for(hours)
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
    long_multiplier, short_multiplier = _relative_value_multipliers(row)
    rows = _align(
        legs["long"],
        legs["short"],
        timeframe,
        long_multiplier=long_multiplier,
        short_multiplier=short_multiplier,
    )
    result = {
        "status": "ok" if rows else "unavailable",
        "sample_source": "historical_ohlcv_close_proxy",
        "timeframe": timeframe,
        "rows": rows,
        "cached_at": time.time(),
    }
    _atomic_json(cache_path, result)
    return _with_sampled_rows(result, max_points=max_points)


def _with_sampled_rows(payload: dict[str, Any], *, max_points: int) -> dict[str, Any]:
    result = dict(payload)
    result["rows"] = evenly_sample(list(payload.get("rows") or []), max_points=max_points)
    return result


def evenly_sample(rows: list[dict[str, Any]], *, max_points: int) -> list[dict[str, Any]]:
    """Sample the whole time range while retaining its first and latest points."""
    limit = max(1, int(max_points))
    if len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[-1]]
    last_index = len(rows) - 1
    indices = [(index * last_index) // (limit - 1) for index in range(limit)]
    return [rows[index] for index in indices]


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


def _align(
    long_rows: list[list[float]],
    short_rows: list[list[float]],
    timeframe: str,
    *,
    long_multiplier: float = 1.0,
    short_multiplier: float = 1.0,
) -> list[dict[str, Any]]:
    interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[timeframe]
    long_map = {int(item[0] // interval_ms): float(item[4]) for item in long_rows if float(item[4]) > 0}
    short_map = {int(item[0] // interval_ms): float(item[4]) for item in short_rows if float(item[4]) > 0}
    rows = []
    for bucket in sorted(long_map.keys() & short_map.keys()):
        long_close = long_map[bucket] * long_multiplier
        short_close = short_map[bucket] * short_multiplier
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


def _relative_value_multipliers(row: dict[str, Any]) -> tuple[float, float]:
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    value = notes.get("relative_value") if isinstance(notes.get("relative_value"), dict) else {}
    try:
        long_multiplier = float(value.get("long_multiplier", 1.0))
        short_multiplier = float(value.get("short_multiplier", 1.0))
    except (TypeError, ValueError):
        return 1.0, 1.0
    return max(long_multiplier, 0.000001), max(short_multiplier, 0.000001)


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
