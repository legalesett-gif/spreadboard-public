"""Disabled-by-default Pushover alerts for SpreadBoard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spreadboard import accounts, api_spreads, board, chart_catalog

CONFIG_PATH = Path(__file__).with_name("config.json")
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
# Operators already read *_worker_status.json files for the other workers; the
# alert path is the one with no way to answer "why did nothing arrive?".
STATUS_PATH = RUNTIME_DIR / "alert_worker_status.json"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"
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
    website_session_path = cfg.get("website_storage_state_path") or cfg.get(
        "premium_storage_state_path"
    )
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
    priority: int | None = None,
    retry: int | None = None,
    expire: int | None = None,
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
    if priority is not None:
        normalized_priority = max(-2, min(2, int(priority)))
        payload["priority"] = normalized_priority
        if normalized_priority == 2:
            # Pushover's emergency contract requires both values. Native apps
            # repeat the selected sound until the member taps Acknowledge (or
            # the bounded expiry is reached).
            payload["retry"] = max(30, int(retry or 60))
            payload["expire"] = max(1, min(10800, int(expire or 10800)))
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


def validate_pushover_user(
    *, app_token: str, user_key: str, timeout: float = 10.0
) -> dict[str, Any]:
    """Validate a Pushover recipient without sending a notification.

    The raw response may contain a request identifier and the user key must
    never be reflected.  Only active device names and platform labels are kept
    for server-side matching; the account API returns counts, not names.
    """
    body = urllib.parse.urlencode({"token": app_token, "user": user_key}).encode("utf-8")
    request = urllib.request.Request(
        PUSHOVER_VALIDATE_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = _json_or_text(response.read().decode("utf-8", errors="replace"))
            if not isinstance(parsed, dict):
                return {
                    "ok": False,
                    "status": response.status,
                    "error": "pushover_validation_invalid_response",
                }
            return {
                "ok": response.status == 200 and parsed.get("status") == 1,
                "status": response.status,
                "devices": [str(item) for item in parsed.get("devices") or [] if item],
                "licenses": [str(item) for item in parsed.get("licenses") or [] if item],
                "errors": [str(item)[:160] for item in parsed.get("errors") or []],
            }
    except urllib.error.HTTPError as exc:
        parsed = _json_or_text(exc.read().decode("utf-8", errors="replace"))
        errors = parsed.get("errors") if isinstance(parsed, dict) else []
        return {
            "ok": False,
            "status": exc.code,
            "error": "pushover_user_not_valid",
            "devices": [],
            "licenses": [],
            "errors": [str(item)[:160] for item in errors or []],
        }
    except Exception:  # noqa: BLE001 - never expose network internals to a member.
        return {
            "ok": False,
            "status": None,
            "error": "pushover_validation_unavailable",
            "devices": [],
            "licenses": [],
            "errors": [],
        }


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
        results.append(
            {"name": name, "ok": bool(result.get("ok")), "detail": _public_detail(result)}
        )
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


def _empty_run() -> dict[str, Any]:
    return {
        "generated_at": None,
        "evaluated": 0,
        "triggered": 0,
        "delivered": 0,
        "rules_considered": 0,
        "skipped": {
            "inactive_subscriber": 0,
            "disabled_rule": 0,
            "no_value": 0,
            "condition_not_met": 0,
        },
        "rejected": {},
        "latency_seconds": {"samples": 0, "p50": None, "p95": None, "max": None},
    }


def _latency_summary(samples: list[float]) -> dict[str, Any]:
    if not samples:
        return {"samples": 0, "p50": None, "p95": None, "max": None}
    ordered = sorted(samples)

    def pick(fraction: float) -> float:
        # Nearest-rank: with a handful of alerts per poll, interpolation would
        # invent a latency no alert actually had.
        index = min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))
        return round(ordered[index], 3)

    return {
        "samples": len(ordered),
        "p50": pick(0.50),
        "p95": pick(0.95),
        "max": round(ordered[-1], 3),
    }


def _condition_latency(rule: dict[str, Any], now: float) -> float:
    """Seconds from the condition first holding to this delivery.

    A rule that crosses and fires within one poll has no stored `condition_since`
    yet, which is a real zero rather than missing data.
    """
    raw = str(rule.get("condition_since") or "").strip()
    if not raw:
        return 0.0
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, now - started.timestamp())


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
        self.poll_seconds = max(1.0, float(poll_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="spreadboard-market-alerts", daemon=True
        )
        self._custom_quote_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._custom_quote_cursor = 0
        self.last_run: dict[str, Any] = _empty_run()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def write_status(self, path: Path | str | None = None) -> Path:
        """Publish the last run beside the other worker status files."""
        target = Path(path) if path is not None else STATUS_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.last_run, indent=2, sort_keys=True) + "\n"
        # Same-directory temp then rename, so a reader never sees half a file.
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)
        return target

    def start(self) -> None:
        if not self.running:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.running:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        self._stop.wait(2.0)
        while not self._stop.is_set():
            try:
                self.check_once()
                self.write_status()
            except Exception as exc:  # noqa: BLE001
                print(f"spreadboard-market-alerts: {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(self.poll_seconds)

    def check_once(self, *, rule_ids: set[int] | None = None) -> dict[str, int]:
        """Evaluate market alerts, optionally restricting a diagnostic run.

        The filter is deliberately server-side.  It lets operations exercise
        one temporary rule end to end without evaluating or triggering every
        other subscriber rule during the test.
        """
        # Ask who is waiting before building anything. This runs every ten
        # seconds and `limit=None` materialises the entire board -- every row of
        # every token, the largest payload the process ever holds, on its own
        # cache key. Doing that for an empty rule table was several hundred
        # megabytes rebuilt six times a minute to deliver nothing.
        run = _empty_run()
        run["generated_at"] = datetime.now(tz=UTC).isoformat()
        latencies: list[float] = []
        user_ids = accounts.list_market_alert_user_ids(db_path=self.accounts_path)
        if not user_ids:
            self.last_run = run
            return {"evaluated": 0, "triggered": 0, "delivered": 0}
        rules_by_user = {}
        for user_id in user_ids:
            rules = accounts.list_market_alert_rules(user_id, db_path=self.accounts_path)
            if rule_ids is not None:
                rules = [rule for rule in rules if int(rule.get("id") or 0) in rule_ids]
            if rules:
                rules_by_user[user_id] = rules
        chart_keys = {
            str(rule.get("route_key") or "")
            for rules in rules_by_user.values()
            for rule in rules
            if rule.get("enabled")
            and accounts.token_from_alert_key(str(rule.get("route_key") or "")) is None
        }
        token_targets = {
            token
            for rules in rules_by_user.values()
            for rule in rules
            if rule.get("enabled")
            if (token := accounts.token_from_alert_key(str(rule.get("route_key") or "")))
        }
        # Production keeps one complete, continuously repriced route universe
        # resident in the web process.  Alert evaluation asks it only for the
        # exact routes/tokens members track.  The fallback retains standalone
        # and fixture compatibility, but no healthy production poll parses or
        # groups the 25k-route discovery snapshot.
        from spreadboard import warm_query_projection

        targeted, warm_status = warm_query_projection.LIVE_UNIVERSE.target_rows(
            route_keys=chart_keys,
            tokens=token_targets,
        )
        if warm_status.get("ready"):
            structural_rows = targeted
            board_rows = [row for row in targeted if _alert_row_verified(row)]
        else:
            market = api_spreads.load_spreads(
                board_path=self.board_path,
                include_stale=False,
                include_unverified=False,
                limit=None,
            )
            board_rows = [
                row
                for row in market.get("rows") or []
                if isinstance(row, dict) and row.get("route_key")
            ]
            structural_rows = []
        rows = {str(row["route_key"]): row for row in board_rows}
        # A saved chart can remain useful after its route cools, becomes stale,
        # or is temporarily held out of the normal verified board.  Preserve a
        # structural row for those keys, then take a fresh bounded exact quote
        # instead of silently leaving the rule unevaluated.
        missing_standard_keys = {
            key for key in chart_keys if not key.startswith("CUSTOM:") and key not in rows
        }
        if missing_standard_keys and not warm_status.get("ready"):
            structural = api_spreads.load_spreads(
                board_path=self.board_path,
                include_stale=True,
                include_unverified=True,
                limit=None,
            )
            structural_rows = [
                row
                for row in structural.get("rows") or []
                if isinstance(row, dict)
                and str(row.get("route_key") or "") in missing_standard_keys
            ]
        status_rows = {
            str(row.get("route_key") or ""): row
            for row in [*board_rows, *structural_rows]
            if row.get("route_key")
        }
        rows.update(self._custom_alert_rows(chart_keys, board_rows, structural_rows))
        tokens = token_metrics(board_rows)
        evaluated = triggered = delivered = 0
        app_token = os.environ.get("SPREADBOARD_PUSHOVER_APP_TOKEN", "").strip()
        public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip().rstrip("/")
        for user_id in user_ids:
            user = accounts.get_user_object(user_id, db_path=self.accounts_path)
            if user is None or not user.subscription_active:
                run["skipped"]["inactive_subscriber"] += len(rules_by_user.get(user_id, []))
                run["rules_considered"] += len(rules_by_user.get(user_id, []))
                continue
            delivery = accounts.notification_delivery(user_id, db_path=self.accounts_path)
            for rule in rules_by_user.get(user_id, []):
                run["rules_considered"] += 1
                if not rule.get("enabled"):
                    run["skipped"]["disabled_rule"] += 1
                    continue
                metric = str(rule.get("metric") or "")
                token = accounts.token_from_alert_key(str(rule.get("route_key") or ""))
                if token is not None:
                    row = None
                    value = (tokens.get(token) or {}).get(metric)
                else:
                    route_key = str(rule.get("route_key") or "")
                    row = rows.get(route_key)
                    if metric in {"route_deliverable", "quote_age_seconds"}:
                        # Stale structural rows are invalid price evidence but
                        # are exactly the evidence a freshness or rail-status
                        # rule needs. Prefer a successful exact refresh; fall
                        # back to the structural age/rail state when it fails.
                        row = row or status_rows.get(route_key)
                    value = _rule_value(row, metric) if row else None
                if value is None:
                    # No quote reached this route at all. Indistinguishable from
                    # "condition not met" in the totals, and the usual cause of
                    # a rule that a member swears never fires.
                    run["skipped"]["no_value"] += 1
                    continue
                evaluated += 1
                body = _alert_body(rule, metric, value, row, tokens.get(token or ""))
                notification = accounts.record_market_alert_evaluation(
                    user_id,
                    int(rule["id"]),
                    value=value,
                    title=_notification_title(rule, metric, token is not None),
                    body=body,
                    db_path=self.accounts_path,
                )
                if notification is None:
                    run["skipped"]["condition_not_met"] += 1
                    continue
                triggered += 1
                latencies.append(_condition_latency(rule, time.time()))
                if not app_token:
                    run["rejected"]["pushover_unconfigured"] = (
                        run["rejected"].get("pushover_unconfigured", 0) + 1
                    )
                elif not delivery:
                    run["rejected"]["pushover_not_enabled_by_member"] = (
                        run["rejected"].get("pushover_not_enabled_by_member", 0) + 1
                    )
                if app_token and delivery:
                    result = send_pushover_message(
                        app_token=app_token,
                        user_key=delivery["user_key"],
                        title=notification["title"],
                        message=notification["body"],
                        url=_notification_url(public_url, rule, token),
                        device=delivery.get("device"),
                        sound="siren",
                        priority=2,
                        retry=60,
                        expire=10800,
                    )
                    delivered += int(bool(result.get("ok")))
                    if not result.get("ok"):
                        status_code = result.get("status")
                        reason = (
                            f"pushover_http_{status_code}"
                            if status_code
                            else "pushover_error"
                        )
                        run["rejected"][reason] = run["rejected"].get(reason, 0) + 1
        run["evaluated"] = evaluated
        run["triggered"] = triggered
        run["delivered"] = delivered
        run["latency_seconds"] = _latency_summary(latencies)
        self.last_run = run
        return {"evaluated": evaluated, "triggered": triggered, "delivered": delivered}

    def _custom_alert_rows(
        self,
        route_keys: set[str],
        board_rows: list[dict[str, Any]],
        structural_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Resolve any chart alert from a live row or bounded exact quote.

        Most charts select a route that is already on the verified live board.
        That match is free and stays at the scanner cadence.  Cooled, stale,
        guarded and genuinely custom combinations retain their structural leg
        data and receive an exact quote.  Quotes are deduplicated across users,
        cached, and bounded per poll so a DEX provider cannot stall all alerts.
        """
        now = time.monotonic()
        ttl = max(10.0, float(os.environ.get("SPREADBOARD_CUSTOM_ALERT_QUOTE_SECONDS", "30")))
        output: dict[str, dict[str, Any]] = {}
        unresolved = []
        structural_by_key = {
            str(row.get("route_key") or ""): row
            for row in structural_rows or []
            if row.get("route_key")
        }
        for route_key in sorted(route_keys):
            custom_route = chart_catalog.route_from_key(route_key)
            route = custom_route or structural_by_key.get(route_key)
            if route is None:
                continue
            matched = (
                _matching_board_route(route, board_rows)
                if custom_route is not None
                else next(
                    (
                        row
                        for row in board_rows
                        if str(row.get("route_key") or "") == route_key
                    ),
                    None,
                )
            )
            if matched is not None:
                output[route_key] = {**matched, "route_key": route_key}
                self._custom_quote_cache[route_key] = (now, output[route_key])
                continue
            cached = self._custom_quote_cache.get(route_key)
            if cached and now - cached[0] <= ttl:
                output[route_key] = cached[1]
            else:
                unresolved.append((route_key, route))

        limit = max(1, min(8, int(os.environ.get("SPREADBOARD_CUSTOM_ALERT_QUOTE_LIMIT", "4"))))
        if unresolved:
            offset = self._custom_quote_cursor % len(unresolved)
            unresolved = unresolved[offset:] + unresolved[:offset]
            self._custom_quote_cursor += limit
        selected = unresolved[:limit]
        quoted_by_key: dict[str, dict[str, Any] | None] = {}
        if selected:
            # An exact provider quote can take several seconds. Running four
            # unrelated watched routes serially made the fourth alert arrive a
            # minute late even though every threshold had already crossed.
            with ThreadPoolExecutor(max_workers=min(4, len(selected))) as executor:
                future_routes = {
                    executor.submit(_quote_custom_alert_route, route): route_key
                    for route_key, route in selected
                }
                for future in as_completed(future_routes):
                    route_key = future_routes[future]
                    try:
                        quoted_by_key[route_key] = future.result()
                    except Exception:  # noqa: BLE001 - one provider must not block other alerts.
                        quoted_by_key[route_key] = None
        for route_key, _route in selected:
            quoted = quoted_by_key.get(route_key)
            if quoted is not None:
                quoted = {**quoted, "route_key": route_key}
                output[route_key] = quoted
                self._custom_quote_cache[route_key] = (now, quoted)
                continue
            cached = self._custom_quote_cache.get(route_key)
            if cached and now - cached[0] <= ttl * 4:
                output[route_key] = cached[1]
        return output


def _alert_row_verified(row: dict[str, Any]) -> bool:
    """Safe local evidence for price/spread alerts without a board rebuild.

    Funding may remain current while basis cools, so freshness is deliberately
    enforced by ``_rule_value`` for spread and by ``token_metrics`` for token
    price.  Retaining the row here lets a funding alert keep working without
    relabelling an old spread as live.
    """

    guard = row.get("tokenized_guard") or {}
    return not (
        row.get("identity_mismatch")
        or row.get("mirage_guarded")
        or row.get("thin_book")
        or row.get("deliverable") is False
        or (isinstance(guard, dict) and guard.get("rankable") is False)
    )


def send_user_test_alert(
    user_id: int, *, accounts_path: Path | str = accounts.DEFAULT_DB_PATH
) -> dict[str, Any]:
    app_token = os.environ.get("SPREADBOARD_PUSHOVER_APP_TOKEN", "").strip()
    if not app_token:
        return {"ok": False, "error": "pushover_app_not_configured"}
    delivery = accounts.notification_delivery(user_id, db_path=accounts_path)
    if not delivery:
        return {"ok": False, "error": "pushover_user_not_configured"}
    validation = validate_pushover_user(
        app_token=app_token,
        user_key=delivery["user_key"],
    )
    if not validation.get("ok"):
        return {
            "ok": False,
            "status": validation.get("status"),
            "error": validation.get("error") or "pushover_user_not_valid",
            "active_device_count": 0,
        }
    device = str(delivery.get("device") or "")
    devices = validation.get("devices") or []
    if device and device not in devices:
        return {
            "ok": False,
            "status": validation.get("status"),
            "error": "pushover_device_not_active",
            "active_device_count": len(devices),
        }
    result = send_pushover_message(
        app_token=app_token,
        user_key=delivery["user_key"],
        title="SpreadBoard test",
        message="Pushover delivery is active for your SpreadBoard account.",
        device=delivery.get("device"),
        sound=delivery.get("sound"),
    )
    provider_errors = []
    response = result.get("response")
    if isinstance(response, dict):
        provider_errors = [str(item)[:160] for item in response.get("errors") or []]
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "error": (
            result.get("error") or ("pushover_delivery_rejected" if not result.get("ok") else None)
        ),
        "provider_errors": provider_errors,
        "active_device_count": len(devices),
        "accepted_by_pushover": bool(result.get("ok")),
    }


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
    prices: dict[str, dict[tuple[str, str, str], float]] = {}
    funding: dict[str, float] = {}
    for row in rows:
        token = str(row.get("token") or "").upper()
        if not token:
            continue
        price_is_current = (
            "spread_quote_current" not in row
            or row.get("spread_quote_current") is True
            or api_spreads.spread_quote_current(row)
        )
        if price_is_current:
            for side in ("long", "short"):
                value = _float(row.get(f"{side}_price"))
                if value is not None and value > 0:
                    identity = (
                        str(row.get(f"{side}_venue") or "").casefold(),
                        str(row.get(f"{side}_market_type") or "").casefold(),
                        str(
                            row.get(f"{side}_market_symbol")
                            or row.get(f"{side}_symbol")
                            or side
                        ).upper(),
                    )
                    prices.setdefault(token, {})[identity] = value
        carry = _float(row.get("funding_daily_pct"))
        if carry is None:
            carry = _float(row.get("funding_projected_24h_pct"))
        if carry is None:
            carry = _float(row.get("funding_24h_pct"))
        if carry is not None:
            funding[token] = max(funding.get(token, float("-inf")), carry)

    metrics: dict[str, dict[str, float]] = {}
    for token, market_prices in prices.items():
        values = list(market_prices.values())
        ordered = sorted(values)
        middle = len(ordered) // 2
        median = (
            ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
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
    "funding_24h_pct": ("current-rate 24h paired funding", True),
    "open_spread_pct": ("open spread", True),
    "token_price": ("price", False),
    "token_funding_24h_pct": ("best current-rate 24h funding", True),
    "route_deliverable": ("deposit / withdrawal route", False),
    "quote_age_seconds": ("quote age", False),
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
    if metric == "route_deliverable":
        shown = "transferable" if value >= 0.5 else "blocked"
        limit = "transferable" if rule["operator"] == "gte" else "blocked"
    elif metric == "quote_age_seconds":
        shown = _duration_label(value)
        limit = _duration_label(float(rule["threshold"]))
    else:
        shown = f"{value:+.4f}%" if is_pct else f"{value:,.6g}"
        limit = f"{float(rule['threshold']):+.4f}%" if is_pct else f"{float(rule['threshold']):,.6g}"
    direction = "at or above" if rule["operator"] == "gte" else "at or below"
    where = ""
    if row is not None:
        where = f" on {row.get('long_venue') or '?'} -> {row.get('short_venue') or '?'}"
    elif token_view:
        where = " across every venue quoting it"
    if metric == "route_deliverable":
        return f"{rule['symbol']} {label} is {shown}{where}; alert condition is {limit}."
    return f"{rule['symbol']} {label} is {shown}{where}; threshold {direction} {limit}."


def _rule_value(row: dict[str, Any] | None, metric: str) -> float | None:
    if not row:
        return None
    if metric == "route_deliverable":
        deliverable = row.get("deliverable")
        return float(deliverable) if isinstance(deliverable, bool) else None
    if metric == "quote_age_seconds":
        age_min = api_spreads.quote_age_min(row)
        return max(0.0, age_min * 60.0) if age_min is not None else None
    if (
        metric == "open_spread_pct"
        and row.get("spread_quote_current") is not True
        and not api_spreads.spread_quote_current(row)
    ):
        return None
    # These must name fields the board actually produces. They did not: every
    # spread rule read None and silently never fired, so a member could set a
    # threshold, watch the board cross it, and never be told. The displayed
    # value comes first because that is the number the threshold was set
    # against; the older names stay as fallbacks for rules stored before.
    keys = (
        (
            "funding_daily_pct",
            "funding_projected_24h_pct",
            "funding_24h_pct",
            "funding_net_24h_pct",
            "net_funding_24h_pct",
        )
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


def _duration_label(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60.0:.1f} min"


def _notification_title(rule: dict[str, Any], metric: str, token_wide: bool) -> str:
    kind = {
        "token_price": "price",
        "token_funding_24h_pct": "funding",
        "funding_24h_pct": "funding",
        "open_spread_pct": "spread",
        "route_deliverable": "D/W status",
        "quote_age_seconds": "quote freshness",
    }.get(metric, "market")
    scope = "token" if token_wide else "route"
    return f"{rule['symbol']} {scope} {kind} alert"


def _notification_url(
    public_url: str,
    rule: dict[str, Any],
    token: str | None,
) -> str | None:
    if not public_url:
        return None
    if token is not None:
        return f"{public_url}/token/{urllib.parse.quote(token, safe='')}"
    route_key = urllib.parse.quote(str(rule.get("route_key") or ""), safe="")
    return f"{public_url}/pair/{route_key}"


def _matching_board_route(
    selected: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the canonical equivalent of a route built in Custom charts."""
    scalar_fields = (
        "token",
        "long_venue",
        "long_market_type",
        "short_venue",
        "short_market_type",
    )
    for candidate in candidates:
        if any(
            str(candidate.get(field) or "").casefold() != str(selected.get(field) or "").casefold()
            for field in scalar_fields
        ):
            continue
        if any(
            _route_symbol(candidate, side).casefold() != _route_symbol(selected, side).casefold()
            for side in ("long", "short")
        ):
            continue
        return candidate
    return None


def _route_symbol(row: dict[str, Any], side: str) -> str:
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    inputs = notes.get("route_inputs") if isinstance(notes.get("route_inputs"), dict) else {}
    leg = inputs.get(side) if isinstance(inputs.get(side), dict) else {}
    return str(
        row.get(f"{side}_market_symbol") or row.get(f"{side}_symbol") or leg.get("symbol") or ""
    )


def _quote_custom_alert_route(route: dict[str, Any]) -> dict[str, Any] | None:
    """Take one public exact quote for an alert-only custom chart route."""
    from spreadboard.fast_quotes import FastQuoteRefresher, supports_native_order_book

    native = all(
        supports_native_order_book(
            str(route.get(f"{side}_venue") or ""),
            str(route.get(f"{side}_market_type") or ""),
        )
        for side in ("long", "short")
    )
    if native:
        refresher = FastQuoteRefresher()
        try:
            result = refresher.quote_route(
                route,
                target_notional_usd=api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD,
            )
        except Exception:  # noqa: BLE001 - one route must not stop every user's alerts.
            return None
        finally:
            refresher.close()
    else:
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "route_quote_worker.py"),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                input=json.dumps(route, separators=(",", ":"), default=str),
                capture_output=True,
                text=True,
                timeout=float(os.environ.get("SPREADBOARD_CHART_SAMPLE_TIMEOUT_SECONDS", "22")),
                check=False,
            )
            result = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError, OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
    row = result.get("row") if isinstance(result, dict) else None
    return row if result.get("status") == "ok" and isinstance(row, dict) else None


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
