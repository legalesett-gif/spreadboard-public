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
    probe_notional,
    public_rails,
    route_taxonomy,
    tokenized_assets,
    venue_funding_history,
)


MAX_PRICE_RATIO = 3.0
TARGET_NOTIONAL_USD = probe_notional.TARGET_NOTIONAL_USD
MAX_BOOK_AGE_SECONDS = max(
    30.0, float(os.environ.get("SPREADBOARD_CATALOG_BOOK_AGE_SECONDS", "180"))
)
CACHE_SECONDS = max(0.25, float(os.environ.get("SPREADBOARD_CATALOG_PAIR_CACHE_SECONDS", "2")))
LEVERAGED_TOKEN_PATTERN = re.compile(r"^[A-Z0-9]+[2-5][LS]$")

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, int, bool], tuple[float, dict[str, Any]]] = {}


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
    include_short_spot: bool = False,
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
    cache_key = (symbol, int(max_age_seconds), bool(include_short_spot))
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
        and not _is_onchain_spot(item)
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
            for long_leg, short_leg in _directions(
                left, right, include_short_spot=include_short_spot
            ):
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
    include_short_spot: bool = False,
    admissible_spreads_only: bool = False,
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
        if not isinstance(item, dict) or _is_onchain_spot(item):
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
            include_short_spot=include_short_spot,
        )
        if admissible_spreads_only:
            # The global route index needs every current positive pair, not
            # every negative mirror direction. Filtering inside this per-token
            # loop avoids retaining 100k+ temporary route dictionaries while
            # preserving the uncapped exact-token builder used by detail pages.
            routes = [
                row
                for row in payload.get("routes") or []
                if api_spreads.spread_evidence_state(row)
                in {"verified", "research"}
            ]
            payload = {
                **payload,
                "ok": bool(routes),
                "route_count": len(routes),
                "displayed_route_count": len(routes),
                "routes": routes,
                "admissible_spreads_only": True,
            }
        output[token] = _limited(payload, limit_per_token)
    return output


def dex_futures_routes(
    dex_routes: list[dict[str, Any]],
    *,
    books: dict[str, live_book_cache.CachedBook],
    include_history: bool = True,
) -> list[dict[str, Any]]:
    """Recombine each current exact OKX DEX quote with every fresh future.

    The provider quote is directional and reusable for its exact chain and
    contract. Pairing only the scanner row which happened to request it wastes
    that quote and hides the other CEX futures venues. This expansion performs
    no network request: it combines the already-paid $500 DEX VWAP with the
    shared current futures-book catalogue and live funding cache.
    """

    current_sources: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for candidate in dex_routes:
        if not isinstance(candidate, dict):
            continue
        if not api_spreads.matched_probe_verified(candidate):
            continue
        dex_side = next(
            (
                side
                for side in ("long", "short")
                if route_taxonomy.leg_is_dex(
                    venue=candidate.get(f"{side}_venue"),
                    market_type=candidate.get(f"{side}_market_type"),
                )
            ),
            "",
        )
        token = _token(candidate.get("token"))
        chain, contract = _dex_identity(candidate, dex_side)
        dex_input = _route_input(candidate, dex_side)
        dex_quote_ts_us = int(
            _number(dex_input.get("quote_ts_us"))
            or _number(candidate.get("dex_quote_ts_us"))
            or 0
        )
        blockers = [str(item) for item in (candidate.get("blockers") or [])]
        tokenized_unverified = (
            str(candidate.get("asset_class") or "crypto") == "tokenized"
            and candidate.get("tokenized_identity_verified") is not True
        )
        if (
            not token
            or not dex_side
            or not chain
            or not contract
            or not dex_quote_ts_us
            or candidate.get("mirage_guarded")
            or any(item.startswith("mirage_guard:") for item in blockers)
            or tokenized_unverified
            or candidate.get("dex_expansion_verified") is False
        ):
            continue
        if not api_spreads.spread_quote_current({"quote_ts_us": dex_quote_ts_us}):
            continue
        identity = (token, chain, contract, dex_side)
        current = current_sources.get(identity)
        current_ts = int((current or {}).get("_dex_quote_ts_us") or 0)
        if current is None or dex_quote_ts_us >= current_ts:
            current_sources[identity] = {
                **candidate,
                "_dex_chain": chain,
                "_dex_contract": contract,
                "_dex_side": dex_side,
                "_dex_quote_ts_us": dex_quote_ts_us,
                "_dex_input": dex_input,
            }
    if not current_sources:
        return []

    wanted = {identity[0] for identity in current_sources}
    catalog = chart_catalog.load()
    futures_by_token: dict[str, list[Leg]] = {}
    seen_markets: set[tuple[str, str, str]] = set()
    for item in catalog.get("markets") or []:
        if not isinstance(item, dict) or str(item.get("market_type") or "") != "Futures":
            continue
        token = _token(item.get("token"))
        venue = str(item.get("venue") or "")
        symbol = str(item.get("symbol") or "")
        identity = (venue, "Futures", symbol)
        if token not in wanted or not venue or not symbol or identity in seen_markets:
            continue
        book = books.get(live_book_cache.cache_key(*identity))
        if book is None:
            continue
        seen_markets.add(identity)
        try:
            contract_size = float(item.get("contract_size") or 1.0)
        except (TypeError, ValueError):
            contract_size = 1.0
        futures_by_token.setdefault(token, []).append(
            Leg(
                token=token,
                venue=venue,
                market_type="Futures",
                symbol=symbol,
                quote=str(item.get("quote") or "").upper(),
                contract_size=contract_size if contract_size > 0 else 1.0,
                book=book,
            )
        )

    funding = bulk_quotes.load_funding()
    rails = public_rails.load_public_rails()
    expanded: dict[tuple[Any, ...], dict[str, Any]] = {}
    for source in current_sources.values():
        token = _token(source.get("token"))
        dex_side = str(source["_dex_side"])
        dex_input = source["_dex_input"]
        price = _number(
            dex_input.get("ask_vwap" if dex_side == "long" else "bid_vwap")
        ) or _number(
            source.get("dex_ask_vwap" if dex_side == "long" else "dex_bid_vwap")
        )
        quote_ts_us = int(source["_dex_quote_ts_us"])
        if price is None or price <= 0 or quote_ts_us <= 0:
            continue
        amount = TARGET_NOTIONAL_USD / price
        dex_leg = Leg(
            token=token,
            venue=str(source.get(f"{dex_side}_venue") or ""),
            market_type="Spot",
            symbol=str(
                dex_input.get("symbol")
                or source.get(f"{dex_side}_market_symbol")
                or source["_dex_contract"]
            ),
            quote=str(
                dex_input.get("quote")
                or source.get(f"{dex_side}_quote")
                or "USDT"
            ).upper(),
            contract_size=1.0,
            book=live_book_cache.CachedBook(
                bids=[[price, amount]],
                asks=[[price, amount]],
                quote_ts_us=quote_ts_us,
                source="okx_dex_exact_matched_quote",
            ),
        )
        for future in futures_by_token.get(token) or []:
            long_leg, short_leg = (
                (dex_leg, future) if dex_side == "long" else (future, dex_leg)
            )
            if _reject_reason(token, long_leg, short_leg, rails):
                continue
            row = _route(
                token,
                long_leg,
                short_leg,
                funding,
                rails,
                include_history=include_history,
            )
            source_blockers = [str(item) for item in (source.get("blockers") or [])]
            route_blockers = [str(item) for item in (row.get("blockers") or [])]
            blockers = list(dict.fromkeys([*source_blockers, *route_blockers]))
            quote_source = dex_input.get("quote_source") or source.get("dex_quote_source")
            route_plan = dex_input.get("route_plan") or source.get("dex_route_plan") or ()
            row.update(
                {
                    "route_kind": "DEX-FUTURES",
                    "source_name": "Complete OKX DEX quote x futures catalogue",
                    "source_kind": "dex_discovered",
                    "raw_source_kind": source.get("raw_source_kind") or "dex_discovered",
                    "dex_chain": source["_dex_chain"],
                    "dex_contract": source["_dex_contract"],
                    "identity_key": _dex_identity_key(
                        source["_dex_chain"], source["_dex_contract"]
                    ),
                    "dex_quote_ts_us": quote_ts_us,
                    "matched_size_notional_usd": TARGET_NOTIONAL_USD,
                    "target_notional_usd": TARGET_NOTIONAL_USD,
                    "liquidity_evidence_kind": "matched_size_vwap",
                    "dex_gas_estimate_usd": dex_input.get("gas_estimate_usd")
                    or source.get("dex_gas_estimate_usd"),
                    "dex_slippage_bps": dex_input.get("slippage_bps")
                    or source.get("dex_slippage_bps"),
                    "dex_price_impact_pct": dex_input.get("price_impact_pct")
                    or source.get("dex_price_impact_pct"),
                    "dex_quote_source": quote_source,
                    "dex_mev_protection": dex_input.get("mev_protection")
                    or source.get("dex_mev_protection"),
                    "dex_transfer_time_seconds": dex_input.get("transfer_time_seconds")
                    or source.get("dex_transfer_time_seconds"),
                    "dex_route_plan": tuple(route_plan),
                    "asset_class": source.get("asset_class") or "crypto",
                    "token_name": source.get("token_name"),
                    "market_cap_usd": source.get("market_cap_usd"),
                    "fdv_usd": source.get("fdv_usd"),
                    "metadata_volume_24h_usd": source.get("metadata_volume_24h_usd"),
                    "listing_age_days": source.get("listing_age_days"),
                    "listing_age_source": source.get("listing_age_source"),
                    "blockers": blockers,
                    "mirage_guarded": bool(source.get("mirage_guarded"))
                    or any(item.startswith("mirage_guard:") for item in blockers),
                }
            )
            # Restore the provider leg's public identity after _route used Spot
            # internally to calculate cash-and-carry funding mechanics.
            for suffix in (
                "venue",
                "market_type",
                "market_symbol",
                "quote",
                "exchange_url",
                "deposit_enabled",
                "withdraw_enabled",
                "volume_24h_usd",
            ):
                value = source.get(f"{dex_side}_{suffix}")
                if value is not None:
                    row[f"{dex_side}_{suffix}"] = value
            for suffix in ("bid", "ask", "bid_vwap", "ask_vwap"):
                value = dex_input.get(suffix)
                if value is not None:
                    row[f"{dex_side}_{suffix}"] = value
            gas = _number(row.get("dex_gas_estimate_usd"))
            matched = _number(row.get("depth_weighted_spread_pct"))
            row["gas_adjusted_spread_pct"] = (
                matched - gas / TARGET_NOTIONAL_USD * 100.0
                if matched is not None and gas is not None
                else matched
            )
            expanded[route_identity(row)] = row
    return list(expanded.values())


def _route_input(row: dict[str, Any], side: str) -> dict[str, Any]:
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    nested = inputs.get(side) if isinstance(inputs.get(side), dict) else {}
    return dict(nested)


def _dex_identity(row: dict[str, Any], side: str) -> tuple[str, str]:
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    identities = notes.get("identity") if isinstance(notes.get("identity"), dict) else {}
    identity = identities.get(side) if isinstance(identities.get(side), dict) else {}
    chain = str(identity.get("chain_id") or row.get("dex_chain") or "").strip()
    contract = str(
        identity.get("token_address") or row.get("dex_contract") or ""
    ).strip().casefold()
    return chain, contract


def _dex_identity_key(chain: str, contract: str) -> str:
    return (
        f"solana:501/token:{contract}"
        if str(chain) == "501"
        else f"eip155:{chain}/erc20:{contract.casefold()}"
    )


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
    include_short_spot: bool = False,
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
            for long_leg, short_leg in _directions(
                left, right, include_short_spot=include_short_spot
            ):
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
    research_view = str(payload.get("evidence_view") or "") == "research"
    current_spread_routes = [
        row
        for row in routes
        if api_spreads.spread_quote_current(row)
        and (research_view or not row.get("mirage_guarded"))
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
    combined = [*(payload.get("routes") or []), *extra_routes]
    structural_counts: dict[tuple[Any, ...], int] = {}
    for candidate in combined:
        if isinstance(candidate, dict):
            identity = _structural_route_identity(candidate)
            structural_counts[identity] = structural_counts.get(identity, 0) + 1
    structural_seen: dict[tuple[Any, ...], int] = {}
    for row in combined:
        if not isinstance(row, dict):
            continue
        identity = route_identity(row)
        route_index = seen.get(identity)
        structural_identity = _structural_route_identity(row)
        # Some discovery adapters omit exact symbols while the warm catalogue
        # has them. Merge only when this venue/type shape is unique across the
        # combined evidence; otherwise two genuine contracts could collapse.
        if route_index is None and structural_counts.get(structural_identity) == 2:
            route_index = structural_seen.get(structural_identity)
        if route_index is not None:
            existing = routes[route_index]
            # The warm catalogue owns the current exact-book economics. The
            # bounded scanner can still carry evidence the catalogue does not,
            # notably settled 1d/7d/30d windows and provider metadata. Fill
            # only missing fields so that evidence survives without replacing
            # the fresher matched quote or the canonical chart route.
            for key, value in row.items():
                if key not in existing or existing.get(key) is None:
                    existing[key] = value
            _merge_conservative_route_evidence(existing, row)
            continue
        seen[identity] = len(routes)
        structural_seen[structural_identity] = len(routes)
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


def _structural_route_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    """Symbol-free fallback used only when one route shape is unambiguous."""

    return (
        str(row.get("token") or "").upper(),
        str(row.get("route_kind") or "").upper(),
        str(row.get("long_venue") or "").casefold(),
        str(row.get("long_market_type") or "").casefold(),
        str(row.get("short_venue") or "").casefold(),
        str(row.get("short_market_type") or "").casefold(),
    )


def _merge_conservative_route_evidence(
    target: dict[str, Any], source: dict[str, Any]
) -> None:
    """Keep stronger safety evidence without replacing fresher economics."""

    target_blockers = [str(item) for item in (target.get("blockers") or [])]
    source_blockers = [str(item) for item in (source.get("blockers") or [])]
    # A current matched catalogue book supersedes an older missing-depth flag,
    # but identity and execution warnings remain relevant until disproved.
    if api_spreads.matched_probe_verified(target):
        source_blockers = [item for item in source_blockers if item != "depth_unverified"]
    blockers = list(dict.fromkeys([*target_blockers, *source_blockers]))
    if blockers:
        target["blockers"] = blockers
    for key in ("mirage_guarded", "identity_warning", "identity_mismatch", "thin_book"):
        if source.get(key) is True:
            target[key] = True
    if source.get("deliverable") is False:
        target["deliverable"] = False
    source_guard = source.get("tokenized_guard")
    target_guard = target.get("tokenized_guard")
    if (
        isinstance(source_guard, dict)
        and source_guard.get("rankable") is False
        and not (isinstance(target_guard, dict) and target_guard.get("rankable") is False)
    ):
        target["tokenized_guard"] = dict(source_guard)


def route_identity(row: dict[str, Any]) -> tuple[Any, ...]:
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
        str(row.get("route_kind") or "").upper(),
        leg("long"),
        leg("short"),
    )


# Compatibility for callers/tests which used the old private spelling. New
# funding catalogues use the public name so every lane deduplicates identically.
_merge_route_identity = route_identity


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
        if not isinstance(item, dict) or _is_onchain_spot(item):
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


def _directions(
    left: Leg,
    right: Leg,
    *,
    include_short_spot: bool = False,
) -> list[tuple[Leg, Leg]]:
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
    if include_short_spot:
        return [(spot, future), (future, spot)]
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
    requires_existing_spot_inventory = (
        long_leg.market_type == "Futures" and short_leg.market_type == "Spot"
    )
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
        "requires_existing_spot_inventory": requires_existing_spot_inventory,
        "execution_note": (
            "Short spot inventory or borrow is required."
            if requires_existing_spot_inventory
            else None
        ),
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


def _is_onchain_spot(item: dict[str, Any]) -> bool:
    """Keep provider-quoted on-chain spot legs out of the order-book catalogue.

    Aster, Hyperliquid and Lighter have ordinary futures order books and belong
    in this complete pair builder. Contract-address spot swaps remain provider
    quoted and merge in later; only OKX DEX gets the product DEX label.
    """

    return route_taxonomy.leg_is_onchain_spot(
        venue=item.get("venue"), market_type=item.get("market_type")
    )


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
