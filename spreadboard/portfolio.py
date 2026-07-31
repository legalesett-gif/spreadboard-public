"""User-owned spread positions joined to current public market data."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any

from spreadboard import accounts, api_spreads
from spreadboard.fast_quotes import FastQuoteRefresher


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
    hydrated = [_hydrate_position(item, rows) for item in positions]
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


def _hydrate_position(position: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    current = _matching_route(position, rows)
    if current is None and position.get("status") == "open":
        current = _quote_position(position)
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
    result = dict(position)
    result.update(
        {
            "market_status": "live" if current else "unavailable",
            "current_route": current,
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
    route_key = str(position.get("route_key") or "")
    exact = next((row for row in rows if str(row.get("route_key") or "") == route_key), None)
    if exact:
        return exact
    return next(
        (
            row
            for row in rows
            if str(row.get("token") or "").upper() == str(position.get("token") or "").upper()
            and str(row.get("long_venue") or "") == str(position.get("long_venue") or "")
            and str(row.get("long_market_type") or "") == str(position.get("long_market_type") or "")
            and str(row.get("short_venue") or "") == str(position.get("short_venue") or "")
            and str(row.get("short_market_type") or "") == str(position.get("short_market_type") or "")
        ),
        None,
    )


def _quote_position(position: dict[str, Any]) -> dict[str, Any] | None:
    row = {
        "route_key": position.get("route_key"),
        "token": position.get("token"),
        "route_kind": _route_kind(position),
        "long_venue": position.get("long_venue"),
        "long_market_type": position.get("long_market_type"),
        "long_market_symbol": position.get("long_symbol"),
        "short_venue": position.get("short_venue"),
        "short_market_type": position.get("short_market_type"),
        "short_market_symbol": position.get("short_symbol"),
        "notes": {
            "route_inputs": {
                "long": {"symbol": position.get("long_symbol")},
                "short": {"symbol": position.get("short_symbol")},
            }
        },
    }
    refresher = FastQuoteRefresher()
    try:
        result = refresher.quote_route(row, target_notional_usd=50.0)
    finally:
        refresher.close()
    quoted = result.get("row") if result.get("status") == "ok" else None
    if not isinstance(quoted, dict):
        return None
    route_inputs = ((quoted.get("notes") or {}).get("route_inputs") or {})
    long_leg = route_inputs.get("long") or {}
    short_leg = route_inputs.get("short") or {}
    quoted.update(
        {
            "long_bid": long_leg.get("bid"),
            "long_ask": long_leg.get("ask"),
            "short_bid": short_leg.get("bid"),
            "short_ask": short_leg.get("ask"),
        }
    )
    return quoted


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
    ) -> None:
        self.board_path = board_path
        self.accounts_path = Path(accounts_path)
        self.poll_seconds = max(10.0, float(poll_seconds))
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
                hydrated = _hydrate_position(raw, rows)
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
