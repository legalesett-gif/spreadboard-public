"""Canonical public-API spread rows grouped into token-level market views."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import gc
import json
import os
import re
import statistics
from threading import Lock
import time
from typing import Any

from spreadboard import board, exchange_links, public_rails, token_metadata
from spreadarb.api_discovery.identity import WatchAsset, load_watchlist

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEX_WATCHLIST_PATH = ROOT / "data" / "api_discovery_watchlist.json"
DEFAULT_API_DISCOVERY_PATH = RUNTIME_DIR / "api_discovery_latest.json"
DEFAULT_MAX_AGE_MIN = float(os.environ.get("SPREADBOARD_LIVE_MAX_AGE_MIN", "4"))
DEFAULT_LIMIT = 25

# Spot-DEX is outside the current public product. Spot-Spot remains a first-class
# arbitrage lane and must participate in grouping and top-25 ranking.
# Spot-DEX was retired, which zeroed a lane uacryptoinvest populates with 20+
# tokens. Re-enabled; set SPREADBOARD_RETIRE_DEX_SPOT=1 to restore the old
# behaviour if the lane proves noisy.
RETIRED_ROUTE_KINDS = frozenset(
    {"DEX-SPOT"}
    if str(os.environ.get("SPREADBOARD_RETIRE_DEX_SPOT", "")).strip().lower()
    in {"1", "true", "yes", "on"}
    else set()
)


@dataclass(frozen=True)
class SpreadTerminalRow:
    token: str
    token_name: str | None
    route_key: str
    route_kind: str
    source_group: str
    source_label: str
    source_name: str | None
    long_venue: str | None
    long_market_type: str | None
    short_venue: str | None
    short_market_type: str | None
    executable_spread_pct: float | None
    depth_weighted_spread_pct: float | None
    displayed_open_spread_pct: float | None
    funding_apr_pct: float | None
    funding_daily_pct: float | None
    funding_spread_pct: float | None
    depth_usd: float | None
    validation_state: str | None
    executor_status: str | None
    status: str
    blockers: list[str]
    next_action: str
    freshness: str
    age_min: float | None
    quote_ts_us: int | None
    href: str
    long_price: float | None = None
    short_price: float | None = None
    long_funding_pct: float | None = None
    short_funding_pct: float | None = None
    funding_24h_pct: float | None = None
    funding_projected_24h_pct: float | None = None
    funding_24h_source: str | None = None
    long_funding_interval_hours: float | None = None
    short_funding_interval_hours: float | None = None
    long_funding_interval_assumed: bool = False
    short_funding_interval_assumed: bool = False
    long_next_funding_ts_us: int | None = None
    short_next_funding_ts_us: int | None = None
    long_volume_24h_usd: float | None = None
    short_volume_24h_usd: float | None = None
    long_bid: float | None = None
    long_ask: float | None = None
    short_bid: float | None = None
    short_ask: float | None = None
    long_deposit_enabled: bool | None = None
    long_withdraw_enabled: bool | None = None
    short_deposit_enabled: bool | None = None
    short_withdraw_enabled: bool | None = None
    long_market_symbol: str | None = None
    short_market_symbol: str | None = None
    long_exchange_url: str | None = None
    short_exchange_url: str | None = None
    dex_chain: str | None = None
    dex_contract: str | None = None
    raw_source_kind: str | None = None
    mirage_guarded: bool = False
    live_book: bool = False

    def to_dict(self) -> dict[str, Any]:
        # asdict() recurses and deep-copies; every field on this row is a scalar
        # or a flat list, so the recursion was 9.8s of pure waste per request at
        # 34k rows. Callers treat the result as read-only.
        return dict(self.__dict__)


def load_spreads(
    *,
    api_path: Path | str = DEFAULT_API_DISCOVERY_PATH,
    board_path: Path | str = board.DEFAULT_BOARD_PATH,
    q: str | None = None,
    exchange: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    min_spread_pct: float | None = None,
    min_abs_funding_24h_pct: float | None = None,
    min_abs_funding_apr_pct: float | None = None,
    funding_only: bool = False,
    include_stale: bool = False,
    require_deliverable: bool = False,
    include_unverified: bool = False,
    max_age_min: float | None = DEFAULT_MAX_AGE_MIN,
    sort_by: str = "edge",
    direction: str = "desc",
    offset: int = 0,
    limit: int | None = DEFAULT_LIMIT,
    now: float | None = None,
) -> dict[str, Any]:
    """Return token-grouped routes sourced only from public exchange APIs.

    Website rows are read solely for hidden reconciliation metrics. They never
    enter the displayed row set.
    """

    current_time = time.time() if now is None else now
    api_path = Path(api_path)
    board_path = Path(board_path)
    # Grouping every route into a public payload is the dominant per-request
    # cost -- 1-3s at 2.5k rows, and it scales with the universe we now carry.
    # The board only moves every 20s, so identical queries against an unchanged
    # snapshot are served from the last result.
    cache_key = (
        str(api_path), str(board_path), q, exchange, kind, source, min_spread_pct,
        min_abs_funding_24h_pct, min_abs_funding_apr_pct, funding_only, include_stale,
        include_unverified, max_age_min, sort_by, direction, offset, limit,
    )
    try:
        stamp = api_path.stat().st_mtime_ns
    except OSError:
        stamp = 0
    with _SNAPSHOT_CACHE_LOCK:
        cached = _RESULT_CACHE.get(cache_key)
    if (
        cached is not None
        and cached[0] == stamp
        and 0.0 <= current_time - cached[1] < _RESULT_CACHE_TTL_SECONDS
    ):
        return cached[2]
    metadata = token_metadata.load_token_metadata()
    rails = public_rails.load_public_rails()
    api_rows, api_meta = _load_api_discovery_rows(
        api_path,
        now=current_time,
        metadata=metadata,
        rails=rails,
    )
    board_rows, board_meta = _load_board_rows(board_path, now=current_time)
    # Applied per request rather than inside the row cache: a cached price is a
    # stale price, and this is the whole point of streaming the books.
    api_rows = apply_live_books(api_rows, _live_books(), now=current_time)
    all_rows = _dedupe_rows(api_rows)
    # Diagnostic: how DEX-sourced rows classify BEFORE retirement is applied.
    # Futures-DEX is currently empty on the public board while discovery still
    # reports DEX rows, so surface the raw distribution to pinpoint the loss.
    dex_raw_kind_counts = dict(
        sorted(
            Counter(
                row.route_kind
                for row in all_rows
                if row.route_kind.startswith("DEX-")
                or row.raw_source_kind == "dex_discovered_rows"
            ).items()
        )
    )
    all_rows = [row for row in all_rows if row.route_kind not in RETIRED_ROUTE_KINDS]
    held_out = [row for row in all_rows if _is_mirage_guarded(row)]
    # Headline rankings must be executable research leads, not ticker
    # dislocations with unresolved identity, inventory, or transfer rails.
    # Guarded rows remain available through the explicit audit switch.
    # Operator correction 2026-08-01: large spreads on this board are REAL --
    # he has captured a 150% spread for real money -- so hiding them loses the
    # very opportunities the product exists to surface. Guarded rows are now
    # shown by default and carry `mirage_guarded` so the UI can badge them as
    # unverified rather than silently dropping them. Set
    # SPREADBOARD_HIDE_GUARDED_ROWS=1 to restore the old hide-by-default.
    ranked_rows = (
        all_rows
        if include_unverified or not _hide_guarded_rows()
        else [
            row
            for row in all_rows
            if not _is_mirage_guarded(row)
        ]
    )
    public_universe = (
        ranked_rows
        if include_stale
        else [row for row in ranked_rows if row.freshness == "fresh"]
    )
    # Undeliverable routes stay visible and filterable, but must never occupy a
    # headline slot -- a 100% edge you cannot settle is not the best opportunity
    # on the board, and ranking it as one pushes real tokens off the page.
    rankable_universe = [
        row
        for row in public_universe
        if route_deliverable(row) is not False and row_is_presentable(row)
    ]
    # A funding farm holds both legs -- long spot on one venue, short futures on
    # another -- and never moves the coin between them, so transfer rails are
    # irrelevant to whether the carry is collectable. Only quote trustworthiness
    # matters: the two legs must be the same asset, on books deep enough to price.
    rankable_funding_universe = [
        row
        for row in public_universe
        if funding_intervals_known(row)
        and not price_ratio_implausible(row)
        and not leg_volume_too_thin(row)
        and not is_venue_specific_leveraged_token(row)
        and not is_non_perpetual_or_inverse(row)
    ]
    route_kind_token_counts = _route_kind_token_counts(public_universe)
    release_lane_token_counts = _release_lane_token_counts(public_universe)
    filtered = _filter_rows(
        ranked_rows,
        q=q,
        exchange=exchange,
        kind=kind,
        source=source,
        min_spread_pct=min_spread_pct,
        min_abs_funding_24h_pct=(
            min_abs_funding_24h_pct
            if min_abs_funding_24h_pct is not None
            else (
                min_abs_funding_apr_pct / 365.0
                if min_abs_funding_apr_pct is not None
                else None
            )
        ),
        funding_only=funding_only,
        include_stale=include_stale,
    )
    # Executability is not a ranking preference, it is a condition of being shown
    # at all. The headline lists were filtered while the board itself was not, so
    # SNOW printed 873,150,483,949,072% between an OKX DEX quote at 3.4e-11 and
    # an Ourbit quote at 299 -- two different assets wearing one ticker, already
    # flagged identity_mismatch and displayed anyway. 1,112 rows were above 100%
    # and 578 above 1000% on that basis.
    #
    # This is an identity and liquidity test, never a size ceiling: VANRY's
    # genuine ~100% edge has both legs within a sane price ratio on real books
    # and survives untouched. include_unverified=1 still shows everything for
    # auditing.
    if not include_unverified:
        unverifiable = unverifiable_price_outliers(filtered)
        filtered = [
            row
            for row in filtered
            if not price_ratio_implausible(row)
            and not leg_volume_too_thin(row)
            and not is_venue_specific_leveraged_token(row)
            and not is_non_perpetual_or_inverse(row)
            and row.route_key not in unverifiable
            and pays_something(row)
        ]
        # A spread is a claim about prices you could trade at. Rows quoted from
        # last trades cannot support that claim, so they are kept out of the
        # spread lanes. The funding lanes still carry them: a carry comes from
        # the funding rate, not from the book.
        if not funding_only:
            filtered = [row for row in filtered if not spread_is_untrustworthy(row)]
        # A spot arb needs the coin moved, so a shut rail is not a spread at all
        # -- ESPORTS printed 150% into a Mexc deposit that was closed, which is
        # why it printed at all, and every spot row the reference product lists
        # has both rails open. This is opt-in rather than automatic because the
        # rail-reopen watcher reads this same function and needs those tokens
        # tracked; only the board asks for it. Unknown status is left alone.
        if require_deliverable:
            filtered = [
                row
                for row in filtered
                if getattr(row, "route_kind", None) not in TRANSFER_ROUTE_KINDS
                or route_deliverable(row) is not False
            ]
    normalized_sort = _normalize_sort(sort_by)
    normalized_direction = "asc" if str(direction).casefold() == "asc" else "desc"
    filtered.sort(
        key=lambda row: _sort_value(row, normalized_sort),
        reverse=normalized_direction == "desc",
    )
    groups = _group_rows(filtered)
    for group in groups:
        (group.get("routes") or []).sort(
            key=lambda row: _route_dict_sort_value(row, normalized_sort),
            reverse=normalized_direction == "desc",
        )
    groups.sort(
        key=lambda group: _group_sort_value(group, normalized_sort),
        reverse=normalized_direction == "desc",
    )
    normalized_offset = max(0, int(offset or 0))
    visible_groups = (
        groups[normalized_offset : normalized_offset + max(0, limit)]
        if limit is not None
        else groups[normalized_offset:]
    )
    visible_route_keys = {
        route["route_key"]
        for group in visible_groups
        for route in group.get("routes") or []
    }
    visible = [row for row in filtered if row.route_key in visible_route_keys]
    summary = _summary(
        all_rows,
        filtered,
        visible,
        groups=groups,
        visible_groups=visible_groups,
        offset=normalized_offset,
        limit=limit,
    )
    _ = _reconciliation_summary(all_rows, board_rows, board_meta)

    payload = {
        "ok": not api_meta.get("error") and api_meta.get("status") == "fresh",
        "mode": "canonical_public_api_spreads",
        "filters": {
            "q": q,
            "exchange": exchange,
            "kind": kind,
            "source": "public_api",
            "min_spread_pct": min_spread_pct,
            "min_abs_funding_24h_pct": min_abs_funding_24h_pct,
            "min_abs_funding_apr_pct": min_abs_funding_apr_pct,
            "funding_only": funding_only,
            "include_stale": include_stale,
            "include_unverified": include_unverified,
            "max_age_min": max_age_min,
            "sort": normalized_sort,
            "direction": normalized_direction,
            "offset": normalized_offset,
            "limit": limit,
        },
        "summary": summary,
        "pagination": {
            "offset": normalized_offset,
            "limit": limit,
            "returned_rows": len(visible_groups),
            "matching_rows": len(groups),
            "has_previous": normalized_offset > 0,
            "has_more": normalized_offset + len(visible_groups) < len(groups),
        },
        "source_health": {
            "canonical_api": {
                **_public_source_health(api_meta),
                "mirage_guarded_count": len(held_out),
                "dex_raw_kind_counts": dex_raw_kind_counts,
                "lane_token_counts": release_lane_token_counts,
                "top_25_ready": {
                    kind: count >= DEFAULT_LIMIT
                    for kind, count in release_lane_token_counts.items()
                },
            },
        },
        "exchange_options": _exchange_options(public_universe),
        "route_kind_counts": dict(
            sorted(Counter(row.route_kind for row in public_universe).items())
        ),
        "route_kind_token_counts": route_kind_token_counts,
        "lane_token_counts": release_lane_token_counts,
        "top_edges": _top_unique_groups(rankable_universe, metric="edge"),
        "top_funding": _top_unique_groups(rankable_funding_universe, metric="funding"),
        "groups": visible_groups,
        "rows": [_public_row(row) for row in visible],
    }
    with _SNAPSHOT_CACHE_LOCK:
        # Each entry is a fully materialised payload. At 34k rows a handful of
        # them is hundreds of megabytes, which took a 4GB box to 156MB free and
        # left the container unhealthy. Keep only the few most recent queries.
        if len(_RESULT_CACHE) >= _RESULT_CACHE_MAX_ENTRIES:
            _RESULT_CACHE.clear()
        _RESULT_CACHE[cache_key] = (stamp, current_time, payload)
    return payload


def _route_kind_token_counts(
    rows: list[SpreadTerminalRow],
) -> dict[str, int]:
    tokens: dict[str, set[str]] = {}
    for row in rows:
        tokens.setdefault(row.route_kind, set()).add(row.token)
    return {kind: len(values) for kind, values in sorted(tokens.items())}


def lane_rankable(row: "SpreadTerminalRow") -> bool:
    """Would a member actually be able to take this route in its lane?

    The headline lists already rank on deliverability and trustworthiness, but
    the per-lane counts did not, so "top 25 ready" could be satisfied by rows
    with a shut rail, a ticker collision, or a book too thin to price.

    Only SPOT and DEX-SPOT move the coin between venues. A funding farm holds
    both legs where it bought them, so a transfer rail says nothing about
    whether its carry is collectable -- which is why deliverability is applied
    per lane rather than across the board.
    """
    if price_ratio_implausible(row) or leg_volume_too_thin(row):
        return False
    if getattr(row, "route_kind", None) in TRANSFER_ROUTE_KINDS:
        return route_deliverable(row) is not False
    return True


def _release_lane_token_counts(
    rows: list[SpreadTerminalRow],
) -> dict[str, int]:
    tokens = {
        "FUTURES": set(),
        "FUTURES-SPOT": set(),
        "SPOT": set(),
        "DEX-FUTURES": set(),
        "DEX-SPOT": set(),
    }
    for row in rows:
        if not lane_rankable(row):
            continue
        if row.route_kind == "DEX-SPOT":
            tokens["DEX-SPOT"].add(row.token)
        elif row.route_kind == "FUTURES":
            tokens["FUTURES"].add(row.token)
        elif row.route_kind in {"FUTURES-SPOT", "SPOT-FUTURES"}:
            tokens["FUTURES-SPOT"].add(row.token)
        elif row.route_kind == "SPOT":
            tokens["SPOT"].add(row.token)
        elif row.route_kind in {"DEX-FUTURES", "FUTURES-DEX"}:
            tokens["DEX-FUTURES"].add(row.token)
    return {kind: len(values) for kind, values in tokens.items()}


# The snapshot is re-read on every request. That was affordable at 4.7MB, but
# carrying the full positive-funding universe multiplies it, and re-parsing tens
# of megabytes per page view is not. The file is rewritten wholesale, so its
# mtime is a sound cache key; _propagate_funding_by_leg is idempotent, so reusing
# an already-propagated payload is safe.
_BULK_KEYS = ("api_discovered_rows", "dex_discovered_rows")
_SNAPSHOT_CACHE_LOCK = Lock()
_ROW_CACHE: dict[tuple[str, int], tuple[float, list["SpreadTerminalRow"]]] = {}
_ROW_CACHE_TTL_SECONDS = float(os.environ.get("SPREADBOARD_ROW_CACHE_SECONDS", "5"))
_RESULT_CACHE: dict[tuple[Any, ...], tuple[int, float, dict[str, Any]]] = {}
_RESULT_CACHE_TTL_SECONDS = float(os.environ.get("SPREADBOARD_RESULT_CACHE_SECONDS", "90"))
_RESULT_CACHE_MAX_ENTRIES = int(os.environ.get("SPREADBOARD_RESULT_CACHE_ENTRIES", "4"))
# Shortlist a few more tokens than we display so lane filtering inside the
# grouping cannot leave the headline short.
TOP_GROUP_SHORTLIST = 24


# The websocket worker already streams order books for the busiest legs, but the
# board only ever read them indirectly, through whatever the fast-quote worker
# last wrote to disk. That is what made a route minutes old on a page load.
# Prices are now taken from the live store at request time, so a streamed route
# is as current as the exchange feed rather than as current as the last file.
LIVE_BOOK_MAX_AGE_SECONDS = float(os.environ.get("SPREADBOARD_LIVE_BOOK_AGE_SECONDS", "30"))
LIVE_BOOK_TARGET_NOTIONAL_USD = float(
    os.environ.get("SPREADBOARD_LIVE_BOOK_NOTIONAL_USD", "50")
)


def _live_books() -> dict[str, Any]:
    try:
        from spreadboard import live_book_cache

        if not live_book_cache.DEFAULT_PATH.exists():
            return {}
        return live_book_cache.LiveBookStore().load_all(
            max_age_seconds=LIVE_BOOK_MAX_AGE_SECONDS
        )
    except Exception:  # noqa: BLE001 - a missing feed must not take the board down.
        return {}


def _book_side(book: Any, side: str) -> tuple[float | None, float | None]:
    """Top of book and the depth-weighted price at the standard probe size."""
    from spreadarb.api_discovery.orderbook import depth_weighted_price

    levels = book.asks if side == "ask" else book.bids
    if not levels:
        return None, None
    top = _float_or_none(levels[0][0])
    try:
        vwap = _float_or_none(
            depth_weighted_price(levels, LIVE_BOOK_TARGET_NOTIONAL_USD)
        )
    except Exception:  # noqa: BLE001
        vwap = None
    return top, vwap or top


def live_prices_for(routes: list[dict[str, Any]]) -> dict[str, tuple[float | None, float | None]]:
    """Spread and carry straight from the streaming books, for a set of rendered routes.

    The push path must not go through the grouped board's cache: a cached price
    is exactly what the stream exists to correct.
    """
    books = _live_books()
    if not books or not routes:
        return {}
    from spreadboard import live_book_cache

    out: dict[str, tuple[float | None, float | None]] = {}
    for route in routes:
        long_book = books.get(
            live_book_cache.cache_key(
                str(route.get("long_venue") or ""), str(route.get("long_market_type") or ""),
                str(route.get("long_market_symbol") or ""))
        )
        short_book = books.get(
            live_book_cache.cache_key(
                str(route.get("short_venue") or ""), str(route.get("short_market_type") or ""),
                str(route.get("short_market_symbol") or ""))
        )
        # One live leg is enough. Requiring both meant a route could only move
        # if every venue on it streamed, which left Futures-Spot and DEX lanes
        # frozen -- and a DEX leg has no websocket to stream from at all, so
        # those could never have moved. The leg that is live is re-priced and
        # the other keeps its last quoted price, which is strictly closer to the
        # market than leaving the whole row stale.
        if long_book is None and short_book is None:
            continue
        if long_book is not None:
            ask, _ = _book_side(long_book, "ask")
        else:
            ask = _float_or_none(route.get("long_ask")) or _float_or_none(route.get("long_price"))
        if short_book is not None:
            bid, _ = _book_side(short_book, "bid")
        else:
            bid = _float_or_none(route.get("short_bid")) or _float_or_none(route.get("short_price"))
        if not ask or not bid or ask <= 0:
            continue
        out[str(route["route_key"])] = (
            (bid / ask - 1.0) * 100.0,
            _float_or_none(route.get("funding_daily_pct")),
        )
    return out


def apply_live_books(
    rows: list["SpreadTerminalRow"], books: dict[str, Any], *, now: float
) -> list["SpreadTerminalRow"]:
    """Re-price every route whose two legs are both streaming right now."""
    if not books or not rows:
        return rows
    from spreadboard import live_book_cache

    updated: list[SpreadTerminalRow] = []
    for row in rows:
        long_key = live_book_cache.cache_key(
            str(row.long_venue or ""), str(row.long_market_type or ""),
            str(row.long_market_symbol or ""),
        )
        short_key = live_book_cache.cache_key(
            str(row.short_venue or ""), str(row.short_market_type or ""),
            str(row.short_market_symbol or ""),
        )
        long_book = books.get(long_key)
        short_book = books.get(short_key)
        # One live leg is enough, the same rule live_prices_for follows. This
        # decides what the board filters on, so requiring both meant a route
        # with one fresh leg was ranked and filtered on a price from the last
        # scan while the stream showed something else.
        if long_book is None and short_book is None:
            updated.append(row)
            continue
        if long_book is not None:
            ask, ask_vwap = _book_side(long_book, "ask")
        else:
            ask = _float_or_none(row.long_ask) or _float_or_none(row.long_price)
            ask_vwap = ask
        if short_book is not None:
            bid, bid_vwap = _book_side(short_book, "bid")
        else:
            bid = _float_or_none(row.short_bid) or _float_or_none(row.short_price)
            bid_vwap = bid
        if not ask or not bid or ask <= 0 or bid <= 0:
            updated.append(row)
            continue
        executable = (bid / ask - 1.0) * 100.0
        depth = (
            (bid_vwap / ask_vwap - 1.0) * 100.0
            if ask_vwap and bid_vwap and ask_vwap > 0
            else executable
        )
        stamps = [
            int(book.quote_ts_us)
            for book in (long_book, short_book)
            if book is not None
        ]
        quote_ts_us = min(stamps) if stamps else int(row.quote_ts_us or 0)
        age_min = max(0.0, (now - quote_ts_us / 1_000_000) / 60.0)
        updated.append(
            replace(
                row,
                long_price=ask,
                short_price=bid,
                executable_spread_pct=executable,
                depth_weighted_spread_pct=depth,
                displayed_open_spread_pct=executable,
                quote_ts_us=quote_ts_us,
                age_min=age_min,
                freshness="fresh",
                status="live",
                live_book=True,
            )
        )
    return updated


def _fast_quote_delta_path(path: Path) -> Path:
    return Path(path).with_name("api_discovery_fast_quotes.json")


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


_FAST_REFRESH_META_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


def _fast_quote_refresh_meta(delta_path: Path) -> dict[str, Any]:
    """The fast worker's own report of its last cycle, from the delta it wrote.

    Cached on mtime: the header is a few hundred bytes but it sits behind the
    delta's rows, so reading it means parsing the file.
    """
    mtime = _mtime_ns(delta_path)
    if not mtime:
        return {}
    key = (str(delta_path), mtime)
    with _SNAPSHOT_CACHE_LOCK:
        cached = _FAST_REFRESH_META_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        payload = json.loads(delta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    meta = payload.get("fast_quote_refresh")
    meta = dict(meta) if isinstance(meta, dict) else {}
    with _SNAPSHOT_CACHE_LOCK:
        _FAST_REFRESH_META_CACHE.clear()
        _FAST_REFRESH_META_CACHE[key] = meta
    return meta


def _apply_fast_quote_delta(
    rows: list["SpreadTerminalRow"],
    delta_path: Path,
    *,
    now: float,
    metadata: dict[str, dict[str, Any]],
    rails: dict[str, dict[str, Any]],
) -> list["SpreadTerminalRow"]:
    """Overlay the routes the fast worker just re-quoted.

    Only a few hundred routes move each minute. Rebuilding all of them from a
    rewritten snapshot cost a full parse and re-materialisation every 60s, which
    is what kept the board pinned at 100% of a small machine.
    """
    try:
        payload = json.loads(delta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return rows
    fresh: dict[str, SpreadTerminalRow] = {}
    for raw in payload.get("rows") or []:
        if not isinstance(raw, dict):
            continue
        bucket = (
            "dex_discovered_rows"
            if str(raw.get("source_kind") or "") == "dex_discovered"
            else "api_discovered_rows"
        )
        row = _row_from_api(raw, bucket=bucket, now=now, metadata=metadata, rails=rails)
        fresh[row.route_key] = row
    if not fresh:
        return rows
    merged = [fresh.pop(row.route_key, row) for row in rows]
    merged.extend(fresh.values())
    return merged


def _cached_snapshot(path: Path) -> dict[str, Any]:
    """Parse the snapshot without holding onto the raw tree.

    Caching the parsed payload AND the row objects built from it meant carrying
    the same data twice: a 77MB snapshot became roughly 700MB of Python, and the
    droplet sat at 177MB free three minutes after a restart, leaving no room for
    the discovery worker to finish. The rows are what every caller actually uses,
    and they have their own cache, so the tree is released as soon as they exist.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _load_api_discovery_rows(
    path: Path,
    *,
    now: float,
    metadata: dict[str, dict[str, Any]] | None = None,
    rails: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[SpreadTerminalRow], dict[str, Any]]:
    try:
        payload = _cached_snapshot(path)
    except OSError as exc:
        return [], {
            "status": "missing",
            "path": str(path),
            "error": str(exc),
            "row_count": 0,
        }
    except json.JSONDecodeError as exc:
        return [], {
            "status": "error",
            "path": str(path),
            "error": str(exc),
            "row_count": 0,
        }

    # Building a dataclass per row is the other per-request cost, and it grows
    # with the universe. Only freshness and age depend on `now`, and the board
    # itself only moves every 20s, so a few seconds of reuse is invisible.
    #
    # The delta is keyed separately: the discovery snapshot changes every 20
    # minutes, the fast-quote delta every minute. Keying on both means a price
    # refresh no longer forces the whole board to be rebuilt.
    delta_path = _fast_quote_delta_path(path)
    cache_key = (str(path), path.stat().st_mtime_ns, _mtime_ns(delta_path))
    with _SNAPSHOT_CACHE_LOCK:
        cached_rows = _ROW_CACHE.get(cache_key)
    if cached_rows is not None and 0.0 <= now - cached_rows[0] < _ROW_CACHE_TTL_SECONDS:
        rows = cached_rows[1]
    else:
        _propagate_funding_by_leg(payload)
        rows = [
            _row_from_api(
                raw,
                bucket=bucket,
                now=now,
                metadata=metadata or {},
                rails=rails or {},
            )
            for bucket in ("api_discovered_rows", "dex_discovered_rows")
            for raw in payload.get(bucket) or []
            if isinstance(raw, dict)
        ]
        rows = _apply_fast_quote_delta(
            rows, delta_path, now=now, metadata=metadata or {}, rails=rails or {}
        )
        with _SNAPSHOT_CACHE_LOCK:
            _ROW_CACHE.clear()
            _ROW_CACHE[cache_key] = (now, rows)
        # The tree is no longer referenced by anything but this frame.
        payload = {key: value for key, value in payload.items() if key not in _BULK_KEYS}
        gc.collect()
    updated_at = _str_or_none(payload.get("updated_at"))
    discovery_age = _iso_age_min(updated_at, now=now)
    # Since the fast worker started writing a delta instead of rewriting the
    # snapshot, the snapshot's own `fast_quote_refresh` is frozen at whenever the
    # scan wrote it. The delta carries the current one, and it is what decides
    # whether the board reads as live: without this the page reports the age of
    # the last discovery scan and members are told the feed is reconnecting.
    fast_refresh = _fast_quote_refresh_meta(delta_path)
    if not fast_refresh:
        fast_refresh = (
            payload.get("fast_quote_refresh")
            if isinstance(payload.get("fast_quote_refresh"), dict)
            else {}
        )
    fast_updated_at = _str_or_none(fast_refresh.get("updated_at"))
    fast_age = (
        _iso_age_min(fast_updated_at, now=now)
        if fast_refresh.get("status") == "ok"
        and (_int_or_none(fast_refresh.get("updated_routes")) or 0) > 0
        else None
    )
    effective_age = min(
        (value for value in (discovery_age, fast_age) if value is not None),
        default=None,
    )
    effective_updated_at = (
        fast_updated_at
        if fast_age is not None
        and (discovery_age is None or fast_age <= discovery_age)
        else updated_at
    )
    return rows, {
        "status": _freshness(effective_age, DEFAULT_MAX_AGE_MIN),
        "path": str(path),
        "updated_at": effective_updated_at,
        "age_min": effective_age,
        "discovery_updated_at": updated_at,
        "discovery_age_min": discovery_age,
        "fast_quote_age_min": fast_age,
        "row_count": len(rows),
        "api_discovered_count": len(payload.get("api_discovered_rows") or []),
        "dex_discovered_count": len(payload.get("dex_discovered_rows") or []),
        "executor_ready_count": len(payload.get("executor_ready_rows") or []),
        "expires_at": payload.get("expires_at"),
        "worker_status": ((payload.get("source_refresh") or {}).get("status")),
        "dex_spot_source": _dex_spot_source_status(payload),
        "fast_quote_refresh": payload.get("fast_quote_refresh"),
    }


def _propagate_funding_by_leg(payload: dict[str, Any]) -> None:
    """Reuse one settled futures-leg result across every route using that leg."""

    raw_rows = [
        row
        for bucket in ("api_discovered_rows", "dex_discovered_rows")
        for row in payload.get(bucket) or []
        if isinstance(row, dict)
    ]
    leg_funding: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in raw_rows:
        for side in ("long", "short"):
            key = _raw_futures_leg_key(row, side)
            funding = _raw_leg_funding(row, side)
            if key is None or not funding:
                continue
            existing = leg_funding.get(key)
            if existing is None or _funding_leg_quality(funding) > _funding_leg_quality(existing):
                leg_funding[key] = dict(funding)

    fields = (
        "status",
        "reason",
        "funding_24h_pct",
        "projected_24h_pct",
        "current_funding_pct",
        "funding_interval_hours",
        "funding_interval_assumed",
        "next_funding_ts_us",
        "samples",
    )
    for row in raw_rows:
        notes = row.setdefault("notes", {})
        if not isinstance(notes, dict):
            notes = {}
            row["notes"] = notes
        funding = notes.setdefault("funding", {})
        if not isinstance(funding, dict):
            funding = {}
            notes["funding"] = funding
        settled: dict[str, float | None] = {}
        projected: dict[str, float | None] = {}
        has_futures = False
        for side in ("long", "short"):
            if row.get(f"{side}_market_type") != "Futures":
                settled[side] = 0.0
                projected[side] = 0.0
                continue
            has_futures = True
            key = _raw_futures_leg_key(row, side)
            source = leg_funding.get(key) if key is not None else None
            target = funding.setdefault(side, {})
            if not isinstance(target, dict):
                target = {}
                funding[side] = target
            if source:
                for field in fields:
                    if target.get(field) is None and source.get(field) is not None:
                        target[field] = source[field]
            settled[side] = _float_or_none(target.get("funding_24h_pct"))
            projected[side] = _float_or_none(target.get("projected_24h_pct"))
        if has_futures and all(settled.get(side) is not None for side in ("long", "short")):
            row["funding_24h_pct"] = settled["short"] - settled["long"]
            row["funding_24h_source"] = "settled_public_events"
        if has_futures and all(projected.get(side) is not None for side in ("long", "short")):
            row["funding_projected_24h_pct"] = projected["short"] - projected["long"]


def _raw_futures_leg_key(
    row: dict[str, Any],
    side: str,
) -> tuple[str, str, str] | None:
    if row.get(f"{side}_market_type") != "Futures":
        return None
    token = str(row.get("token") or "").upper()
    venue = str(row.get(f"{side}_venue") or "")
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    leg_input = inputs.get(side) if isinstance(inputs.get(side), dict) else {}
    symbol = str(leg_input.get("symbol") or f"{token}/USDT:USDT")
    return (token, venue, symbol) if token and venue and symbol else None


def _raw_leg_funding(row: dict[str, Any], side: str) -> dict[str, Any]:
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    funding = notes.get("funding") if isinstance(notes.get("funding"), dict) else {}
    return funding.get(side) if isinstance(funding.get(side), dict) else {}


def _funding_leg_quality(funding: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(_float_or_none(funding.get("funding_24h_pct")) is not None),
        int(_float_or_none(funding.get("projected_24h_pct")) is not None),
        int(_float_or_none(funding.get("current_funding_pct")) is not None),
    )


def _dex_spot_source_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Report whether the OKX DEX spot quote source actually ran.

    Futures-DEX renders empty whenever this source is skipped. The rows counted
    by `dex_discovered_count` are Hyperliquid/Aster perpetuals, which classify as
    Futures-Futures, so that number is not evidence that DEX quoting works.
    """

    sources = ((payload.get("source_refresh") or {}).get("sources")) or []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("name") == "okx_dex_quote":
            return {
                "name": "okx_dex_quote",
                "status": source.get("status"),
                "rows": source.get("rows"),
                "blockers": list(source.get("blockers") or []),
                "errors": [
                    str(error)[:240]
                    for error in (source.get("errors") or [])[:5]
                ],
                "details": {
                    key: value
                    for key, value in dict(source.get("details") or {}).items()
                    if key in {"provider", "quote_count"}
                },
            }
    return {"name": "okx_dex_quote", "status": "absent", "rows": 0, "blockers": []}


def _public_source_health(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        key: meta.get(key)
        for key in (
            "status",
            "updated_at",
            "age_min",
            "discovery_updated_at",
            "discovery_age_min",
            "fast_quote_age_min",
            "row_count",
            "api_discovered_count",
            "dex_discovered_count",
            "dex_spot_source",
            "expires_at",
            "fast_quote_refresh",
        )
        if meta.get(key) is not None
    }


def _apply_live_funding(raw: dict[str, Any]) -> dict[str, Any]:
    """Overlay the current funding rate the bulk sweep fetched for each leg.

    The rate a row carries otherwise comes from whenever the discovery scan or
    the rotating quote worker last reached that venue. Measured against the
    venues directly: 554 of 5,382 futures legs carried no rate at all and 424
    more disagreed, because the quote worker is a fresh process each cycle and
    covers about three venues of eighteen per pass.
    """
    from spreadboard import bulk_quotes

    legs = bulk_quotes.load_funding()
    if not legs:
        return raw
    notes = raw.get("notes")
    updated_notes: dict[str, Any] | None = None
    for side in ("long", "short"):
        if raw.get(f"{side}_market_type") != "Futures":
            continue
        key = f"{raw.get(f'{side}_venue')}|{raw.get(f'{side}_market_symbol')}"
        entry = legs.get(key)
        if not entry:
            continue
        if updated_notes is None:
            updated_notes = dict(notes) if isinstance(notes, dict) else {}
            existing = updated_notes.get("route_inputs")
            updated_notes["route_inputs"] = dict(existing) if isinstance(existing, dict) else {}
        route_inputs = updated_notes["route_inputs"]
        leg = dict(route_inputs.get(side) or {})
        leg["current_funding_pct"] = entry.get("rate_pct")
        if entry.get("interval_hours") is not None:
            leg["funding_interval_hours"] = entry["interval_hours"]
        if entry.get("next_funding_ts_us") is not None:
            leg["next_funding_ts_us"] = entry["next_funding_ts_us"]
        route_inputs[side] = leg
    if updated_notes is None:
        return raw
    return {**raw, "notes": updated_notes}


def _mirror_if_spot_sale_required(raw: dict[str, Any]) -> dict[str, Any]:
    """Re-orient a route that could only be taken the other way round.

    A route with the futures leg long and the spot leg short cannot be taken as
    written -- it sells spot you do not own. The board handled that by negating
    the carry while still printing the legs in the original order, so a row read
    "long Gate Futures, short Gate Spot" while its +0.29%/day described the
    opposite trade. Anyone following the label put on the losing side.

    Worse, the spread was never re-derived. GUA showed 192.29% buying BitMart
    futures at 0.05186 and selling Gate spot at 0.15158; the trade you can
    actually do -- buy Gate spot at 0.15491, short BitMart futures at 0.05186 --
    is -66.5%. The headline edge only existed in the direction nobody can trade.

    Mirroring here, before the row is built, means the label, the spread, the
    carry and the ranking all describe the same position.
    """
    if str(raw.get("short_market_type") or "") != "Spot":
        return raw
    if str(raw.get("long_market_type") or "") != "Futures":
        return raw

    mirrored = dict(raw)
    for key in raw:
        if key.startswith("long_"):
            partner = "short_" + key[len("long_") :]
            mirrored[key] = raw.get(partner)
            mirrored[partner] = raw[key]

    # Every per-side block moves with its leg, not with the label: notes carries
    # `funding`, `identity` and `route_inputs`, and swapping only one of them
    # left Gate's funding rate sitting on Mexc's spot leg and lost the DEX
    # leg's chain and contract.
    notes = raw.get("notes")
    if isinstance(notes, dict):
        swapped_notes = dict(notes)
        for name, block in notes.items():
            if isinstance(block, dict) and ("long" in block or "short" in block):
                swapped_notes[name] = {
                    **block,
                    "long": block.get("short"),
                    "short": block.get("long"),
                }
        mirrored["notes"] = swapped_notes

    # The edge is now buy the old short leg, sell the old long leg.
    original_legs = (raw.get("notes") or {}).get("route_inputs") or {}
    buy = original_legs.get("short") or {}
    sell = original_legs.get("long") or {}
    for spread_key, bid_key, ask_key in (
        ("executable_spread_pct", "bid_vwap", "ask_vwap"),
        ("depth_weighted_spread_pct", "bid_vwap", "ask_vwap"),
        ("displayed_open_spread_pct", "bid", "ask"),
    ):
        if spread_key not in raw:
            continue
        sell_bid = _float_or_none(sell.get(bid_key)) or _float_or_none(sell.get("bid"))
        buy_ask = _float_or_none(buy.get(ask_key)) or _float_or_none(buy.get("ask"))
        mirrored[spread_key] = (
            (sell_bid / buy_ask - 1.0) * 100.0
            if sell_bid and buy_ask and buy_ask > 0
            else None
        )

    # Short-minus-long now runs the other way, so the stored carry flips with it.
    for key in (
        "funding_daily_pct",
        "funding_projected_24h_pct",
        "funding_spread_apr_pct",
        "funding_apr_pct",
        "funding_24h_pct",
        "funding_spread_pct",
    ):
        value = _float_or_none(raw.get(key))
        if value is not None:
            mirrored[key] = -value
    return mirrored


def _row_from_api(
    raw: dict[str, Any],
    *,
    bucket: str,
    now: float,
    metadata: dict[str, dict[str, Any]] | None = None,
    rails: dict[str, dict[str, Any]] | None = None,
) -> SpreadTerminalRow:
    raw = _apply_live_funding(_mirror_if_spot_sale_required(raw))
    token = str(raw.get("token") or "").upper().strip()
    long_venue = _str_or_none(raw.get("long_venue"))
    short_venue = _str_or_none(raw.get("short_venue"))
    long_market_type = _str_or_none(raw.get("long_market_type"))
    short_market_type = _str_or_none(raw.get("short_market_type"))
    source_kind = _str_or_none(raw.get("source_kind"))
    route_kind = _route_kind(
        long_venue=long_venue,
        long_market_type=long_market_type,
        short_venue=short_venue,
        short_market_type=short_market_type,
        source_kind=source_kind,
    )
    quote_ts_us = _int_or_none(raw.get("quote_ts_us"))
    age = _age_min(now, quote_ts_us)
    source_group = "public_api"
    executor_status = _str_or_none(raw.get("executor_status"))
    validation = _str_or_none(raw.get("validation_state"))
    blockers = _string_list(raw.get("blockers"))
    funding_apr = _float_or_none(
        raw.get("funding_spread_apr_pct")
        if raw.get("funding_spread_apr_pct") is not None
        else raw.get("funding_apr_pct")
    )
    funding_daily = _float_or_none(raw.get("funding_daily_pct"))
    funding_24h = _float_or_none(raw.get("funding_24h_pct"))
    funding_projected_24h = _float_or_none(
        raw.get("funding_projected_24h_pct"),
        funding_daily,
        funding_apr / 365.0 if funding_apr is not None else None,
    )
    route_key = "|".join(
        [token or "?", long_venue or "?", long_market_type or "?", short_venue or "?", short_market_type or "?"]
    )
    notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
    route_inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    identities = notes.get("identity") if isinstance(notes.get("identity"), dict) else {}
    long_identity = identities.get("long") if isinstance(identities.get("long"), dict) else {}
    short_identity = identities.get("short") if isinstance(identities.get("short"), dict) else {}
    funding = notes.get("funding") if isinstance(notes.get("funding"), dict) else {}
    long_funding = funding.get("long") if isinstance(funding.get("long"), dict) else {}
    short_funding = funding.get("short") if isinstance(funding.get("short"), dict) else {}
    long_market_symbol = _str_or_none(
        (route_inputs.get("long") or {}).get("symbol")
        if isinstance(route_inputs.get("long"), dict)
        else None
    )
    short_market_symbol = _str_or_none(
        (route_inputs.get("short") or {}).get("symbol")
        if isinstance(route_inputs.get("short"), dict)
        else None
    )
    dex_identity = (
        long_identity
        if "dex" in str(long_venue or "").casefold()
        else short_identity
        if "dex" in str(short_venue or "").casefold()
        else {}
    )
    dex_chain = _str_or_none(dex_identity.get("chain_id"))
    dex_contract = _str_or_none(dex_identity.get("token_address"))
    long_rails = public_rails.rail_state(rails or {}, long_venue, token)
    short_rails = public_rails.rail_state(rails or {}, short_venue, token)
    blockers.extend(
        _route_mirage_reasons(
            raw=raw,
            long_market_type=long_market_type,
            short_market_type=short_market_type,
            long_rails=long_rails,
            short_rails=short_rails,
        )
    )
    blockers.extend(
        _dex_contract_mirage_reasons(
            token=token,
            chain_id=dex_chain,
            contract=dex_contract,
            identity_key=_str_or_none(raw.get("identity_key")),
        )
    )
    return SpreadTerminalRow(
        token=token,
        token_name=token_metadata.token_name(token, metadata or {}),
        route_key=route_key,
        route_kind=route_kind,
        source_group=source_group,
        source_label="Public API",
        source_name=_str_or_none(raw.get("source_name")),
        long_venue=long_venue,
        long_market_type=long_market_type,
        short_venue=short_venue,
        short_market_type=short_market_type,
        executable_spread_pct=_float_or_none(raw.get("executable_spread_pct")),
        depth_weighted_spread_pct=_float_or_none(raw.get("depth_weighted_spread_pct")),
        displayed_open_spread_pct=(
            _float_or_none(raw.get("executable_spread_pct"))
            if _float_or_none(raw.get("executable_spread_pct")) is not None
            else _float_or_none(raw.get("depth_weighted_spread_pct"))
        ),
        funding_apr_pct=funding_apr,
        funding_daily_pct=(
            funding_daily
            if funding_daily is not None
            else (funding_apr / 365.0 if funding_apr is not None else None)
        ),
        funding_spread_pct=funding_daily,
        depth_usd=_depth_from_api(raw),
        validation_state=validation,
        executor_status=executor_status,
        status="live" if _freshness(age, DEFAULT_MAX_AGE_MIN) == "fresh" else "unavailable",
        blockers=blockers,
        next_action="Open route details",
        freshness=_freshness(age, DEFAULT_MAX_AGE_MIN),
        age_min=age,
        quote_ts_us=quote_ts_us,
        href=f"/token/{token}" if token else "/markets",
        long_price=_float_or_none(
            raw.get("long_price"),
            raw.get("long_ask_vwap"),
            raw.get("long_ask"),
            raw.get("long_mark"),
            _nested_float(route_inputs, "long", "ask_vwap"),
            _nested_float(route_inputs, "long", "ask"),
        ),
        short_price=_float_or_none(
            raw.get("short_price"),
            raw.get("short_bid_vwap"),
            raw.get("short_bid"),
            raw.get("short_mark"),
            _nested_float(route_inputs, "short", "bid_vwap"),
            _nested_float(route_inputs, "short", "bid"),
        ),
        long_funding_pct=_float_or_none(
            raw.get("long_funding_pct"),
            raw.get("long_funding"),
            _nested_float(route_inputs, "long", "current_funding_pct"),
            long_funding.get("current_funding_pct"),
            long_funding.get("rate_pct"),
        ),
        short_funding_pct=_float_or_none(
            raw.get("short_funding_pct"),
            raw.get("short_funding"),
            _nested_float(route_inputs, "short", "current_funding_pct"),
            short_funding.get("current_funding_pct"),
            short_funding.get("rate_pct"),
        ),
        funding_24h_pct=funding_24h,
        funding_projected_24h_pct=funding_projected_24h,
        funding_24h_source=_str_or_none(raw.get("funding_24h_source")),
        long_funding_interval_hours=_float_or_none(
            _nested_float(route_inputs, "long", "funding_interval_hours"),
            long_funding.get("funding_interval_hours"),
            long_funding.get("interval_hours"),
        ),
        short_funding_interval_hours=_float_or_none(
            _nested_float(route_inputs, "short", "funding_interval_hours"),
            short_funding.get("funding_interval_hours"),
            short_funding.get("interval_hours"),
        ),
        long_funding_interval_assumed=bool(
            long_funding.get(
                "funding_interval_assumed",
                long_funding.get("interval_assumed", False),
            )
        ),
        short_funding_interval_assumed=bool(
            short_funding.get(
                "funding_interval_assumed",
                short_funding.get("interval_assumed", False),
            )
        ),
        long_next_funding_ts_us=_int_or_none(
            _float_or_none(
                _nested_float(route_inputs, "long", "next_funding_ts_us"),
                long_funding.get("next_funding_ts_us"),
            )
        ),
        short_next_funding_ts_us=_int_or_none(
            _float_or_none(
                _nested_float(route_inputs, "short", "next_funding_ts_us"),
                short_funding.get("next_funding_ts_us"),
            )
        ),
        long_volume_24h_usd=_nested_float(route_inputs, "long", "volume_24h_usd"),
        short_volume_24h_usd=_nested_float(route_inputs, "short", "volume_24h_usd"),
        long_bid=_nested_float(route_inputs, "long", "bid"),
        long_ask=_nested_float(route_inputs, "long", "ask"),
        short_bid=_nested_float(route_inputs, "short", "bid"),
        short_ask=_nested_float(route_inputs, "short", "ask"),
        long_deposit_enabled=_bool_or_none(long_rails.get("deposit")),
        long_withdraw_enabled=_bool_or_none(long_rails.get("withdraw")),
        short_deposit_enabled=_bool_or_none(short_rails.get("deposit")),
        short_withdraw_enabled=_bool_or_none(short_rails.get("withdraw")),
        long_market_symbol=long_market_symbol,
        short_market_symbol=short_market_symbol,
        long_exchange_url=exchange_links.exchange_market_url(
            venue=long_venue,
            market_type=long_market_type,
            market_symbol=long_market_symbol,
            token=token,
        ),
        short_exchange_url=exchange_links.exchange_market_url(
            venue=short_venue,
            market_type=short_market_type,
            market_symbol=short_market_symbol,
            token=token,
        ),
        dex_chain=dex_chain,
        dex_contract=dex_contract,
        raw_source_kind=source_kind or bucket,
        mirage_guarded=any(str(item).startswith("mirage_guard:") for item in blockers),
    )


def _load_board_rows(path: Path, *, now: float) -> tuple[list[SpreadTerminalRow], dict[str, Any]]:
    snapshot = board.load_board(path, include_stale=True, max_age_min=None, limit=None, now=now)
    rows = [_row_from_board(row) for row in snapshot.rows]
    return rows, {
        "status": "error" if snapshot.error else _freshness(snapshot.age_min, DEFAULT_MAX_AGE_MIN),
        "path": snapshot.source_path,
        "error": snapshot.error,
        "age_min": snapshot.age_min,
        "row_count": len(rows),
        "fresh_count": len([row for row in rows if row.freshness == "fresh"]),
        "stale_count": len([row for row in rows if row.freshness == "stale"]),
    }


def _row_from_board(row: board.BoardRow) -> SpreadTerminalRow:
    source_group = "website_verified"
    blockers = list(row.blockers or [])
    freshness = _freshness(row.age_min, DEFAULT_MAX_AGE_MIN)
    status = _status_for_row(
        freshness=freshness,
        executor_status=None,
        blockers=blockers,
        validation_state=row.strategy_verdict,
    )
    return SpreadTerminalRow(
        token=row.symbol,
        token_name=None,
        route_key=row.route_key,
        route_kind=row.kind,
        source_group=source_group,
        source_label="Website verified",
        source_name=row.source_tab or "legacy_public_verification",
        long_venue=row.long_venue,
        long_market_type=row.long_market_type,
        short_venue=row.short_venue,
        short_market_type=row.short_market_type,
        executable_spread_pct=row.spread_pct,
        depth_weighted_spread_pct=row.depth_weighted_spread_pct,
        displayed_open_spread_pct=row.displayed_open_spread_pct,
        funding_apr_pct=row.funding_apr_pct,
        funding_daily_pct=(
            row.funding_apr_pct / 365.0 if row.funding_apr_pct is not None else None
        ),
        funding_spread_pct=row.funding_spread_pct,
        depth_usd=row.depth_usd,
        validation_state=row.strategy_verdict,
        executor_status=None,
        status=status,
        blockers=blockers,
        next_action=row.next_action or _next_action(status, blockers, source_group=source_group),
        freshness=freshness,
        age_min=row.age_min,
        quote_ts_us=row.ingested_at_us,
        href=f"/pair/{board.route_key_url(row.route_key)}",
        long_price=row.long_price,
        short_price=row.short_price,
        long_funding_pct=row.long_funding_pct,
        short_funding_pct=row.short_funding_pct,
        long_deposit_enabled=row.long_deposit_enabled,
        long_withdraw_enabled=row.long_withdraw_enabled,
        short_deposit_enabled=row.short_deposit_enabled,
        short_withdraw_enabled=row.short_withdraw_enabled,
        long_market_symbol=None,
        short_market_symbol=None,
        long_exchange_url=exchange_links.exchange_market_url(
            venue=row.long_venue,
            market_type=row.long_market_type,
            market_symbol=None,
            token=row.symbol,
        ),
        short_exchange_url=exchange_links.exchange_market_url(
            venue=row.short_venue,
            market_type=row.short_market_type,
            market_symbol=None,
            token=row.symbol,
        ),
        raw_source_kind="website_verified",
    )


def _filter_rows(rows: list[SpreadTerminalRow], **filters: Any) -> list[SpreadTerminalRow]:
    q = str(filters.get("q") or "").upper().strip()
    exchange = str(filters.get("exchange") or "").casefold().strip()
    kind = _normalize_kind_filter(filters.get("kind"))
    min_spread = filters.get("min_spread_pct")
    min_funding = filters.get("min_abs_funding_24h_pct")
    funding_only = bool(filters.get("funding_only"))
    include_stale = bool(filters.get("include_stale"))

    output: list[SpreadTerminalRow] = []
    for row in rows:
        if q and q not in f"{row.token} {row.token_name or ''}".upper():
            continue
        if exchange and exchange not in " ".join(filter(None, [row.long_venue, row.short_venue])).casefold():
            continue
        if kind:
            if kind == "FUTURES-SPOT-PAIR":
                if row.route_kind not in {"FUTURES-SPOT", "SPOT-FUTURES"}:
                    continue
            elif row.route_kind != kind:
                continue
        if not include_stale and row.freshness == "stale":
            continue
        spread = _entrance_spread(row)
        # Carry and spread are separate mechanisms and a spread floor must not
        # decide a funding row. A farm whose basis never converges can still pay
        # well -- the reference product's whole futures lane runs on negative
        # open spreads, -0.15% to -0.52% -- and applying the floor here dropped
        # exactly those rows before the funding test ever ran.
        if not funding_only and min_spread is not None and spread < float(min_spread):
            continue
        effective_funding = _effective_funding_24h(row)
        # The funding lane is about carry you RECEIVE. A route that pays is not a
        # farm, and ranking on magnitude put a -500% payer above a +200% earner.
        if funding_only and not (effective_funding is not None and effective_funding > 0):
            continue
        if (
            min_funding is not None
            and abs(effective_funding or 0.0) < float(min_funding)
        ):
            continue
        output.append(row)
    return output


def _route_mirage_reasons(
    *,
    raw: dict[str, Any],
    long_market_type: str | None,
    short_market_type: str | None,
    long_rails: dict[str, Any],
    short_rails: dict[str, Any],
) -> list[str]:
    spread = max(
        abs(_float_or_none(raw.get("executable_spread_pct")) or 0.0),
        abs(_float_or_none(raw.get("depth_weighted_spread_pct")) or 0.0),
    )
    reasons: list[str] = []
    if short_market_type == "Spot" and long_market_type != "Spot":
        reasons.append("condition:spot_sell_inventory_required")
    raw_blockers = {str(item) for item in raw.get("blockers") or []}
    identity_unverified = (
        "identity_unverified" in raw_blockers
        or "cex_identity_unverified" in raw_blockers
        or any(item.startswith("identity_collision:") for item in raw_blockers)
    )
    raw_source = str(raw.get("source_kind") or raw.get("raw_source_kind") or "")
    is_dex = "dex" in raw_source.casefold()
    already_guarded = any(item.startswith("mirage_guard:") for item in raw_blockers)
    if is_dex and identity_unverified and not already_guarded:
        reasons.append("mirage_guard:dex_cex_identity_unverified")
    elif spread >= 25.0 and identity_unverified and not already_guarded:
        reasons.append("mirage_guard:high_dislocation_identity_unverified")
    if spread < 1.0:
        return reasons
    if long_market_type == "Spot" and short_market_type == "Spot":
        compatibility = public_rails.transfer_compatibility(long_rails, short_rails)
        status = str(compatibility.get("status") or "unknown")
        if status == "incompatible":
            reasons.append(
                "mirage_guard:spot_transfer_incompatible"
            )
        elif status != "compatible":
            reasons.append(f"condition:spot_transfer_{status}")
    return reasons


def _dex_contract_mirage_reasons(
    *,
    token: str,
    chain_id: str | None,
    contract: str | None,
    identity_key: str | None = None,
    watchlist: dict[str, WatchAsset] | None = None,
) -> list[str]:
    if not chain_id and not contract:
        return []
    if not chain_id or not contract:
        return ["mirage_guard:dex_contract_incomplete"]
    canonical_identity = (
        f"solana:501/token:{contract}"
        if str(chain_id) == "501"
        else f"eip155:{chain_id}/erc20:{contract.casefold()}"
    )
    if identity_key and str(identity_key).casefold() == canonical_identity.casefold():
        return []
    assets = watchlist if watchlist is not None else load_watchlist(DEX_WATCHLIST_PATH)
    asset = assets.get(str(token).upper())
    if asset is None:
        return ["mirage_guard:dex_contract_unregistered"]
    if str(chain_id) == "501":
        expected = str(asset.solana_mint or "")
        matches = expected == contract
    else:
        try:
            expected = str((asset.evm_contracts or {}).get(int(chain_id)) or "")
        except ValueError:
            expected = ""
        matches = bool(expected) and expected.casefold() == contract.casefold()
    return [] if matches else ["mirage_guard:dex_contract_mismatch"]


def _hide_guarded_rows() -> bool:
    """Whether identity-unverified rows are dropped instead of badged."""
    return str(os.environ.get("SPREADBOARD_HIDE_GUARDED_ROWS", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


# Two venues quoting the same asset cannot be an order of magnitude apart -- that
# would be free money at absurd scale. CAT was seen at 0.000001366 on Kucoin spot
# and 806.75 on Bitget futures: a 590,000,000x ratio, i.e. two unrelated tokens
# sharing a ticker. This is a statement about identity, not about opportunity
# size. Tightened 10x -> 3x on 2026-08-01: ANTHROPIC slipped through at 9.8x
# (881% edge) while the reference product showed 3.13%. 3x still preserves a
# 150% capture (2.5x), which the operator has taken for real money.
MAX_CROSS_VENUE_PRICE_RATIO = 3.0


def price_ratio_implausible(row: "SpreadTerminalRow") -> bool:
    """True when the two legs are too far apart to be the same asset."""
    long_price = _float_or_none(getattr(row, "long_price", None))
    short_price = _float_or_none(getattr(row, "short_price", None))
    if not long_price or not short_price or long_price <= 0 or short_price <= 0:
        return False
    ratio = max(long_price, short_price) / min(long_price, short_price)
    return ratio > MAX_CROSS_VENUE_PRICE_RATIO


# Only these lanes require the coin to physically move between venues. Futures
# legs settle in margin, and a DEX leg sits in your own wallet, so neither needs
# a transfer rail.
TRANSFER_ROUTE_KINDS = frozenset({"SPOT", "DEX-SPOT"})

# Spot cannot be shorted. In a futures/spot pair the futures leg is the one that
# gets shorted and the spot leg is simply held long. So a route printed as "long
# futures / short spot" is not a fresh entry at all -- it requires spot inventory
# you already hold. That is a stronger constraint than any deposit rail.
SHORT_SPOT_ROUTE_KINDS = frozenset({"FUTURES-SPOT"})


def requires_existing_spot_inventory(row: "SpreadTerminalRow") -> bool:
    """True when capturing this route would mean selling spot you must already own.

    route_kind alone is not enough: DEX-FUTURES rows appear in BOTH directions.
    `long OKX DEX 1(Spot) / short Kraken Futures` is a normal farm, but
    `long Kucoin Futures / short OKX DEX 1(Spot)` shorts a spot leg sitting in
    your own wallet -- the same constraint as FUTURES-SPOT, under a kind name
    that does not say so. Decide on the leg market types.
    """
    if getattr(row, "route_kind", None) in SHORT_SPOT_ROUTE_KINDS:
        return True
    return (
        getattr(row, "short_market_type", None) == "Spot"
        and getattr(row, "long_market_type", None) == "Futures"
    )

# A price from a book with almost no turnover is noise. U2U ranked at 124%
# against a Kraken leg doing $14.78 of daily volume; LRC at 81% against an HTX
# leg doing zero. VANRY, the one genuine 100%+ edge, has real volume both sides.
MIN_LEG_VOLUME_24H_USD = 10_000.0


def leg_volume_too_thin(row: "SpreadTerminalRow") -> bool:
    """True when either leg's 24h turnover is too small to trust the quote.

    An exact zero means the venue published nothing, not that a live order book
    traded nothing all day: Upbit reports 0 for every market, which was deleting
    KAITO from the board entirely. Unknown turnover is handled by the price
    consensus test instead, which needs no volume to spot a lone bad quote.
    """
    for value in (
        getattr(row, "long_volume_24h_usd", None),
        getattr(row, "short_volume_24h_usd", None),
    ):
        volume = _float_or_none(value)
        if volume is not None and 0.0 < volume < MIN_LEG_VOLUME_24H_USD:
            return True
    return False


# A leg nobody can corroborate -- no turnover published and a price far from what
# every other venue quotes -- is a broken feed. IOTX sat at 0.00669 on Coinbase
# against 0.0023 everywhere else, printing 190% across 36 routes, and slipped
# under the 3x identity bar. The test needs BOTH conditions: VANRY's real ~100%
# edge is roughly 2x apart too, but its legs carry genuine turnover, so it stays.
PRICE_CONSENSUS_DEVIATION = 0.5


# Gate's ETH3L and XT's ETH3L are not the same instrument. Each venue mints its
# own leveraged product with its own NAV, rebalancing schedule and inception
# price, so the gap between them is a naming coincidence rather than an edge --
# and it cannot be closed, because the token cannot move between venues. These
# were every remaining route above 100%: SPCX3L, LTC3S, TSLA3L, ARB3L, LINK3L,
# ETH3L, ETH5S, SUI3L, NVDA3S, BTC5L, CFX3L, all Gate against XT.
#
# SOXL and the other tokenized equities are NOT caught: they are one underlying
# that several venues wrap, and the letters before the L are not a leverage
# multiple.
LEVERAGED_TOKEN_PATTERN = re.compile(r"^[A-Z0-9]+[2-5][LS]$")


def is_venue_specific_leveraged_token(row: "SpreadTerminalRow") -> bool:
    """True for a leveraged product that only exists inside one venue."""
    token = str(getattr(row, "token", "") or "").upper()
    if not LEVERAGED_TOKEN_PATTERN.match(token):
        return False
    # Same venue, both legs: a venue's own spot against its own perp is fine.
    return getattr(row, "long_venue", None) != getattr(row, "short_venue", None)


#: `BASE/QUOTE:SETTLE` settles in a stablecoin or dollar for a linear contract.
#: An inverse (coin-margined) contract settles in the base asset instead.
LINEAR_SETTLE_ASSETS = {"USDT", "USDC", "USD", "USDE", "FDUSD", "BUSD", "DAI", "TUSD", "USD1"}
#: Kraken FI_XRPUSD_260828, Binance BTCUSD_261225: a dated contract carries its
#: expiry in the symbol.
DATED_CONTRACT_PATTERN = re.compile(r"-\d{6,8}$")


def is_non_perpetual_or_inverse(row: "SpreadTerminalRow") -> bool:
    """True for a leg that is not a linear perpetual, so does not belong here.

    An inverse contract quotes funding against the base asset, so applying the
    linear formula to it is not a small error: Kraken's PI_XRPUSD read
    -0.4409%/h against a real -0.00073%/h and headlined XRP at +10.61%/day.
    Dated futures have no funding at all, and their gap to spot is basis that
    converges at expiry rather than an edge anyone can farm. Neither appears on
    the reference product.
    """
    for side in ("long", "short"):
        if str(getattr(row, f"{side}_market_type", "") or "") != "Futures":
            continue
        symbol = str(getattr(row, f"{side}_market_symbol", "") or "")
        if not symbol:
            continue
        if DATED_CONTRACT_PATTERN.search(symbol):
            return True
        _, separator, settle = symbol.partition(":")
        if separator and settle and settle.upper() not in LINEAR_SETTLE_ASSETS:
            return True
    return False


#: How wide a ticker-priced spread may be before it stops being believable.
#: For a liquid market the last trade sits near the mid, so a small edge quoted
#: that way is sound -- the reference product's entire Spot-Spot lane is
#: 0.10-0.21% and Binance's ASR is quoted exactly like this. A large edge is
#: different: it is the empty book that creates it.
TICKER_PRICE_TRUST_LIMIT_PCT = float(
    os.environ.get("SPREADBOARD_TICKER_SPREAD_LIMIT_PCT", "2.0")
)


def spread_is_ticker_derived(row: "SpreadTerminalRow") -> bool:
    """True when a leg is priced off a last trade rather than a book."""
    for side in ("long", "short"):
        bid = _float_or_none(getattr(row, f"{side}_bid", None))
        ask = _float_or_none(getattr(row, f"{side}_ask", None))
        if bid is not None and ask is not None and bid == ask:
            return True
    return False


def spread_is_untrustworthy(row: "SpreadTerminalRow") -> bool:
    """A ticker-priced row claiming an edge far too large to believe.

    bid == ask means the quote came from a ticker, which says how we captured
    it, not that the market is thin -- Binance's ASR is quoted that way and is
    the reference product's top Spot-Spot row at 0.21%. Rejecting every such row
    dropped exactly the tight liquid band that board is made of.

    What cannot stand is a ticker price carrying a large edge. Upbit's BIO last
    traded at 0.01825 while its book was bid 0.02069 / ask 0.0588: buying costs
    three times the shown price, turning +45.97% into roughly -55%. 328 of the
    379 routes printing over 20% were priced this way.
    """
    if not spread_is_ticker_derived(row):
        return False
    return abs(_entrance_spread(row)) > TICKER_PRICE_TRUST_LIMIT_PCT


def pays_something(row: "SpreadTerminalRow") -> bool:
    """Is there anything to earn on this route, either leg of it?

    Every pair is emitted in both directions, so half of all rows are the mirror
    of a real one and lose money by construction. Showing them doubled the board
    for no one's benefit.

    Either side qualifies on its own, because the two are independent: a basis
    farm is entered at a NEGATIVE spread and paid in funding -- SIREN sits at
    -53% open with +138% APR -- while a spread trade can be worth taking with
    funding against it. Only a route where both are against you is dead.
    """
    spread = _float_or_none(getattr(row, "executable_spread_pct", None))
    if spread is not None and spread > 0:
        return True
    carry = _effective_funding_24h(row)
    return carry is not None and carry > 0


def row_is_presentable(row: "SpreadTerminalRow") -> bool:
    """Would this row survive the board's own filters?

    The headline panels ranked off a universe that applied only deliverability
    and price sanity, so rows the board itself rejects still led the page:
    SHIB3S, a 3x leveraged token, headlined Top Arbitrage Edges at +177% while
    the lane beside it correctly refused to list it. The panels and the lane
    must answer the same question.
    """
    return (
        not price_ratio_implausible(row)
        and not leg_volume_too_thin(row)
        and not is_venue_specific_leveraged_token(row)
        and not is_non_perpetual_or_inverse(row)
        and not spread_is_untrustworthy(row)
    )


def unverifiable_price_outliers(rows: list["SpreadTerminalRow"]) -> set[str]:
    """Route keys whose quote disagrees with the market and cannot be vouched for."""
    trusted: dict[str, list[float]] = {}
    for row in rows:
        for side in ("long", "short"):
            price = _float_or_none(getattr(row, f"{side}_price", None))
            volume = _float_or_none(getattr(row, f"{side}_volume_24h_usd", None))
            if price and price > 0 and volume and volume >= MIN_LEG_VOLUME_24H_USD:
                trusted.setdefault(row.token, []).append(price)
    reference = {
        token: statistics.median(prices)
        for token, prices in trusted.items()
        if len(prices) >= 3
    }
    flagged: set[str] = set()
    for row in rows:
        anchor = reference.get(row.token)
        if not anchor or anchor <= 0:
            continue
        for side in ("long", "short"):
            price = _float_or_none(getattr(row, f"{side}_price", None))
            volume = _float_or_none(getattr(row, f"{side}_volume_24h_usd", None))
            if not price or price <= 0:
                continue
            if volume and volume >= MIN_LEG_VOLUME_24H_USD:
                continue
            if abs(price / anchor - 1.0) > PRICE_CONSENSUS_DEVIATION:
                flagged.add(row.route_key)
                break
    return flagged


def route_deliverable(row: "SpreadTerminalRow") -> bool | None:
    """Can the coin actually be moved from the buy venue to the sell venue?

    A persistently huge spread on an identical contract is almost always a
    closed rail, not an opportunity: if it were deliverable, arbitrage would
    have closed it in minutes. SIREN sat at ~100% between OKX DEX and Kucoin
    with a byte-identical BEP20 contract purely because Kucoin deposits were
    shut. Returns None when the rail status is unknown.
    """
    long_venue = getattr(row, "long_venue", None)
    if long_venue and long_venue == getattr(row, "short_venue", None):
        # Cash-and-carry inside one account: there is nothing to deliver anywhere.
        return True
    kind = getattr(row, "route_kind", None)
    if kind in SHORT_SPOT_ROUTE_KINDS:
        # Only the short spot leg needs delivering into.
        dest_in = getattr(row, "short_deposit_enabled", None)
        return None if dest_in is None else bool(dest_in)
    if kind not in TRANSFER_ROUTE_KINDS:
        return True
    source_out = getattr(row, "long_withdraw_enabled", None)
    dest_in = getattr(row, "short_deposit_enabled", None)
    if source_out is None and dest_in is None:
        return None
    # A single known-shut rail is enough to block the trade, even if the other
    # side is unknown -- treating "unknown" as fine let shut rails through.
    if source_out is False or dest_in is False:
        return False
    if source_out is None or dest_in is None:
        return None
    return True


def _is_mirage_guarded(row: SpreadTerminalRow) -> bool:
    return any(str(item).startswith("mirage_guard:") for item in row.blockers)


def _normalize_kind_filter(value: Any) -> str:
    kind = str(value or "").upper().strip()
    return {
        "FUTURES-FUTURES": "FUTURES",
        "FUTURES-SPOT": "FUTURES-SPOT-PAIR",
        "SPOT-FUTURES": "FUTURES-SPOT-PAIR",
        "SPOT-SPOT": "SPOT",
        "FUTURES-DEX": "DEX-FUTURES",
        "SPOT-DEX": "DEX-SPOT",
    }.get(kind, kind)


def _summary(
    all_rows: list[SpreadTerminalRow],
    filtered: list[SpreadTerminalRow],
    visible: list[SpreadTerminalRow],
    *,
    groups: list[dict[str, Any]],
    visible_groups: list[dict[str, Any]],
    offset: int,
    limit: int | None,
) -> dict[str, Any]:
    return {
        "total_rows": len(all_rows),
        "total_tokens": len({row.token for row in all_rows}),
        "visible_rows": len(filtered),
        "matching_rows": len(filtered),
        "returned_rows": len(visible),
        "matching_tokens": len(groups),
        "returned_tokens": len(visible_groups),
        "offset": offset,
        "limit": limit,
        "fresh_rows": len([row for row in filtered if row.freshness == "fresh"]),
        "stale_rows": len([row for row in filtered if row.freshness == "stale"]),
        "api_rows": len(all_rows),
        "dex_rows": len([row for row in all_rows if row.raw_source_kind == "dex_discovered"]),
        "funding_rows": len([row for row in filtered if _effective_funding_24h(row) is not None]),
        "max_executable_spread_pct": max(
            (
                _float_or_none(row.executable_spread_pct) or 0.0
                for row in filtered
                if route_deliverable(row) is not False
                and not price_ratio_implausible(row)
                and not leg_volume_too_thin(row)
            ),
            default=None,
        ),
        "undeliverable_rows": len(
            [row for row in filtered if route_deliverable(row) is False]
        ),
        "identity_mismatch_rows": len(
            [row for row in filtered if price_ratio_implausible(row)]
        ),
        "thin_book_rows": len([row for row in filtered if leg_volume_too_thin(row)]),
        "max_depth_weighted_spread_pct": max(
            (_entrance_spread(row) for row in filtered),
            default=None,
        ),
        # The raw funding_apr_pct is the un-normalised source value; the summary
        # must report the same basis the board ranks and displays.
        "max_abs_funding_apr_pct": max(
            (abs(normalised_funding(row)[1] or 0.0) for row in filtered),
            default=None,
        ),
        "max_abs_funding_24h_pct": max(
            (abs(_effective_funding_24h(row) or 0.0) for row in filtered),
            default=None,
        ),
    }


def _top_unique_groups(rows: list[SpreadTerminalRow], *, metric: str) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in rows
        if row.freshness == "fresh"
        and (metric != "funding" or _effective_funding_24h(row) is not None)
    ]
    # Grouping builds a public dict for every route of every token. Keeping only
    # the top 8 afterwards meant doing that for the whole universe three times a
    # request -- 17s at 12k rows. Rank tokens on the cheap row-level metric
    # first, then group just those.
    best_by_token: dict[str, float] = {}
    for row in candidates:
        value = (
            (_effective_funding_24h(row) or 0.0)
            if metric == "funding"
            else _entrance_spread(row)
        )
        if value > best_by_token.get(row.token, float("-inf")):
            best_by_token[row.token] = value
    shortlist = {
        token
        for token, _ in sorted(best_by_token.items(), key=lambda item: -item[1])[
            : TOP_GROUP_SHORTLIST
        ]
    }
    groups = _group_rows([row for row in candidates if row.token in shortlist])
    if metric == "edge":
        groups = [
            group
            for group in groups
            if (_float_or_none(group.get("best_edge_pct")) or 0.0) > 0
        ]
    groups.sort(
        key=lambda group: _float_or_none(
            group.get("best_funding_24h_pct")
            if metric == "funding"
            else group.get("best_edge_pct")
        )
        or 0.0,
        reverse=True,
    )
    return [
        {key: value for key, value in group.items() if key != "routes"}
        for group in groups[:8]
    ]


def _group_rows(rows: list[SpreadTerminalRow]) -> list[dict[str, Any]]:
    grouped: dict[str, list[SpreadTerminalRow]] = {}
    for row in rows:
        grouped.setdefault(row.token, []).append(row)
    output: list[dict[str, Any]] = []
    for token, token_rows in grouped.items():
        # The headline matches the board convention: buy at the current ask and
        # sell at the current bid. Matched-size VWAP remains execution context.
        token_rows.sort(
            key=lambda row: (
                _entrance_spread(row),
                _float_or_none(row.executable_spread_pct) or -999999.0,
                _float_or_none(row.depth_usd) or 0.0,
                -(row.age_min or 999999.0),
            ),
            reverse=True,
        )
        routes = [_public_row(row) for row in token_rows]
        # A token's headline -- and therefore its rank in every lane listing --
        # must come from a route someone could actually take. Otherwise a shut
        # rail or a ticker collision buys a top-25 slot and pushes a genuinely
        # tradeable token off the page entirely.
        tradeable_rows = [
            row
            for row in token_rows
            if route_deliverable(row) is not False
            and not price_ratio_implausible(row)
            and not leg_volume_too_thin(row)
        ]
        best = max(tradeable_rows or token_rows, key=_entrance_spread)
        funding_rows = [
            row for row in token_rows if _effective_funding_24h(row) is not None
        ]
        # Rank by the SIGNED carry: the farm you would put on receives funding.
        # Every route has a mirror -- ESPORTS' only funding source is Gate at
        # +0.0366%/4h, giving +0.2196%/day one way and -0.2196%/day the other --
        # so picking by magnitude can headline the leg that PAYS. Production
        # reported -119.57% APR where the reference product showed +91.76%.
        best_funding = max(
            funding_rows,
            key=lambda row: (_effective_funding_24h(row) or 0.0),
            default=None,
        )
        output.append(
            {
                "token": token,
                "token_name": best.token_name,
                "href": best.href,
                "route_count": len(token_rows),
                "venues": sorted(
                    {
                        venue
                        for row in token_rows
                        for venue in (row.long_venue, row.short_venue)
                        if venue
                    }
                ),
                "route_kinds": sorted({row.route_kind for row in token_rows}),
                "best_route": _public_row(best),
                "best_edge_pct": _entrance_spread(best),
                "best_funding_route": (
                    _public_row(best_funding) if best_funding is not None else None
                ),
                "best_funding_apr_pct": (
                    normalised_funding(best_funding)[1]
                    if best_funding is not None
                    else None
                ),
                "best_funding_24h_pct": (
                    _effective_funding_24h(best_funding)
                    if best_funding is not None
                    else None
                ),
                "best_funding_24h_basis": (
                    "settled_public_events"
                    if best_funding is not None and best_funding.funding_24h_pct is not None
                    else "projected_current_rate"
                    if best_funding is not None
                    else None
                ),
                "age_min": min(
                    (row.age_min for row in token_rows if row.age_min is not None),
                    default=None,
                ),
                "routes": routes,
            }
        )
    return output


def _group_sort_value(group: dict[str, Any], sort_by: str) -> Any:
    routes = group.get("routes") or []
    if not routes:
        return -999999.0
    if sort_by == "token":
        return str(group.get("token") or "")
    if sort_by in {"funding", "funding_abs"}:
        value = _float_or_none(group.get("best_funding_24h_pct"))
        if sort_by == "funding":
            return value if value is not None else -999999.0
        return abs(value or 0.0)
    if sort_by == "age":
        return _float_or_none(group.get("age_min")) or 999999999.0
    if sort_by == "depth":
        return max((_float_or_none(row.get("depth_usd")) or 0.0 for row in routes), default=0.0)
    if sort_by == "edge":
        return _float_or_none(group.get("best_edge_pct")) or 0.0
    return _row_sort_key_dict(group.get("best_route") or {})


def _row_sort_key_dict(row: dict[str, Any]) -> tuple[float, float, float, float]:
    spread = max(0.0, _entrance_spread_dict(row))
    funding = max(0.0, _float_or_none(row.get("funding_apr_pct")) or 0.0) / 10.0
    depth = min(_float_or_none(row.get("depth_usd")) or 0.0, 250_000.0) / 10_000.0
    return (spread + funding + depth, funding, spread, -(_float_or_none(row.get("age_min")) or 999999.0))


def _route_dict_sort_value(row: dict[str, Any], sort_by: str) -> Any:
    if sort_by == "edge":
        return _entrance_spread_dict(row)
    if sort_by == "funding":
        funding = _effective_funding_24h_dict(row)
        return funding if funding is not None else -999999.0
    if sort_by == "funding_abs":
        funding = _effective_funding_24h_dict(row)
        return abs(funding) if funding is not None else -999999.0
    if sort_by == "depth":
        return _float_or_none(row.get("depth_usd")) or 0.0
    if sort_by == "age":
        return _float_or_none(row.get("age_min")) or 999999999.0
    if sort_by == "token":
        return str(row.get("token") or "")
    return _row_sort_key_dict(row)


def _dedupe_rows(rows: list[SpreadTerminalRow]) -> list[SpreadTerminalRow]:
    selected: dict[str, SpreadTerminalRow] = {}
    for row in rows:
        current = selected.get(row.route_key)
        if current is None or (row.quote_ts_us or 0) > (current.quote_ts_us or 0):
            selected[row.route_key] = row
    return list(selected.values())


def _row_sort_key(row: SpreadTerminalRow) -> tuple[float, float, float, float]:
    fresh_bonus = 10_000.0 if row.freshness == "fresh" else 0.0
    spread = max(0.0, _entrance_spread(row))
    funding = max(0.0, _float_or_none(row.funding_apr_pct) or 0.0) / 10.0
    depth = min(_float_or_none(row.depth_usd) or 0.0, 250_000.0) / 10_000.0
    return (fresh_bonus + spread + funding + depth, funding, spread, -(row.age_min or 999999.0))


# Funding rates are quoted per settlement interval, and those intervals differ
# between venues (1h, 4h, 8h). Subtracting a 4-hourly rate from a 1-hourly one
# is subtracting different units: AIXBT showed a 12.28% "funding spread" and a
# 4482% APR purely from a 4h leg differenced against a 1h leg. Both sides must
# be converted to the same per-day basis first.
def _per_day(rate_pct: float | None, interval_hours: float | None) -> float | None:
    rate = _float_or_none(rate_pct)
    if rate is None:
        return None
    hours = _float_or_none(interval_hours)
    if not hours or hours <= 0:
        hours = 8.0  # exchanges default to 8h when they do not publish one
    return rate * (24.0 / hours)


def funding_intervals_known(row: "SpreadTerminalRow") -> bool:
    """Every leg that pays funding published its interval, so the conversion is sound.

    With an unknown interval the 8h fallback can be wrong by 8x, which is how
    AGLD reached a 3570% APR. Such routes still display, but must not be ranked.

    A leg that publishes no funding rate at all is a spot leg: it pays nothing,
    which is known, not unknown. Demanding an interval from it excluded every
    SPOT-FUTURES / FUTURES-SPOT / DEX-FUTURES row -- i.e. the classic funding
    farm -- from the ranking, which is why the top-funding list was entirely
    futures-futures while the reference product's is mostly spot-futures.
    """
    for side in ("long", "short"):
        if _float_or_none(getattr(row, f"{side}_funding_pct", None)) is None:
            continue
        if not _float_or_none(getattr(row, f"{side}_funding_interval_hours", None)):
            return False
    return True


def normalised_funding(row: "SpreadTerminalRow") -> tuple[float | None, float | None]:
    """Net carry as (percent per day, APR percent), both legs on a common basis.

    A delta-neutral position pays funding on the long leg and receives it on the
    short leg, so the net is short-minus-long. For routes that would require
    selling spot you do not own, the executable trade is the mirror image (hold
    spot long, short the futures), so the carry flips sign with it.

    A settled 24h sum is a measurement; annualising one instantaneous print is a
    forecast, so the measurement wins whenever we have it. Kraken Futures settles
    HOURLY and caps at +/-0.5%/h: AGLD's current print extrapolated to 9.78%/day
    (3570% APR) while the 24 rates it actually paid summed to 5.66%/day.
    """
    net_daily = _float_or_none(getattr(row, "funding_24h_pct", None))
    if net_daily is None:
        long_daily = _per_day(
            getattr(row, "long_funding_pct", None),
            getattr(row, "long_funding_interval_hours", None),
        )
        short_daily = _per_day(
            getattr(row, "short_funding_pct", None),
            getattr(row, "short_funding_interval_hours", None),
        )
        if long_daily is not None or short_daily is not None:
            net_daily = (short_daily or 0.0) - (long_daily or 0.0)
        else:
            # Fast-quote and board rows carry only a pre-computed projection.
            net_daily = _float_or_none(getattr(row, "funding_projected_24h_pct", None))
    if net_daily is None:
        return None, None
    if requires_existing_spot_inventory(row):
        net_daily = -net_daily
    return net_daily, net_daily * 365.0


def _public_row(row: SpreadTerminalRow) -> dict[str, Any]:
    payload = row.to_dict()
    payload["deliverable"] = route_deliverable(row)
    payload["requires_transfer"] = getattr(row, "route_kind", None) in TRANSFER_ROUTE_KINDS
    payload["identity_mismatch"] = price_ratio_implausible(row)
    payload["thin_book"] = leg_volume_too_thin(row)
    payload["funding_intervals_known"] = funding_intervals_known(row)
    # A route built from ticker quotes has a top-of-book "depth" nobody measured.
    # Showing that number unqualified is the same lie as showing a shut rail as
    # an opportunity, so the UI must be able to say which is which.
    payload["depth_unverified"] = any(
        str(item) == "depth_unverified" for item in row.blockers
    )
    payload["requires_spot_inventory"] = requires_existing_spot_inventory(row)
    daily, apr = normalised_funding(row)
    if daily is not None:
        payload["funding_daily_pct"] = daily
        payload["funding_spread_pct"] = daily
        payload["funding_apr_pct"] = apr
    payload["executable_direction"] = (
        "hold spot long, short futures"
        if requires_existing_spot_inventory(row)
        else "long the buy leg, short the sell leg"
    )
    payload["conditions"] = [
        item.removeprefix("mirage_guard:").removeprefix("condition:")
        for item in row.blockers
        if item.startswith("condition:")
        or item.removeprefix("mirage_guard:") in {"spot_sell_inventory_required"}
    ]
    for key in (
        "blockers",
        "executor_status",
        "next_action",
        "raw_source_kind",
        "source_group",
        "source_label",
        "source_name",
        "status",
        "validation_state",
    ):
        payload.pop(key, None)
    return payload


def _normalize_sort(value: str | None) -> str:
    normalized = str(value or "edge").casefold().strip()
    return normalized if normalized in {"rank", "edge", "funding", "funding_abs", "depth", "age", "token"} else "rank"


def _sort_value(row: SpreadTerminalRow, sort_by: str) -> Any:
    if sort_by == "edge":
        return _entrance_spread(row)
    if sort_by == "funding":
        funding = _effective_funding_24h(row)
        return funding if funding is not None else -999999.0
    if sort_by == "funding_abs":
        funding = _effective_funding_24h(row)
        return abs(funding) if funding is not None else -999999.0
    if sort_by == "depth":
        return _float_or_none(row.depth_usd) or 0.0
    if sort_by == "age":
        return row.age_min if row.age_min is not None else 999999999.0
    if sort_by == "token":
        return row.token
    return _row_sort_key(row)


def _entrance_spread(row: SpreadTerminalRow) -> float:
    open_spread = _float_or_none(row.displayed_open_spread_pct)
    if open_spread is not None:
        return open_spread
    executable = _float_or_none(row.executable_spread_pct)
    if executable is not None:
        return executable
    depth_spread = _float_or_none(row.depth_weighted_spread_pct)
    if depth_spread is not None:
        return depth_spread
    return -999999.0


def _entrance_spread_dict(row: dict[str, Any]) -> float:
    open_spread = _float_or_none(row.get("displayed_open_spread_pct"))
    if open_spread is not None:
        return open_spread
    executable = _float_or_none(row.get("executable_spread_pct"))
    if executable is not None:
        return executable
    depth_spread = _float_or_none(row.get("depth_weighted_spread_pct"))
    if depth_spread is not None:
        return depth_spread
    return -999999.0


# Selection, sorting and display must all read the SAME number. The raw
# funding_24h_pct fields carry the route as printed, without the spot-inventory
# direction flip that normalised_funding applies, so a FUTURES-SPOT row used to
# publish best_funding_apr_pct and best_funding_24h_pct with opposite signs.
def _effective_funding_24h(row: SpreadTerminalRow) -> float | None:
    return normalised_funding(row)[0]


def _effective_funding_24h_dict(row: dict[str, Any]) -> float | None:
    # _public_row already writes the normalised per-day carry here.
    normalised = _float_or_none(row.get("funding_daily_pct"))
    if normalised is not None:
        return normalised
    settled = _float_or_none(row.get("funding_24h_pct"))
    return settled if settled is not None else _float_or_none(row.get("funding_projected_24h_pct"))


def _exchange_counts(rows: list[SpreadTerminalRow]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for venue in {row.long_venue, row.short_venue}:
            if venue:
                counts[venue] += 1
    return dict(sorted(counts.items()))


def _exchange_options(rows: list[SpreadTerminalRow]) -> list[str]:
    venues = set()
    for row in rows:
        if row.long_venue:
            venues.add(row.long_venue)
        if row.short_venue:
            venues.add(row.short_venue)
    return sorted(venues)


def _route_kind(
    *,
    long_venue: str | None,
    long_market_type: str | None,
    short_venue: str | None,
    short_market_type: str | None,
    source_kind: str | None,
) -> str:
    venues = f"{long_venue or ''} {short_venue or ''}".casefold()
    market_types = {
        str(long_market_type or "").casefold(),
        str(short_market_type or "").casefold(),
    }
    is_dex = "dex" in market_types or any(
        token in venues for token in ("jupiter", "zerox", "0x", "okx dex")
    )
    if is_dex:
        if "futures" in market_types:
            return "DEX-FUTURES"
        return "DEX-SPOT"
    if long_market_type == "Futures" and short_market_type == "Futures":
        return "FUTURES"
    if long_market_type == "Spot" and short_market_type == "Futures":
        return "SPOT-FUTURES"
    if long_market_type == "Futures" and short_market_type == "Spot":
        return "FUTURES-SPOT"
    if long_market_type == "Spot" and short_market_type == "Spot":
        return "SPOT"
    return "UNKNOWN"


def _reconciliation_summary(
    api_rows: list[SpreadTerminalRow],
    website_rows: list[SpreadTerminalRow],
    website_meta: dict[str, Any],
) -> dict[str, Any]:
    """Compare sources without allowing website data into product rows."""

    api_routes = {_route_fingerprint(row): row for row in api_rows}
    website_routes = {_route_fingerprint(row): row for row in website_rows}
    matched = sorted(set(api_routes) & set(website_routes))
    drifts: list[float] = []
    for key in matched:
        api_spread = _float_or_none(api_routes[key].executable_spread_pct)
        website_spread = _float_or_none(website_routes[key].executable_spread_pct)
        if api_spread is not None and website_spread is not None:
            drifts.append(abs(api_spread - website_spread))
    api_tokens = {row.token for row in api_rows}
    website_tokens = {row.token for row in website_rows}
    return {
        "status": "checked" if website_rows else "unavailable",
        "website_age_min": website_meta.get("age_min"),
        "api_route_count": len(api_routes),
        "website_route_count": len(website_routes),
        "matched_route_count": len(matched),
        "matched_token_count": len(api_tokens & website_tokens),
        "api_only_token_count": len(api_tokens - website_tokens),
        "website_only_token_count": len(website_tokens - api_tokens),
        "median_spread_drift_pct_points": _median(drifts),
        "display_source": "public_api_only",
    }


def _route_fingerprint(row: SpreadTerminalRow) -> tuple[str, str, str, str, str]:
    return (
        row.token,
        str(row.long_venue or "").casefold(),
        str(row.long_market_type or "").casefold(),
        str(row.short_venue or "").casefold(),
        str(row.short_market_type or "").casefold(),
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _status_for_row(
    *,
    freshness: str,
    executor_status: str | None,
    blockers: list[str],
    validation_state: str | None,
) -> str:
    if freshness == "stale":
        return "stale"
    if executor_status in {"ready", "executor_ready"}:
        return "executor_ready"
    if blockers:
        return "setup_needed"
    if validation_state in {"quote_verified", "ok", "matched_board"}:
        return "watch_only"
    return "research"


def _next_action(status: str, blockers: list[str], *, source_group: str) -> str:
    if status == "stale":
        return "refresh source before ranking this route"
    if source_group == "api_discovery":
        return "verify identity, funding, private state, and executor coverage"
    if source_group == "dex_discovery":
        return "prove exact chain/contract and DEX route feasibility"
    if blockers:
        return "inspect blockers before any trade decision"
    return "open route detail and compare funding/depth"


def _depth_from_api(raw: dict[str, Any]) -> float | None:
    notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
    screen = notes.get("screen") if isinstance(notes.get("screen"), dict) else {}
    return _float_or_none(screen.get("liquidity_usd"))


def _nested_float(value: Any, section: str, key: str) -> float | None:
    section_value = value.get(section) if isinstance(value, dict) else None
    if not isinstance(section_value, dict):
        return None
    return _float_or_none(section_value.get(key))


def _freshness(age_min: float | None, max_age_min: float | None) -> str:
    if age_min is None:
        return "unknown"
    if max_age_min is not None and age_min > max_age_min:
        return "stale"
    return "fresh"


def _age_min(now: float, ts_us: int | None) -> float | None:
    if ts_us is None:
        return None
    return max(0.0, (now - (ts_us / 1_000_000.0)) / 60.0)


def _iso_age_min(value: str | None, *, now: float) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed.timestamp()) / 60.0)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _float_or_none(*values: Any) -> float | None:
    # Called 1.6M times per snapshot rebuild, almost always through a coalescing
    # chain whose leading arguments are None. Raising and catching TypeError for
    # each of those cost 8.6s a rebuild on the droplet; None and float are the
    # two overwhelmingly common cases, so test them before reaching for float().
    for value in values:
        if value is None:
            continue
        if type(value) is float:
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
