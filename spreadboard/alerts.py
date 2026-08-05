"""Disabled-by-default Pushover alerts for SpreadBoard."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from spreadboard import accounts, api_spreads, board

CONFIG_PATH = Path(__file__).with_name("config.json")
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "telegram_channel_url": "",
    "pushover_app_token": "",
    "pushover_users": [],
    "premium_users": [],
    "alerts_enabled": False,
    "alert_min_spread_pct": 8,
    "website_storage_state_path": "",
    "premium_storage_state_path": "",
    "okx_dex_quotes_enabled": True,
    "okx_dex_quote_notional_usd": 30,
}


def load_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except json.JSONDecodeError:
        cfg["config_error"] = "config.json is not valid JSON"
        return cfg
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["pushover_users"] = [
        user
        for user in [*(cfg.get("pushover_users") or []), *(cfg.get("premium_users") or [])]
        if isinstance(user, dict) and (user.get("name") or user.get("pushover_user_key"))
    ]
    cfg["premium_users"] = cfg["pushover_users"]
    return cfg


def config_flags(cfg: dict[str, Any]) -> dict[str, Any]:
    users = _pushover_users(cfg)
    website_session_path = cfg.get("website_storage_state_path") or cfg.get("premium_storage_state_path")
    return {
        "alerts_enabled": bool(cfg.get("alerts_enabled")),
        "alert_min_spread_pct": _float_or_default(cfg.get("alert_min_spread_pct"), 8.0),
        "telegram_channel_configured": bool(str(cfg.get("telegram_channel_url") or "").strip()),
        "pushover_configured": bool(str(cfg.get("pushover_app_token") or "").strip()),
        "pushover_user_count": len(users),
        "premium_user_count": len(users),
        "website_storage_state_configured": bool(str(website_session_path or "").strip()),
        "premium_storage_state_configured": bool(str(website_session_path or "").strip()),
        "okx_dex_quotes_enabled": bool(cfg.get("okx_dex_quotes_enabled", True)),
        "config_error": cfg.get("config_error"),
    }


def send_pushover_message(
    *,
    app_token: str,
    user_key: str,
    title: str,
    message: str,
    url: str | None = None,
    device: str | None = None,
    sound: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    payload = {
        "token": app_token,
        "user": user_key,
        "title": title,
        "message": message,
    }
    if url:
        payload["url"] = url
    if device:
        payload["device"] = device
    if sound:
        payload["sound"] = sound
    body = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        PUSHOVER_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            parsed = _json_or_text(text)
            ok = response.status == 200 and isinstance(parsed, dict) and parsed.get("status") == 1
            return {"ok": ok, "status": response.status, "response": parsed}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "response": _json_or_text(text)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "error": str(exc)}


def send_test_alerts(cfg: dict[str, Any]) -> dict[str, Any]:
    token = str(cfg.get("pushover_app_token") or "").strip()
    users = _pushover_users(cfg)
    if not token:
        return {"ok": False, "error": "pushover_app_token is not configured", "results": []}
    results = []
    for user in users:
        name = str(user.get("name") or "Pushover recipient")
        result = send_pushover_message(
            app_token=token,
            user_key=str(user.get("pushover_user_key")),
            title="SpreadBoard test alert",
            message="This is a test alert from your local SpreadBoard app.",
        )
        results.append({"name": name, "ok": bool(result.get("ok")), "detail": _public_detail(result)})
    return {"ok": all(item["ok"] for item in results) if results else False, "results": results}


class AlertWatcher:
    """Poll the board and alert when a symbol newly crosses the configured threshold."""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        board_path: Path | str = board.DEFAULT_BOARD_PATH,
        poll_seconds: float = 60.0,
    ) -> None:
        self.cfg = cfg
        self.board_path = board_path
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_above: set[str] | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._thread = threading.Thread(target=self._run, name="spreadboard-alerts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.check_once()
            self._stop.wait(self.poll_seconds)

    def check_once(self) -> list[dict[str, Any]]:
        if not self.cfg.get("alerts_enabled"):
            return []
        threshold = _float_or_default(self.cfg.get("alert_min_spread_pct"), 8.0)
        snapshot = board.load_board(self.board_path)
        above = {row.symbol for row in snapshot.rows if row.spread_pct >= threshold}
        if self._previous_above is None:
            self._previous_above = above
            return []
        new_symbols = sorted(above - self._previous_above)
        self._previous_above = above
        if not new_symbols:
            return []
        token = str(self.cfg.get("pushover_app_token") or "").strip()
        users = _pushover_users(self.cfg)
        if not token or not users:
            return []
        by_symbol = {row.symbol: row for row in snapshot.rows}
        results: list[dict[str, Any]] = []
        for symbol in new_symbols:
            row = by_symbol.get(symbol)
            if row is None:
                continue
            message = (
                f"{row.symbol} crossed {threshold:.1f}% on SpreadBoard: "
                f"{row.spread_pct:.1f}% by buying on {row.long_venue or '?'} "
                f"and selling on {row.short_venue or '?'}."
            )
            for user in users:
                result = send_pushover_message(
                    app_token=token,
                    user_key=str(user.get("pushover_user_key")),
                    title="New SpreadBoard opportunity",
                    message=message,
                )
                results.append(
                    {
                        "symbol": symbol,
                        "name": str(user.get("name") or "Pushover recipient"),
                        "ok": bool(result.get("ok")),
                        "detail": _public_detail(result),
                    }
                )
        return results


class UserMarketAlertWorker:
    """Evaluate authenticated route rules and deliver crossing notifications."""

    def __init__(
        self,
        *,
        board_path: Path | str = board.DEFAULT_BOARD_PATH,
        accounts_path: Path | str = accounts.DEFAULT_DB_PATH,
        poll_seconds: float = 10.0,
    ) -> None:
        self.board_path = Path(board_path)
        self.accounts_path = Path(accounts_path)
        self.poll_seconds = max(5.0, float(poll_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="spreadboard-market-alerts", daemon=True)

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
        self._stop.wait(5.0)
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001
                print(f"spreadboard-market-alerts: {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(self.poll_seconds)

    def check_once(self) -> dict[str, int]:
        # Ask who is waiting before building anything. This runs every ten
        # seconds and `limit=None` materialises the entire board -- every row of
        # every token, the largest payload the process ever holds, on its own
        # cache key. Doing that for an empty rule table was several hundred
        # megabytes rebuilt six times a minute to deliver nothing.
        user_ids = accounts.list_market_alert_user_ids(db_path=self.accounts_path)
        if not user_ids:
            return {"evaluated": 0, "triggered": 0, "delivered": 0}
        market = api_spreads.load_spreads(
            board_path=self.board_path,
            include_stale=False,
            include_unverified=False,
            limit=None,
        )
        board_rows = [
            row for row in market.get("rows") or []
            if isinstance(row, dict) and row.get("route_key")
        ]
        rows = {str(row["route_key"]): row for row in board_rows}
        tokens = token_metrics(board_rows)
        evaluated = triggered = delivered = 0
        app_token = os.environ.get("SPREADBOARD_PUSHOVER_APP_TOKEN", "").strip()
        public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip().rstrip("/")
        for user_id in user_ids:
            user = accounts.get_user_object(user_id, db_path=self.accounts_path)
            if user is None or not user.subscription_active:
                continue
            delivery = accounts.notification_delivery(user_id, db_path=self.accounts_path)
            for rule in accounts.list_market_alert_rules(user_id, db_path=self.accounts_path):
                if not rule.get("enabled"):
                    continue
                metric = str(rule.get("metric") or "")
                token = accounts.token_from_alert_key(str(rule.get("route_key") or ""))
                if token is not None:
                    row = None
                    value = (tokens.get(token) or {}).get(metric)
                else:
                    row = rows.get(str(rule.get("route_key") or ""))
                    value = _rule_value(row, metric) if row else None
                if value is None:
                    continue
                evaluated += 1
                body = _alert_body(rule, metric, value, row, tokens.get(token or ""))
                notification = accounts.record_market_alert_evaluation(
                    user_id,
                    int(rule["id"]),
                    value=value,
                    title=(
                        f"{rule['symbol']} {'price' if metric == 'token_price' else 'alert'}"
                        if token is not None
                        else f"{rule['symbol']} route alert"
                    ),
                    body=body,
                    db_path=self.accounts_path,
                )
                if notification is None:
                    continue
                triggered += 1
                if app_token and delivery:
                    result = send_pushover_message(
                        app_token=app_token,
                        user_key=delivery["user_key"],
                        title=notification["title"],
                        message=notification["body"],
                        url=f"{public_url}/pair/{urllib.parse.quote(str(rule['route_key']), safe='')}" if public_url else None,
                        device=delivery.get("device"),
                        sound=delivery.get("sound"),
                    )
                    delivered += int(bool(result.get("ok")))
        return {"evaluated": evaluated, "triggered": triggered, "delivered": delivered}


def send_user_test_alert(user_id: int, *, accounts_path: Path | str = accounts.DEFAULT_DB_PATH) -> dict[str, Any]:
    app_token = os.environ.get("SPREADBOARD_PUSHOVER_APP_TOKEN", "").strip()
    if not app_token:
        return {"ok": False, "error": "pushover_app_not_configured"}
    delivery = accounts.notification_delivery(user_id, db_path=accounts_path)
    if not delivery:
        return {"ok": False, "error": "pushover_user_not_configured"}
    result = send_pushover_message(
        app_token=app_token,
        user_key=delivery["user_key"],
        title="SpreadBoard test",
        message="Pushover delivery is active for your SpreadBoard account.",
        device=delivery.get("device"),
        sound=delivery.get("sound"),
    )
    return {"ok": bool(result.get("ok")), "status": result.get("status"), "error": result.get("error")}


def token_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per-asset values a token alert can be set against.

    A token rule watches the asset, not one venue pair -- "tell me when DOGE
    trades above 0.20", or "when anything on this token pays more than 0.05% a
    day". So price is taken as the MEDIAN of the legs quoting it, which is what
    makes it trustworthy: one venue printing a stale or dislocated quote moves
    a mean and cannot move a median, and this board has already had a single bad
    quote become a headline.

    Funding is the best net 24h carry available on the token, because that is
    the one a member would actually put on.
    """
    prices: dict[str, list[float]] = {}
    funding: dict[str, float] = {}
    for row in rows:
        token = str(row.get("token") or "").upper()
        if not token:
            continue
        for side in ("long", "short"):
            value = _float(row.get(f"{side}_price"))
            if value is not None and value > 0:
                prices.setdefault(token, []).append(value)
        carry = _float(row.get("funding_24h_pct"))
        if carry is None:
            carry = _float(row.get("funding_projected_24h_pct"))
        if carry is not None:
            funding[token] = max(funding.get(token, float("-inf")), carry)

    metrics: dict[str, dict[str, float]] = {}
    for token, values in prices.items():
        ordered = sorted(values)
        middle = len(ordered) // 2
        median = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2
        )
        metrics.setdefault(token, {})["token_price"] = median
    for token, carry in funding.items():
        if carry > float("-inf"):
            metrics.setdefault(token, {})["token_funding_24h_pct"] = carry
    return metrics


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


#: How each metric reads in the notification, and whether it is a percentage.
_METRIC_LABELS = {
    "funding_24h_pct": ("24h paired funding", True),
    "open_spread_pct": ("open spread", True),
    "token_price": ("price", False),
    "token_funding_24h_pct": ("best 24h funding", True),
}


def _alert_body(
    rule: dict[str, Any],
    metric: str,
    value: float,
    row: dict[str, Any] | None,
    token_view: dict[str, float] | None,
) -> str:
    """What the member reads. It must say the number and what it crossed."""
    label, is_pct = _METRIC_LABELS.get(metric, ("value", True))
    shown = f"{value:+.4f}%" if is_pct else f"{value:,.6g}"
    limit = (
        f"{float(rule['threshold']):+.4f}%"
        if is_pct
        else f"{float(rule['threshold']):,.6g}"
    )
    direction = "at or above" if rule["operator"] == "gte" else "at or below"
    where = ""
    if row is not None:
        where = f" on {row.get('long_venue') or '?'} -> {row.get('short_venue') or '?'}"
    elif token_view:
        where = " across every venue quoting it"
    return f"{rule['symbol']} {label} is {shown}{where}; threshold {direction} {limit}."


def _rule_value(row: dict[str, Any] | None, metric: str) -> float | None:
    if not row:
        return None
    # These must name fields the board actually produces. They did not: every
    # spread rule read None and silently never fired, so a member could set a
    # threshold, watch the board cross it, and never be told. The displayed
    # value comes first because that is the number the threshold was set
    # against; the older names stay as fallbacks for rules stored before.
    keys = (
        ("funding_24h_pct", "funding_net_24h_pct", "net_funding_24h_pct")
        if metric == "funding_24h_pct"
        else (
            "displayed_open_spread_pct",
            "executable_spread_pct",
            "depth_weighted_spread_pct",
            "open_spread_pct",
            "entry_spread_pct",
            "spread_pct",
        )
    )
    for key in keys:
        try:
            value = row.get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _public_detail(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "error": result.get("error"),
        "response": result.get("response"),
    }


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:500]


def _pushover_users(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    users = cfg.get("pushover_users") or cfg.get("premium_users") or []
    return [u for u in users if isinstance(u, dict) and u.get("pushover_user_key")]


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
