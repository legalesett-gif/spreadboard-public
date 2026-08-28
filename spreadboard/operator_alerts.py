"""Deduplicated owner-only operational alerts.

Product monitors must never post diagnostics into the subscriber group.  This
module deliberately supports only the encrypted, opt-in Pushover destinations
of administrator accounts and always logs the transition as a fallback.

Delivery accounting exists because the original implementation persisted the
new fault state and then attempted Pushover.  A rejected or timed-out push was
therefore invisible: the next poll saw no transition and never retried, and the
caller discarded the returned ``delivered``/``errors`` fields.  Fault state and
delivery state are now tracked separately -- the fault record is still written
first, because losing the fact that a fault occurred is worse than a duplicate
notification, but an undelivered transition stays pending and is retried with
bounded backoff until the provider accepts it or the attempt budget is spent.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from spreadboard import accounts, alerts

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_STATE_PATH = RUNTIME_DIR / "operator_alert_state.json"
#: Append-only, secret-free incident/delivery history.  The state file only
#: holds the latest state per key, which made it impossible to answer "did the
#: owner actually receive the 19:25 OOM alert?" after the fact.
DEFAULT_LEDGER_PATH = RUNTIME_DIR / "operator_alert_ledger.jsonl"
#: A push that the provider rejects is retried on the next poll, backing off so
#: a provider outage cannot become a delivery storm.
RETRY_BACKOFF_SECONDS = (30.0, 120.0, 300.0, 900.0, 1800.0)
MAX_DELIVERY_ATTEMPTS = len(RETRY_BACKOFF_SECONDS) + 1
_LOCK = threading.Lock()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": {}}
    if not isinstance(value, dict):
        return {"events": {}}
    if not isinstance(value.get("events"), dict):
        value["events"] = {}
    return value


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _append_ledger(path: Path, record: dict[str, Any]) -> None:
    """Never let audit writing break an alert."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError:  # pragma: no cover - the alert itself still logs.
        LOGGER.warning("operator alert ledger unavailable", exc_info=False)


def _error_class(error: str) -> str:
    """Record the shape of a failure, never a token or a URL."""

    text = str(error or "").lower()
    for marker, label in (
        ("timeout", "timeout"),
        ("timed out", "timeout"),
        ("connection", "connection"),
        ("network", "connection"),
        ("429", "rate_limited"),
        ("rate limit", "rate_limited"),
        ("401", "rejected_credentials"),
        ("403", "rejected_credentials"),
        ("invalid", "rejected_request"),
        ("400", "rejected_request"),
        ("500", "provider_error"),
        ("502", "provider_error"),
        ("503", "provider_error"),
    ):
        if marker in text:
            return label
    return "unknown"


def _delivery_state(previous: dict[str, Any]) -> dict[str, Any]:
    value = previous.get("delivery")
    return dict(value) if isinstance(value, dict) else {}


def _send(
    *,
    app_token: str,
    title: str,
    rendered: str,
    active: bool,
    db_path: Path | str,
) -> tuple[int, list[str]]:
    delivered = 0
    errors: list[str] = []
    try:
        admin_ids = {
            int(user["id"])
            for user in accounts.list_users(db_path=db_path)
            if bool(user.get("is_admin"))
        }
        for user_id in sorted(admin_ids & set(accounts.list_pushover_user_ids(db_path=db_path))):
            destination = accounts.notification_delivery(user_id, db_path=db_path)
            if not destination:
                continue
            result = alerts.send_pushover_message(
                app_token=app_token,
                user_key=str(destination["user_key"]),
                device=str(destination.get("device") or "") or None,
                sound=str(destination.get("sound") or "pushover"),
                title=str(title)[:120],
                message=rendered[:500],
                priority=1 if active else 0,
            )
            if result.get("ok"):
                delivered += 1
            else:
                errors.append(str(result.get("error") or "delivery_rejected"))
    except Exception as exc:
        LOGGER.exception("owner alert delivery failed")
        errors.append(type(exc).__name__)
    return delivered, errors


def notify_transition(
    key: str,
    *,
    active: bool,
    title: str,
    message: str,
    db_path: Path | str = accounts.DEFAULT_DB_PATH,
    state_path: Path | str = DEFAULT_STATE_PATH,
    ledger_path: Path | str | None = None,
) -> dict[str, Any]:
    """Deliver once when a fault opens and once when it recovers.

    A transition whose push the provider did not accept stays pending and is
    retried on later calls, so the owner is not silently left uninformed.
    """

    clean_key = str(key).strip()
    if not clean_key:
        raise ValueError("operator_alert_key_required")
    path = Path(state_path)
    ledger = Path(ledger_path) if ledger_path is not None else (
        path.parent / DEFAULT_LEDGER_PATH.name
    )
    now = time.time()

    with _LOCK:
        state = _load(path)
        events = state.setdefault("events", {})
        previous = events.get(clean_key) if isinstance(events.get(clean_key), dict) else {}
        changed = bool(previous.get("active")) != bool(active)
        delivery = _delivery_state(previous)
        pending = bool(delivery.get("pending"))
        attempts = int(delivery.get("attempts") or 0)

        if not changed:
            # The fault state is unchanged, but a previous transition may never
            # have reached the provider.  Retry it rather than losing it.
            if not pending:
                return {
                    "changed": False,
                    "delivered": 0,
                    "active": bool(active),
                    "incident_id": previous.get("incident_id"),
                }
            if attempts >= MAX_DELIVERY_ATTEMPTS:
                return {
                    "changed": False,
                    "delivered": 0,
                    "active": bool(active),
                    "incident_id": previous.get("incident_id"),
                    "delivery": "attempts_exhausted",
                }
            if now < float(delivery.get("next_attempt_unix") or 0.0):
                return {
                    "changed": False,
                    "delivered": 0,
                    "active": bool(active),
                    "incident_id": previous.get("incident_id"),
                    "delivery": "retry_scheduled",
                }
            retry = True
            incident_id = str(previous.get("incident_id") or uuid.uuid4().hex[:12])
            send_title = str(previous.get("title") or title)
            send_message = str(previous.get("message") or message)
            send_active = bool(previous.get("active"))
        else:
            retry = False
            # One incident id spans the fault and its recovery.  A new fault
            # after a recovery opens a new incident, so a second OOM is never
            # hidden behind the first.
            incident_id = (
                uuid.uuid4().hex[:12]
                if active
                else str(previous.get("incident_id") or uuid.uuid4().hex[:12])
            )
            send_title = str(title)
            send_message = str(message)
            send_active = bool(active)
            attempts = 0
            events[clean_key] = {
                "active": bool(active),
                "changed_at_unix": now,
                "title": send_title[:120],
                "message": send_message[:500],
                "incident_id": incident_id,
                "opened_at_unix": now if active else previous.get("opened_at_unix"),
                "recovered_at_unix": None if active else now,
                "delivery": {
                    "pending": True,
                    "attempts": 0,
                    "delivered": 0,
                    "last_error": None,
                    "next_attempt_unix": now,
                },
            }
            state["updated_at_unix"] = now
            _atomic_write(path, state)

    rendered = send_message if send_active else f"RECOVERED: {send_message}"
    if send_active:
        LOGGER.error("%s: %s", send_title, rendered)
    else:
        LOGGER.info("%s: %s", send_title, rendered)

    app_token = os.environ.get("SPREADBOARD_PUSHOVER_APP_TOKEN", "").strip()
    if not app_token:
        _append_ledger(
            ledger,
            {
                "at_unix": now,
                "incident_id": incident_id,
                "key": clean_key,
                "severity": "fault" if send_active else "recovery",
                "event": "skipped",
                "reason": "pushover_not_configured",
                "attempt": attempts + 1,
                "delivered": 0,
            },
        )
        return {
            "changed": not retry,
            "delivered": 0,
            "active": bool(active),
            "incident_id": incident_id,
            "delivery": "pushover_not_configured",
        }

    delivered, errors = _send(
        app_token=app_token,
        title=send_title,
        rendered=rendered,
        active=send_active,
        db_path=db_path,
    )
    attempt_number = attempts + 1
    accepted = delivered > 0

    with _LOCK:
        state = _load(path)
        events = state.setdefault("events", {})
        current = events.get(clean_key) if isinstance(events.get(clean_key), dict) else {}
        # Only delivery accounting is written here.  A delivery failure must
        # never clear or alter the underlying fault state.
        if str(current.get("incident_id") or "") == incident_id:
            current["delivery"] = {
                "pending": not accepted and attempt_number < MAX_DELIVERY_ATTEMPTS,
                "attempts": attempt_number,
                "delivered": delivered,
                "last_error": _error_class(errors[0]) if errors else None,
                "last_attempt_unix": time.time(),
                "next_attempt_unix": time.time()
                + RETRY_BACKOFF_SECONDS[
                    min(attempt_number - 1, len(RETRY_BACKOFF_SECONDS) - 1)
                ],
            }
            events[clean_key] = current
            state["updated_at_unix"] = time.time()
            _atomic_write(path, state)

    _append_ledger(
        ledger,
        {
            "at_unix": time.time(),
            "incident_id": incident_id,
            "key": clean_key,
            "severity": "fault" if send_active else "recovery",
            "event": "delivered" if accepted else "delivery_failed",
            "attempt": attempt_number,
            "delivered": delivered,
            "error_class": _error_class(errors[0]) if errors else None,
            "retry": retry,
        },
    )
    return {
        "changed": not retry,
        "retry": retry,
        "delivered": delivered,
        "active": bool(active),
        "incident_id": incident_id,
        "attempts": attempt_number,
        "errors": errors,
    }
