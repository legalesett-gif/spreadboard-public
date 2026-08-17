"""User-owned spread positions joined to current public market data."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time
from typing import Any

from spreadboard import (
    accounts,
    api_spreads,
    bulk_quotes,
    chart_catalog,
    live_book_cache,
    market_history,
    portfolio_funding,
    position_markets,
)


PORTFOLIO_INPUT_CACHE_SECONDS = max(
    0.25, float(os.environ.get("SPREADBOARD_PORTFOLIO_INPUT_CACHE_SECONDS", "2"))
)
_PORTFOLIO_INPUT_CACHE_LOCK = threading.Lock()
_PORTFOLIO_INPUT_CACHE: dict[
    str,
    tuple[
        float,
        tuple[
            dict[str, Any],
            dict[str, dict[str, Any]],
            dict[str, Any],
            dict[tuple[str, str, str, str], dict[str, Any]],
            dict[str, Any],
        ],
    ],
] = {}


def portfolio_snapshot(
    user: accounts.User,
    *,
    board_path: Path,
    accounts_path: Path | str = accounts.DEFAULT_DB_PATH,
    evaluate_alerts: bool = True,
) -> dict[str, Any]:
    positions = accounts.list_positions(user.id, db_path=accounts_path)
    if not positions:
        notifications = accounts.list_notifications(user.id, db_path=accounts_path)
        return {
            "ok": True,
            "generated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "user": user.public_dict(),
            "summary": _portfolio_totals([], user.monthly_capital_usd),
            "positions": [],
            "notifications": notifications,
        }
    # A position is an exact saved pair, not a ranked-board row. Rebuilding the
    # entire 25k-route scanner here made the first account request after a
    # cache generation wait almost a minute even though every value below is
    # already available from resident exact-market books, the chart catalogue,
    # public per-leg funding and the encrypted private ledger. An empty ranked
    # row set makes that independence explicit; resolve_position_route still
    # validates both saved symbols against the complete warm catalogue.
    rows: list[dict[str, Any]] = []
    books, funding_legs, catalogue, market_index, funding_snapshot = _market_inputs(
        accounts_path
    )
    hydrated = [
        _hydrate_position(
            item,
            rows,
            books=books,
            funding_legs=funding_legs,
            catalogue=catalogue,
            market_index=market_index,
            funding_snapshot=funding_snapshot,
        )
        for item in positions
    ]
    if evaluate_alerts:
        for item in hydrated:
            _evaluate_position_alerts(user.id, item, accounts_path=accounts_path)
    notifications = accounts.list_notifications(user.id, db_path=accounts_path)
    totals = _portfolio_totals(hydrated, user.monthly_capital_usd)
    # What the money currently at work is earning, kept separate from the
    # monthly figure: one asks how the month went, the other what is deployed.
    totals.update(deployed_capital_summary(hydrated))
    return {
        "ok": True,
        "generated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "user": user.public_dict(),
        "summary": totals,
        "positions": hydrated,
        "notifications": notifications,
    }


def _market_inputs(
    accounts_path: Path | str,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[tuple[str, str, str, str], dict[str, Any]],
    dict[str, Any],
]:
    """Single-flight the large read-only inputs shared by account requests."""

    key = str(Path(accounts_path))
    now = time.monotonic()
    with _PORTFOLIO_INPUT_CACHE_LOCK:
        cached = _PORTFOLIO_INPUT_CACHE.get(key)
        if cached is not None and now - cached[0] <= PORTFOLIO_INPUT_CACHE_SECONDS:
            return cached[1]
        books = _live_books()
        funding_legs = bulk_quotes.load_funding()
        catalogue = chart_catalog.load()
        inputs = (
            books,
            funding_legs,
            catalogue,
            position_markets.catalogue_market_index(catalogue),
            portfolio_funding.load(),
        )
        _PORTFOLIO_INPUT_CACHE[key] = (time.monotonic(), inputs)
        if len(_PORTFOLIO_INPUT_CACHE) > 8:
            oldest = min(
                _PORTFOLIO_INPUT_CACHE,
                key=lambda item: _PORTFOLIO_INPUT_CACHE[item][0],
            )
            if oldest != key:
                _PORTFOLIO_INPUT_CACHE.pop(oldest, None)
        return inputs


def _hydrate_position(
    position: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    books: dict[str, Any] | None = None,
    funding_legs: dict[str, dict[str, Any]] | None = None,
    catalogue: dict[str, Any] | None = None,
    market_index: dict[tuple[str, str, str, str], dict[str, Any]] | None = None,
    funding_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market = position_markets.resolve_position_route(
        position,
        rows,
        catalogue=catalogue,
        market_index=market_index,
    )
    current = market.get("current_row")
    position_quote = None
    if position.get("status") == "open":
        position_quote = _quote_position(
            position,
            books=books or {},
            resolved_market=market,
        )
        current = _merge_position_quotes(
            market.get("canonical_route") or current or _position_route_shell(position),
            current,
            position_quote,
            _history_quote(str(market.get("history_route_key") or "")),
        )
    funding = _position_funding(position, current, funding_legs or {})
    account_marks = portfolio_funding.exact_marks(position, funding_snapshot)
    movement_quote = _mark_to_market_quote(position, position_quote, account_marks)
    long_mark = (
        _number(position.get("long_exit_price"))
        if position.get("status") == "closed"
        else _number(movement_quote.get("long_exit"))
    )
    short_mark = (
        _number(position.get("short_exit_price"))
        if position.get("status") == "closed"
        else _number(movement_quote.get("short_exit"))
    )
    long_entry = _number(position.get("long_entry_price")) or 0.0
    short_entry = _number(position.get("short_entry_price")) or 0.0
    long_quantity = _number(position.get("long_quantity")) or 0.0
    short_quantity = _number(position.get("short_quantity")) or 0.0
    long_pnl = (long_mark - long_entry) * long_quantity if long_mark is not None else None
    short_pnl = (short_entry - short_mark) * short_quantity if short_mark is not None else None
    price_pnl = long_pnl + short_pnl if long_pnl is not None and short_pnl is not None else None
    settled_funding = portfolio_funding.exact_funding(position, funding_snapshot)
    funding_usd = _number(settled_funding.get("amount_usd"))
    fees = (_number(position.get("entry_fees_usd")) or 0.0) + (
        _number(position.get("exit_fees_usd")) or 0.0
    )
    borrow_costs = _number(position.get("borrow_costs_usd")) or 0.0
    gas_costs = _number(position.get("gas_costs_usd")) or 0.0
    transfer_costs = _number(position.get("transfer_costs_usd")) or 0.0
    slippage_costs = _number(position.get("slippage_costs_usd")) or 0.0
    # Actual entry and exit fills already include execution slippage. Retain the
    # measured amount as model-validation evidence, but never deduct it again.
    other_costs = borrow_costs + gas_costs + transfer_costs
    total_costs = fees + other_costs
    total_pnl = (
        price_pnl + funding_usd - total_costs
        if price_pnl is not None and funding_usd is not None and settled_funding.get("known")
        else None
    )
    long_ask = _number(movement_quote.get("long_entry"))
    short_bid = _number(movement_quote.get("short_entry"))
    open_spread = _spread(long_ask, short_bid)
    marked_spread = _spread(long_mark, short_mark)
    listing_status = str(market.get("listing_status") or "unlisted")
    if (
        current
        and (long_mark is not None or short_mark is not None)
        and listing_status == "unlisted"
    ):
        listing_status = "listed"
    if position.get("status") == "closed":
        quote_status = "closed"
    elif long_mark is not None and short_mark is not None:
        quote_status = "live"
    elif long_mark is not None or short_mark is not None:
        quote_status = "partial"
    elif listing_status == "listed":
        quote_status = "refreshing"
    else:
        quote_status = "unavailable"
    _capital = capital_metrics({**position, "total_pnl_usd": total_pnl})
    result = dict(position)
    result.update(
        {
            "market_status": (
                "live"
                if long_mark is not None and short_mark is not None
                else "partial"
                if long_mark is not None or short_mark is not None
                else "unavailable"
            ),
            "market_listing_status": listing_status,
            "quote_status": quote_status,
            "quote_source": movement_quote.get("source"),
            "quote_ts_us": movement_quote.get("quote_ts_us"),
            "quote_age_seconds": _quote_age_seconds(movement_quote),
            "quote_refresh_needed": bool(
                position.get("status") == "open"
                and market.get("canonical_route")
                and quote_status != "live"
            ),
            "canonical_route": market.get("canonical_route"),
            "chart_route_key": market.get("chart_route_key")
            or (current or {}).get("route_key")
            or position.get("route_key"),
            "chart_history_route_key": market.get("history_route_key"),
            "current_route": current,
            "current_funding": funding,
            "current_net_funding_24h_pct": funding.get("net_projected_24h_pct"),
            "long_mark_price": long_mark,
            "short_mark_price": short_mark,
            "long_mark_basis": (
                "stored_exit"
                if position.get("status") == "closed"
                else movement_quote.get("long_basis")
            ),
            "short_mark_basis": (
                "stored_exit"
                if position.get("status") == "closed"
                else movement_quote.get("short_basis")
            ),
            "long_price_pnl_usd": long_pnl,
            "short_price_pnl_usd": short_pnl,
            "price_pnl_usd": price_pnl,
            "funding_income_usd": funding_usd,
            "funding_known": bool(settled_funding.get("known")),
            "funding_source": settled_funding.get("source"),
            "funding_sync_status": settled_funding.get("status"),
            "funding_event_count": settled_funding.get("event_count"),
            "funding_synced_at": settled_funding.get("synced_at"),
            "funding_latest_event_at": settled_funding.get("latest_event_at"),
            "fees_usd": fees,
            "borrow_costs_usd": borrow_costs,
            "gas_costs_usd": gas_costs,
            "transfer_costs_usd": transfer_costs,
            "slippage_costs_usd": slippage_costs,
            "slippage_included_in_fills": True,
            "other_costs_usd": other_costs,
            "total_costs_usd": total_costs,
            "total_pnl_usd": total_pnl,
            "current_open_spread_pct": open_spread,
            # Keep the old response key for existing alert rules, but the value
            # is now the current marked long/short basis -- not a liquidation
            # estimate. New UI/API consumers should use the explicit key.
            "current_exit_spread_pct": marked_spread,
            "current_marked_spread_pct": marked_spread,
            # Measured against both funded legs. This divided by the raw
            # `capital_usd` column, which the position form labels "per leg",
            # so every return on the page read about twice its true size.
            "return_pct": _capital.get("return_on_capital_pct"),
            **_capital,
        }
    )
    return result


def _matching_route(position: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return position_markets.resolve_position_route(position, rows).get("current_row")


def _quote_position(
    position: dict[str, Any],
    *,
    books: dict[str, Any],
    resolved_market: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Fresh bid/ask references for exact saved CEX markets.

    Portfolio accounting is mark-to-market. It must not walk the book for the
    saved quantity because that turns an unrealised PnL number into a
    hypothetical market-order liquidation estimate. The caller uses the
    midpoint only when the operator-side snapshot has no venue mark/fair price.
    """
    row: dict[str, Any] = {
        "route_key": position.get("route_key"),
        "token": position.get("token"),
        "route_kind": _route_kind(position),
        "long_venue": position.get("long_venue"),
        "long_market_type": position.get("long_market_type"),
        "long_market_symbol": position.get("long_symbol"),
        "short_venue": position.get("short_venue"),
        "short_market_type": position.get("short_market_type"),
        "short_market_symbol": position.get("short_symbol"),
    }
    found = False
    for side in ("long", "short"):
        venue = str(position.get(f"{side}_venue") or "")
        market_type = str(position.get(f"{side}_market_type") or "")
        symbol = str(position.get(f"{side}_symbol") or "")
        canonical_type = position_markets.normalize_market_type(venue, market_type)
        candidates = list(dict.fromkeys((market_type, canonical_type)))
        book = next(
            (
                books.get(live_book_cache.cache_key(venue, candidate, symbol))
                for candidate in candidates
                if books.get(live_book_cache.cache_key(venue, candidate, symbol)) is not None
            ),
            None,
        )
        if book is None:
            continue
        bids = list(getattr(book, "bids", None) or [])
        asks = list(getattr(book, "asks", None) or [])
        bid = _best_price(bids)
        ask = _best_price(asks)
        if bid is not None:
            row[f"{side}_bid"] = bid
        if ask is not None:
            row[f"{side}_ask"] = ask
        row[f"{side}_quote_ts_us"] = getattr(book, "quote_ts_us", None)
        found = found or bid is not None or ask is not None
    if found:
        timestamps = [
            int(value)
            for side in ("long", "short")
            if (value := row.get(f"{side}_quote_ts_us")) is not None
        ]
        row["quote_ts_us"] = min(timestamps) if timestamps else None
        row["position_quote_source"] = "resident_book_midpoint"
    return row if found else None


def _best_price(levels: list[list[float]]) -> float | None:
    """First valid positive price from an already side-sorted book."""

    for raw in levels:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        price = _number(raw[0])
        size = _number(raw[1])
        if price is not None and size is not None and price > 0 and size > 0:
            return price
    return None


def _mark_to_market_quote(
    position: dict[str, Any],
    position_quote: dict[str, Any] | None,
    account_marks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Quantity-independent accounting marks for an open position.

    Preference order is an exact-contract/venue reference imported by the
    read-only operator worker, then the midpoint of the exact saved CEX book.
    DEX legs deliberately have no book fallback: an aggregator swap quote is
    executable-route evidence, not a neutral accounting mark.
    """

    result: dict[str, Any] = {}
    sources: list[str] = []
    timestamps: list[int] = []
    for side in ("long", "short"):
        external = account_marks.get(side)
        if external:
            price = _number(external.get("price_usd"))
            if price is not None:
                result[f"{side}_exit"] = price
                result[f"{side}_basis"] = str(
                    external.get("basis") or external.get("source") or "reference_price"
                )
                sources.append(str(external.get("source") or "exact_account_mark"))
                try:
                    stamp = datetime.fromisoformat(
                        str(external.get("quoted_at") or external.get("synced_at") or "").replace(
                            "Z", "+00:00"
                        )
                    )
                    timestamps.append(int(stamp.timestamp() * 1_000_000))
                except ValueError:
                    pass
            continue
        if _is_dex_leg(position, side):
            continue
        bid = _number((position_quote or {}).get(f"{side}_bid"))
        ask = _number((position_quote or {}).get(f"{side}_ask"))
        midpoint = _midpoint(bid, ask)
        if midpoint is not None:
            result[f"{side}_exit"] = midpoint
            result[f"{side}_basis"] = "bid_ask_midpoint"
        if ask is not None:
            result[f"{side}_entry"] = ask
        if bid is not None:
            result[f"{side}_entry_bid"] = bid
        if bid is not None or ask is not None:
            sources.append(
                str(
                    (position_quote or {}).get("position_quote_source")
                    or "resident_book_midpoint"
                )
            )
            stamp = _number((position_quote or {}).get(f"{side}_quote_ts_us"))
            if stamp is not None:
                timestamps.append(int(stamp))
    # A new spread sells the short at its bid; keep the naming explicit.
    result["short_entry"] = result.pop("short_entry_bid", None)
    if timestamps:
        result["quote_ts_us"] = min(timestamps)
    if sources:
        result["source"] = "+".join(dict.fromkeys(sources))
    return result


def _midpoint(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _is_dex_leg(position: dict[str, Any], side: str) -> bool:
    market_type = str(position.get(f"{side}_market_type") or "").casefold()
    venue = str(position.get(f"{side}_venue") or "").casefold()
    return market_type == "dex" or " dex " in f" {venue} "


def _history_quote(route_key: str) -> dict[str, Any] | None:
    if not route_key:
        return None
    max_age = max(
        30.0,
        float(os.environ.get("SPREADBOARD_POSITION_MARK_MAX_AGE_SECONDS", "90")),
    )
    try:
        rows = market_history.load_history(
            route_key=route_key,
            max_points=1,
            since_us=int((time.time() - max_age) * 1_000_000),
        )
    except Exception:  # noqa: BLE001 - a missing history cache is non-fatal.
        return None
    if not rows:
        return None
    item = rows[-1]
    return {
        "route_key": route_key,
        "token": item.get("token"),
        "route_kind": item.get("route_kind"),
        "long_venue": item.get("long_venue"),
        "long_market_type": item.get("long_market_type"),
        "short_venue": item.get("short_venue"),
        "short_market_type": item.get("short_market_type"),
        "long_bid": item.get("long_bid_price"),
        "long_ask": item.get("long_ask_price"),
        "short_bid": item.get("short_bid_price"),
        "short_ask": item.get("short_ask_price"),
        "long_quote_ts_us": item.get("quote_ts_us"),
        "short_quote_ts_us": item.get("quote_ts_us"),
        "quote_ts_us": item.get("quote_ts_us"),
        "position_quote_source": str(item.get("sample_source") or "exact_route_history"),
    }


def _merge_position_quotes(
    structural: dict[str, Any],
    *candidates: dict[str, Any] | None,
) -> dict[str, Any]:
    """Take the freshest exact quote independently for each saved leg."""

    current = next((item for item in candidates if isinstance(item, dict)), {})
    result = {**current, **structural}
    current_notes = current.get("notes") if isinstance(current.get("notes"), dict) else {}
    structural_notes = structural.get("notes") if isinstance(structural.get("notes"), dict) else {}
    if current_notes or structural_notes:
        notes = {**current_notes, **structural_notes}
        current_inputs = (
            current_notes.get("route_inputs")
            if isinstance(current_notes.get("route_inputs"), dict)
            else {}
        )
        structural_inputs = (
            structural_notes.get("route_inputs")
            if isinstance(structural_notes.get("route_inputs"), dict)
            else {}
        )
        notes["route_inputs"] = {
            side: {
                **(current_inputs.get(side) if isinstance(current_inputs.get(side), dict) else {}),
                **(
                    structural_inputs.get(side)
                    if isinstance(structural_inputs.get(side), dict)
                    else {}
                ),
            }
            for side in ("long", "short")
        }
        result["notes"] = notes
    sources: list[str] = []
    timestamps: list[int] = []
    for side in ("long", "short"):
        chosen = None
        chosen_ts = -1
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if (
                _number(candidate.get(f"{side}_bid")) is None
                and _number(candidate.get(f"{side}_ask")) is None
            ):
                continue
            timestamp = int(
                _number(candidate.get(f"{side}_quote_ts_us"))
                or _number(candidate.get("quote_ts_us"))
                or 0
            )
            if chosen is None or timestamp >= chosen_ts:
                chosen = candidate
                chosen_ts = timestamp
        if chosen is None:
            continue
        for field in ("bid", "ask"):
            value = _number(chosen.get(f"{side}_{field}"))
            if value is not None:
                result[f"{side}_{field}"] = value
        result[f"{side}_quote_ts_us"] = chosen_ts or None
        if chosen_ts:
            timestamps.append(chosen_ts)
        source = str(chosen.get("position_quote_source") or "canonical_public_row")
        sources.append(source)
    if timestamps:
        result["quote_ts_us"] = min(timestamps)
    if sources:
        result["position_quote_source"] = "+".join(dict.fromkeys(sources))
    return result


def _position_route_shell(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "route_key": position_markets.normalized_route_key(position),
        "token": position.get("token"),
        "route_kind": _route_kind(position),
        "long_venue": position.get("long_venue"),
        "long_market_type": position_markets.normalize_market_type(
            position.get("long_venue"), position.get("long_market_type")
        ),
        "long_market_symbol": position.get("long_symbol"),
        "short_venue": position.get("short_venue"),
        "short_market_type": position_markets.normalize_market_type(
            position.get("short_venue"), position.get("short_market_type")
        ),
        "short_market_symbol": position.get("short_symbol"),
    }


def _quote_age_seconds(current: dict[str, Any] | None) -> float | None:
    timestamp = _number((current or {}).get("quote_ts_us"))
    if timestamp is None:
        return None
    return max(0.0, time.time() - timestamp / 1_000_000.0)


def _live_books() -> dict[str, Any]:
    try:
        store = live_book_cache.LiveBookStore()
        try:
            return store.load_all(max_age_seconds=api_spreads.LIVE_BOOK_MAX_AGE_SECONDS)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - a missing cache makes marks unavailable, not the account.
        return {}


def _position_funding(
    position: dict[str, Any],
    current: dict[str, Any] | None,
    funding_legs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Current public rates for the exact recorded legs, independent of route rank."""
    legs: dict[str, dict[str, Any]] = {}
    daily: dict[str, float] = {}
    for side in ("long", "short"):
        market_type = str(position.get(f"{side}_market_type") or "")
        venue = str(position.get(f"{side}_venue") or "")
        symbol = str(position.get(f"{side}_symbol") or "")
        if market_type.casefold() != "futures":
            legs[side] = {
                "status": "not_applicable",
                "rate_pct": None,
                "interval_hours": None,
                "projected_24h_pct": 0.0,
            }
            daily[side] = 0.0
            continue
        entry = funding_legs.get(f"{venue}|{symbol}") or {}
        rate = _number(entry.get("rate_pct"))
        interval = _number(entry.get("interval_hours"))
        if rate is None and current:
            rate = _number(
                current.get(f"{side}_current_funding_pct")
                if current.get(f"{side}_current_funding_pct") is not None
                else current.get(f"{side}_funding_pct")
            )
            interval = interval or _number(current.get(f"{side}_funding_interval_hours"))
        projected = (
            rate * (24.0 / interval) if rate is not None and interval and interval > 0 else None
        )
        legs[side] = {
            "status": "live" if projected is not None else "unavailable",
            "rate_pct": rate,
            "interval_hours": interval,
            "projected_24h_pct": projected,
            "next_funding_ts_us": entry.get("next_funding_ts_us"),
        }
        if projected is not None:
            daily[side] = projected
    net = daily.get("short", 0.0) - daily.get("long", 0.0) if len(daily) == 2 else None
    return {
        "long": legs.get("long") or {},
        "short": legs.get("short") or {},
        "net_projected_24h_pct": net,
        "status": "live" if net is not None else "partial" if daily else "unavailable",
    }


def _portfolio_totals(
    positions: list[dict[str, Any]], monthly_capital: float | None
) -> dict[str, Any]:
    open_positions = [item for item in positions if item.get("status") == "open"]
    closed_positions = [item for item in positions if item.get("status") == "closed"]
    known = [
        float(item["total_pnl_usd"]) for item in positions if item.get("total_pnl_usd") is not None
    ]
    funding_total = sum(
        float(item.get("funding_income_usd") or 0.0)
        for item in positions
        if item.get("funding_known")
    )
    funding_unknown = sum(not bool(item.get("funding_known")) for item in positions)
    funding = funding_total if funding_unknown == 0 else None
    open_funding_values = [
        float(item["funding_income_usd"])
        for item in open_positions
        if item.get("funding_income_usd") is not None and item.get("funding_known")
    ]
    open_funding = (
        sum(open_funding_values)
        if len(open_funding_values) == len(open_positions)
        else None
    )
    realized_values = [
        float(item["total_pnl_usd"])
        for item in closed_positions
        if item.get("total_pnl_usd") is not None
    ]
    unrealized_values = [
        float(item["total_pnl_usd"])
        for item in open_positions
        if item.get("total_pnl_usd") is not None
    ]
    realized = sum(realized_values) if len(realized_values) == len(closed_positions) else None
    unrealized = sum(unrealized_values) if len(unrealized_values) == len(open_positions) else None
    total = sum(known) if len(known) == len(positions) else None
    configured_capital = _number(monthly_capital)
    tracked_capital = sum(
        float(item.get("capital_usd") or 0.0)
        for item in positions
        if _number(item.get("capital_usd")) is not None
    )
    capital = (
        configured_capital if configured_capital and configured_capital > 0 else tracked_capital
    )
    return {
        "open_positions": len(open_positions),
        "closed_positions": len(closed_positions),
        "tracked_positions": len(positions),
        "price_and_funding_pnl_usd": total,
        "realized_pnl_usd": realized,
        "unrealized_pnl_usd": unrealized,
        "open_position_pnl_usd": unrealized,
        "funding_income_usd": funding,
        "open_position_funding_usd": open_funding,
        "funding_unknown_positions": funding_unknown,
        "monthly_capital_usd": capital,
        "capital_basis": "configured_monthly"
        if configured_capital and configured_capital > 0
        else "tracked_positions",
        "monthly_return_pct": (
            total / capital * 100.0 if total is not None and capital and capital > 0 else None
        ),
        "open_position_return_pct": (
            unrealized / capital * 100.0
            if unrealized is not None and capital and capital > 0
            else None
        ),
    }


def _evaluate_position_alerts(
    user_id: int, position: dict[str, Any], *, accounts_path: Path | str
) -> int:
    created = 0
    # ``exit_spread_pct`` is the legacy persisted metric name. Its Portfolio
    # meaning is now current marked spread; saved rules continue to work while
    # the UI labels the accounting basis accurately.
    values = {
        "exit_spread_pct": position.get("current_exit_spread_pct"),
        "open_spread_pct": position.get("current_open_spread_pct"),
        "pnl_usd": position.get("total_pnl_usd"),
        "funding_usd": position.get("funding_income_usd"),
    }
    for rule in position.get("alert_rules") or []:
        if not rule.get("enabled"):
            continue
        value = _number(values.get(str(rule.get("metric"))))
        threshold = _number(rule.get("threshold"))
        if value is None or threshold is None:
            continue
        triggered = value <= threshold if rule.get("operator") == "lte" else value >= threshold
        notification = accounts.record_alert_evaluation(
            user_id,
            int(rule["id"]),
            condition_met=triggered,
            title=f"{position.get('token')} position alert",
            body=f"{str(rule.get('metric')).replace('_', ' ')} is {value:.4f}; rule {rule.get('operator')} {threshold:.4f}.",
            db_path=accounts_path,
        )
        created += int(notification is not None)
    return created


class PositionAlertWorker:
    """Continuously evaluate user-owned position rules against current books."""

    def __init__(
        self,
        *,
        board_path: Path,
        accounts_path: Path | str = accounts.DEFAULT_DB_PATH,
        poll_seconds: float = 30.0,
        quote_scheduler: Any = None,
    ) -> None:
        self.board_path = board_path
        self.accounts_path = Path(accounts_path)
        self.poll_seconds = max(10.0, float(poll_seconds))
        self.quote_scheduler = quote_scheduler
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="spreadboard-position-alerts", daemon=True
        )

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        if not self.running:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.running:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        self._stop.wait(10.0)
        while not self._stop.is_set():
            try:
                summary = self.check_once()
                if summary["users"]:
                    print(f"spreadboard-position-alerts: {summary}", flush=True)
            except Exception as exc:  # noqa: BLE001 - market refresh remains independent.
                print(f"spreadboard-position-alerts: {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(self.poll_seconds)

    def check_once(self) -> dict[str, int]:
        # Alert evaluation uses the same exact warm sources as Portfolio. A
        # ranked-board regroup adds no position evidence and can block the
        # worker long enough to miss a threshold crossing.
        rows: list[dict[str, Any]] = []
        books = _live_books()
        funding_legs = bulk_quotes.load_funding()
        catalogue = chart_catalog.load()
        market_index = position_markets.catalogue_market_index(catalogue)
        # Alert evaluation must use the same exact, position-windowed funding
        # ledger and operator reference marks as the authenticated Portfolio
        # page. Without this snapshot, total-PnL/funding rules are withheld and
        # DEX legs can lose their current reference mark in the background.
        funding_snapshot = portfolio_funding.load()
        user_count = position_count = notification_count = 0
        for user_id in accounts.list_alert_user_ids(db_path=self.accounts_path):
            user = accounts.get_user_object(user_id, db_path=self.accounts_path)
            if user is None or not user.subscription_active:
                continue
            user_count += 1
            for raw in accounts.list_positions(user_id, db_path=self.accounts_path):
                if raw.get("status") != "open" or not any(
                    rule.get("enabled") for rule in raw.get("alert_rules") or []
                ):
                    continue
                position_count += 1
                hydrated = _hydrate_position(
                    raw,
                    rows,
                    books=books,
                    funding_legs=funding_legs,
                    catalogue=catalogue,
                    market_index=market_index,
                    funding_snapshot=funding_snapshot,
                )
                if (
                    hydrated.get("quote_refresh_needed")
                    and callable(self.quote_scheduler)
                    and isinstance(hydrated.get("canonical_route"), dict)
                ):
                    self.quote_scheduler(hydrated["canonical_route"])
                notification_count += _evaluate_position_alerts(
                    user_id, hydrated, accounts_path=self.accounts_path
                )
        return {
            "users": user_count,
            "positions": position_count,
            "notifications": notification_count,
        }


def _route_kind(position: dict[str, Any]) -> str:
    types = {
        str(position.get("long_market_type") or ""),
        str(position.get("short_market_type") or ""),
    }
    if types == {"Futures"}:
        return "FUTURES"
    if types == {"Spot"}:
        return "SPOT"
    return "FUTURES-SPOT"


def _spread(denominator: float | None, numerator: float | None) -> float | None:
    return (
        (numerator / denominator - 1.0) * 100.0
        if denominator and numerator and denominator > 0
        else None
    )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def capital_metrics(position: dict[str, Any]) -> dict[str, Any]:
    """Notional controlled, capital committed, and the return on that capital.

    Notional and capital are deliberately distinct. A delta-neutral pair run at
    2x controls twice the notional its capital would suggest, and reporting one
    as the other overstates efficiency exactly where a farm looks most
    attractive.

    The position notional is the larger leg rather than the sum: a hedged pair
    is one position expressed twice, and adding the legs double counts it.
    """
    long_usd = (
        float(position["long_quantity"]) * float(position["long_entry_price"])
        if _number(position.get("long_quantity")) is not None
        and _number(position.get("long_entry_price")) is not None
        else None
    )
    short_usd = (
        float(position["short_quantity"]) * float(position["short_entry_price"])
        if _number(position.get("short_quantity")) is not None
        and _number(position.get("short_entry_price")) is not None
        else None
    )
    sides = [value for value in (long_usd, short_usd) if value is not None]
    # Only the smaller leg is hedged. Whatever the larger one carries beyond it
    # is naked exposure, and calling that "farm size" hides a directional bet.
    matched = min(sides) if sides else None
    unhedged = (max(sides) - min(sides)) if len(sides) == 2 else None

    # The operator's own per-leg figure. Kept, but never used as the position
    # total: the column is labelled "Allocated capital per leg" in the form.
    allocated_per_leg = _number(position.get("capital_usd"))
    allocated_per_leg = (
        allocated_per_leg if allocated_per_leg and allocated_per_leg > 0 else None
    )

    # Both legs are funded, so both legs are capital. Fills are the truth when
    # present; the per-leg allocation doubled is the fallback when they are not.
    if sides:
        committed = sum(sides)
    elif allocated_per_leg is not None:
        committed = allocated_per_leg * 2.0
    else:
        committed = None
    committed = committed if committed and committed > 0 else None

    pnl = _number(position.get("total_pnl_usd"))
    return {
        "long_notional_usd": long_usd,
        "short_notional_usd": short_usd,
        "matched_notional_usd": matched,
        "unhedged_notional_usd": unhedged,
        "capital_committed_usd": committed,
        "allocated_capital_per_leg_usd": allocated_per_leg,
        "return_on_capital_pct": (
            round(pnl / committed * 100.0, 4)
            if pnl is not None and committed is not None
            else None
        ),
    }


def deployed_capital_summary(positions: list[dict[str, Any]]) -> dict[str, Any]:
    """What the money currently at work is earning, across everything open.

    Closed positions returned their capital, so they are excluded: including
    them answers "how did the month go", which the monthly figure already
    covers, rather than "what is deployed right now actually returning".
    """
    open_positions = [
        item for item in positions if str(item.get("status") or "open") == "open"
    ]
    deployed = 0.0
    notional = 0.0
    pnl_total = 0.0
    pnl_known = True
    for item in open_positions:
        metrics = capital_metrics(item)
        deployed += float(metrics["capital_committed_usd"] or 0.0)
        notional += float(metrics["matched_notional_usd"] or 0.0)
        value = _number(item.get("total_pnl_usd"))
        if value is None:
            pnl_known = False
        else:
            pnl_total += float(value)
    return {
        "deployed_capital_usd": round(deployed, 4),
        "deployed_notional_usd": round(notional, 4),
        "open_return_on_capital_pct": (
            round(pnl_total / deployed * 100.0, 4)
            if deployed > 0 and pnl_known
            else None
        ),
    }
