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

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from itertools import pairwise
import threading
import time
from collections.abc import Iterator
from typing import Any

from spreadboard.fast_quotes import VENUE_IDS

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))
DEFAULT_CACHE_PATH = RUNTIME_DIR / "venue_funding_history.json"
SCHEMA = "spreadboard.venue_funding_history.v5"

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
        "market_paused",
        "unsupported_venue",
    }
)
RETRYABLE_STATUSES = frozenset({"api_error", "client_unavailable"})

WINDOW_DAYS: tuple[int, ...] = (1, 7, 30)
#: A displayed realised window must be supported by a nearly complete sequence
#: of exact settlement events.  A start/end span alone is insufficient: two
#: rows thirty days apart do not make a truthful thirty-day return.
MIN_EVENT_COVERAGE = 0.90
MAX_BOUNDARY_INTERVALS = 1.5
MAX_INTERNAL_GAP_INTERVALS = 2.0
HISTORY_PAGE_SIZE = 100
PRIORITY_HISTORY_PAGES = 10


def _window_expiry_ms(status: dict[str, Any] | None, label: str) -> int | None:
    """Return the first instant when a cached rolling total stops being exact."""

    detail = ((status or {}).get("window_details") or {}).get(label) or {}
    if not detail.get("complete"):
        return None
    try:
        latest_event_at = int(detail["latest_event_at"])
        interval_ms = int(float(detail["inferred_interval_hours"]) * 3_600_000)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if latest_event_at <= 0 or interval_ms <= 0:
        return None
    return latest_event_at + interval_ms


def _current_leg_windows(
    values: dict[str, float | None] | None,
    status: dict[str, Any] | None,
    *,
    now_ms: int,
    live_leg: dict[str, Any] | None = None,
) -> tuple[dict[str, float | None], int | None]:
    """Fail closed when an exact aggregate crosses its next settlement."""

    current: dict[str, float | None] = {}
    next_expiry: int | None = None
    source = values or {}
    for label in ("1d", "7d", "30d"):
        value = source.get(label)
        expiry = _window_expiry_ms(status, label)
        detail = ((status or {}).get("window_details") or {}).get(label) or {}
        if live_leg and expiry is not None:
            try:
                live_next_ms = int(live_leg["next_funding_ts_us"]) // 1000
                live_interval_ms = int(float(live_leg["interval_hours"]) * 3_600_000)
                exact_latest_ms = int(detail["latest_event_at"])
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
            else:
                # A live venue schedule can tighten before the historical
                # series has enough rows to infer it. If a newer scheduled
                # settlement is already missing, the aggregate is stale now;
                # otherwise it expires at the earlier of both schedules.
                if live_interval_ms > 0 and live_next_ms > exact_latest_ms:
                    last_scheduled_ms = live_next_ms - live_interval_ms
                    if exact_latest_ms < last_scheduled_ms:
                        expiry = min(expiry, last_scheduled_ms)
                    else:
                        expiry = min(expiry, live_next_ms)
        if value is None or expiry is None or now_ms >= expiry:
            current[label] = None
            continue
        current[label] = value
        next_expiry = expiry if next_expiry is None else min(next_expiry, expiry)
    return current, next_expiry


def realised_window_details(
    entries: list[dict[str, Any]], *, now_ms: int | None = None
) -> dict[str, Any]:
    """Validate and sum exact settlements for trailing 1d/7d/30d windows.

    Windows use ``(now - duration, now]``. Duplicate timestamps are counted
    once, future/invalid rows are rejected, and a value is published only when
    the event cadence proves that the beginning, end, and interior are covered.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    by_timestamp: dict[int, float] = {}
    discarded_invalid = 0
    discarded_future = 0
    discarded_duplicates = 0
    for entry in entries:
        try:
            timestamp = int(entry["timestamp"])
            rate = float(entry["fundingRate"])
        except (KeyError, TypeError, ValueError, OverflowError):
            discarded_invalid += 1
            continue
        if not math.isfinite(rate) or timestamp <= 0:
            discarded_invalid += 1
            continue
        if timestamp > now:
            discarded_future += 1
            continue
        if timestamp in by_timestamp:
            discarded_duplicates += 1
        by_timestamp[timestamp] = rate
    stamped = sorted(by_timestamp.items())
    output: dict[str, float | None] = {f"{days}d": None for days in WINDOW_DAYS}
    windows: dict[str, dict[str, Any]] = {}
    for days in WINDOW_DAYS:
        label = f"{days}d"
        window_ms = days * 86_400_000
        since = now - window_ms
        points = [(timestamp, rate) for timestamp, rate in stamped if since < timestamp <= now]
        diffs = [
            current[0] - previous[0]
            for previous, current in pairwise(points)
            if current[0] > previous[0]
        ]
        # Funding cadence can tighten temporarily (for example 4h -> 2h) for
        # volatile contracts. The 90th-percentile ordinary gap represents the
        # slow schedule and avoids declaring a complete mixed-cadence series
        # incomplete merely because it also paid more frequently.
        ordered_diffs = sorted(diffs)
        interval_ms = (
            int(ordered_diffs[min(len(ordered_diffs) - 1, math.ceil(len(ordered_diffs) * 0.9) - 1)])
            if ordered_diffs
            else None
        )
        if interval_ms is not None:
            from spreadboard import funding_interval

            interval_hours = funding_interval.normalise(interval_ms / 3_600_000)
            interval_ms = int(interval_hours * 3_600_000) if interval_hours else None
        expected = (
            max(1, round(window_ms / interval_ms))
            if interval_ms and interval_ms > 0
            else None
        )
        coverage_pct = (
            min(100.0, len(points) / expected * 100.0) if expected else 0.0
        )
        start_gap = points[0][0] - since if points else None
        end_gap = now - points[-1][0] if points else None
        max_gap = max(diffs) if diffs else None
        complete = bool(
            interval_ms
            and expected
            and len(points) >= math.ceil(expected * MIN_EVENT_COVERAGE)
            and start_gap is not None
            and start_gap <= interval_ms * MAX_BOUNDARY_INTERVALS
            and end_gap is not None
            and end_gap <= interval_ms * MAX_BOUNDARY_INTERVALS
            and (max_gap is None or max_gap <= interval_ms * MAX_INTERNAL_GAP_INTERVALS)
        )
        if complete:
            incomplete_reason = None
        elif not points:
            incomplete_reason = "no_settlements_in_window"
        elif not interval_ms or not expected:
            incomplete_reason = "settlement_cadence_unresolved"
        elif len(points) < math.ceil(expected * MIN_EVENT_COVERAGE):
            incomplete_reason = "insufficient_event_coverage"
        elif start_gap is None or start_gap > interval_ms * MAX_BOUNDARY_INTERVALS:
            incomplete_reason = "start_boundary_not_covered"
        elif end_gap is None or end_gap > interval_ms * MAX_BOUNDARY_INTERVALS:
            incomplete_reason = "latest_settlement_too_old"
        else:
            incomplete_reason = "internal_settlement_gap"
        if complete:
            output[label] = sum(rate for _timestamp, rate in points) * 100.0
        windows[label] = {
            "complete": complete,
            "incomplete_reason": incomplete_reason,
            "event_count": len(points),
            "expected_event_count": expected,
            "coverage_pct": round(coverage_pct, 2),
            "inferred_interval_hours": (
                round(interval_ms / 3_600_000, 4) if interval_ms else None
            ),
            "earliest_event_at": points[0][0] if points else None,
            "latest_event_at": points[-1][0] if points else None,
            "max_gap_hours": round(max_gap / 3_600_000, 4) if max_gap else None,
        }
    return {
        "windows": output,
        "window_details": windows,
        "settlement_count": len(stamped),
        "oldest_event_at": stamped[0][0] if stamped else None,
        "latest_event_at": stamped[-1][0] if stamped else None,
        "discarded_invalid_count": discarded_invalid,
        "discarded_future_count": discarded_future,
        "discarded_duplicate_count": discarded_duplicates,
    }


def realised_windows(
    entries: list[dict[str, Any]], *, now_ms: int | None = None
) -> dict[str, float | None]:
    """Sum the settlements inside each window, as percent.

    Each entry is a payment that already happened, so the realised return is
    their sum -- no interval arithmetic and no interpolation.
    """
    return realised_window_details(entries, now_ms=now_ms)["windows"]


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
    # urllib requires an ASCII URL. BitMart lists Unicode market ids (for
    # example 龙虾USDT); interpolating them raw raises UnicodeEncodeError before
    # the provider is contacted and leaves the history sweep retrying forever.
    from urllib.parse import quote

    encoded_symbol = quote(native_symbol, safe="")
    try:
        from urllib.request import Request, urlopen

        request = Request(
            template.format(symbol=encoded_symbol),
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
    max_pages: int = PRIORITY_HISTORY_PAGES,
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning one venue's settled funding rows."""
    return list(
        leg_history_outcome(
            venue,
            symbol,
            client_factory=client_factory,
            days=days,
            max_pages=max_pages,
        )["entries"]
    )


def _history_pages(
    client: Any,
    venue: str,
    symbol: str,
    *,
    since: int,
    now_ms: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch bounded, provider-aware pages of exact settlement events.

    CCXT unifies the row shape but not the pagination direction.  Several
    adapters ignore ``since`` and return only the newest 100 rows unless their
    native cursor is supplied.  Keep these rules explicit and stop on any
    repeated page so a provider quirk cannot create an infinite loop.
    """
    # These adapters expose provider-specific pagination through CCXT. Their
    # first page is the newest slice, so the old generic forward cursor simply
    # requested that same slice again and left 30d blank even for established
    # contracts. Let each adapter use its documented cursor/deterministic
    # strategy, with both calls and total rows still bounded here.
    if venue in {"Bingx", "Coinbase International", "Bybit"}:
        limit = max(1, int(max_pages)) * HISTORY_PAGE_SIZE
        page = client.fetch_funding_rate_history(
            symbol,
            since=since,
            limit=limit,
            params={
                "paginate": True,
                "paginationCalls": max(1, int(max_pages)),
            },
        ) or []
        return list(page), min(max(1, int(max_pages)), max(1, math.ceil(len(page) / HISTORY_PAGE_SIZE)))

    rows: list[dict[str, Any]] = []
    fingerprints: set[tuple[int, int]] = set()
    cursor: int | str | None = None
    pages = 0
    for page_number in range(1, max(1, int(max_pages)) + 1):
        params: dict[str, Any] = {}
        request_since: int | None = since
        if venue == "Mexc":
            params["page_num"] = page_number
        elif venue == "Bitget":
            params["pageNo"] = page_number
        elif venue == "XT":
            # XT ignores ``since`` and returns the newest 100 rows. Its native
            # cursor is the oldest row id from the previous response; CCXT's
            # generic helper selects the newest id and repeats the same page.
            request_since = None
            if cursor is not None:
                params["id"] = str(cursor)
        elif venue == "Gate":
            # Gate treats ``since`` as a lower bound and returns the *oldest*
            # rows after it.  Combining that with a backwards ``until`` cursor
            # traps every later page in an old slice and never reaches the
            # latest settlement.  Start at the newest page instead, then walk
            # backwards until the requested boundary is covered.
            request_since = None
            if cursor is not None:
                params["until"] = cursor - 1
        elif venue == "OKX":
            request_since = None
            if cursor is not None:
                params["after"] = str(cursor)
        elif venue == "WhiteBIT" and cursor is not None:
            # WhiteBIT's endpoint expects epoch seconds, not milliseconds.
            params["endDate"] = max(1, int((cursor - 1) / 1000))
        elif cursor is not None:
            # Generic adapters normally page forward from ``since``.
            request_since = cursor + 1
        kwargs: dict[str, Any] = {
            "since": request_since,
            "limit": HISTORY_PAGE_SIZE,
        }
        if params:
            kwargs["params"] = params
        page = client.fetch_funding_rate_history(symbol, **kwargs) or []
        if not page:
            break
        timestamps = sorted(
            int(item["timestamp"])
            for item in page
            if item.get("timestamp") is not None
        )
        if not timestamps:
            break
        fingerprint = (timestamps[0], timestamps[-1])
        if fingerprint in fingerprints:
            break
        fingerprints.add(fingerprint)
        pages += 1
        rows.extend(page)
        if venue in {"Gate", "OKX", "WhiteBIT", "Mexc", "Bitget", "XT"} and timestamps[0] <= since:
            break
        if venue in {"Gate", "OKX", "WhiteBIT", "Mexc", "Bitget", "XT"}:
            if venue == "XT":
                oldest = min(
                    page,
                    key=lambda item: int(item.get("timestamp") or now_ms),
                )
                cursor = str((oldest.get("info") or {}).get("id") or "") or None
                if cursor is None:
                    break
            else:
                cursor = timestamps[0]
        else:
            # A generic adapter that returned only recent rows despite a past
            # since cursor cannot be safely back-paged without venue semantics.
            newest = timestamps[-1]
            if newest >= now_ms or newest <= (cursor or since):
                break
            cursor = newest
    return rows, pages


def _history_page_budget(
    status: dict[str, Any] | None,
    cached: dict[str, float | None] | None,
    *,
    priority: bool,
) -> int:
    """Choose enough pages without making every catalogue pass maximal.

    One page is sufficient for an ordinary 8-hour market over 30 days.  Faster
    markets (Gate 4-hour, Aster hourly, and similar) require multiple pages on
    every refresh; otherwise a later shallow maintenance pass can replace a
    valid aggregate with an incomplete slice.  Previously incomplete legs also
    receive a deep pass so a one-page source check cannot masquerade as archive
    completion.
    """
    if priority:
        return PRIORITY_HISTORY_PAGES
    previous = status or {}
    values = cached or {}
    if int(previous.get("history_pages") or 0) > 1:
        return PRIORITY_HISTORY_PAGES
    if str(previous.get("status") or "") in {"ok", "ok_cached"} and any(
        values.get(label) is None for label in ("1d", "7d", "30d")
    ):
        return PRIORITY_HISTORY_PAGES
    return 1


def leg_history_outcome(
    venue: str,
    symbol: str,
    *,
    client_factory: Any = None,
    days: int = 30,
    max_pages: int = PRIORITY_HISTORY_PAGES,
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
        now_ms = int(time.time() * 1000)
        # One extra day lets the completeness validator infer the cadence at
        # the trailing-window boundary without counting that older settlement.
        since = now_ms - (days + 1) * 86_400_000
        entries, pages = _history_pages(
            client,
            venue,
            symbol,
            since=since,
            now_ms=now_ms,
            max_pages=max_pages,
        )
        return {
            "status": "ok" if entries else "no_history_rows",
            "entries": entries,
            "pages": pages,
        }
    except Exception as exc:  # noqa: BLE001 - one unreachable venue must not stop the sweep.
        # BingX keeps paused synthetic contracts in its public market catalogue
        # with active=true, but its history endpoint answers code 109415. That
        # is a definitive source classification, not a transient outage and not
        # a zero return. Keep the windows blank and stop retrying it forever.
        message = str(exc).casefold()
        if venue == "Bingx" and ("109415" in message or "pause currently" in message):
            return {"status": "market_paused", "entries": []}
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


def _status_was_attempted(status: dict[str, Any] | None) -> bool:
    """Whether v4 has made at least one real provider attempt for this leg.

    A retryable provider failure is not a successful classification, but it is
    still an attempt. Keeping those concepts separate lets the initial catalog
    sweep finish without pretending a timeout is an honest empty history.
    """
    return bool(status) and bool(
        status.get("last_attempt_at")
        or status.get("last_attempt_status")
        or status.get("status")
    )


def _priority_refresh_due(
    status: dict[str, Any] | None,
    values: dict[str, float | None] | None,
    *,
    now_ms: int,
) -> bool:
    """Whether a subscriber-visible leg needs another provider check now."""

    if not _status_was_attempted(status):
        return True
    outcome = str(
        (status or {}).get("last_attempt_status")
        or (status or {}).get("status")
        or ""
    )
    if outcome in RETRYABLE_STATUSES:
        return True
    if outcome != "ok":
        return False
    expiries = [
        expiry
        for label in ("1d", "7d", "30d")
        if (values or {}).get(label) is not None
        for expiry in [_window_expiry_ms(status, label)]
        if expiry is not None
    ]
    if not expiries:
        # A successful but incomplete archive receives one deep priority pass;
        # after that it advances only at the next known settlement.
        if not (status or {}).get("deep_history_checked_at"):
            return True
        latest = (status or {}).get("latest_event_at")
        details = (status or {}).get("window_details") or {}
        intervals = [
            float(detail.get("inferred_interval_hours") or 0.0)
            for detail in details.values()
            if isinstance(detail, dict)
        ]
        interval_hours = max(intervals, default=0.0)
        try:
            return bool(
                latest
                and interval_hours > 0
                and now_ms >= int(latest) + int(interval_hours * 3_600_000)
            )
        except (TypeError, ValueError, OverflowError):
            return False
    return now_ms >= min(expiries)


#: These fetches are network-bound, not CPU-bound: measured at ~3.4s each on
#: production while the loop that issued them was strictly sequential. That
#: capped the sweep at 285 legs/hour against the 1,175/hour needed to hold an
#: 8-hour settlement cadence, so 68% of legs sat past their next settlement and
#: fail-closed to blank -- the exact aggregates existed and could not be shown.
#: Fetching a batch at a time closes a 4.1x gap for almost no memory: each
#: worker holds one venue's history response.
#: Its own knob. ``SPREADBOARD_FUNDING_HISTORY_WORKERS`` was already taken --
#: it sets --funding-workers on the snapshot finalize worker, an unrelated
#: concern -- and reusing the name silently coupled the two, so tuning either
#: would move the other. Production's value of 14 was driving this pool by
#: accident; it is now requested deliberately in compose.
FUNDING_HISTORY_FETCH_WORKERS = max(
    1, int(os.environ.get("SPREADBOARD_FUNDING_HISTORY_FETCH_WORKERS", "6"))
)
#: Never point the whole pool at one venue; that trades a throughput problem
#: for a rate-limit ban.
FUNDING_HISTORY_PER_VENUE = max(
    1, int(os.environ.get("SPREADBOARD_FUNDING_HISTORY_PER_VENUE", "2"))
)


def _fetch_leg_outcome(venue: str, symbol: str, page_budget: int) -> dict[str, Any]:
    """One leg's history, tolerating adapters that predate ``max_pages``."""

    try:
        return leg_history_outcome(venue, symbol, max_pages=page_budget)
    except TypeError as exc:
        if "max_pages" not in str(exc):
            raise
        return leg_history_outcome(venue, symbol)


def _venue_diverse_chunks(
    items: list[tuple[str, str]], size: int, per_venue: int
) -> Iterator[list[tuple[str, str]]]:
    """Batches that spread across venues, keeping the caller's priority order.

    Legs are ordered by staleness, and a venue settles all of its contracts on
    one schedule -- so the most-overdue legs arrive as long runs from the same
    venue. Slicing that list consecutively put six legs of one venue into a
    six-slot pool, where the per-venue cap throttled it back to two: measured
    as 1.7x instead of the ~6x the pool should give.

    Taking the next item from a DIFFERENT venue each time fills the pool with
    work that can actually run in parallel. Order is still priority order --
    an item is only ever deferred to the next batch, never dropped or
    reordered relative to its own venue.
    """

    size = max(1, size)
    pending = list(items)
    while pending:
        chunk: list[tuple[str, str]] = []
        used: dict[str, int] = {}
        leftover: list[tuple[str, str]] = []
        for item in pending:
            venue = item[0]
            if len(chunk) < size and used.get(venue, 0) < per_venue:
                chunk.append(item)
                used[venue] = used.get(venue, 0) + 1
            else:
                leftover.append(item)
        if not chunk:
            return
        yield chunk
        pending = leftover


def _fetch_outcomes_in_batches(
    items: list[tuple[str, str]],
    *,
    page_budget_for: Any,
    deadline: float,
    workers: int | None = None,
):
    """Yield ``(venue, symbol, page_budget, outcome)`` preserving ``items`` order.

    Results are yielded in the caller's priority order so the existing
    staleness ordering still decides who gets refreshed when the budget runs
    out. Only the waiting is shared.
    """

    # Resolved at call time, not bound as a default: a default argument would
    # freeze the value at import and ignore both the environment and any test
    # that adjusts it.
    workers = max(1, int(workers if workers is not None else FUNDING_HISTORY_FETCH_WORKERS))
    per_venue = max(1, int(FUNDING_HISTORY_PER_VENUE))
    venue_guards: dict[str, threading.Semaphore] = {}

    def _run(item: tuple[str, str]) -> tuple[tuple[str, str], int, dict[str, Any]]:
        venue, symbol = item
        budget = page_budget_for(venue, symbol)
        guard = venue_guards.setdefault(venue, threading.Semaphore(per_venue))
        with guard:
            return item, budget, _fetch_leg_outcome(venue, symbol, budget)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk in _venue_diverse_chunks(items, workers, per_venue):
            if time.monotonic() >= deadline:
                return
            futures = [pool.submit(_run, item) for item in chunk]
            for future in futures:
                try:
                    (venue, symbol), budget, outcome = future.result()
                except Exception:  # noqa: BLE001, S112 - leg misses this sweep.
                    continue
                yield venue, symbol, budget, outcome


def build(
    legs: list[tuple[str, str]],
    *,
    priority_legs: list[tuple[str, str]] | None = None,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    budget_seconds: float = 240.0,
    priority_only: bool = False,
    priority_recency_order: bool = False,
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
    # v5 applies stricter completeness and de-duplication rules that cannot be
    # reconstructed from v4's three aggregate numbers.  Never carry an old
    # aggregate across the schema boundary: blank is safer than false realised
    # history, and priority legs refill immediately from exact provider rows.
    same_schema = previous.get("schema") == SCHEMA
    windows: dict[str, dict[str, float | None]] = (
        dict(previous.get("legs") or {}) if same_schema else {}
    )
    leg_updated_at: dict[str, str] = (
        dict(previous.get("leg_updated_at") or {}) if same_schema else {}
    )
    leg_status: dict[str, dict[str, Any]] = (
        dict(previous.get("leg_status") or {}) if same_schema else {}
    )
    ordered = list(dict.fromkeys(legs))
    start = int(previous.get("next_cursor") or 0) % max(1, len(ordered))
    priorities = list(dict.fromkeys(priority_legs or []))
    # Cross-process subscriber demand is already sorted newest-first. Reusing
    # a cursor from the previous ordering can jump into the middle of the new
    # list and leave the route the member just opened overdue. Once a recent
    # leg is refreshed it is removed by `_priority_refresh_due` until its next
    # settlement, so starting at the front does not starve remaining due legs.
    priority_start = (
        0
        if priority_recency_order
        else int(previous.get("priority_next_cursor") or 0) % max(1, len(priorities))
    )
    rotated_priorities = priorities[priority_start:] + priorities[:priority_start]
    now_ms = int(time.time() * 1000)
    due_priorities = [
        item
        for item in rotated_priorities
        if _priority_refresh_due(
            leg_status.get(f"{item[0]}|{item[1]}"),
            windows.get(f"{item[0]}|{item[1]}"),
            now_ms=now_ms,
        )
    ]
    retryable_or_pending = [
        item
        for item in ordered
        if not _status_was_attempted(leg_status.get(f"{item[0]}|{item[1]}"))
        or str(
            (leg_status.get(f"{item[0]}|{item[1]}") or {}).get("last_attempt_status")
            or (leg_status.get(f"{item[0]}|{item[1]}") or {}).get("status")
            or ""
        )
        in RETRYABLE_STATUSES
    ]
    leading = list(dict.fromkeys([*priorities, *retryable_or_pending]))

    def _staleness(item: tuple[str, str]) -> tuple[int, int]:
        """Most-overdue exact aggregate first, then the plain rotation.

        The background pass walked a fixed cursor, so with ~10,470 legs, a
        two-minute budget and 4-8 hour settlement schedules, a leg's window
        expired long before the rotation came back to it. Production showed
        9,081 legs holding a stored 24h aggregate while only 1,965 were still
        current -- the data existed and could not be shown. Refreshing whatever
        has just crossed its settlement converts that stored history into
        displayable windows without fetching anything extra.
        """

        key = f"{item[0]}|{item[1]}"
        _current, next_expiry = _current_leg_windows(
            windows.get(key), leg_status.get(key), now_ms=now_ms
        )
        if next_expiry is None:
            # Nothing current to lose: it is already blank, so refresh it early.
            return (0, 0)
        return (1, int(next_expiry))

    background = [item for item in ordered[start:] + ordered[:start] if item not in leading]
    background.sort(key=_staleness)
    rotated = due_priorities if priority_only else leading + background
    attempted = 0
    priority_attempted = 0
    background_attempted = 0
    retryable_errors = 0
    refreshed_at = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
    priority_set = set(priorities)

    def _page_budget_for(venue: str, symbol: str) -> int:
        key = f"{venue}|{symbol}"
        return _history_page_budget(
            leg_status.get(key),
            windows.get(key),
            priority=(venue, symbol) in priority_set,
        )

    for venue, symbol, page_budget, outcome in _fetch_outcomes_in_batches(
        rotated, page_budget_for=_page_budget_for, deadline=deadline
    ):
        if time.monotonic() >= deadline:
            break
        attempted += 1
        is_priority = (venue, symbol) in priority_set
        if is_priority:
            priority_attempted += 1
        key = f"{venue}|{symbol}"
        entries = list(outcome.get("entries") or [])
        outcome_status = str(outcome.get("status") or "api_error")
        if entries:
            details = realised_window_details(entries)
            windows[key] = details["windows"]
            leg_updated_at[key] = refreshed_at
            leg_status[key] = {
                "status": "ok",
                "updated_at": refreshed_at,
                "last_attempt_at": refreshed_at,
                "last_attempt_status": "ok",
                "settlement_count": details["settlement_count"],
                "available_windows": sum(
                    value is not None for value in windows[key].values()
                ),
                "history_pages": int(outcome.get("pages") or 1),
                "deep_history_checked_at": (
                    refreshed_at
                    if page_budget > 1
                    else (leg_status.get(key) or {}).get("deep_history_checked_at")
                ),
                "oldest_event_at": details["oldest_event_at"],
                "latest_event_at": details["latest_event_at"],
                "window_details": details["window_details"],
                "discarded_invalid_count": details["discarded_invalid_count"],
                "discarded_future_count": details["discarded_future_count"],
                "discarded_duplicate_count": details["discarded_duplicate_count"],
            }
        elif outcome_status in RETRYABLE_STATUSES:
            retryable_errors += 1
            cached = windows.get(key) or {}
            prior_status = leg_status.get(key) or {}
            was_classified = _status_is_classified(prior_status)
            leg_status[key] = {
                **{
                    field: prior_status.get(field)
                    for field in (
                        "history_pages",
                        "deep_history_checked_at",
                        "oldest_event_at",
                        "latest_event_at",
                        "window_details",
                        "discarded_invalid_count",
                        "discarded_future_count",
                        "discarded_duplicate_count",
                    )
                    if prior_status.get(field) is not None
                },
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
            # A definitive source response that no longer resolves a market is
            # not permission to keep presenting an old opportunity.
            windows.pop(key, None)
            leg_updated_at.pop(key, None)
            leg_status[key] = {
                "status": outcome_status,
                "updated_at": None,
                "last_attempt_at": refreshed_at,
                "last_attempt_status": outcome_status,
                "settlement_count": 0,
                "available_windows": 0,
            }
        if not is_priority:
            background_attempted += 1
    catalog_keys = {f"{venue}|{symbol}" for venue, symbol in ordered}
    catalog_attempted = sum(
        _status_was_attempted(leg_status.get(key)) for key in catalog_keys
    )
    catalog_classified = sum(
        _status_is_classified(leg_status.get(key)) for key in catalog_keys
    )
    current_windows = {
        key: _current_leg_windows(
            windows.get(key), leg_status.get(key), now_ms=int(time.time() * 1000)
        )[0]
        for key in catalog_keys
    }
    window_leg_counts = {
        label: sum((current_windows.get(key) or {}).get(label) is not None for key in catalog_keys)
        for label in ("1d", "7d", "30d")
    }
    fully_complete = sum(
        all((current_windows.get(key) or {}).get(label) is not None for label in ("1d", "7d", "30d"))
        for key in catalog_keys
    )
    deep_history_pending = sum(
        str((leg_status.get(key) or {}).get("status") or "") in {"ok", "ok_cached"}
        and not (leg_status.get(key) or {}).get("deep_history_checked_at")
        and not all(
            (windows.get(key) or {}).get(label) is not None
            for label in ("1d", "7d", "30d")
        )
        for key in catalog_keys
    )
    payload = {
        "schema": SCHEMA,
        "updated_at": refreshed_at,
        "next_cursor": (
            start
            if priority_only
            else (start + background_attempted) % max(1, len(ordered))
        ),
        "priority_next_cursor": (
            (
                0
                if priority_recency_order
                else (priority_start + priority_attempted) % max(1, len(priorities))
            )
            if priority_only
            else int(previous.get("priority_next_cursor") or 0)
        ),
        "catalog_leg_count": len(catalog_keys),
        "catalog_attempted_leg_count": catalog_attempted,
        "catalog_classified_leg_count": catalog_classified,
        "catalog_pending_leg_count": max(0, len(catalog_keys) - catalog_attempted),
        "catalog_coverage_pct": round(
            (catalog_classified / len(catalog_keys) * 100.0) if catalog_keys else 100.0,
            2,
        ),
        "catalog_source_check_pct": round(
            (catalog_attempted / len(catalog_keys) * 100.0) if catalog_keys else 100.0,
            2,
        ),
        "window_leg_counts": window_leg_counts,
        "window_coverage_pct": {
            label: round((count / len(catalog_keys) * 100.0) if catalog_keys else 100.0, 2)
            for label, count in window_leg_counts.items()
        },
        "fully_complete_leg_count": fully_complete,
        "deep_history_pending_leg_count": deep_history_pending,
        "latest_cycle_attempted": attempted,
        "latest_cycle_background_attempted": background_attempted,
        "latest_cycle_retryable_error_count": retryable_errors,
        "latest_cycle_priority_only": bool(priority_only),
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
) -> dict[str, Any]:
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
    attempted = sum(_status_was_attempted(statuses.get(key)) for key in keys)
    classified = sum(_status_is_classified(statuses.get(key)) for key in keys)
    retryable = sum(
        str((statuses.get(key) or {}).get("last_attempt_status") or (statuses.get(key) or {}).get("status") or "")
        in RETRYABLE_STATUSES
        for key in keys
    )
    values = payload.get("legs") or {} if payload.get("schema") == SCHEMA else {}
    now_ms = int(time.time() * 1000)
    live_legs: dict[str, dict[str, Any]] = {}
    if Path(cache_path).resolve() == Path(DEFAULT_CACHE_PATH).resolve():
        from spreadboard import bulk_quotes

        live_legs = bulk_quotes.load_funding()
    current_values = {
        key: _current_leg_windows(
            values.get(key), statuses.get(key), now_ms=now_ms, live_leg=live_legs.get(key)
        )[0]
        for key in keys
    }
    stored_window_leg_counts = {
        label: sum((values.get(key) or {}).get(label) is not None for key in keys)
        for label in ("1d", "7d", "30d")
    }
    window_leg_counts = {
        label: sum((current_values.get(key) or {}).get(label) is not None for key in keys)
        for label in ("1d", "7d", "30d")
    }
    fully_complete = sum(
        all((current_values.get(key) or {}).get(label) is not None for label in ("1d", "7d", "30d"))
        for key in keys
    )
    deep_history_pending = sum(
        str((statuses.get(key) or {}).get("status") or "") in {"ok", "ok_cached"}
        and not (statuses.get(key) or {}).get("deep_history_checked_at")
        and not all(
            (values.get(key) or {}).get(label) is not None
            for label in ("1d", "7d", "30d")
        )
        for key in keys
    )
    total = len(keys)
    pending = max(0, total - attempted)
    overdue_window_leg_counts = {
        label: max(0, stored_window_leg_counts[label] - window_leg_counts[label])
        for label in ("1d", "7d", "30d")
    }
    current_window_catch_up_complete = not any(overdue_window_leg_counts.values())
    return {
        "catalog_leg_count": total,
        "attempted_leg_count": attempted,
        "classified_leg_count": classified,
        "pending_leg_count": pending,
        "retryable_error_leg_count": retryable,
        "coverage_pct": round((classified / total * 100.0) if total else 100.0, 2),
        "source_check_pct": round((attempted / total * 100.0) if total else 100.0, 2),
        "window_leg_counts": window_leg_counts,
        "stored_window_leg_counts": stored_window_leg_counts,
        "overdue_window_leg_counts": overdue_window_leg_counts,
        "window_coverage_pct": {
            label: round((count / total * 100.0) if total else 100.0, 2)
            for label, count in window_leg_counts.items()
        },
        "fully_complete_leg_count": fully_complete,
        "deep_history_pending_leg_count": deep_history_pending,
        "catch_up_complete": pending == 0,
        "current_window_catch_up_complete": current_window_catch_up_complete,
        "history_catch_up_complete": (
            pending == 0
            and deep_history_pending == 0
            and current_window_catch_up_complete
        ),
    }


_CACHE: dict[str, Any] = {
    "stamp": None,
    "legs": {},
    "current_legs": {},
    "current_until_ms": None,
    "funding_stamp": None,
    "leg_status": {},
    "leg_updated_at": {},
}


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
        if payload.get("schema") != SCHEMA:
            _CACHE["legs"] = {}
            _CACHE["current_legs"] = {}
            _CACHE["leg_status"] = {}
            _CACHE["leg_updated_at"] = {}
        else:
            _CACHE["legs"] = payload.get("legs") or {}
            _CACHE["leg_status"] = payload.get("leg_status") or {}
            _CACHE["leg_updated_at"] = payload.get("leg_updated_at") or {}
        _CACHE["stamp"] = stamp
        _CACHE["current_until_ms"] = None
    now_ms = int(time.time() * 1000)
    live_legs: dict[str, dict[str, Any]] = {}
    if path.resolve() == Path(DEFAULT_CACHE_PATH).resolve():
        from spreadboard import bulk_quotes

        try:
            funding_stamp = bulk_quotes.FUNDING_CACHE_PATH.stat().st_mtime_ns
        except OSError:
            funding_stamp = None
        if _CACHE.get("funding_stamp") != funding_stamp:
            _CACHE["funding_stamp"] = funding_stamp
            _CACHE["current_until_ms"] = None
        live_legs = bulk_quotes.load_funding()
    if (
        _CACHE.get("current_until_ms") is None
        or now_ms >= int(_CACHE["current_until_ms"])
    ):
        current_legs: dict[str, dict[str, float | None]] = {}
        expiries: list[int] = []
        for key, values in _CACHE["legs"].items():
            current, expiry = _current_leg_windows(
                values,
                _CACHE["leg_status"].get(key),
                now_ms=now_ms,
                live_leg=live_legs.get(key),
            )
            current_legs[key] = current
            if expiry is not None:
                expiries.append(expiry)
        _CACHE["current_legs"] = current_legs
        _CACHE["current_until_ms"] = min(expiries) if expiries else 2**63 - 1
    return _CACHE["current_legs"]


def route_history_status(route: dict[str, Any]) -> dict[str, Any]:
    """Explain blank windows without confusing token age with API coverage."""
    current_legs = load()
    sides: dict[str, Any] = {}
    for side in ("long", "short"):
        venue = str(route.get(f"{side}_venue") or "")
        symbol = str(route.get(f"{side}_market_symbol") or route.get(f"{side}_symbol") or "")
        if str(route.get(f"{side}_market_type") or "").casefold() != "futures":
            sides[side] = {"status": "not_applicable", "available_windows": 3}
            continue
        key = f"{venue}|{symbol}"
        status = dict(_CACHE["leg_status"].get(key) or {})
        values = current_legs.get(key) or {}
        details = {
            label: dict(detail)
            for label, detail in (status.get("window_details") or {}).items()
            if isinstance(detail, dict)
        }
        raw_values = _CACHE["legs"].get(key) or {}
        for label in ("1d", "7d", "30d"):
            if raw_values.get(label) is not None and values.get(label) is None:
                details.setdefault(label, {})["complete"] = False
                details[label]["incomplete_reason"] = "settlement_refresh_overdue"
        status.update(
            {
                "status": status.get("status") or ("collecting" if key not in _CACHE["legs"] else "partial"),
                "available_windows": sum(values.get(label) is not None for label in ("1d", "7d", "30d")),
                "updated_at": _CACHE["leg_updated_at"].get(key) or status.get("updated_at"),
                "window_details": details,
            }
        )
        sides[side] = status
    windows = route_windows(route)
    window_notes: dict[str, str] = {}
    reason_labels = {
        "no_settlements_in_window": "returned no settlements in the trailing window",
        "settlement_cadence_unresolved": "did not expose enough events to resolve its settlement cadence",
        "insufficient_event_coverage": "returned fewer exact settlements than the complete window requires",
        "start_boundary_not_covered": "does not reach the beginning of the trailing window",
        "latest_settlement_too_old": "has not exposed a recent enough settlement",
        "internal_settlement_gap": "contains a gap larger than the venue's ordinary funding cadence",
        "settlement_refresh_overdue": "is awaiting the next exact settlement refresh",
    }
    for label in ("1d", "7d", "30d"):
        gaps: list[str] = []
        for side, status in sides.items():
            if status.get("status") == "not_applicable":
                continue
            detail = (status.get("window_details") or {}).get(label) or {}
            reason = str(detail.get("incomplete_reason") or "")
            if not reason:
                continue
            venue = str(route.get(f"{side}_venue") or side.title())
            count = detail.get("event_count")
            expected = detail.get("expected_event_count")
            evidence = (
                f" ({count}/{expected} events)"
                if count is not None and expected is not None
                else ""
            )
            gaps.append(f"{venue} {reason_labels.get(reason, reason.replace('_', ' '))}{evidence}")
        if gaps:
            window_notes[label] = "; ".join(gaps) + "."
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
        note = "Exact settlement rows exist, but their cadence does not yet prove a complete trailing window."
    else:
        note = "All exact-leg settlement windows are available."
    return {
        "status": "complete" if all(windows.get(label) is not None for label in ("1d", "7d", "30d")) else "partial",
        "available_windows": sum(windows.get(label) is not None for label in ("1d", "7d", "30d")),
        "sides": sides,
        "window_notes": window_notes,
        "note": note,
    }


def route_windows(
    route: dict[str, Any],
    *,
    legs: dict[str, dict[str, float | None]] | None = None,
) -> dict[str, float | None]:
    """Net realised carry for a route: what the short leg took less what the long paid.

    A spot leg pays no funding, so it contributes zero rather than unknown --
    the pair is still fully determined by its futures leg.
    """
    net: dict[str, float | None] = {f"{days}d": None for days in WINDOW_DAYS}
    has_futures_leg = any(
        str(route.get(f"{side}_market_type") or "").casefold() == "futures"
        and "dex" not in str(route.get(f"{side}_venue") or "").casefold()
        for side in ("long", "short")
    )
    if not has_futures_leg:
        return net
    exact_legs = load() if legs is None else legs
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
        sides[side] = exact_legs.get(key)
    if sides["long"] is None or sides["short"] is None:
        return net
    for label in net:
        long_value = sides["long"].get(label)
        short_value = sides["short"].get(label)
        if long_value is None or short_value is None:
            continue
        net[label] = short_value - long_value
    return net
