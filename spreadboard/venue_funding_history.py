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
SCHEMA = "spreadboard.venue_funding_history.v4"

# Only these outcomes prove that a leg was genuinely classified. Provider and
# client failures remain retryable; counting them as complete is how a brief
# outage previously made a 9,604-leg catalogue look 100% covered.
CLASSIFIED_STATUSES = frozenset(
    {
        "ok",
        "ok_cached",
        "no_history_rows",
        "unsupported_history_api",
        "symbol_not_indexed",
        "unsupported_venue",
    }
)
RETRYABLE_STATUSES = frozenset({"api_error", "client_unavailable"})

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


#: Venues CCXT cannot give settled history for. BitMart is the only one, and it
#: appears in the top funding rows, so its native endpoint is worth the code.
NATIVE_HISTORY = {
    "BitMart": "https://api-cloud-v2.bitmart.com/contract/public/funding-rate-history?symbol={symbol}&limit=500",
}


def _native_leg_history_outcome(venue: str, symbol: str) -> dict[str, Any]:
    """Settled funding from a native endpoint with an explicit outcome."""
    template = NATIVE_HISTORY.get(venue)
    if not template:
        return {"status": "unsupported_venue", "entries": []}
    native_symbol = symbol.split(":")[0].replace("/", "")
    try:
        from urllib.request import Request, urlopen

        request = Request(
            template.format(symbol=native_symbol),
            headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"},
        )
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 - one venue must not stop the sweep.
        return {
            "status": "api_error",
            "entries": [],
            "error_type": type(exc).__name__,
        }
    if str(payload.get("code") or "1000") != "1000":
        return {
            "status": "api_error",
            "entries": [],
            "error_type": "BitMartResponseError",
        }
    rows = (payload.get("data") or {}).get("list") or []
    entries: list[dict[str, Any]] = []
    for row in rows:
        try:
            entries.append(
                {
                    "timestamp": int(row["funding_time"]),
                    "fundingRate": float(row["funding_rate"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "status": "ok" if entries else "no_history_rows",
        "entries": entries,
    }


def _native_leg_history(venue: str, symbol: str) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only native settlement rows."""
    return list(_native_leg_history_outcome(venue, symbol)["entries"])


def leg_history(
    venue: str,
    symbol: str,
    *,
    client_factory: Any = None,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning one venue's settled funding rows."""
    return list(
        leg_history_outcome(
            venue,
            symbol,
            client_factory=client_factory,
            days=days,
        )["entries"]
    )


def leg_history_outcome(
    venue: str,
    symbol: str,
    *,
    client_factory: Any = None,
    days: int = 30,
) -> dict[str, Any]:
    """Return rows plus a truthful, retry-aware source classification."""
    if venue in NATIVE_HISTORY:
        return _native_leg_history_outcome(venue, symbol)
    exchange_id = VENUE_IDS.get(venue)
    if not exchange_id:
        return {"status": "unsupported_venue", "entries": []}
    if not symbol:
        return {"status": "symbol_not_indexed", "entries": []}
    try:
        client = (client_factory or _client)(exchange_id)
        if client is None:
            return {
                "status": "client_unavailable",
                "entries": [],
                "error_type": _CLIENT_ERRORS.get(exchange_id, "ClientUnavailable"),
            }
        if not getattr(client, "has", {}).get("fetchFundingRateHistory"):
            return {"status": "unsupported_history_api", "entries": []}
        if symbol not in getattr(client, "symbols", []) or []:
            return {"status": "symbol_not_indexed", "entries": []}
        since = int((time.time() - days * 86_400) * 1000)
        entries = client.fetch_funding_rate_history(symbol, since=since, limit=1000) or []
        return {
            "status": "ok" if entries else "no_history_rows",
            "entries": entries,
        }
    except Exception as exc:  # noqa: BLE001 - one unreachable venue must not stop the sweep.
        return {
            "status": "api_error",
            "entries": [],
            "error_type": type(exc).__name__,
        }


_CLIENTS: dict[str, Any] = {}
_CLIENT_ERRORS: dict[str, str] = {}
_CLIENT_FAILURE_AT: dict[str, float] = {}
CLIENT_RETRY_SECONDS = 60.0
#: CCXT renamed some adapters; VENUE_IDS still carries the older names.
_ALIASES = {"gateio": ("gate", "gateio"), "coinbaseexchange": ("coinbaseexchange", "coinbase")}


def _client(exchange_id: str) -> Any:
    if exchange_id in _CLIENTS:
        return _CLIENTS[exchange_id]
    failed_at = _CLIENT_FAILURE_AT.get(exchange_id)
    if failed_at is not None and time.monotonic() - failed_at < CLIENT_RETRY_SECONDS:
        return None
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
        except Exception as exc:  # noqa: BLE001
            _CLIENT_ERRORS[exchange_id] = type(exc).__name__
            client = None
    if client is None and exchange_id not in _CLIENT_ERRORS:
        _CLIENT_ERRORS[exchange_id] = "AdapterUnavailable"
    if client is None:
        _CLIENT_FAILURE_AT[exchange_id] = time.monotonic()
    else:
        _CLIENTS[exchange_id] = client
        _CLIENT_ERRORS.pop(exchange_id, None)
        _CLIENT_FAILURE_AT.pop(exchange_id, None)
    return client


def _status_is_classified(status: dict[str, Any] | None) -> bool:
    return str((status or {}).get("status") or "") in CLASSIFIED_STATUSES


def build(
    legs: list[tuple[str, str]],
    *,
    priority_legs: list[tuple[str, str]] | None = None,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    budget_seconds: float = 240.0,
) -> dict[str, dict[str, float | None]]:
    """Realised windows for each (venue, symbol), written where the board reads.

    Bounded by a time budget: this runs beside everything else on a small box,
    and a partial sweep that lands is worth more than a complete one that gets
    killed.
    """
    deadline = time.monotonic() + budget_seconds
    path = Path(cache_path)
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    windows: dict[str, dict[str, float | None]] = dict(previous.get("legs") or {})
    leg_updated_at: dict[str, str] = dict(previous.get("leg_updated_at") or {})
    # v3 treated every empty result, including client/API failures, as a genuine
    # empty history. Keep its valid settled windows but reclassify every source
    # under v4 before declaring catalogue coverage complete.
    leg_status: dict[str, dict[str, Any]] = (
        dict(previous.get("leg_status") or {})
        if previous.get("schema") == SCHEMA
        else {}
    )
    ordered = list(dict.fromkeys(legs))
    start = int(previous.get("next_cursor") or 0) % max(1, len(ordered))
    priorities = list(dict.fromkeys(priority_legs or []))
    rotated = priorities + [item for item in ordered[start:] + ordered[:start] if item not in priorities]
    attempted = 0
    background_attempted = 0
    retryable_errors = 0
    refreshed_at = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
    for venue, symbol in rotated:
        if time.monotonic() >= deadline:
            break
        attempted += 1
        outcome = leg_history_outcome(venue, symbol)
        entries = list(outcome.get("entries") or [])
        outcome_status = str(outcome.get("status") or "api_error")
        key = f"{venue}|{symbol}"
        if entries:
            windows[key] = realised_windows(entries)
            leg_updated_at[key] = refreshed_at
            leg_status[key] = {
                "status": "ok",
                "updated_at": refreshed_at,
                "last_attempt_at": refreshed_at,
                "last_attempt_status": "ok",
                "settlement_count": len(entries),
                "available_windows": sum(
                    value is not None for value in windows[key].values()
                ),
            }
        elif outcome_status in RETRYABLE_STATUSES:
            retryable_errors += 1
            cached = windows.get(key) or {}
            prior_status = leg_status.get(key) or {}
            was_classified = _status_is_classified(prior_status)
            leg_status[key] = {
                # A v4 classification remains valid through a temporary outage.
                # Legacy cached windows have no proven source outcome, so they
                # remain explicitly unclassified until a provider answers.
                "status": (
                    "ok_cached"
                    if cached and was_classified
                    else "unclassified_cached"
                    if cached
                    else outcome_status
                ),
                "updated_at": leg_updated_at.get(key),
                "last_attempt_at": refreshed_at,
                "last_attempt_status": outcome_status,
                "error_type": str(outcome.get("error_type") or "ProviderError")[:80],
                "settlement_count": 0,
                "available_windows": sum(
                    cached.get(label) is not None for label in ("1d", "7d", "30d")
                ),
            }
        else:
            cached = windows.get(key) or {}
            leg_status[key] = {
                "status": "ok_cached" if cached else outcome_status,
                "updated_at": leg_updated_at.get(key),
                "last_attempt_at": refreshed_at,
                "last_attempt_status": outcome_status,
                "settlement_count": 0,
                "available_windows": sum(
                    cached.get(label) is not None for label in ("1d", "7d", "30d")
                ),
            }
        if (venue, symbol) not in priorities:
            background_attempted += 1
    catalog_keys = {f"{venue}|{symbol}" for venue, symbol in ordered}
    catalog_attempted = sum(
        _status_is_classified(leg_status.get(key)) for key in catalog_keys
    )
    payload = {
        "schema": SCHEMA,
        "updated_at": refreshed_at,
        "next_cursor": (start + background_attempted) % max(1, len(ordered)),
        "catalog_leg_count": len(catalog_keys),
        "catalog_attempted_leg_count": catalog_attempted,
        "catalog_pending_leg_count": max(0, len(catalog_keys) - catalog_attempted),
        "catalog_coverage_pct": round(
            (catalog_attempted / len(catalog_keys) * 100.0) if catalog_keys else 100.0,
            2,
        ),
        "latest_cycle_attempted": attempted,
        "latest_cycle_background_attempted": background_attempted,
        "latest_cycle_retryable_error_count": retryable_errors,
        "leg_updated_at": leg_updated_at,
        "leg_status": leg_status,
        "legs": windows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
    return windows


def coverage_summary(
    legs: list[tuple[str, str]], *, cache_path: Path | str = DEFAULT_CACHE_PATH
) -> dict[str, int | float | bool]:
    """How much of the current catalog has a successful source classification.

    A classified leg counts as attempted even when the venue returned no rows.
    That distinction prevents an unsupported or short-history market from
    keeping the catch-up worker in a permanent tight loop, while a never-seen
    catalog leg remains pending until it has genuinely been queried.
    """
    keys = {f"{venue}|{symbol}" for venue, symbol in legs if venue and symbol}
    try:
        payload = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    statuses = (
        payload.get("leg_status") or {}
        if payload.get("schema") == SCHEMA
        else {}
    )
    attempted = sum(_status_is_classified(statuses.get(key)) for key in keys)
    retryable = sum(
        str((statuses.get(key) or {}).get("last_attempt_status") or (statuses.get(key) or {}).get("status") or "")
        in RETRYABLE_STATUSES
        for key in keys
    )
    total = len(keys)
    pending = max(0, total - attempted)
    return {
        "catalog_leg_count": total,
        "attempted_leg_count": attempted,
        "pending_leg_count": pending,
        "retryable_error_leg_count": retryable,
        "coverage_pct": round((attempted / total * 100.0) if total else 100.0, 2),
        "catch_up_complete": pending == 0,
    }


_CACHE: dict[str, Any] = {"stamp": None, "legs": {}, "leg_status": {}, "leg_updated_at": {}}


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
        _CACHE["leg_status"] = payload.get("leg_status") or {}
        _CACHE["leg_updated_at"] = payload.get("leg_updated_at") or {}
        _CACHE["stamp"] = stamp
    return _CACHE["legs"]


def route_history_status(route: dict[str, Any]) -> dict[str, Any]:
    """Explain blank windows without confusing token age with API coverage."""
    load()
    sides: dict[str, Any] = {}
    for side in ("long", "short"):
        venue = str(route.get(f"{side}_venue") or "")
        symbol = str(route.get(f"{side}_market_symbol") or route.get(f"{side}_symbol") or "")
        if str(route.get(f"{side}_market_type") or "").casefold() != "futures":
            sides[side] = {"status": "not_applicable", "available_windows": 3}
            continue
        key = f"{venue}|{symbol}"
        status = dict(_CACHE["leg_status"].get(key) or {})
        values = _CACHE["legs"].get(key) or {}
        status.update(
            {
                "status": status.get("status") or ("collecting" if key not in _CACHE["legs"] else "partial"),
                "available_windows": sum(values.get(label) is not None for label in ("1d", "7d", "30d")),
                "updated_at": _CACHE["leg_updated_at"].get(key) or status.get("updated_at"),
            }
        )
        sides[side] = status
    windows = route_windows(route)
    outcomes = {
        str(item.get("last_attempt_status") or item.get("status") or "")
        for item in sides.values()
        if item.get("status") != "not_applicable"
    }
    if outcomes.intersection(RETRYABLE_STATUSES):
        note = "A venue history request failed temporarily and remains queued for retry; prior valid windows are retained."
    elif "unsupported_history_api" in outcomes:
        note = "One exact venue adapter does not expose settled funding history through its public API."
    elif "symbol_not_indexed" in outcomes:
        note = "One exact venue symbol is not currently resolved by the public history adapter."
    elif "unsupported_venue" in outcomes:
        note = "One venue has no supported settled-funding history source."
    elif "no_history_rows" in outcomes:
        note = "The venue history request succeeded but returned no settled funding rows for one exact symbol."
    elif any(windows.get(label) is None for label in ("1d", "7d", "30d")):
        note = "Settled rows exist, but the returned history does not yet cover at least 80% of every displayed window."
    else:
        note = "All exact-leg settlement windows are available."
    return {
        "status": "complete" if all(windows.get(label) is not None for label in ("1d", "7d", "30d")) else "partial",
        "available_windows": sum(windows.get(label) is not None for label in ("1d", "7d", "30d")),
        "sides": sides,
        "note": note,
    }


def route_windows(route: dict[str, Any]) -> dict[str, float | None]:
    """Net realised carry for a route: what the short leg took less what the long paid.

    A spot leg pays no funding, so it contributes zero rather than unknown --
    the pair is still fully determined by its futures leg.
    """
    legs = load()
    net: dict[str, float | None] = {f"{days}d": None for days in WINDOW_DAYS}
    sides: dict[str, dict[str, float | None] | None] = {}
    for side in ("long", "short"):
        venue = str(route.get(f"{side}_venue") or "")
        is_futures = (
            str(route.get(f"{side}_market_type") or "").casefold() == "futures"
            and "dex" not in venue.casefold()
        )
        if not is_futures:
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
