"""Deduplicated owner-only operational alerts.

Product monitors must never post diagnostics into the subscriber group.  This
module deliberately supports only the encrypted, opt-in Pushover destinations
of administrator accounts and always logs the transition as a fallback.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from spreadboard import accounts, alerts

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_STATE_PATH = RUNTIME_DIR / "operator_alert_state.json"
_LOCK = threading.Lock()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": {}}
    return value if isinstance(value, dict) else {"events": {}}


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


def notify_transition(
    key: str,
    *,
    active: bool,
    title: str,
    message: str,
    db_path: Path | str = accounts.DEFAULT_DB_PATH,
    state_path: Path | str = DEFAULT_STATE_PATH,
) -> dict[str, Any]:
    """Deliver once when a fault opens and once when it recovers."""

    clean_key = str(key).strip()
    if not clean_key:
        raise ValueError("operator_alert_key_required")
    path = Path(state_path)
    now = time.time()
    with _LOCK:
        state = _load(path)
        events = state.setdefault("events", {})
        previous = events.get(clean_key) if isinstance(events.get(clean_key), dict) else {}
        changed = bool(previous.get("active")) != bool(active)
        if not changed:
            return {"changed": False, "delivered": 0, "active": bool(active)}
        events[clean_key] = {
            "active": bool(active),
            "changed_at_unix": now,
            "title": str(title)[:120],
            "message": str(message)[:500],
        }
        state["updated_at_unix"] = now
        _atomic_write(path, state)

    rendered = message if active else f"RECOVERED: {message}"
    LOGGER.error("%s: %s", title, rendered) if active else LOGGER.info("%s: %s", title, rendered)
    app_token = os.environ.get("SPREADBOARD_PUSHOVER_APP_TOKEN", "").strip()
    if not app_token:
        return {
            "changed": True,
            "delivered": 0,
            "active": bool(active),
            "delivery": "pushover_not_configured",
        }

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
    return {
        "changed": True,
        "delivered": delivered,
        "active": bool(active),
        "errors": errors,
    }
