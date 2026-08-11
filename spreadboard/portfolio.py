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
    position_markets,
)


def portfolio_snapshot(
    user: accounts.User,
    *,
    board_path: Path,
    accounts_path: Path | str = accounts.DEFAULT_DB_PATH,
    evaluate_alerts: bool = True,
) -> dict[str, Any]:
    positions = accounts.list_positions(user.id, db_path=accounts_path)
    market = api_spreads.load_spreads(
        board_path=board_path,
        include_stale=False,
        include_unverified=False,
        limit=None,
    )
    rows = [row for row in market.get("rows") or [] if isinstance(row, dict)]
    books = _live_books()
    funding_legs = bulk_quotes.load_funding()
    catalogue = chart_catalog.load()
    market_index = position_markets.catalogue_market_index(catalogue)
    hydrated = [
        _hydrate_position(
            item,
            rows,
            books=books,
            funding_legs=funding_legs,
            catalogue=catalogue,
            market_index=market_index,
        )
        for item in positions
    ]
    if evaluate_alerts:
        for item in hydrated:
            _evaluate_position_alerts(user.id, item, accounts_path=accounts_path)
    notifications = accounts.list_notifications(user.id, db_path=accounts_path)
    totals = _portfolio_totals(hydrated, user.monthly_capital_usd)
    return {
        "ok": True,
        "generated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "user": user.public_dict(),
        "summary": totals,
        "positions": hydrated,
        "notifications": notifications,
    }


def _hydrate_position(
    position: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    books: dict[str, Any] | None = None,
    funding_legs: dict[str, dict[str, Any]] | None = None,
    catalogue: dict[str, Any] | None = None,
    market_index: dict[tuple[str, str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    market = position_markets.resolve_position_route(
        position,
        rows,
        catalogue=catalogue,
        market_index=market_index,
    )
    current = market.get("current_row")
    if position.get("status") == "open":
        current = _merge_position_quotes(
            market.get("canonical_route") or current or _position_route_shell(position),
            current,
            _quote_position(position, books=books or {}),
            _history_quote(str(market.get("history_route_key") or "")),
        )
    funding = _position_funding(position, current, funding_legs or {})
    long_mark = _number(position.get("long_exit_price")) if position.get("status") == "closed" else _number((current or {}).get("long_bid"))
    short_mark = _number(position.get("short_exit_price")) if position.get("status") == "closed" else _number((current or {}).get("short_ask"))
    long_entry = _number(position.get("long_entry_price")) or 0.0
    short_entry = _number(position.get("short_entry_price")) or 0.0
    long_quantity = _number(position.get("long_quantity")) or 0.0
    short_quantity = _number(position.get("short_quantity")) or 0.0
    long_pnl = (long_mark - long_entry) * long_quantity if long_mark is not None else None
    short_pnl = (short_entry - short_mark) * short_quantity if short_mark is not None else None
    price_pnl = long_pnl + short_pnl if long_pnl is not None and short_pnl is not None else None
    funding_usd = sum(_number(item.get("amount_usd")) or 0.0 for item in position.get("funding_cashflows") or [])
    fees = (_number(position.get("entry_fees_usd")) or 0.0) + (_number(position.get("exit_fees_usd")) or 0.0)
    total_pnl = price_pnl + funding_usd - fees if price_pnl is not None else None
    long_ask = _number((current or {}).get("long_ask"))
    short_bid = _number((current or {}).get("short_bid"))
    open_spread = _spread(long_ask, short_bid)
    exit_spread = _spread(_number((current or {}).get("short_ask")), _number((current or {}).get("long_bid")))
    listing_status = str(market.get("listing_status") or "unlisted")
    if current and (long_mark is not None or short_mark is not None) and listing_status == "unlisted":
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
            "quote_source": (current or {}).get("position_quote_source"),
            "quote_ts_us": (current or {}).get("quote_ts_us"),
            "quote_age_seconds": _quote_age_seconds(current),
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
            "long_price_pnl_usd": long_pnl,
            "short_price_pnl_usd": short_pnl,
            "price_pnl_usd": price_pnl,
            "funding_income_usd": funding_usd,
            "fees_usd": fees,
            "total_pnl_usd": total_pnl,
            "current_open_spread_pct": open_spread,
            "current_exit_spread_pct": exit_spread,
            "return_pct": (
                total_pnl / float(position["capital_usd"]) * 100.0
                if total_pnl is not None and _number(position.get("capital_usd"))
                else None
            ),
        }
    )
    return result


def _matching_route(position: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return position_markets.resolve_position_route(position, rows).get("current_row")


def _quote_position(
    position: dict[str, Any], *, books: dict[str, Any]
) -> dict[str, Any] | None:
    """Mark a manual route from the resident cache, never a page-time API call."""
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
        if bids:
            row[f"{side}_bid"] = _number(bids[0][0])
        if asks:
            row[f"{side}_ask"] = _number(asks[0][0])
        row[f"{side}_quote_ts_us"] = getattr(book, "quote_ts_us", None)
        found = True
    if found:
        timestamps = [
            int(value)
            for side in ("long", "short")
            if (value := row.get(f"{side}_quote_ts_us")) is not None
        ]
        row["quote_ts_us"] = min(timestamps) if timestamps else None
        row["position_quote_source"] = "resident_websocket_book"
    return row if found else None


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
    structural_notes = (
        structural.get("notes") if isinstance(structural.get("notes"), dict) else {}
    )
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
                **(
                    current_inputs.get(side)
                    if isinstance(current_inputs.get(side), dict)
                    else {}
                ),
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
            if _number(candidate.get(f"{side}_bid")) is None and _number(
                candidate.get(f"{side}_ask")
            ) is None:
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
        projected = rate * (24.0 / interval) if rate is not None and interval and interval > 0 else None
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


def _portfolio_totals(positions: list[dict[str, Any]], monthly_capital: float | None) -> dict[str, Any]:
    open_positions = [item for item in positions if item.get("status") == "open"]
    closed_positions = [item for item in positions if item.get("status") == "closed"]
    known = [float(item["total_pnl_usd"]) for item in positions if item.get("total_pnl_usd") is not None]
    funding = sum(float(item.get("funding_income_usd") or 0.0) for item in positions)
    realized = sum(float(item.get("total_pnl_usd") or 0.0) for item in closed_positions)
    unrealized = sum(float(item.get("total_pnl_usd") or 0.0) for item in open_positions if item.get("total_pnl_usd") is not None)
    total = sum(known)
    configured_capital = _number(monthly_capital)
    tracked_capital = sum(
        float(item.get("capital_usd") or 0.0)
        for item in positions
        if _number(item.get("capital_usd")) is not None
    )
    capital = configured_capital if configured_capital and configured_capital > 0 else tracked_capital
    return {
        "open_positions": len(open_positions),
        "closed_positions": len(closed_positions),
        "tracked_positions": len(positions),
        "price_and_funding_pnl_usd": total,
        "realized_pnl_usd": realized,
        "unrealized_pnl_usd": unrealized,
        "funding_income_usd": funding,
        "monthly_capital_usd": capital,
        "capital_basis": "configured_monthly" if configured_capital and configured_capital > 0 else "tracked_positions",
        "monthly_return_pct": total / capital * 100.0 if capital and capital > 0 else None,
    }


def _evaluate_position_alerts(user_id: int, position: dict[str, Any], *, accounts_path: Path | str) -> int:
    created = 0
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
        market = api_spreads.load_spreads(
            board_path=self.board_path,
            include_stale=False,
            include_unverified=False,
            limit=None,
        )
        rows = [row for row in market.get("rows") or [] if isinstance(row, dict)]
        books = _live_books()
        funding_legs = bulk_quotes.load_funding()
        catalogue = chart_catalog.load()
        market_index = position_markets.catalogue_market_index(catalogue)
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
        return {"users": user_count, "positions": position_count, "notifications": notification_count}


def _route_kind(position: dict[str, Any]) -> str:
    types = {str(position.get("long_market_type") or ""), str(position.get("short_market_type") or "")}
    if types == {"Futures"}:
        return "FUTURES"
    if types == {"Spot"}:
        return "SPOT"
    return "FUTURES-SPOT"


def _spread(denominator: float | None, numerator: float | None) -> float | None:
    return (numerator / denominator - 1.0) * 100.0 if denominator and numerator and denominator > 0 else None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
