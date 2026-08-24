"""Canonical public-API spread rows grouped into token-level market views."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import gc
import json
import logging
import os
import re
import statistics
from threading import Lock
import time
from typing import Any

from spreadboard import (
    board,
    exchange_links,
    market_events,
    probe_notional,
    public_rails,
    route_taxonomy,
    token_metadata,
    tokenized_assets,
    venue_funding_history,
)
from spreadarb.api_discovery.identity import WatchAsset, load_watchlist

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger("spreadboard.api_spreads")

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
    funding_age_min: float | None = None
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
    long_quote: str | None = None
    short_quote: str | None = None
    market_cap_usd: float | None = None
    fdv_usd: float | None = None
    metadata_volume_24h_usd: float | None = None
    listing_age_days: float | None = None
    listing_age_source: str | None = None
    asset_class: str = "crypto"
    liquidity_evidence_kind: str | None = None
    matched_size_notional_usd: float | None = None
    gas_adjusted_spread_pct: float | None = None
    dex_gas_estimate_usd: float | None = None
    dex_slippage_bps: float | None = None
    dex_price_impact_pct: float | None = None
    dex_quote_source: str | None = None
    dex_bid_vwap: float | None = None
    dex_ask_vwap: float | None = None
    dex_quote_ts_us: int | None = None
    dex_mev_protection: str | None = None
    dex_transfer_time_seconds: float | None = None
    dex_route_plan: tuple[str, ...] = ()

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
    quote: str | None = None,
    min_volume_24h_usd: float | None = None,
    min_market_cap_usd: float | None = None,
    max_market_cap_usd: float | None = None,
    min_fdv_usd: float | None = None,
    max_fdv_usd: float | None = None,
    max_listing_age_days: float | None = None,
    persistence: str | None = None,
    asset_class: str | None = None,
    funding_only: bool = False,
    include_stale: bool = False,
    require_deliverable: bool = False,
    # A route we cannot depth-verify was hidden outright, so UNITREE carried
    # 24 real routes -- Gate->Hyperliquid at 4.40%, Mexc->Ourbit at 2.65% --
    # and displayed 0.000%. Ourbit and Mexc futures tickers publish no
    # top-of-book size, so nothing CAN verify them and they could never appear.
    # Hiding a dislocation is only defensible if the alternative is presenting
    # it as executable; it is not, because the Depth cell states "unverified"
    # per row. Mirage-guarded and price-implausible routes stay hidden: those
    # are known-false, not merely unmeasured.
    include_unverified: bool = True,
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
        min_abs_funding_24h_pct, min_abs_funding_apr_pct, quote, min_volume_24h_usd,
        min_market_cap_usd, max_market_cap_usd, min_fdv_usd, max_fdv_usd,
        max_listing_age_days, persistence, asset_class, funding_only, include_stale,
        include_unverified, require_deliverable, max_age_min, sort_by, direction, offset, limit,
    )
    try:
        delta_path = _fast_quote_delta_path(api_path)
        stamp = (
            api_path.stat().st_mtime_ns,
            _mtime_ns(delta_path),
            token_metadata.DEFAULT_CACHE_PATH.stat().st_mtime_ns
            if token_metadata.DEFAULT_CACHE_PATH.exists()
            else 0,
            venue_funding_history.DEFAULT_CACHE_PATH.stat().st_mtime_ns
            if venue_funding_history.DEFAULT_CACHE_PATH.exists()
            else 0,
            market_events.DEFAULT_CACHE_PATH.stat().st_mtime_ns
            if market_events.DEFAULT_CACHE_PATH.exists()
            else 0,
            tokenized_assets.DEFAULT_REGISTRY_PATH.stat().st_mtime_ns
            if tokenized_assets.DEFAULT_REGISTRY_PATH.exists()
            else 0,
        )
    except OSError:
        stamp = (0, 0, 0, 0, 0, 0)
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
    live_books = _live_books()
    api_rows = apply_live_books(api_rows, live_books, now=current_time)
    if _normalize_kind_filter(kind) == "DEX-FUTURES":
        api_rows = _expand_current_dex_futures_pairs(
            api_rows,
            books=live_books,
            now=current_time,
            metadata=metadata,
            rails=rails,
        )
    all_rows = _dedupe_rows(api_rows)
    # Product DEX diagnostics are OKX-only. Source provenance is intentionally
    # not counted here because several ordinary Futures/Spot venues still use
    # legacy dex_* discovery buckets.
    dex_raw_kind_counts = dict(
        sorted(
            Counter(
                row.route_kind
                for row in all_rows
                if row.route_kind.startswith("DEX-")
            ).items()
        )
    )
    all_rows = [
        row
        for row in all_rows
        if row.route_kind not in RETIRED_ROUTE_KINDS
        and not quote_basis_mismatch(row)
    ]
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
        if route_deliverable(row) is not False
        and not _is_mirage_guarded(row)
        and spread_leader_ready(row)
        and tokenized_route_rankable(row)
    ]
    # A funding farm holds both legs -- long spot on one venue, short futures on
    # another -- and never moves the coin between them, so transfer rails are
    # irrelevant to whether the carry is collectable. Only quote trustworthiness
    # matters: the two legs must be the same asset, on books deep enough to price.
    rankable_funding_universe = [
        row
        for row in public_universe
        if funding_intervals_known(row)
        and not _is_mirage_guarded(row)
        and not price_ratio_implausible(row)
        and not leg_volume_too_thin(row)
        and not is_venue_specific_leveraged_token(row)
        and not is_non_perpetual_or_inverse(row)
        and tokenized_route_rankable(row)
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
        quote=quote,
        min_volume_24h_usd=min_volume_24h_usd,
        min_market_cap_usd=min_market_cap_usd,
        max_market_cap_usd=max_market_cap_usd,
        min_fdv_usd=min_fdv_usd,
        max_fdv_usd=max_fdv_usd,
        max_listing_age_days=max_listing_age_days,
        persistence=persistence,
        asset_class=asset_class,
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
    attach_funding_history(visible_groups)
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
            "quote": quote,
            "min_volume_24h_usd": min_volume_24h_usd,
            "min_market_cap_usd": min_market_cap_usd,
            "max_market_cap_usd": max_market_cap_usd,
            "min_fdv_usd": min_fdv_usd,
            "max_fdv_usd": max_fdv_usd,
            "max_listing_age_days": max_listing_age_days,
            "persistence": persistence,
            "asset_class": asset_class,
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
        "asset_class_counts": dict(Counter(row.asset_class for row in public_universe)),
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
    if (
        _is_mirage_guarded(row)
        or price_ratio_implausible(row)
        or leg_volume_too_thin(row)
        or not tokenized_route_rankable(row)
    ):
        return False
    if getattr(row, "route_kind", None) in TRANSFER_ROUTE_KINDS:
        return route_deliverable(row) is not False
    return True


def lane_current_ready(row: "SpreadTerminalRow") -> bool:
    """Whether a route can honestly count toward the live-ready lane promise.

    ``lane_rankable`` answers the slower structural question (identity,
    liquidity and rails) and remains useful for the complete pair catalogue.
    A health badge labelled "Top 25 ready" is a current-data promise, so it
    additionally requires the same matched-quote age boundary as spread
    leaders.
    """

    return lane_rankable(row) and spread_leader_ready(row)


def tokenized_route_rankable(row: "SpreadTerminalRow") -> bool:
    if getattr(row, "asset_class", "crypto") != "tokenized":
        return True
    return tokenized_assets.classify(row.to_dict()).get("status") == "verified"


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
        if not lane_current_ready(row):
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
_ROW_CACHE: dict[
    tuple[Any, ...],
    tuple[float, list["SpreadTerminalRow"], dict[str, Any]],
] = {}
# Every input that can change a materialised row is part of the cache key. A
# five-second TTL therefore only forced the same 40-80 MB snapshot to be parsed
# once per warmed view; it added CPU load without improving freshness.
_ROW_CACHE_TTL_SECONDS = float(os.environ.get("SPREADBOARD_ROW_CACHE_SECONDS", "900"))
_RESULT_CACHE: dict[tuple[Any, ...], tuple[Any, float, dict[str, Any]]] = {}
_RESULT_CACHE_TTL_SECONDS = float(os.environ.get("SPREADBOARD_RESULT_CACHE_SECONDS", "90"))
_RESULT_CACHE_MAX_ENTRIES = int(os.environ.get("SPREADBOARD_RESULT_CACHE_ENTRIES", "4"))
# Shortlist a few more tokens than we display so lane filtering inside the
# grouping cannot leave the headline short.
TOP_GROUP_SHORTLIST = 24


#: Maximum age of a matched-size price allowed to lead a *spread* ranking.
#:
#: The wider five-minute row window keeps a temporarily disconnected route
#: searchable and lets its funding/history stay useful.  It is much too wide
#: for a headline that claims an executable basis right now: KOMA and GUA both
#: remained positive leaders for 2-3 minutes after direct $50 requotes showed
#: the basis had converged or reversed.  Current funding is ranked separately,
#: so withholding an old spread never hides its carry evidence.
SPREAD_LEADER_MAX_AGE_MIN = max(
    0.25,
    float(os.environ.get("SPREADBOARD_SPREAD_LEADER_MAX_AGE_SECONDS", "90")) / 60.0,
)
# The read window must cover the same interval that a downstream row may call
# current. Production previously read only ten seconds of a complete 55-second
# bulk generation, so 19k current books existed while every initial page and
# health lane appeared empty. This does not relax truth: apply_live_books keeps
# the older leg timestamp and every leader rechecks SPREAD_LEADER_MAX_AGE_MIN.
LIVE_BOOK_MAX_AGE_SECONDS = max(
    SPREAD_LEADER_MAX_AGE_MIN * 60.0,
    float(os.environ.get("SPREADBOARD_LIVE_BOOK_AGE_SECONDS", "90")),
)
LIVE_BOOK_TARGET_NOTIONAL_USD = probe_notional.TARGET_NOTIONAL_USD


def quote_age_min(row: Any, *, now: float | None = None) -> float | None:
    """Current age for either a row object or a serialized route mapping.

    Serialized pages can remain warm for minutes, so their stored ``age_min``
    is only an observation from build time. Prefer the absolute quote timestamp
    whenever it exists and recompute age at the moment of use.
    """

    getter = (
        row.get
        if isinstance(row, dict)
        else lambda key, default=None: getattr(row, key, default)
    )
    quote_ts_us = _float_or_none(getter("quote_ts_us"))
    if quote_ts_us is not None and quote_ts_us > 0:
        return (
            (time.time() if now is None else float(now))
            - quote_ts_us / 1_000_000.0
        ) / 60.0
    return _float_or_none(getter("age_min"))


def spread_quote_current(
    row: Any,
    *,
    max_age_min: float = SPREAD_LEADER_MAX_AGE_MIN,
    now: float | None = None,
) -> bool:
    """Whether both legs' matched quote is recent enough to call current."""

    age = quote_age_min(row, now=now)
    return age is not None and 0.0 <= age <= max_age_min


def matched_probe_verified(row: Any) -> bool:
    """Whether ``row`` proves the product's complete matched-size probe.

    ``depth_weighted_spread_pct`` survived several historical probe-size
    changes, so the number alone is not evidence that today's $500 target was
    filled. API-discovery rows carry ``matched_size_notional_usd``; warm
    catalogue rows carry ``target_notional_usd``; and rows rebuilt directly
    from resident books carry ``live_book`` plus ``depth_usd``. One of those
    explicit proofs must reach the current target and the route must not be
    labelled depth-unverified.
    """

    getter = (
        row.get
        if isinstance(row, dict)
        else lambda key, default=None: getattr(row, key, default)
    )
    if _float_or_none(getter("depth_weighted_spread_pct")) is None:
        return False
    blockers = getter("blockers", []) or []
    if bool(getter("depth_unverified")) or "depth_unverified" in blockers:
        return False
    proofs = [
        _float_or_none(getter("matched_size_notional_usd")),
        _float_or_none(getter("target_notional_usd")),
    ]
    if bool(getter("live_book")) or bool(getter("catalog_pair")):
        proofs.append(_float_or_none(getter("depth_usd")))
    return max((value or 0.0 for value in proofs), default=0.0) >= (
        LIVE_BOOK_TARGET_NOTIONAL_USD
    )


def _live_books() -> dict[str, Any]:
    now_us = int(time.time() * 1_000_000)
    cutoff_us = now_us - int(LIVE_BOOK_MAX_AGE_SECONDS * 1_000_000)

    def still_current(book: Any) -> bool:
        try:
            return int(book.quote_ts_us) >= cutoff_us
        except (AttributeError, TypeError, ValueError):
            return False

    try:
        from spreadboard import live_book_cache

        if not live_book_cache.DEFAULT_PATH.exists():
            raise FileNotFoundError(live_book_cache.DEFAULT_PATH)
        store = live_book_cache.LiveBookStore()
        try:
            current = store.load_all(max_age_seconds=LIVE_BOOK_MAX_AGE_SECONDS)
        finally:
            store.close()
        # A venue-sized writer transaction can briefly leave a reader with an
        # incomplete generation. Merge only still-current books from the last
        # successful read; their own quote timestamps keep this fail-safe
        # inside the exact same 90-second truth boundary as a normal read.
        with _LIVE_BOOK_FALLBACK_LOCK:
            fallback = {
                key: book
                for key, book in _LAST_GOOD_LIVE_BOOKS.items()
                if still_current(book)
            }
            merged = {**fallback, **current}
            _LAST_GOOD_LIVE_BOOKS.clear()
            _LAST_GOOD_LIVE_BOOKS.update(merged)
        return merged
    except Exception:  # noqa: BLE001 - a missing feed must not take the board down.
        # Do not turn a transient SQLite handoff into an empty Markets page.
        # This is bounded by each book's real quote timestamp, never by when it
        # happened to be cached in this process.
        with _LIVE_BOOK_FALLBACK_LOCK:
            fallback = {
                key: book
                for key, book in _LAST_GOOD_LIVE_BOOKS.items()
                if still_current(book)
            }
            _LAST_GOOD_LIVE_BOOKS.clear()
            _LAST_GOOD_LIVE_BOOKS.update(fallback)
            return dict(fallback)


_LIVE_BOOK_FALLBACK_LOCK = Lock()
_LAST_GOOD_LIVE_BOOKS: dict[str, Any] = {}


def _book_side(book: Any, side: str) -> tuple[float | None, float | None]:
    """Top of book and a *complete* VWAP at the standard probe size.

    A one-level bulk ticker is still a useful current bid/ask, but a missing
    size must never be promoted to verified matched depth.  Callers therefore
    get ``None`` when the ladder cannot actually fill the full probe.
    """
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
    return top, vwap


def _route_has_dex_leg(route: Any) -> bool:
    """Whether one side cannot have a centralised-exchange websocket book."""

    return any(
        str(
            route.get(f"{side}_market_type")
            if isinstance(route, dict)
            else getattr(route, f"{side}_market_type", "")
        ).casefold()
        == "dex"
        for side in ("long", "short")
    )


def _stream_funding_daily(
    route: dict[str, Any], legs: dict[str, dict[str, Any]]
) -> float | None:
    """Current net daily carry from the latest bulk funding sweep.

    The grouped market payload intentionally has a long cache lifetime. Funding
    is a separate, much smaller file and changes every sweep, so the push path
    overlays it directly instead of leaving the visible rate frozen until a
    structural board rebuild.
    """

    daily: dict[str, float] = {}
    saw_live_leg = False
    for side in ("long", "short"):
        if str(route.get(f"{side}_market_type") or "") != "Futures":
            daily[side] = 0.0
            continue
        symbol = str(route.get(f"{side}_market_symbol") or "")
        entry = legs.get(f"{route.get(f'{side}_venue')}|{symbol}") if symbol else None
        if entry:
            rate = _float_or_none(entry.get("rate_pct"))
            interval = _float_or_none(entry.get("interval_hours"))
            saw_live_leg = True
        else:
            return None
        value = _per_day(rate, interval)
        if value is None:
            return None
        daily[side] = value
    if not saw_live_leg:
        return None
    return daily["short"] - daily["long"]


_FAST_ROUTE_UPDATE_LOCK = Lock()
_FAST_ROUTE_UPDATE_CACHE: dict[str, Any] = {
    "key": None,
    "exact": {},
    "simple": {},
}


def _fast_route_identity(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("token") or "").upper(),
        str(row.get("long_venue") or ""),
        str(row.get("long_market_type") or ""),
        str(row.get("long_market_symbol") or ""),
        str(row.get("short_venue") or ""),
        str(row.get("short_market_type") or ""),
        str(row.get("short_market_symbol") or ""),
    )


def _fast_quote_updates_for(
    routes: list[dict[str, Any]],
) -> dict[str, tuple[Any, ...]]:
    """Current compact-worker quotes for exact routes outside resident books.

    The expanded catalogue is deliberately broader than the websocket set.
    Without this second current source, those extra pairs retained whatever
    matched spread their structural page generation happened to carry.
    """
    if not routes:
        return {}
    path = _fast_quote_delta_path(DEFAULT_API_DISCOVERY_PATH)
    key = (str(path), _mtime_ns(path))
    with _FAST_ROUTE_UPDATE_LOCK:
        if _FAST_ROUTE_UPDATE_CACHE.get("key") == key:
            exact = _FAST_ROUTE_UPDATE_CACHE["exact"]
            simple = _FAST_ROUTE_UPDATE_CACHE["simple"]
        else:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            exact: dict[tuple[str, ...], tuple[Any, ...]] = {}
            simple_candidates: dict[tuple[str, ...], list[tuple[Any, ...]]] = {}
            now = time.time()
            for item in payload.get("rows") or []:
                if not isinstance(item, dict):
                    continue
                raw = _project_route_funding(_mirror_if_spot_sale_required(item))
                notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
                inputs = (
                    notes.get("route_inputs")
                    if isinstance(notes.get("route_inputs"), dict)
                    else {}
                )
                long_input = inputs.get("long") if isinstance(inputs.get("long"), dict) else {}
                short_input = (
                    inputs.get("short") if isinstance(inputs.get("short"), dict) else {}
                )
                identity = (
                    str(raw.get("token") or "").upper(),
                    str(raw.get("long_venue") or ""),
                    str(raw.get("long_market_type") or ""),
                    str(long_input.get("symbol") or ""),
                    str(raw.get("short_venue") or ""),
                    str(raw.get("short_market_type") or ""),
                    str(short_input.get("symbol") or ""),
                )
                quote_ts_us = _int_or_none(raw.get("quote_ts_us"))
                if quote_ts_us is None:
                    continue
                age_seconds = max(0.0, now - quote_ts_us / 1_000_000)
                if age_seconds > LIVE_BOOK_MAX_AGE_SECONDS:
                    continue
                matched = _float_or_none(raw.get("depth_weighted_spread_pct"))
                top = _float_or_none(raw.get("executable_spread_pct"))
                spread = matched if matched is not None else top
                update = (
                    spread,
                    _float_or_none(raw.get("funding_daily_pct")),
                    quote_ts_us,
                    "matched_vwap" if matched is not None else "top_book" if top is not None else None,
                )
                current = exact.get(identity)
                if current is None or int(current[2] or 0) <= quote_ts_us:
                    exact[identity] = update
                simple_key = identity[:3] + identity[4:6]
                simple_candidates.setdefault(simple_key, []).append(update)
            simple = {
                candidate_key: updates[0]
                for candidate_key, updates in simple_candidates.items()
                if len(updates) == 1
            }
            _FAST_ROUTE_UPDATE_CACHE.update(
                {"key": key, "exact": exact, "simple": simple}
            )
    updates: dict[str, tuple[Any, ...]] = {}
    for route in routes:
        identity = _fast_route_identity(route)
        update = exact.get(identity)
        if update is None:
            update = simple.get(identity[:3] + identity[4:6])
        if update is not None and route.get("route_key"):
            updates[str(route["route_key"])] = update
    return updates


def live_route_updates_for(
    routes: list[dict[str, Any]],
    *,
    include_funding: bool = True,
    include_basis: bool = False,
) -> dict[str, tuple[Any, ...]]:
    """Spread, carry and quote time straight from the current book store.

    The push path must not go through the grouped board's cache: a cached price
    is exactly what the stream exists to correct.  Returning the quote time as
    well is important for cached API payloads: a newly matched pair of ladders
    can make an old structural route current again, but it must use the older
    of the two real book timestamps rather than pretending the HTTP request was
    the quote time.
    """
    if not routes:
        return {}
    from spreadboard import live_book_cache
    from spreadboard import bulk_quotes

    # A rendered page usually contains 25-100 routes. Loading and JSON-decoding
    # every ~20k resident book for those few keys made 7D/30D Rankings take
    # 9-11 seconds despite using a precomputed artifact. Read the exact keys in
    # one bounded SQLite query; the broad board build still uses load_all().
    wanted_keys = {
        live_book_cache.cache_key(
            str(route.get(f"{side}_venue") or ""),
            str(route.get(f"{side}_market_type") or ""),
            str(route.get(f"{side}_market_symbol") or ""),
        )
        for route in routes
        for side in ("long", "short")
        if route.get(f"{side}_venue") and route.get(f"{side}_market_symbol")
    }
    books = live_book_cache.load_live_books_by_keys(
        wanted_keys,
        max_age_seconds=LIVE_BOOK_MAX_AGE_SECONDS,
    )
    out: dict[str, tuple[Any, ...]] = {}
    funding_legs = bulk_quotes.load_funding() if include_funding else {}
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
        # Mixing one current CEX book with one older quote can manufacture a
        # spread that never existed. CEX routes therefore move only when both
        # books are live. A DEX leg has no websocket by definition, so those
        # routes may still reprice their one streamable CEX leg.
        price_is_live = not (long_book is None and short_book is None)
        if price_is_live and not _route_has_dex_leg(route):
            price_is_live = long_book is not None and short_book is not None
        # A current CEX book cannot renew an old on-chain quote. The DEX leg's
        # own timestamp remains the freshness boundary for the mixed route.
        if price_is_live and _route_has_dex_leg(route):
            price_is_live = spread_quote_current(route)
        funding_daily = _stream_funding_daily(route, funding_legs) if include_funding else None
        if not price_is_live:
            if funding_daily is not None:
                update = (None, funding_daily, None, None)
                out[str(route["route_key"])] = update if include_basis else update[:3]
            continue
        prior_depth_verified = matched_probe_verified(route)
        if long_book is not None:
            ask, ask_vwap = _book_side(long_book, "ask")
        else:
            ask = _float_or_none(route.get("long_ask")) or _float_or_none(route.get("long_price"))
            ask_vwap = ask if prior_depth_verified else None
        if short_book is not None:
            bid, bid_vwap = _book_side(short_book, "bid")
        else:
            bid = _float_or_none(route.get("short_bid")) or _float_or_none(route.get("short_price"))
            bid_vwap = bid if prior_depth_verified else None
        if not ask or not bid or ask <= 0:
            continue
        measured_depth_spread = (
            (bid_vwap / ask_vwap - 1.0) * 100.0
            if bid_vwap is not None and ask_vwap is not None and ask_vwap > 0
            else None
        )
        # A top-of-book-only fast leg is not allowed to erase a verified $500
        # route. It can update the top quote, but the UI must keep the last
        # timestamped depth result until a fresh ladder replaces it. Returning
        # None here was what made DEX cards and valid bulk routes disappear as
        # soon as the browser stream attached.
        live_depth_spread = measured_depth_spread
        spread_basis = "matched_vwap" if measured_depth_spread is not None else None
        if live_depth_spread is None and prior_depth_verified:
            live_depth_spread = _float_or_none(route.get("depth_weighted_spread_pct"))
            if live_depth_spread is not None:
                spread_basis = "retained_matched_vwap"
        if live_depth_spread is None:
            # The page renders `depth_weighted_spread_pct` when the probe can be
            # proven and `executable_spread_pct` when it cannot, and says which.
            # The feed carried only the first, so a route that could not prove
            # the size streamed None and the client wrote a dash over a number
            # the server had just rendered. Raising the probe to $500 made that
            # the normal case: every stored `depth_usd` was stamped 50.0, so
            # `prior_depth_verified` above went false almost everywhere.
            #
            # Two live sides always produce a real top-of-book edge. Sending it
            # keeps the feed at least as strong as the render it is correcting.
            live_depth_spread = (bid / ask - 1.0) * 100.0
            spread_basis = "top_book"
        if measured_depth_spread is None:
            # A top-only tick may retain a still-current prior $500 quote, but
            # it cannot renew that proof.  Preserve the quote's own timestamp.
            quote_ts_us = int(_float_or_none(route.get("quote_ts_us")) or 0) or None
        else:
            stamps = [
                int(book.quote_ts_us)
                for book in (long_book, short_book)
                if book is not None and getattr(book, "quote_ts_us", None)
            ]
            # The DEX leg does not stream.  Its last exact quote remains the
            # freshness boundary even while the CEX leg moves continuously.
            if _route_has_dex_leg(route):
                stored_ts = int(_float_or_none(route.get("quote_ts_us")) or 0)
                if stored_ts:
                    stamps.append(stored_ts)
            quote_ts_us = min(stamps) if stamps else None
        update = (
            live_depth_spread,
            funding_daily if include_funding else None,
            quote_ts_us,
            spread_basis,
        )
        out[str(route["route_key"])] = update if include_basis else update[:3]
    # A complete fast-quote cycle covers routes that are intentionally outside
    # the resident websocket set. Prefer a two-book live update when present;
    # otherwise use the exact current compact-worker route instead of retaining
    # an older structural/group value on screen.
    for route_key, fast in _fast_quote_updates_for(routes).items():
        existing = out.get(route_key)
        if existing is None:
            out[route_key] = fast if include_basis else fast[:3]
            continue
        existing_basis = existing[3] if include_basis and len(existing) > 3 else None
        fast_basis = fast[3] if len(fast) > 3 else None
        prefer_fast_spread = (
            include_basis
            and fast[0] is not None
            and fast_basis in {"matched_vwap", "retained_matched_vwap"}
            and existing_basis not in {"matched_vwap", "retained_matched_vwap"}
        )
        spread = (
            fast[0]
            if prefer_fast_spread
            else existing[0]
            if existing[0] is not None
            else fast[0]
        )
        funding = existing[1] if existing[1] is not None else fast[1]
        timestamp = (
            fast[2]
            if prefer_fast_spread
            else existing[2]
            if existing[0] is not None
            else fast[2]
        )
        basis = (
            fast_basis
            if prefer_fast_spread
            else existing_basis
            if existing[0] is not None
            else fast_basis
        )
        merged = (spread, funding, timestamp, basis)
        out[route_key] = merged if include_basis else merged[:3]
    if include_funding:
        # Funding has a dedicated exact-leg freshness cache. Never let a rate
        # embedded in the slower discovery/fast-price artefacts survive after
        # that leg has expired. Ensure every requested route receives an
        # explicit value (including None) so HTML, JSON and Telegram all clear
        # stale current carry together.
        for route in routes:
            route_key = str(route.get("route_key") or "")
            if not route_key:
                continue
            funding = _stream_funding_daily(route, funding_legs)
            existing = out.get(route_key)
            if existing is None:
                merged = (None, funding, None, None)
            else:
                basis = existing[3] if len(existing) > 3 else None
                merged = (existing[0], funding, existing[2], basis)
            out[route_key] = merged if include_basis else merged[:3]
    return out


def live_prices_for(
    routes: list[dict[str, Any]], *, include_funding: bool = True
) -> dict[str, tuple[float | None, float | None]]:
    """Compatibility view of :func:`live_route_updates_for` for UI streams."""

    return {
        route_key: (spread, funding)
        for route_key, (spread, funding, _quote_ts_us) in live_route_updates_for(
            routes, include_funding=include_funding
        ).items()
        if spread is not None or funding is not None
    }


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
        # Never rank a CEX route on a mixed-time pair of books. DEX routes are
        # the deliberate exception because their on-chain leg cannot stream.
        if long_book is None and short_book is None:
            updated.append(row)
            continue
        if not _route_has_dex_leg(row) and (long_book is None or short_book is None):
            updated.append(row)
            continue
        prior_depth_verified = (
            "depth_unverified" not in (row.blockers or [])
            and (_float_or_none(row.depth_usd) or 0.0) >= LIVE_BOOK_TARGET_NOTIONAL_USD
        )
        if long_book is not None:
            ask, ask_vwap = _book_side(long_book, "ask")
        else:
            ask = _float_or_none(row.long_ask) or _float_or_none(row.long_price)
            ask_vwap = ask if prior_depth_verified else None
        if short_book is not None:
            bid, bid_vwap = _book_side(short_book, "bid")
        else:
            bid = _float_or_none(row.short_bid) or _float_or_none(row.short_price)
            bid_vwap = bid if prior_depth_verified else None
        if not ask or not bid or ask <= 0 or bid <= 0:
            updated.append(row)
            continue
        executable = (bid / ask - 1.0) * 100.0
        depth_verified = bool(ask_vwap and bid_vwap and ask_vwap > 0 and bid_vwap > 0)
        depth = (
            (bid_vwap / ask_vwap - 1.0) * 100.0
            if depth_verified
            else None
        )
        blockers = [item for item in row.blockers if item != "depth_unverified"]
        if not depth_verified:
            blockers.append("depth_unverified")
        stamps = [
            int(book.quote_ts_us)
            for book in (long_book, short_book)
            if book is not None
        ]
        # DEX routes intentionally reprice the streamable CEX leg against a
        # cached on-chain quote. Preserve the missing leg's older timestamp;
        # otherwise each CEX tick made a never-refreshed DEX quote look new.
        if long_book is None or short_book is None:
            if row.quote_ts_us:
                stamps.append(int(row.quote_ts_us))
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
                depth_usd=LIVE_BOOK_TARGET_NOTIONAL_USD if depth_verified else None,
                blockers=list(dict.fromkeys(blockers)),
                quote_ts_us=quote_ts_us,
                age_min=age_min,
                freshness="fresh",
                status="live",
                live_book=True,
            )
        )
    return updated


def _expand_current_dex_futures_pairs(
    rows: list[SpreadTerminalRow],
    *,
    books: dict[str, Any],
    now: float,
    metadata: dict[str, dict[str, Any]],
    rails: dict[str, dict[str, Any]],
) -> list[SpreadTerminalRow]:
    """Fan each paid OKX quote out across every current futures catalogue leg."""

    if not books:
        return rows
    from spreadboard import catalog_pairs

    source_rows: list[dict[str, Any]] = []
    for row in rows:
        if row.route_kind != "DEX-FUTURES":
            continue
        source = row.to_dict()
        source["dex_expansion_verified"] = bool(
            lane_rankable(row)
            and matched_probe_verified(row)
            and spread_quote_current(
                {"quote_ts_us": row.dex_quote_ts_us or 0}
            )
        )
        source["tokenized_identity_verified"] = tokenized_route_rankable(row)
        source_rows.append(source)
    expanded = catalog_pairs.dex_futures_routes(
        source_rows,
        books=books,
        # The selected historical window is read from the exact shared venue
        # archive after grouping; do not perform per-route history work here.
        include_history=False,
    )
    if not expanded:
        return rows
    converted = [
        _row_from_catalog_dex_route(
            route,
            now=now,
            metadata=metadata,
            rails=rails,
        )
        for route in expanded
    ]
    # Scanner and catalogue keys use different serialisations for the same
    # economic legs. Keep every unique source row, then let the complete
    # catalogue replacement win for an identical pair because it proves the
    # canonical matched size and uses the freshest shared futures book.
    deduplicated: dict[tuple[Any, ...], SpreadTerminalRow] = {}
    for row in rows:
        deduplicated[catalog_pairs.route_identity(row.to_dict())] = row
    for row in converted:
        deduplicated[catalog_pairs.route_identity(row.to_dict())] = row
    return list(deduplicated.values())


def _row_from_catalog_dex_route(
    route: dict[str, Any],
    *,
    now: float,
    metadata: dict[str, dict[str, Any]],
    rails: dict[str, dict[str, Any]],
) -> SpreadTerminalRow:
    """Adapt a no-network catalogue expansion into the canonical row type."""

    raw = dict(route)
    route_inputs: dict[str, dict[str, Any]] = {}
    identities: dict[str, dict[str, Any]] = {}
    for side in ("long", "short"):
        route_inputs[side] = {
            "symbol": route.get(f"{side}_market_symbol"),
            "quote": route.get(f"{side}_quote"),
            "current_funding_pct": route.get(f"{side}_funding_pct"),
            "funding_interval_hours": route.get(f"{side}_funding_interval_hours"),
            "next_funding_ts_us": route.get(f"{side}_next_funding_ts_us"),
            "quote_notional_usd": route.get("matched_size_notional_usd"),
        }
        if route_taxonomy.leg_is_dex(
            venue=route.get(f"{side}_venue"),
            market_type=route.get(f"{side}_market_type"),
        ):
            identities[side] = {
                "chain_id": route.get("dex_chain"),
                "token_address": route.get("dex_contract"),
            }
            route_inputs[side].update(
                {
                    "gas_estimate_usd": route.get("dex_gas_estimate_usd"),
                    "slippage_bps": route.get("dex_slippage_bps"),
                    "price_impact_pct": route.get("dex_price_impact_pct"),
                    "quote_source": route.get("dex_quote_source"),
                    "mev_protection": route.get("dex_mev_protection"),
                    "transfer_time_seconds": route.get("dex_transfer_time_seconds"),
                    "route_plan": route.get("dex_route_plan") or (),
                    "quote_ts_us": route.get("dex_quote_ts_us"),
                    "bid_vwap": route.get(f"{side}_bid_vwap"),
                    "ask_vwap": route.get(f"{side}_ask_vwap"),
                }
            )
    raw["notes"] = {"route_inputs": route_inputs, "identity": identities}
    converted = _row_from_api(
        raw,
        bucket="dex_discovered_rows",
        now=now,
        metadata=metadata,
        rails=rails,
    )
    return replace(
        converted,
        route_key=str(route.get("route_key") or converted.route_key),
        route_kind="DEX-FUTURES",
        source_name=str(route.get("source_name") or "Complete OKX DEX pairs"),
        depth_usd=_float_or_none(route.get("depth_usd")),
        matched_size_notional_usd=_float_or_none(
            route.get("matched_size_notional_usd")
        ),
        gas_adjusted_spread_pct=_float_or_none(route.get("gas_adjusted_spread_pct")),
        live_book=True,
    )


def _fast_quote_delta_path(path: Path) -> Path:
    return Path(path).with_name("api_discovery_fast_quotes.json")


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


_FAST_REFRESH_META_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


def fast_quote_health(
    delta_path: Path | str | None = None, *, now: float | None = None
) -> dict[str, Any]:
    """Return the compact worker health block without loading the full board."""

    path = (
        Path(delta_path)
        if delta_path is not None
        else _fast_quote_delta_path(DEFAULT_API_DISCOVERY_PATH)
    )
    meta = _fast_quote_refresh_meta(path)
    if not meta:
        return {}
    result = dict(meta)
    age = _iso_age_min(_str_or_none(result.get("updated_at")), now=time.time() if now is None else now)
    result["age_min"] = age
    return result


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
            if route_taxonomy.source_is_dex(raw.get("source_kind"))
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
        snapshot_mtime = path.stat().st_mtime_ns
    except OSError as exc:
        return [], {
            "status": "missing",
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
    # The funding overlay is a third input and has to be keyed too. Without it a
    # sweep could refresh every rate on the board and the cached rows would keep
    # the old ones until the snapshot happened to move.
    from spreadboard import bulk_quotes

    cache_key = (
        str(path),
        snapshot_mtime,
        _mtime_ns(delta_path),
        _mtime_ns(Path(bulk_quotes.FUNDING_CACHE_PATH)),
        _mtime_ns(token_metadata.DEFAULT_CACHE_PATH),
        _mtime_ns(public_rails.DEFAULT_CACHE_PATH),
    )
    with _SNAPSHOT_CACHE_LOCK:
        cached_rows = _ROW_CACHE.get(cache_key)
    if cached_rows is not None and 0.0 <= now - cached_rows[0] < _ROW_CACHE_TTL_SECONDS:
        rows = cached_rows[1]
        snapshot_meta = cached_rows[2]
    else:
        try:
            payload = _cached_snapshot(path)
        except json.JSONDecodeError as exc:
            return [], {
                "status": "error",
                "path": str(path),
                "error": str(exc),
                "row_count": 0,
            }
        _propagate_funding_by_leg(payload)
        live_funding = bulk_quotes.load_funding()
        rows = [
            _row_from_api(
                raw,
                bucket=bucket,
                now=now,
                metadata=metadata or {},
                rails=rails or {},
                live_funding=live_funding,
            )
            for bucket in ("api_discovered_rows", "dex_discovered_rows")
            for raw in payload.get(bucket) or []
            if isinstance(raw, dict)
        ]
        rows = _apply_fast_quote_delta(
            rows, delta_path, now=now, metadata=metadata or {}, rails=rails or {}
        )
        snapshot_meta = {
            "updated_at": payload.get("updated_at"),
            "api_discovered_count": len(payload.get("api_discovered_rows") or []),
            "dex_discovered_count": len(payload.get("dex_discovered_rows") or []),
            "executor_ready_count": len(payload.get("executor_ready_rows") or []),
            "expires_at": payload.get("expires_at"),
            "worker_status": ((payload.get("source_refresh") or {}).get("status")),
            "dex_spot_source": _dex_spot_source_status(payload),
            "fast_quote_refresh": payload.get("fast_quote_refresh"),
        }
        with _SNAPSHOT_CACHE_LOCK:
            _ROW_CACHE.clear()
            _ROW_CACHE[cache_key] = (now, rows, snapshot_meta)
        # The tree is no longer referenced by anything but this frame.
        payload = {key: value for key, value in payload.items() if key not in _BULK_KEYS}
        gc.collect()
    updated_at = _str_or_none(snapshot_meta.get("updated_at"))
    discovery_age = _iso_age_min(updated_at, now=now)
    # Since the fast worker started writing a delta instead of rewriting the
    # snapshot, the snapshot's own `fast_quote_refresh` is frozen at whenever the
    # scan wrote it. The delta carries the current one, and it is what decides
    # whether the board reads as live: without this the page reports the age of
    # the last discovery scan and members are told the feed is reconnecting.
    fast_refresh = _fast_quote_refresh_meta(delta_path)
    if not fast_refresh:
        cached_refresh = snapshot_meta.get("fast_quote_refresh")
        fast_refresh = cached_refresh if isinstance(cached_refresh, dict) else {}
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
        "api_discovered_count": snapshot_meta.get("api_discovered_count"),
        "dex_discovered_count": snapshot_meta.get("dex_discovered_count"),
        "executor_ready_count": snapshot_meta.get("executor_ready_count"),
        "expires_at": snapshot_meta.get("expires_at"),
        "worker_status": snapshot_meta.get("worker_status"),
        "dex_spot_source": snapshot_meta.get("dex_spot_source"),
        "fast_quote_refresh": fast_refresh or snapshot_meta.get("fast_quote_refresh"),
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
    """Report the exact-identity OKX DEX provider separately."""

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


def _leg_symbol(raw: dict[str, Any], side: str) -> str | None:
    """The venue symbol for a leg, wherever the snapshot happens to keep it."""
    legs = (raw.get("notes") or {}).get("route_inputs") or {}
    leg = legs.get(side) if isinstance(legs, dict) else None
    for value in (
        (leg or {}).get("symbol") if isinstance(leg, dict) else None,
        raw.get(f"{side}_market_symbol"),
        raw.get(f"{side}_symbol"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return None


def _apply_live_funding(
    raw: dict[str, Any], legs: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Overlay the current funding rate the bulk sweep fetched for each leg.

    The rate a row carries otherwise comes from whenever the discovery scan or
    the rotating quote worker last reached that venue. Measured against the
    venues directly: 554 of 5,382 futures legs carried no rate at all and 424
    more disagreed, because the quote worker is a fresh process each cycle and
    covers about three venues of eighteen per pass.
    """
    explicit_legs = legs is not None
    if legs is None:
        from spreadboard import bulk_quotes

        legs = bulk_quotes.load_funding()
        if not legs:
            # Direct callers and fixture builders may intentionally provide an
            # already-current in-memory row. Production always passes the
            # loaded cache explicitly, including an empty cache, so expiry is
            # still fail-closed there.
            return raw
    notes = raw.get("notes")
    updated_notes: dict[str, Any] | None = None
    missing_live_leg = False
    observed_ages: list[float] = []
    touched = False
    top_level_current = any(
        raw.get(field) is not None
        for field in (
            "funding_daily_pct",
            "funding_projected_24h_pct",
            "funding_spread_pct",
            "funding_spread_apr_pct",
            "funding_apr_pct",
            "long_funding_pct",
            "short_funding_pct",
            "long_funding",
            "short_funding",
        )
    )
    for side in ("long", "short"):
        if raw.get(f"{side}_market_type") != "Futures":
            continue
        if updated_notes is None:
            updated_notes = dict(notes) if isinstance(notes, dict) else {}
            existing = updated_notes.get("route_inputs")
            updated_notes["route_inputs"] = dict(existing) if isinstance(existing, dict) else {}
            legacy = updated_notes.get("funding")
            updated_notes["funding"] = dict(legacy) if isinstance(legacy, dict) else {}
        route_inputs = updated_notes["route_inputs"]
        leg = dict(route_inputs.get(side) or {})
        legacy_funding = updated_notes["funding"]
        legacy_leg = legacy_funding.get(side)
        had_legacy_current = isinstance(legacy_leg, dict) and any(
            legacy_leg.get(field) is not None
            for field in ("current_funding_pct", "rate_pct", "projected_24h_pct")
        )
        if explicit_legs or legs:
            legacy_funding.pop(side, None)
        # The snapshot carries no top-level market_symbol for a futures leg --
        # all 32,056 of them keep it in notes.route_inputs -- so keying on that
        # field produced "venue|None" every time and the overlay never matched
        # anything at all. Resolve it the way the row itself does.
        symbol = _leg_symbol(raw, side)
        if not symbol:
            had_current = any(
                leg.get(field) is not None
                for field in ("current_funding_pct", "projected_24h_pct")
            )
            leg.pop("current_funding_pct", None)
            leg.pop("projected_24h_pct", None)
            route_inputs[side] = leg
            missing_live_leg = True
            touched = touched or had_current or had_legacy_current or top_level_current
            continue
        entry = legs.get(f"{raw.get(f'{side}_venue')}|{symbol}")
        if not entry:
            had_current = any(
                leg.get(field) is not None
                for field in ("current_funding_pct", "projected_24h_pct")
            )
            leg.pop("current_funding_pct", None)
            leg.pop("projected_24h_pct", None)
            route_inputs[side] = leg
            missing_live_leg = True
            touched = touched or had_current or had_legacy_current or top_level_current
            continue
        touched = True
        leg["current_funding_pct"] = entry.get("rate_pct")
        age_seconds = _float_or_none(entry.get("age_seconds"))
        if age_seconds is not None:
            observed_ages.append(age_seconds)
            leg["funding_observed_at"] = entry.get("observed_at")
            leg["funding_age_seconds"] = age_seconds
        if entry.get("interval_hours") is not None:
            from spreadboard import funding_interval as _fi

            snapped = _fi.normalise(entry["interval_hours"])
            if snapped is not None:
                leg["funding_interval_hours"] = snapped
                # The flag must move with the value. Leaving the scan's stale
                # "assumed" in place kept 385 Bitget legs carrying a published
                # 4h schedule still labelled a guess.
                if entry.get("interval_assumed") is not None:
                    leg["funding_interval_assumed"] = bool(entry["interval_assumed"])
        if entry.get("next_funding_ts_us") is not None:
            leg["next_funding_ts_us"] = entry["next_funding_ts_us"]
        # The sweep gives a current rate and an interval for nearly every
        # futures leg, but only the enrichment step -- which covers 25 tokens a
        # lane -- ever wrote `projected_24h_pct`. A route needs it on BOTH legs
        # to show any carry at all, so 1,498 futures routes displayed no funding
        # while holding a live rate on each side. The projection is exactly this
        # arithmetic; deriving it here is not a new estimate, it is the one the
        # enrichment step would have made.
        #
        rate = _float_or_none(entry.get("rate_pct"))
        interval = _float_or_none(
            entry.get("interval_hours") or leg.get("funding_interval_hours")
        )
        if rate is not None and interval and interval > 0:
            leg["projected_24h_pct"] = rate * 24.0 / interval
        else:
            leg.pop("projected_24h_pct", None)
        route_inputs[side] = leg
    if updated_notes is None or not touched:
        return raw
    updated = {**raw, "notes": updated_notes}
    # Current carry fields are projections from live prints.  If either
    # futures leg is absent/expired, fail closed rather than preserving a
    # discovery-snapshot rate that may be tens of minutes old. Historical
    # settled windows remain independent in venue_funding_history.
    for field in (
        "funding_daily_pct",
        "funding_projected_24h_pct",
        "funding_spread_pct",
        "funding_spread_apr_pct",
        "funding_apr_pct",
        "long_funding_pct",
        "short_funding_pct",
        "long_funding",
        "short_funding",
    ):
        updated[field] = None
    updated["funding_age_min"] = (
        max(observed_ages) / 60.0 if observed_ages and not missing_live_leg else None
    )
    return updated


def _project_route_funding(raw: dict[str, Any]) -> dict[str, Any]:
    """Give a route a carry figure when both legs carry a live rate.

    `_propagate_funding_by_leg` computes the route total at snapshot load, from
    `notes.funding` -- before the live overlay has been applied. The overlay
    then refreshes `notes.route_inputs` per row and nothing recomputes the
    total, so 1,498 futures routes held a live rate on BOTH legs and displayed
    no funding at all.

    This is the same arithmetic the enrichment step performs, and it only fills
    a gap: a settled or already-projected total is left exactly as it was.
    """
    if raw.get("funding_projected_24h_pct") is not None:
        return raw
    notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
    legs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    daily: dict[str, float] = {}
    for side in ("long", "short"):
        if raw.get(f"{side}_market_type") != "Futures":
            # A spot leg pays and receives nothing.
            daily[side] = 0.0
            continue
        leg = legs.get(side) if isinstance(legs, dict) else None
        if not isinstance(leg, dict):
            return raw
        value = _float_or_none(leg.get("projected_24h_pct"))
        if value is None:
            rate = _float_or_none(leg.get("current_funding_pct"))
            interval = _float_or_none(leg.get("funding_interval_hours"))
            if rate is None or not interval or interval <= 0:
                return raw
            value = rate * 24.0 / interval
        daily[side] = value
    if not any(raw.get(f"{side}_market_type") == "Futures" for side in ("long", "short")):
        return raw
    # The short leg receives, the long leg pays.
    projected = daily["short"] - daily["long"]
    return {
        **raw,
        "funding_daily_pct": projected,
        "funding_projected_24h_pct": projected,
        "funding_apr_pct": projected * 365.0,
    }


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
    live_funding: dict[str, dict[str, Any]] | None = None,
) -> SpreadTerminalRow:
    raw = _apply_live_funding(_mirror_if_spot_sale_required(raw), live_funding)
    raw = _project_route_funding(raw)
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
    metadata_entry = (metadata or {}).get(token) or {}
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
    long_input = route_inputs.get("long") if isinstance(route_inputs.get("long"), dict) else {}
    short_input = route_inputs.get("short") if isinstance(route_inputs.get("short"), dict) else {}
    dex_input = (
        long_input
        if route_taxonomy.leg_is_onchain_spot(
            venue=long_venue, market_type=long_market_type
        )
        else short_input
        if route_taxonomy.leg_is_onchain_spot(
            venue=short_venue, market_type=short_market_type
        )
        else {}
    )
    dex_identity = (
        long_identity
        if route_taxonomy.leg_is_onchain_spot(
            venue=long_venue, market_type=long_market_type
        )
        else short_identity
        if route_taxonomy.leg_is_onchain_spot(
            venue=short_venue, market_type=short_market_type
        )
        else {}
    )
    dex_chain = _str_or_none(dex_identity.get("chain_id"))
    dex_contract = _str_or_none(dex_identity.get("token_address"))
    asset_class = tokenized_assets.classify(
        {
            "token": token,
            "token_name": token_metadata.token_name(token, metadata or {}),
            "long_market_symbol": long_market_symbol,
            "short_market_symbol": short_market_symbol,
        }
    )["asset_class"]
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
    matched_size_notional_usd = _float_or_none(
        raw.get("target_notional_usd"),
        dex_input.get("quote_notional_usd") if isinstance(dex_input, dict) else None,
        long_input.get("quote_notional_usd") if isinstance(long_input, dict) else None,
        short_input.get("quote_notional_usd") if isinstance(short_input, dict) else None,
    )
    # A route measured under an earlier probe remains useful top-book/funding
    # context, but it cannot claim today's matched-size spread. Enforce that at
    # ingestion so structural grouping and every downstream surface inherit
    # the same boundary even before the live overlay runs.
    if (
        _float_or_none(raw.get("depth_weighted_spread_pct")) is not None
        and (matched_size_notional_usd or 0.0) < LIVE_BOOK_TARGET_NOTIONAL_USD
        and "depth_unverified" not in blockers
    ):
        blockers.append("depth_unverified")
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
            _nested_float(route_inputs, "long", "current_funding_pct"),
            raw.get("long_funding_pct"),
            raw.get("long_funding"),
            long_funding.get("current_funding_pct"),
            long_funding.get("rate_pct"),
        ),
        short_funding_pct=_float_or_none(
            _nested_float(route_inputs, "short", "current_funding_pct"),
            raw.get("short_funding_pct"),
            raw.get("short_funding"),
            short_funding.get("current_funding_pct"),
            short_funding.get("rate_pct"),
        ),
        funding_24h_pct=funding_24h,
        funding_projected_24h_pct=funding_projected_24h,
        funding_24h_source=_str_or_none(raw.get("funding_24h_source")),
        funding_age_min=_float_or_none(raw.get("funding_age_min")),
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
            (long_input or {}).get(
                "funding_interval_assumed",
                long_funding.get("funding_interval_assumed", long_funding.get("interval_assumed", False)),
            )
        ),
        short_funding_interval_assumed=bool(
            (short_input or {}).get(
                "funding_interval_assumed",
                short_funding.get("funding_interval_assumed", short_funding.get("interval_assumed", False)),
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
        long_quote=_market_quote(route_inputs, "long", long_market_symbol, token),
        short_quote=_market_quote(route_inputs, "short", short_market_symbol, token),
        market_cap_usd=_float_or_none(metadata_entry.get("market_cap_usd")),
        fdv_usd=_float_or_none(metadata_entry.get("fdv_usd")),
        metadata_volume_24h_usd=_float_or_none(
            metadata_entry.get("market_volume_24h_usd")
        ),
        listing_age_days=_listing_age_days(metadata_entry.get("first_seen_at"), now),
        listing_age_source=_str_or_none(metadata_entry.get("listing_age_source")),
        asset_class=str(asset_class),
        liquidity_evidence_kind="minimum_leg_volume_24h",
        matched_size_notional_usd=matched_size_notional_usd,
        gas_adjusted_spread_pct=_float_or_none(raw.get("gas_adjusted_spread_pct")),
        dex_gas_estimate_usd=_float_or_none(
            dex_input.get("gas_estimate_usd") if isinstance(dex_input, dict) else None
        ),
        dex_slippage_bps=_float_or_none(
            dex_input.get("slippage_bps") if isinstance(dex_input, dict) else None
        ),
        dex_price_impact_pct=_float_or_none(
            dex_input.get("price_impact_pct") if isinstance(dex_input, dict) else None
        ),
        dex_quote_source=_str_or_none(
            dex_input.get("quote_source") if isinstance(dex_input, dict) else None
        ),
        dex_bid_vwap=_float_or_none(
            dex_input.get("bid_vwap") if isinstance(dex_input, dict) else None
        ),
        dex_ask_vwap=_float_or_none(
            dex_input.get("ask_vwap") if isinstance(dex_input, dict) else None
        ),
        dex_quote_ts_us=_int_or_none(
            dex_input.get("quote_ts_us") if isinstance(dex_input, dict) else None
        ),
        dex_mev_protection=_str_or_none(
            dex_input.get("mev_protection") if isinstance(dex_input, dict) else None
        ),
        dex_transfer_time_seconds=_float_or_none(
            dex_input.get("transfer_time_seconds") if isinstance(dex_input, dict) else None,
            dex_input.get("estimated_transfer_seconds") if isinstance(dex_input, dict) else None,
        ),
        dex_route_plan=tuple(
            str(item)
            for item in (dex_input.get("route_plan") or [])
            if isinstance(dex_input, dict) and item
        ),
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
    quote = str(filters.get("quote") or "").upper().strip()
    min_volume = _float_or_none(filters.get("min_volume_24h_usd"))
    min_market_cap = _float_or_none(filters.get("min_market_cap_usd"))
    max_market_cap = _float_or_none(filters.get("max_market_cap_usd"))
    min_fdv = _float_or_none(filters.get("min_fdv_usd"))
    max_fdv = _float_or_none(filters.get("max_fdv_usd"))
    max_listing_age = _float_or_none(filters.get("max_listing_age_days"))
    persistence = str(filters.get("persistence") or "").casefold().strip()
    asset_class = str(filters.get("asset_class") or "").casefold().strip()
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
        if asset_class and row.asset_class != asset_class:
            continue
        if quote and quote not in {str(row.long_quote or "").upper(), str(row.short_quote or "").upper()}:
            continue
        route_volume = _route_volume_24h(row)
        if min_volume is not None and (route_volume is None or route_volume < min_volume):
            continue
        if min_market_cap is not None and (
            row.market_cap_usd is None or row.market_cap_usd < min_market_cap
        ):
            continue
        if max_market_cap is not None and (
            row.market_cap_usd is None or row.market_cap_usd > max_market_cap
        ):
            continue
        if min_fdv is not None and (row.fdv_usd is None or row.fdv_usd < min_fdv):
            continue
        if max_fdv is not None and (row.fdv_usd is None or row.fdv_usd > max_fdv):
            continue
        if max_listing_age is not None and (
            row.listing_age_days is None or row.listing_age_days > max_listing_age
        ):
            continue
        if persistence and _funding_persistence_status(row) != persistence:
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


def _route_volume_24h(row: SpreadTerminalRow) -> float | None:
    values = [
        value
        for value in (
            _float_or_none(row.long_volume_24h_usd),
            _float_or_none(row.short_volume_24h_usd),
        )
        if value is not None and value >= 0
    ]
    # A two-leg route is only as liquid as its thinner leg. Metadata-wide token
    # volume is intentionally not substituted for a missing venue leg.
    return min(values) if len(values) == 2 else None


def _funding_persistence_status(row: SpreadTerminalRow) -> str:
    windows = venue_funding_history.route_windows(row.to_dict())
    values = [float(value) for value in windows.values() if value is not None]
    if len(values) < 2:
        return "insufficient"
    if all(value > 0 for value in values):
        return "persistent"
    if all(value <= 0 for value in values):
        return "reversing"
    return "mixed"


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
    # A generic ``asset:TICKER`` identity proves only that symbols match. It
    # cannot distinguish RWA Inc from Allo (both trade as RWA), or migrated and
    # legacy representations of the same ticker. A large cross-venue CEX
    # gap therefore needs exact public contract evidence before it is ranked.
    # DEX rows have a separate exact chain/contract guard below.
    if (
        spread >= 5.0
        and not is_dex
        and not public_rails.exact_contract_match(long_rails, short_rails)
        and not already_guarded
    ):
        reasons.append("mirage_guard:high_dislocation_exact_identity_required")
    if is_dex and spread >= 5.0 and not identity_unverified and not already_guarded:
        notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
        identities = notes.get("identity") if isinstance(notes.get("identity"), dict) else {}
        long_identity = identities.get("long") if isinstance(identities.get("long"), dict) else {}
        short_identity = identities.get("short") if isinstance(identities.get("short"), dict) else {}
        long_is_dex = "dex" in str(raw.get("long_venue") or "").casefold()
        dex_identity = long_identity if long_is_dex else short_identity
        cex_rails = short_rails if long_is_dex else long_rails
        if not public_rails.state_has_exact_contract(
            cex_rails,
            contract=_str_or_none(dex_identity.get("token_address")),
            chain_id=dex_identity.get("chain_id"),
        ):
            reasons.append("mirage_guard:high_dislocation_cex_contract_unverified")
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
    # The product policy is to keep plausible, fresh routes visible and mark
    # unresolved identity with ``mirage_guarded``/``?``.  The stricter
    # price-ratio, book-quality, instrument and deliverability checks below
    # still apply when ``include_unverified`` is false.  Defaulting this flag
    # to true accidentally removed every route for newly listed tokens such as
    # GUA from both the member board and the Telegram snapshot.
    return str(os.environ.get("SPREADBOARD_HIDE_GUARDED_ROWS", "0")).strip().lower() in {
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


def quote_basis_mismatch(row: "SpreadTerminalRow") -> bool:
    """True when two CEX legs are denominated in different quote assets.

    USD, USDC and USDT can trade close to one another, but they are not the
    same basis.  A route comparing them cannot honestly attribute the entire
    difference to the token and therefore does not belong in the standard
    spread or funding rankings.  Unknown quote assets (notably DEX legs) are
    left untouched until their conversion path is explicitly modelled.
    """

    long_quote = str(getattr(row, "long_quote", "") or "").upper().strip()
    short_quote = str(getattr(row, "short_quote", "") or "").upper().strip()
    return bool(long_quote and short_quote and long_quote != short_quote)


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
        not quote_basis_mismatch(row)
        and not price_ratio_implausible(row)
        and not leg_volume_too_thin(row)
        and not is_venue_specific_leveraged_token(row)
        and not is_non_perpetual_or_inverse(row)
        and not spread_is_untrustworthy(row)
        and "depth_unverified" not in (getattr(row, "blockers", []) or [])
    )


def spread_leader_ready(
    row: "SpreadTerminalRow",
    *,
    max_age_min: float = SPREAD_LEADER_MAX_AGE_MIN,
) -> bool:
    """Whether a matched-size quote is recent enough to claim a current edge.

    This deliberately does not change row visibility or funding eligibility.
    A cooled basis is still useful research and may have excellent carry; it
    simply cannot occupy a live spread headline until either the websocket or
    the exact quote rotation refreshes both legs again.
    """

    return (
        row_is_presentable(row)
        and getattr(row, "freshness", "fresh") == "fresh"
        and spread_quote_current(row, max_age_min=max_age_min)
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
    return any(
        str(item).startswith("mirage_guard:")
        for item in (getattr(row, "blockers", None) or [])
    )


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
        "dex_rows": len(
            [row for row in all_rows if row.route_kind.startswith("DEX-")]
        ),
        "funding_rows": len([row for row in filtered if _effective_funding_24h(row) is not None]),
        "max_executable_spread_pct": max(
            (
                _float_or_none(row.executable_spread_pct) or 0.0
                for row in filtered
                if route_deliverable(row) is not False
                and not price_ratio_implausible(row)
                and not leg_volume_too_thin(row)
                and spread_leader_ready(row)
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
            (
                _entrance_spread(row)
                for row in filtered
                if route_deliverable(row) is not False
                and not _is_mirage_guarded(row)
                and not price_ratio_implausible(row)
                and not leg_volume_too_thin(row)
                and spread_leader_ready(row)
                and tokenized_route_rankable(row)
            ),
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
        "max_funding_24h_pct": max(
            (
                _effective_funding_24h(row)
                for row in filtered
                if _effective_funding_24h(row) is not None
            ),
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



def attach_funding_history(
    groups: list[dict[str, Any]],
    *,
    lookup: Any = None,
) -> None:
    """Add realised 1d/7d/30d carry to the rows a member can actually see.

    The reference product shows these beside every pair, so a funding farm can
    be judged without leaving the table. We computed the same windows and
    surfaced them only on Rankings and the per-token view.

    Only the visible slice is priced. Hundreds of hidden alternatives sit
    behind each token and calculating windows for those is pure waste --
    catalog_pairs already draws the line in the same place.

    History is context, never a precondition: if the window cache cannot be
    read the board still renders, simply without it.
    """
    if lookup is None:
        from spreadboard import venue_funding_history

        lookup = venue_funding_history.route_windows
    for group in groups:
        best = group.get("best_route")
        if not isinstance(best, dict):
            continue
        try:
            windows = lookup(best)
        except Exception:
            # Context must never break the board: a window cache that cannot
            # be read costs the columns, not the table.
            LOGGER.warning("funding window lookup failed", exc_info=True)
            continue
        if not windows:
            continue
        best["settled_funding_windows"] = windows
        group["settled_funding_windows"] = windows


def _group_rows(rows: list[SpreadTerminalRow]) -> list[dict[str, Any]]:
    grouped: dict[str, list[SpreadTerminalRow]] = {}
    for row in rows:
        grouped.setdefault(row.token, []).append(row)
    output: list[dict[str, Any]] = []
    for token, token_rows in grouped.items():
        # Rank by the matched-size result whenever depth was actually measured.
        # A one-lot top-book edge is useful context, but it must not put a route
        # in the top results when $50 already crosses the opportunity away.
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
            and not _is_mirage_guarded(row)
            and not price_ratio_implausible(row)
            and not leg_volume_too_thin(row)
            and spread_leader_ready(row)
            and tokenized_route_rankable(row)
        ]
        if tradeable_rows:
            best = max(tradeable_rows, key=_entrance_spread)
        else:
            # Nothing survived the filters, so the fallback is "widest raw" --
            # and on seven live groups that was a DEX route whose on-chain quote
            # had gone stale, so the token displayed "waiting for a two-leg
            # matched quote" while holding 14 to 28 routes quoted right then.
            # Prefer one that can actually be priced: a headline nobody can read
            # is worse than a smaller number somebody can.
            quotable = [
                row
                for row in token_rows
                if spread_quote_current(row)
                and (
                    _float_or_none(row.depth_weighted_spread_pct) is not None
                    or _float_or_none(row.executable_spread_pct) is not None
                )
            ]
            best = max(quotable or token_rows, key=_entrance_spread)
        funding_rows = [
            row
            for row in token_rows
            if _effective_funding_24h(row) is not None
            and not _is_mirage_guarded(row)
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
                # Keep the token and its routes visible when every price is
                # cooling, but do not publish an old value as its current best.
                "best_edge_pct": (
                    _entrance_spread(best) if tradeable_rows else None
                ),
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
                    "projected_current_rate"
                    if best_funding is not None
                    and (
                        best_funding.long_funding_pct is not None
                        or best_funding.short_funding_pct is not None
                        or best_funding.funding_projected_24h_pct is not None
                    )
                    else "settled_public_events"
                    if best_funding is not None and best_funding.funding_24h_pct is not None
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
        # Keep guarded research rows searchable without letting an unresolved
        # identity or transfer path buy a leader slot.  Healthy groups always
        # sort ahead; an all-guarded token remains at the tail with its badge.
        if (group.get("best_route") or {}).get("mirage_guarded"):
            return -999999.0
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
    """One funding print expressed per day.

    The interval multiplies every carry number on the board -- 0.01% is
    0.03%/day on an 8-hour contract and 0.24%/day on a 1-hour one -- so it goes
    through funding_interval, which snaps float noise (3.9999999999999996 came
    through on 1,102 Kucoin legs) and refuses values no perpetual venue uses
    rather than annualising from them.
    """
    from spreadboard import funding_interval

    rate = _float_or_none(rate_pct)
    if rate is None:
        return None
    hours = funding_interval.normalise(interval_hours)
    if hours is None:
        # Not a schedule we recognise. Fall back to the common one rather than
        # drop the row, and funding_intervals_known() still reports it as
        # unverified so nothing presents it as measured.
        hours = funding_interval.DEFAULT_INTERVAL_HOURS
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

    This is the live ``Now`` value, so current leg rates win. Settled 24h is a
    different, historical measurement exposed separately by the Funding radar;
    mixing it into this value made the ``Now`` rank lag the rates visible on the
    exchanges. If no fresh current leg or projection exists, this value is
    deliberately unavailable.
    """
    long_daily = _per_day(
        getattr(row, "long_funding_pct", None),
        getattr(row, "long_funding_interval_hours", None),
    )
    short_daily = _per_day(
        getattr(row, "short_funding_pct", None),
        getattr(row, "short_funding_interval_hours", None),
    )
    has_leg_rate = any(
        _float_or_none(getattr(row, f"{side}_funding_pct", None)) is not None
        for side in ("long", "short")
    )
    if has_leg_rate:
        for side, value in (("long", long_daily), ("short", short_daily)):
            if (
                str(getattr(row, f"{side}_market_type", "") or "") == "Futures"
                and value is None
            ):
                return None, None
        net_daily = (short_daily or 0.0) - (long_daily or 0.0)
    else:
        # Fast-quote and board rows can carry only a pre-computed projection.
        net_daily = _float_or_none(getattr(row, "funding_projected_24h_pct", None))
        if net_daily is None:
            net_daily = _float_or_none(getattr(row, "funding_daily_pct", None))
    if net_daily is None:
        return None, None
    if requires_existing_spot_inventory(row):
        net_daily = -net_daily
    return net_daily, net_daily * 365.0


def _public_row(row: SpreadTerminalRow) -> dict[str, Any]:
    payload = row.to_dict()
    payload["market_events"] = market_events.events_for_route(payload)
    payload["tokenized_guard"] = tokenized_assets.classify(payload)
    payload["deliverable"] = route_deliverable(row)
    payload["requires_transfer"] = getattr(row, "route_kind", None) in TRANSFER_ROUTE_KINDS
    payload["identity_mismatch"] = price_ratio_implausible(row)
    payload["thin_book"] = leg_volume_too_thin(row)
    payload["funding_intervals_known"] = funding_intervals_known(row)
    payload["spread_quote_current"] = spread_quote_current(row)
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
    payload["funding_rank_basis"] = (
        "projected_current_rate"
        if (
            getattr(row, "long_funding_pct", None) is not None
            or getattr(row, "short_funding_pct", None) is not None
            or getattr(row, "funding_projected_24h_pct", None) is not None
        )
        else None
    )
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
    depth_spread = _float_or_none(row.depth_weighted_spread_pct)
    if depth_spread is not None and "depth_unverified" not in (getattr(row, "blockers", []) or []):
        return depth_spread
    open_spread = _float_or_none(row.displayed_open_spread_pct)
    if open_spread is not None:
        return open_spread
    executable = _float_or_none(row.executable_spread_pct)
    if executable is not None:
        return executable
    if depth_spread is not None:
        return depth_spread
    return -999999.0


def _entrance_spread_dict(row: dict[str, Any]) -> float:
    depth_spread = _float_or_none(row.get("depth_weighted_spread_pct"))
    if depth_spread is not None and not row.get("depth_unverified"):
        return depth_spread
    open_spread = _float_or_none(row.get("displayed_open_spread_pct"))
    if open_spread is not None:
        return open_spread
    executable = _float_or_none(row.get("executable_spread_pct"))
    if executable is not None:
        return executable
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
    return _float_or_none(row.get("funding_projected_24h_pct"))


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
    return route_taxonomy.route_kind(
        long_venue=long_venue,
        long_market_type=long_market_type,
        short_venue=short_venue,
        short_market_type=short_market_type,
        source_kind=source_kind,
    )


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
    """How much the thinner leg of this route actually trades in a day.

    This read `notes.screen.liquidity_usd`, a key discovery has never written --
    the same shape of bug as the funding overlay keying on a field that was
    always None. So the Depth column was empty on all 15,943 rows and "sort by
    depth" ranked nothing, silently.

    There is no honest order-book depth to put here: the scan walks the ladder
    to a $50 probe, which cannot answer "how much can I trade before I move the
    price". The 24h volume of the *thinner* leg can, roughly, and it is real and
    present on 89% of rows -- so that is what this returns, and the column is
    labelled 24h volume rather than depth.
    """
    notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
    legs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    volumes = []
    for side in ("long", "short"):
        leg = legs.get(side) if isinstance(legs, dict) else None
        value = _float_or_none((leg or {}).get("volume_24h_usd")) if isinstance(leg, dict) else None
        if value is not None and value > 0:
            volumes.append(value)
    if len(volumes) < 2:
        return None
    # The route is only as tradeable as its thinner side.
    return min(volumes)


def _nested_float(value: Any, section: str, key: str) -> float | None:
    section_value = value.get(section) if isinstance(value, dict) else None
    if not isinstance(section_value, dict):
        return None
    return _float_or_none(section_value.get(key))


_KNOWN_QUOTES = ("USDT", "USDC", "FDUSD", "TUSD", "DAI", "USD", "EUR", "GBP", "BTC", "ETH", "BNB")


def _market_quote(
    route_inputs: dict[str, Any],
    side: str,
    market_symbol: str | None,
    token: str,
) -> str | None:
    leg = route_inputs.get(side) if isinstance(route_inputs, dict) else None
    if isinstance(leg, dict):
        for key in ("quote", "quote_currency", "quoteAsset", "settle"):
            value = str(leg.get(key) or "").upper().strip()
            if value:
                return value
    symbol = str(market_symbol or "").upper().strip()
    if not symbol:
        return None
    if "/" in symbol:
        value = symbol.split("/", 1)[1].split(":", 1)[0].split("-", 1)[0]
        return value or None
    compact = re.sub(r"[^A-Z0-9]", "", symbol)
    token_compact = re.sub(r"[^A-Z0-9]", "", str(token or "").upper())
    if token_compact and compact.startswith(token_compact):
        compact = compact[len(token_compact) :]
    for quote in _KNOWN_QUOTES:
        if compact.startswith(quote) or compact.endswith(quote):
            return quote
    return None


def _listing_age_days(value: Any, now: float) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed.timestamp()) / 86_400.0)


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
