"""Clearly labelled shorter settled periods, separate from strict full windows.

Public history may start after listing or be provider-limited. We therefore say
"since first verified settlement", never infer a listing date. Both futures legs
must cover the SAME interval with the normal cadence/freshness guards. Missing
interior payments do not become a shorter-history return. No venue requests or
writes happen on this read path; bounded compact caches reuse the local ledger.
"""
from __future__ import annotations

import time
from array import array
from bisect import bisect_right
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from spreadboard import bulk_quotes, settlement_store
from spreadboard import venue_funding_history as history

DAY_MS = 86_400_000
POLICY = "verified_available_period_v1"


@lru_cache(maxsize=256)
def _series(path: str, venue: str, symbol: str, revision: str, asof: int):
    del revision  # Cache invalidation is tied to the collector's exact leg revision.
    entries = settlement_store.read(Path(path), venue, symbol, asof)
    return (array("q", (row["timestamp"] for row in entries)),
            array("d", (row["fundingRate"] for row in entries)))


@lru_cache(maxsize=4096)
def _period(path: str, venue: str, symbol: str, revision: str, asof: int,
            since: int, end: int):
    times, rates = _series(path, venue, symbol, revision, asof)
    left, right = bisect_right(times, since), bisect_right(times, end)
    if right - left < 4:
        return None
    # Keep predecessor context for schedule transitions, but no future rows.
    entries = [{"timestamp": times[i], "fundingRate": rates[i]}
               for i in range(max(0, left - 4), right)]
    days = (end - since) / DAY_MS
    # Native exhaustive proof counts the whole response. Subsetting it would
    # falsely certify a missing transition, so only observed cadence is used
    # here. Ordinary full-window validation retains the stronger native proof.
    details = history.realised_window_details(entries, now_ms=end, window_days=(days,))
    detail = details["window_details"][f"{days}d"]
    value = details["windows"][f"{days}d"]
    return (value, detail) if value is not None else None


def route_windows(route: dict[str, Any], *, legs=None, now_ms: int | None = None,
                  cache_path: Path | None = None, store_path: Path | None = None):
    """Return metadata ONLY where a strict window is absent but a shorter one is valid."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    archive = history._load_raw(cache_path=cache_path or history.DEFAULT_CACHE_PATH)
    exact_legs = history.load() if legs is None else legs
    strict = history.route_windows(route) if legs is None else history.route_windows(route, legs=exact_legs)
    if all(value is not None for value in strict.values()):
        return {}
    path = str(store_path or history.RUNTIME_DIR / "funding_settlements.sqlite3")
    live = bulk_quotes.load_funding()
    sources = []
    for side in ("long", "short"):
        venue = str(route.get(f"{side}_venue") or "")
        if str(route.get(f"{side}_market_type") or "").casefold() != "futures" or "dex" in venue.casefold():
            continue
        symbol = str(route.get(f"{side}_market_symbol") or route.get(f"{side}_symbol") or "")
        key = history._history_key(venue, symbol, exact_legs)
        status = (archive.get("leg_status") or {}).get(key) or {}
        if not key or key not in exact_legs or status.get("status") not in {"ok", "ok_cached"}:
            return {}
        revision = str(status.get("updated_at") or "")
        try:
            asof = max(int(datetime.fromisoformat(revision).timestamp() * 1000),
                       int(status.get("latest_event_at") or 0))
        except (TypeError, ValueError, OverflowError):
            return {}
        if asof > now:
            return {}
        try:
            oldest = int(status.get("oldest_event_at") or 0)
        except (TypeError, ValueError, OverflowError):
            return {}
        if oldest <= 0:
            return {}
        sources.append((side, key, venue, key.split("|", 1)[1], revision, asof, oldest))
    if not sources:
        return {}
    end = min(source[5] for source in sources)
    first = max(source[6] for source in sources)
    longest_missing = max(int(label.removesuffix("d")) for label, value in strict.items() if value is None)
    if first <= end - longest_missing * DAY_MS:
        return {}  # Complete-age archives with gaps need repair, not an SQL scan per route.
    starts = []
    for _side, _key, venue, symbol, revision, asof, _oldest in sources:
        times, _rates = _series(path, venue, symbol, revision, asof)
        if not times:
            return {}
        starts.append(times[0])
    first = max(starts)
    result = {}
    for label, full_value in strict.items():
        if full_value is not None:
            continue
        requested_days = int(label.removesuffix("d"))
        since = max(end - requested_days * DAY_MS, first - 1)
        if since <= end - requested_days * DAY_MS or since >= end:
            continue  # Not short history: leave a missing full window unresolved.
        net, counts = 0.0, {}
        for side, key, venue, symbol, revision, asof, _first in sources:
            period = _period(path, venue, symbol, revision, asof, since, end)
            if period is None:
                break
            value, detail = period
            current, _expiry = history._current_leg_windows(
                {label: value}, {"window_details": {label: detail}},
                now_ms=now, live_leg=live.get(key),
            )
            if current.get(label) is None:
                break
            net += value if side == "short" else -value
            counts[side] = detail["event_count"]
        else:
            result[label] = {
                "net": net, "period_kind": "available_history", "policy": POLICY,
                "full_window": False, "requested_days": requested_days,
                "duration_days": (end - since) / DAY_MS,
                "since_ms": since, "asof_ms": end, "first_settlement_ms": first,
                "start_basis": "first_verified_settlement",
                "listing_date_verified": False, "event_counts": counts,
            }
    return result
