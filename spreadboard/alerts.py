"""Disabled-by-default Pushover alerts for SpreadBoard."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from spreadboard import board

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
