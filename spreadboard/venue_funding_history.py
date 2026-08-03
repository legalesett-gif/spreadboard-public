"""Realised funding windows taken from each venue's own settlement history.

Deriving 1d/7d/30d from our own samples is honest but mostly empty: routes
rotate through a 150-route sampling set, so an individual route holds about
86 hours of history and 73 of 78 cells on the funding board showed a dash.

Venues publish what actually settled. `fetch_funding_rate_history` returns
roughly 30 days in well under a second per symbol, and each entry is a payment
that really happened, so summing the entries inside a window is the realised
figure by definition rather than an integration over samples we happened to
take.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any

from spreadboard.fast_quotes import VENUE_IDS

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))
DEFAULT_CACHE_PATH = RUNTIME_DIR / "venue_funding_history.json"

WINDOW_DAYS: tuple[int, ...] = (1, 7, 30)
#: A window needs the venue to have published far enough back to be meaningful.
#: Bybit returns 20 days where Binance returns 30, so a 30d figure from Bybit is
#: really 20 days and must say so rather than under-report the month.
MIN_WINDOW_COVERAGE = 0.8


def realised_windows(
    entries: list[dict[str, Any]], *, now_ms: int | None = None
) -> dict[str, float | None]:
    """Sum the settlements inside each window, as percent.

    Each entry is a payment that already happened, so the realised return is
    their sum -- no interval arithmetic and no interpolation.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    stamped = sorted(
        (
            (int(entry["timestamp"]), float(entry["fundingRate"]))
            for entry in entries
            if entry.get("timestamp") is not None and entry.get("fundingRate") is not None
        ),
        key=lambda item: item[0],
    )
    output: dict[str, float | None] = {f"{days}d": None for days in WINDOW_DAYS}
    if not stamped:
        return output
    earliest = stamped[0][0]
    for days in WINDOW_DAYS:
        window_ms = days * 86_400_000
        since = now - window_ms
        observed = now - max(earliest, since)
        if observed < window_ms * MIN_WINDOW_COVERAGE:
            continue
        output[f"{days}d"] = (
            sum(rate for timestamp, rate in stamped if timestamp >= since) * 100.0
        )
    return output


def leg_history(
    venue: str,
    symbol: str,
    *,
    client_factory: Any = None,
    days: int = 30,
) -> list[dict[str, Any]]:
    """One venue's settled funding for one symbol, best effort."""
    exchange_id = VENUE_IDS.get(venue)
    if not exchange_id or not symbol:
        return []
    try:
        client = (client_factory or _client)(exchange_id)
        if client is None or not getattr(client, "has", {}).get("fetchFundingRateHistory"):
            return []
        if symbol not in getattr(client, "symbols", []) or []:
            return []
        since = int((time.time() - days * 86_400) * 1000)
        return client.fetch_funding_rate_history(symbol, since=since, limit=1000) or []
    except Exception:  # noqa: BLE001 - one unreachable venue must not stop the sweep.
        return []


_CLIENTS: dict[str, Any] = {}
#: CCXT renamed some adapters; VENUE_IDS still carries the older names.
_ALIASES = {"gateio": ("gate", "gateio"), "coinbaseexchange": ("coinbaseexchange", "coinbase")}


def _client(exchange_id: str) -> Any:
    if exchange_id in _CLIENTS:
        return _CLIENTS[exchange_id]
    import ccxt

    client = None
    for candidate in _ALIASES.get(exchange_id, (exchange_id,)):
        klass = getattr(ccxt, candidate, None)
        if klass is None:
            continue
        try:
            client = klass({"enableRateLimit": True, "timeout": 20000})
            client.load_markets()
            break
        except Exception:  # noqa: BLE001
            client = None
    _CLIENTS[exchange_id] = client
    return client


def build(
    legs: list[tuple[str, str]],
    *,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    budget_seconds: float = 240.0,
) -> dict[str, dict[str, float | None]]:
    """Realised windows for each (venue, symbol), written where the board reads.

    Bounded by a time budget: this runs beside everything else on a small box,
    and a partial sweep that lands is worth more than a complete one that gets
    killed.
    """
    deadline = time.monotonic() + budget_seconds
    windows: dict[str, dict[str, float | None]] = {}
    for venue, symbol in dict.fromkeys(legs):
        if time.monotonic() >= deadline:
            break
        entries = leg_history(venue, symbol)
        if entries:
            windows[f"{venue}|{symbol}"] = realised_windows(entries)
    payload = {
        "schema": "spreadboard.venue_funding_history.v1",
        "updated_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
        "legs": windows,
    }
    path = Path(cache_path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
    return windows


_CACHE: dict[str, Any] = {"stamp": None, "legs": {}}


def load(*, cache_path: Path | str = DEFAULT_CACHE_PATH) -> dict[str, dict[str, float | None]]:
    """The cached windows, re-read only when the file changes."""
    path = Path(cache_path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _CACHE["stamp"] != stamp:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        _CACHE["legs"] = payload.get("legs") or {}
        _CACHE["stamp"] = stamp
    return _CACHE["legs"]


def route_windows(route: dict[str, Any]) -> dict[str, float | None]:
    """Net realised carry for a route: what the short leg took less what the long paid.

    A spot leg pays no funding, so it contributes zero rather than unknown --
    the pair is still fully determined by its futures leg.
    """
    legs = load()
    net: dict[str, float | None] = {f"{days}d": None for days in WINDOW_DAYS}
    sides: dict[str, dict[str, float | None] | None] = {}
    for side in ("long", "short"):
        if str(route.get(f"{side}_market_type") or "") != "Futures":
            sides[side] = {label: 0.0 for label in net}
            continue
        key = f"{route.get(f'{side}_venue')}|{route.get(f'{side}_market_symbol')}"
        sides[side] = legs.get(key)
    if sides["long"] is None or sides["short"] is None:
        return net
    for label in net:
        long_value = sides["long"].get(label)
        short_value = sides["short"].get(label)
        if long_value is None or short_value is None:
            continue
        net[label] = short_value - long_value
    return net
