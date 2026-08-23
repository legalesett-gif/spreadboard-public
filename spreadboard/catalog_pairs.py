"""Current pair coverage derived from the complete market catalogue.

Discovery deliberately keeps only a bounded number of rows per token.  That is
appropriate for a scanner, but it is not a complete pair browser: a perfectly
valid Gate/Mexc combination can be absent merely because 28 other combinations
ranked ahead of it when the snapshot was written.  This module closes that gap
without increasing the 40 MB snapshot or weakening its identity guards.

Every result is assembled from three already-warm, process-shared artifacts:

* ``chart_market_catalog.json`` supplies exact venue symbols;
* ``spreadboard_live_books.sqlite3`` supplies current bid/ask books; and
* ``live_funding.json`` supplies current per-leg funding and cadence.

No exchange request occurs in an HTTP or Telegram request.  A pair needs two
fresh books from the same freshness boundary, stays inside the scanner's 3x
price-ratio identity boundary, and never calls a ticker quote "$50 VWAP" when
the stored depth cannot actually fill $50.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import threading
import time
from typing import Any

from spreadarb.api_discovery.orderbook import depth_weighted_price
from spreadboard import (
    api_spreads,
    bulk_quotes,
    chart_catalog,
    exchange_links,
    live_book_cache,
    public_rails,
    route_taxonomy,
    tokenized_assets,
    venue_funding_history,
)


MAX_PRICE_RATIO = 3.0
TARGET_NOTIONAL_USD = float(os.environ.get("SPREADBOARD_LIVE_BOOK_NOTIONAL_USD", "500"))
MAX_BOOK_AGE_SECONDS = max(
    30.0, float(os.environ.get("SPREADBOARD_CATALOG_BOOK_AGE_SECONDS", "180"))
)
CACHE_SECONDS = max(0.25, float(os.environ.get("SPREADBOARD_CATALOG_PAIR_CACHE_SECONDS", "2")))
LEVERAGED_TOKEN_PATTERN = re.compile(r"^[A-Z0-9]+[2-5][LS]$")

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}


def clear_cache() -> None:
    """Invalidate derived pairs after a new shared generation lands."""

    with _CACHE_LOCK:
        _CACHE.clear()


@dataclass(frozen=True, slots=True)
class Leg:
    token: str
    venue: str
    market_type: str
    symbol: str
    quote: str
    contract_size: float
    book: live_book_cache.CachedBook

    @property
    def bid(self) -> float:
        return float(self.book.bids[0][0])

    @property
    def ask(self) -> float:
        return float(self.book.asks[0][0])


def for_token(
    token: str,
    *,
    max_age_seconds: float = MAX_BOOK_AGE_SECONDS,
    limit: int | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return every currently quoteable CEX pair for one catalogue token.

    Futures/futures pairs expose both directions because funding can make the
    negative-basis direction the economically interesting one.  Spot/futures
    exposes only the executable cash-and-carry direction (long spot, short
    futures), and spot/spot exposes the cheaper buy venue toward the dearer sell
    venue after checking public transfer rails.
    """

    symbol = _token(token)
    if not symbol:
        return _empty(symbol, "invalid_token")
    now = time.monotonic()
    cache_key = (symbol, int(max_age_seconds))
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached is not None and now - cached[0] <= CACHE_SECONDS:
                return _limited(cached[1], limit)

    catalog = chart_catalog.load()
    markets = [
        item
        for item in catalog.get("markets") or []
        if isinstance(item, dict)
        and _token(item.get("token")) == symbol
        and not _is_dex(item)
        and str(item.get("market_type") or "") in {"Spot", "Futures"}
    ]
    # A catalogue can retain two aliases for one exact market after an adapter
    # rename.  The venue/type/symbol identity is what the live-book store uses.
    unique_markets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in markets:
        key = (
            str(item.get("venue") or ""),
            str(item.get("market_type") or ""),
            str(item.get("symbol") or ""),
        )
        if all(key):
            unique_markets[key] = item

    legs: list[Leg] = []
    missing_books = 0
    for item in unique_markets.values():
        try:
            book = live_book_cache.load_live_book(
                str(item["venue"]),
                str(item["market_type"]),
                str(item["symbol"]),
                max_age_seconds=max_age_seconds,
            )
        except Exception:  # noqa: BLE001 - a damaged cache must not take down a page.
            book = None
        if book is None:
            missing_books += 1
            continue
        try:
            contract_size = float(item.get("contract_size") or 1.0)
        except (TypeError, ValueError):
            contract_size = 1.0
        legs.append(
            Leg(
                token=symbol,
                venue=str(item["venue"]),
                market_type=str(item["market_type"]),
                symbol=str(item["symbol"]),
                quote=str(item.get("quote") or "").upper(),
                contract_size=contract_size if contract_size > 0 else 1.0,
                book=book,
            )
        )

    funding = bulk_quotes.load_funding()
    rails = public_rails.load_public_rails()
    routes: list[dict[str, Any]] = []
    rejected = {
        "same_venue": 0,
        "price_ratio": 0,
        "quote_mismatch": 0,
        "closed_rail": 0,
        "leveraged": 0,
    }
    for left_index, left in enumerate(legs):
        for right in legs[left_index + 1 :]:
            for long_leg, short_leg in _directions(left, right):
                reason = _reject_reason(symbol, long_leg, short_leg, rails)
                if reason:
                    rejected[reason] += 1
                    continue
                routes.append(_route(symbol, long_leg, short_leg, funding, rails))

    routes.sort(
        key=lambda row: (
            _spread_rank(row),
            _number(row.get("depth_weighted_spread_pct")) is not None,
            _number(row.get("funding_projected_24h_pct")) or float("-inf"),
        ),
        reverse=True,
    )
    payload = {
        "ok": bool(routes),
        "mode": "warm_complete_catalog_pairs",
        "token": symbol,
        "catalog_generated_at": catalog.get("generated_at"),
        "book_max_age_seconds": max_age_seconds,
        "target_notional_usd": TARGET_NOTIONAL_USD,
        "catalog_market_count": len(unique_markets),
        "fresh_market_count": len(legs),
        "missing_book_count": missing_books,
        "route_count": len(routes),
        "displayed_route_count": len(routes),
        "rejected": rejected,
        "routes": routes,
    }
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic(), payload)
        if len(_CACHE) > 128:
            oldest = min(_CACHE, key=lambda key: _CACHE[key][0])
            _CACHE.pop(oldest, None)
    return _limited(payload, limit)


def for_tokens(
    tokens: list[str] | tuple[str, ...] | set[str],
    *,
    max_age_seconds: float = MAX_BOOK_AGE_SECONDS,
    limit_per_token: int | None = None,
    include_history: bool = False,
) -> dict[str, dict[str, Any]]:
    """Build current CEX pair catalogues for several tokens from one book read.

    The grouped board needs only its visible page (normally 25 tokens), but a
    point lookup for every market made that page pay hundreds of SQLite reads.
    This bulk path reads the shared store once and performs the same identity,
    quote-basis, rail and matched-depth checks in memory.  It never calls an
    exchange and is therefore safe for the already-warm page builder.
    """

    wanted = {_token(token) for token in tokens}
    wanted.discard("")
    if not wanted or not live_book_cache.DEFAULT_PATH.exists():
        return {}

    catalog = chart_catalog.load()
    store: live_book_cache.LiveBookStore | None = None
    try:
        store = live_book_cache.LiveBookStore()
        books = store.load_all(max_age_seconds=max_age_seconds)
    except Exception:  # noqa: BLE001 - preserve the bounded scanner on failure.
        return {}
    finally:
        if store is not None:
            store.close()

    markets_by_token: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    for item in catalog.get("markets") or []:
        if not isinstance(item, dict) or _is_dex(item):
            continue
        token = _token(item.get("token"))
        if token not in wanted:
            continue
        key = (
            str(item.get("venue") or ""),
            str(item.get("market_type") or ""),
            str(item.get("symbol") or ""),
        )
        if all(key) and key[1] in {"Spot", "Futures"}:
            markets_by_token.setdefault(token, {})[key] = item

    funding = bulk_quotes.load_funding()
    rails = public_rails.load_public_rails()
    output: dict[str, dict[str, Any]] = {}
    for token in wanted:
        token_markets = markets_by_token.get(token) or {}
        legs: list[Leg] = []
        for (venue, market_type, symbol), item in token_markets.items():
            book = books.get(live_book_cache.cache_key(venue, market_type, symbol))
            if book is None:
                continue
            try:
                contract_size = float(item.get("contract_size") or 1.0)
            except (TypeError, ValueError):
                contract_size = 1.0
            legs.append(
                Leg(
                    token=token,
                    venue=venue,
                    market_type=market_type,
                    symbol=symbol,
                    quote=str(item.get("quote") or "").upper(),
                    contract_size=contract_size if contract_size > 0 else 1.0,
                    book=book,
                )
            )

        payload = _payload_from_legs(
            token,
            legs,
            funding=funding,
            rails=rails,
            catalog_generated_at=catalog.get("generated_at"),
            catalog_market_count=len(token_markets),
            max_age_seconds=max_age_seconds,
            include_history=include_history,
        )
        output[token] = _limited(payload, limit_per_token)
    return output


def filtered(
    payload: dict[str, Any],
    *,
    kind: str | None = None,
    exchange: str | None = None,
    quote: str | None = None,
    funding_only: bool = False,
    min_spread_pct: float | None = None,
    min_abs_funding_24h_pct: float | None = None,
    min_abs_funding_apr_pct: float | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Apply board economics without coupling spread eligibility to funding.

    A positive spread remains a spread candidate when carry is negative.  A
    positive-carry route remains a funding candidate when its opening basis is
    negative.  This is the exact product distinction the bounded discovery
    snapshot could not express after its per-token quota was exhausted.
    """

    normalized_kind = str(kind or "").upper().strip()
    normalized_kind = {
        "FUTURES-FUTURES": "FUTURES",
        "FUTURES-SPOT": "FUTURES-SPOT-PAIR",
        "SPOT-FUTURES": "FUTURES-SPOT-PAIR",
        "SPOT-SPOT": "SPOT",
    }.get(normalized_kind, normalized_kind)
    wanted_exchange = str(exchange or "").casefold().strip()
    wanted_quote = str(quote or "").upper().strip()
    routes: list[dict[str, Any]] = []
    for row in payload.get("routes") or []:
        route_kind = str(row.get("route_kind") or "").upper()
        if normalized_kind == "FUTURES-SPOT-PAIR":
            if route_kind not in {"FUTURES-SPOT", "SPOT-FUTURES"}:
                continue
        elif normalized_kind and route_kind != normalized_kind:
            continue
        if wanted_exchange and wanted_exchange not in " ".join(
            str(row.get(key) or "") for key in ("long_venue", "short_venue")
        ).casefold():
            continue
        if wanted_quote and wanted_quote not in {
            str(row.get("long_quote") or "").upper(),
            str(row.get("short_quote") or "").upper(),
        }:
            continue
        spread = _spread_rank(row)
        carry = _number(row.get("funding_daily_pct"))
        if funding_only:
            if carry is None or carry <= 0:
                continue
        elif spread <= 0:
            continue
        if min_spread_pct is not None and not funding_only and spread < float(min_spread_pct):
            continue
        if min_abs_funding_24h_pct is not None and abs(carry or 0.0) < float(
            min_abs_funding_24h_pct
        ):
            continue
        if min_abs_funding_apr_pct is not None and abs(carry or 0.0) * 365.0 < float(
            min_abs_funding_apr_pct
        ):
            continue
        routes.append(row)

    routes.sort(
        key=(
            (lambda row: _number(row.get("funding_daily_pct")) or float("-inf"))
            if funding_only
            else _spread_rank
        ),
        reverse=True,
    )
    total = len(routes)
    returned = routes if limit is None else routes[: max(0, int(limit))]
    # Historical settlement windows are useful for the rows a member can see,
    # but calculating them for hundreds of hidden alternatives wastes work.
    for row in returned:
        if not row.get("catalog_history_loaded"):
            windows = venue_funding_history.route_windows(row)
            row["settled_funding_windows"] = windows
            row["funding_24h_pct"] = windows.get("1d")
            row["catalog_history_loaded"] = True
    result = dict(payload)
    result.update(
        {
            "ok": bool(returned),
            "mode": "warm_complete_catalog_pairs_filtered_independently",
            "route_count": total,
            "displayed_route_count": len(returned),
            "returned_route_count": len(returned),
            "routes": returned,
        }
    )
    return result


def _payload_from_legs(
    token: str,
    legs: list[Leg],
    *,
    funding: dict[str, dict[str, Any]],
    rails: dict[str, dict[str, Any]],
    catalog_generated_at: Any,
    catalog_market_count: int,
    max_age_seconds: float,
    include_history: bool,
) -> dict[str, Any]:
    routes: list[dict[str, Any]] = []
    rejected = {
        "same_venue": 0,
        "price_ratio": 0,
        "quote_mismatch": 0,
        "closed_rail": 0,
        "leveraged": 0,
    }
    for left_index, left in enumerate(legs):
        for right in legs[left_index + 1 :]:
            for long_leg, short_leg in _directions(left, right):
                reason = _reject_reason(token, long_leg, short_leg, rails)
                if reason:
                    rejected[reason] += 1
                    continue
                routes.append(
                    _route(
                        token,
                        long_leg,
                        short_leg,
                        funding,
                        rails,
                        include_history=include_history,
                    )
                )
    routes.sort(
        key=lambda row: (
            _spread_rank(row),
            _number(row.get("depth_weighted_spread_pct")) is not None,
            _number(row.get("funding_projected_24h_pct")) or float("-inf"),
        ),
        reverse=True,
    )
    return {
        "ok": bool(routes),
        "mode": "warm_complete_catalog_pairs",
        "token": token,
        "catalog_generated_at": catalog_generated_at,
        "book_max_age_seconds": max_age_seconds,
        "target_notional_usd": TARGET_NOTIONAL_USD,
        "catalog_market_count": catalog_market_count,
        "fresh_market_count": len(legs),
        "missing_book_count": max(0, catalog_market_count - len(legs)),
        "route_count": len(routes),
        "displayed_route_count": len(routes),
        "rejected": rejected,
        "routes": routes,
    }


def group(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape a catalogue result for the existing grouped-route renderer."""

    routes = list(payload.get("routes") or [])
    if not routes:
        return {}
    current_spread_routes = [
        row
        for row in routes
        if api_spreads.spread_quote_current(row) and not row.get("mirage_guarded")
    ]
    spread_route = max(
        current_spread_routes or routes,
        key=lambda row: _spread_rank(row),
    )
    funding_routes = [
        row
        for row in routes
        if _number(row.get("funding_projected_24h_pct")) is not None
        and not row.get("mirage_guarded")
    ]
    funding_route = (
        max(funding_routes, key=lambda row: float(row["funding_projected_24h_pct"]))
        if funding_routes
        else None
    )
    venues = sorted(
        {
            str(row.get(key) or "")
            for row in routes
            for key in ("long_venue", "short_venue")
            if row.get(key)
        }
    )
    kinds = sorted({str(row.get("route_kind") or "") for row in routes})
    age = min(
        (_number(row.get("age_min")) for row in routes if _number(row.get("age_min")) is not None),
        default=None,
    )
    return {
        "token": payload.get("token"),
        "token_name": None,
        "coverage_mode": "catalog_live_books",
        "route_count": int(payload.get("route_count") or len(routes)),
        "displayed_route_count": int(
            payload.get("displayed_route_count") or len(routes)
        ),
        "venues": venues,
        "route_kinds": kinds,
        "best_edge_pct": (
            _spread_rank(spread_route) if current_spread_routes else None
        ),
        "best_route": spread_route,
        "best_funding_24h_pct": (
            funding_route.get("funding_projected_24h_pct")
            if funding_route is not None
            else None
        ),
        "best_funding_24h_basis": (
            "current_rate_projection" if funding_route is not None else None
        ),
        "best_funding_route": funding_route,
        "age_min": age,
        "href": f"/token/{payload.get('token')}",
        "routes": routes,
    }


def with_routes(
    payload: dict[str, Any], extra_routes: list[dict[str, Any]], *, limit: int | None = None
) -> dict[str, Any]:
    """Merge current identity-aware DEX rows into a complete CEX pair payload."""

    routes: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], int] = {}
    for row in [*(payload.get("routes") or []), *extra_routes]:
        if not isinstance(row, dict):
            continue
        identity = _merge_route_identity(row)
        if identity in seen:
            existing = routes[seen[identity]]
            # The warm catalogue owns the current exact-book economics. The
            # bounded scanner can still carry evidence the catalogue does not,
            # notably settled 1d/7d/30d windows and provider metadata. Fill
            # only missing fields so that evidence survives without replacing
            # the fresher matched quote or the canonical chart route.
            for key, value in row.items():
                if key not in existing or existing.get(key) is None:
                    existing[key] = value
            continue
        seen[identity] = len(routes)
        routes.append(dict(row))
    routes.sort(
        key=lambda row: (
            _spread_rank(row),
            _number(row.get("depth_weighted_spread_pct")) is not None,
            _number(row.get("funding_projected_24h_pct")) or float("-inf"),
        ),
        reverse=True,
    )
    total = len(routes)
    if limit is not None:
        routes = routes[: max(0, int(limit))]
    result = dict(payload)
    result.update(
        {
            "ok": bool(routes),
            "mode": "warm_complete_catalog_pairs_with_current_dex",
            "route_count": total,
            "displayed_route_count": len(routes),
            "returned_route_count": len(routes),
            "dex_route_count": sum(_route_is_dex(row) for row in routes),
            "routes": routes,
        }
    )
    return result


def _merge_route_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    """Exact economic leg identity, independent of route-key serialization."""

    route_key = str(row.get("route_key") or "")

    def leg(side: str) -> tuple[Any, ...]:
        venue = str(row.get(f"{side}_venue") or "").casefold()
        market_type = str(row.get(f"{side}_market_type") or "").casefold()
        symbol = str(row.get(f"{side}_market_symbol") or "").upper()
        if symbol:
            locator: tuple[Any, ...] = ("symbol", symbol)
        elif "dex" in venue:
            locator = (
                "contract",
                str(row.get("dex_chain") or "").casefold(),
                str(row.get("dex_contract") or "").casefold(),
            )
        else:
            # Incomplete identities must never collapse merely because both
            # omitted a symbol; fall back to their own route key.
            locator = ("route", route_key, side)
        return venue, market_type, *locator

    return (
        str(row.get("token") or "").upper(),
        row.get("route_kind"),
        leg("long"),
        leg("short"),
    )


def all_token_summaries(
    *, max_age_seconds: float = MAX_BOOK_AGE_SECONDS
) -> dict[str, dict[str, Any]]:
    """Best current pair per token from one bulk book read.

    This runs in the disposable ranking worker, never the web process. Reading
    the SQLite store once avoids 22,000 point queries and lets the compact
    leaderboard cover catalogue tokens that the bounded scanner never emitted.
    """

    catalog = chart_catalog.load()
    if not live_book_cache.DEFAULT_PATH.exists():
        return {}
    store: live_book_cache.LiveBookStore | None = None
    try:
        store = live_book_cache.LiveBookStore()
        books = store.load_all(max_age_seconds=max_age_seconds)
    except Exception:  # noqa: BLE001 - keep the prior atomic ranking artifact.
        return {}
    finally:
        if store is not None:
            store.close()
    funding = bulk_quotes.load_funding()
    rails = public_rails.load_public_rails()
    markets_by_token: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    for item in catalog.get("markets") or []:
        if not isinstance(item, dict) or _is_dex(item):
            continue
        token = _token(item.get("token"))
        key = (
            str(item.get("venue") or ""),
            str(item.get("market_type") or ""),
            str(item.get("symbol") or ""),
        )
        if token and all(key) and key[1] in {"Spot", "Futures"}:
            markets_by_token.setdefault(token, {})[key] = item

    output: dict[str, dict[str, Any]] = {}
    for token, token_markets in markets_by_token.items():
        legs: list[Leg] = []
        for (venue, market_type, symbol), item in token_markets.items():
            book = books.get(live_book_cache.cache_key(venue, market_type, symbol))
            if book is None:
                continue
            try:
                contract_size = float(item.get("contract_size") or 1.0)
            except (TypeError, ValueError):
                contract_size = 1.0
            legs.append(
                Leg(
                    token=token,
                    venue=venue,
                    market_type=market_type,
                    symbol=symbol,
                    quote=str(item.get("quote") or "").upper(),
                    contract_size=contract_size if contract_size > 0 else 1.0,
                    book=book,
                )
            )
        best_spread: dict[str, Any] | None = None
        best_funding: dict[str, Any] | None = None
        best_any: dict[str, Any] | None = None
        route_count = 0
        for left_index, left in enumerate(legs):
            for right in legs[left_index + 1 :]:
                for long_leg, short_leg in _directions(left, right):
                    if _reject_reason(token, long_leg, short_leg, rails):
                        continue
                    route_count += 1
                    row = _route(
                        token,
                        long_leg,
                        short_leg,
                        funding,
                        rails,
                        include_history=False,
                    )
                    if best_any is None or _spread_rank(row) > _spread_rank(best_any):
                        best_any = row
                    # The leaderboard says "$50 VWAP". A ticker-only top book
                    # remains useful on the token page but cannot win that rank.
                    # The same applies to an identity warning: keep it in the
                    # complete pair browser with its question mark, but do not
                    # let an unproved symbol match become the token's headline.
                    if (
                        row.get("depth_weighted_spread_pct") is not None
                        and not row.get("mirage_guarded")
                        # Missing age used to fall through as zero and could
                        # therefore win a current leaderboard indefinitely.
                        # The complete pair remains browseable via best_any;
                        # only a timestamped matched quote may headline it.
                        and api_spreads.spread_quote_current(row)
                        and (
                            best_spread is None
                            or _spread_rank(row) > _spread_rank(best_spread)
                        )
                    ):
                        best_spread = row
                    funding_value = _number(row.get("funding_projected_24h_pct"))
                    current_funding = (
                        _number(best_funding.get("funding_projected_24h_pct"))
                        if best_funding is not None
                        else None
                    )
                    if funding_value is not None and not row.get("mirage_guarded") and (
                        current_funding is None or funding_value > current_funding
                    ):
                        best_funding = row
        if best_any is not None:
            output[token] = {
                "fresh_market_count": len(legs),
                "fresh_venue_count": len({leg.venue for leg in legs}),
                "quoteable_pair_count": route_count,
                "best_spread_pct": (
                    _spread_rank(best_spread) if best_spread is not None else None
                ),
                "best_spread_route": best_spread,
                "funding_now_24h_pct": (
                    best_funding.get("funding_projected_24h_pct")
                    if best_funding is not None
                    else None
                ),
                "best_funding_route": best_funding,
                "age_min": max(
                    _number((best_spread or best_any).get("age_min")) or 0.0,
                    _number((best_funding or {}).get("age_min")) or 0.0,
                ),
            }
    return output


def _directions(left: Leg, right: Leg) -> list[tuple[Leg, Leg]]:
    if left.market_type == right.market_type == "Futures":
        if left.venue == right.venue:
            return []
        return [(left, right), (right, left)]
    if left.market_type == right.market_type == "Spot":
        if left.venue == right.venue:
            return []
        left_to_right = right.bid / left.ask - 1.0
        right_to_left = left.bid / right.ask - 1.0
        return [(left, right)] if left_to_right >= right_to_left else [(right, left)]
    spot = left if left.market_type == "Spot" else right
    future = right if left.market_type == "Spot" else left
    return [(spot, future)]


def _reject_reason(
    token: str,
    long_leg: Leg,
    short_leg: Leg,
    rails: dict[str, dict[str, Any]],
) -> str | None:
    if (
        long_leg.venue == short_leg.venue
        and long_leg.market_type == short_leg.market_type
    ):
        return "same_venue"
    # USD/USDC/USDT basis is a different risk from token spread. Ranking a
    # Kraken USD book against a Gate USDT book as if the quotes were identical
    # manufactured small but persistent leaders. Exact-pair rankings compare
    # like-for-like quote assets; cross-quote research remains a custom chart.
    if long_leg.quote and short_leg.quote and long_leg.quote != short_leg.quote:
        return "quote_mismatch"
    low = min(long_leg.ask, short_leg.bid)
    high = max(long_leg.ask, short_leg.bid)
    if low <= 0 or high / low > MAX_PRICE_RATIO:
        return "price_ratio"
    if LEVERAGED_TOKEN_PATTERN.match(token) and long_leg.venue != short_leg.venue:
        return "leveraged"
    if long_leg.market_type == short_leg.market_type == "Spot":
        buy = public_rails.rail_state(rails, long_leg.venue, token)
        sell = public_rails.rail_state(rails, short_leg.venue, token)
        if buy.get("withdraw") is False or sell.get("deposit") is False:
            return "closed_rail"
        compatibility = public_rails.transfer_compatibility(buy, sell)
        if compatibility.get("status") == "incompatible":
            return "closed_rail"
    return None


def _route(
    token: str,
    long_leg: Leg,
    short_leg: Leg,
    funding: dict[str, dict[str, Any]],
    rails: dict[str, dict[str, Any]],
    *,
    include_history: bool = True,
) -> dict[str, Any]:
    long_top, long_vwap, long_depth = _book_price(long_leg, "ask")
    short_top, short_vwap, short_depth = _book_price(short_leg, "bid")
    executable = (short_top / long_top - 1.0) * 100.0
    matched = (
        (short_vwap / long_vwap - 1.0) * 100.0
        if long_vwap is not None and short_vwap is not None
        else None
    )
    route_key = chart_catalog.custom_route_key(
        token,
        {
            "venue": long_leg.venue,
            "market_type": long_leg.market_type,
            "symbol": long_leg.symbol,
        },
        {
            "venue": short_leg.venue,
            "market_type": short_leg.market_type,
            "symbol": short_leg.symbol,
        },
    )
    long_funding = _funding_leg(long_leg, funding)
    short_funding = _funding_leg(short_leg, funding)
    projected = _net_daily(long_leg, short_leg, long_funding, short_funding)
    long_rail = public_rails.rail_state(rails, long_leg.venue, token)
    short_rail = public_rails.rail_state(rails, short_leg.venue, token)
    oldest_us = min(long_leg.book.quote_ts_us, short_leg.book.quote_ts_us)
    age_min = max(0.0, (time.time() - oldest_us / 1_000_000.0) / 60.0)
    identity_ratio = max(long_top, short_top) / min(long_top, short_top)
    row = {
        "token": token,
        "token_name": None,
        "route_key": route_key,
        "route_kind": _route_kind(long_leg, short_leg),
        "source_name": "Complete warm catalogue",
        "catalog_pair": True,
        "long_venue": long_leg.venue,
        "long_market_type": long_leg.market_type,
        "long_market_symbol": long_leg.symbol,
        "short_venue": short_leg.venue,
        "short_market_type": short_leg.market_type,
        "short_market_symbol": short_leg.symbol,
        "long_price": long_top,
        "short_price": short_top,
        "long_ask": long_top,
        "short_bid": short_top,
        "executable_spread_pct": executable,
        "displayed_open_spread_pct": executable,
        "depth_weighted_spread_pct": matched,
        "depth_usd": (
            TARGET_NOTIONAL_USD
            if matched is not None
            else min(long_depth, short_depth)
            if long_depth and short_depth
            else None
        ),
        "depth_unverified": matched is None,
        "target_notional_usd": TARGET_NOTIONAL_USD,
        "quote_ts_us": oldest_us,
        "age_min": age_min,
        "freshness": "fresh",
        "status": "live",
        "long_quote": long_leg.quote,
        "short_quote": short_leg.quote,
        "quote_mismatch": bool(
            long_leg.quote and short_leg.quote and long_leg.quote != short_leg.quote
        ),
        "long_funding_pct": long_funding.get("rate_pct"),
        "short_funding_pct": short_funding.get("rate_pct"),
        "long_funding_interval_hours": long_funding.get("interval_hours"),
        "short_funding_interval_hours": short_funding.get("interval_hours"),
        "long_next_funding_ts_us": long_funding.get("next_funding_ts_us"),
        "short_next_funding_ts_us": short_funding.get("next_funding_ts_us"),
        "funding_projected_24h_pct": projected,
        "funding_daily_pct": projected,
        "funding_spread_pct": projected,
        "funding_apr_pct": projected * 365.0 if projected is not None else None,
        "long_deposit_enabled": _bool(long_rail.get("deposit")),
        "long_withdraw_enabled": _bool(long_rail.get("withdraw")),
        "short_deposit_enabled": _bool(short_rail.get("deposit")),
        "short_withdraw_enabled": _bool(short_rail.get("withdraw")),
        "long_exchange_url": exchange_links.exchange_market_url(
            venue=long_leg.venue,
            market_type=long_leg.market_type,
            market_symbol=long_leg.symbol,
            token=token,
        ),
        "short_exchange_url": exchange_links.exchange_market_url(
            venue=short_leg.venue,
            market_type=short_leg.market_type,
            market_symbol=short_leg.symbol,
            token=token,
        ),
        # 1.5x is not a rejection: the operator has captured real 100%+ moves.
        # It is the point where a reused ticker deserves an explicit question
        # mark unless contract metadata proves identity.
        "mirage_guarded": identity_ratio >= 1.5,
        "identity_warning": identity_ratio >= 1.5,
        "identity_ratio": identity_ratio,
    }
    windows = (
        venue_funding_history.route_windows(row)
        if include_history
        else {"1d": None, "7d": None, "30d": None}
    )
    row["funding_24h_pct"] = windows.get("1d")
    row["settled_funding_windows"] = windows
    row["catalog_history_loaded"] = include_history
    guard = tokenized_assets.classify(row)
    row["tokenized_guard"] = guard
    if guard.get("asset_class") == "tokenized" and guard.get("status") != "verified":
        row["mirage_guarded"] = True
        row["identity_warning"] = True
    return row


def _book_price(leg: Leg, side: str) -> tuple[float, float | None, float | None]:
    levels = leg.book.asks if side == "ask" else leg.book.bids
    top = float(levels[0][0])
    try:
        vwap = depth_weighted_price(
            levels,
            TARGET_NOTIONAL_USD,
            contract_size=leg.contract_size,
        )
    except (TypeError, ValueError):
        vwap = None
    amount = float(levels[0][1]) if len(levels[0]) > 1 else 0.0
    depth = top * amount * leg.contract_size if amount > 0 else None
    return top, float(vwap) if vwap is not None else None, depth


def _funding_leg(
    leg: Leg, funding: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if leg.market_type != "Futures":
        return {"rate_pct": 0.0, "interval_hours": None}
    value = funding.get(f"{leg.venue}|{leg.symbol}")
    return dict(value) if isinstance(value, dict) else {}


def _net_daily(
    long_leg: Leg,
    short_leg: Leg,
    long_funding: dict[str, Any],
    short_funding: dict[str, Any],
) -> float | None:
    # Spot legs contribute zero to a real futures farm, but a Spot-Spot route
    # has no perpetual-funding mechanism at all. Returning 0.0 for that case
    # fabricated a funding measurement and promoted Spot-Spot leaders into the
    # board's Top Funding Pairs lane.
    if long_leg.market_type != "Futures" and short_leg.market_type != "Futures":
        return None
    long_daily = _leg_daily(long_leg, long_funding)
    short_daily = _leg_daily(short_leg, short_funding)
    if long_daily is None or short_daily is None:
        return None
    return short_daily - long_daily


def _leg_daily(leg: Leg, funding: dict[str, Any]) -> float | None:
    if leg.market_type != "Futures":
        return 0.0
    rate = _number(funding.get("rate_pct"))
    interval = _number(funding.get("interval_hours"))
    if rate is None or interval is None or interval <= 0:
        return None
    return rate * 24.0 / interval


def _route_kind(long_leg: Leg, short_leg: Leg) -> str:
    return route_taxonomy.route_kind(
        long_venue=long_leg.venue,
        long_market_type=long_leg.market_type,
        short_venue=short_leg.venue,
        short_market_type=short_leg.market_type,
    )


def _spread_rank(row: dict[str, Any]) -> float:
    matched = _number(row.get("depth_weighted_spread_pct"))
    if matched is not None:
        return matched
    return _number(row.get("executable_spread_pct")) or float("-inf")


def _is_dex(item: dict[str, Any]) -> bool:
    return "dex" in str(item.get("venue") or "").casefold()


def _route_is_dex(row: dict[str, Any]) -> bool:
    return route_taxonomy.route_has_dex(
        long_venue=row.get("long_venue"),
        long_market_type=row.get("long_market_type"),
        short_venue=row.get("short_venue"),
        short_market_type=row.get("short_market_type"),
        source_kind=row.get("source_kind"),
    )


def _token(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "").upper()
        if character.isalnum() or character in "._-"
    )[:32]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _limited(payload: dict[str, Any], limit: int | None) -> dict[str, Any]:
    if limit is None:
        return payload
    result = dict(payload)
    result["routes"] = list(payload.get("routes") or [])[: max(0, int(limit))]
    result["returned_route_count"] = len(result["routes"])
    return result


def _empty(token: str, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "mode": "warm_complete_catalog_pairs",
        "token": token,
        "reason": reason,
        "catalog_market_count": 0,
        "fresh_market_count": 0,
        "missing_book_count": 0,
        "route_count": 0,
        "rejected": {},
        "routes": [],
    }
