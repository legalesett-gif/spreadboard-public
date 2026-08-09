"""Prepaid membership expiry reconciliation and service notifications.

Crypto access has no automatic renewal, so a stored end date is both the
authorization boundary and a customer-service deadline.  This worker turns it
into deterministic seven-day, three-day, one-day, and expired events.  Events
are persisted before delivery, deduplicated by the exact paid term, and retried
after transient delivery errors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

from spreadboard import accounts, mailer


EVENT_WINDOWS = (
    ("one_day", timedelta(days=1)),
    ("three_days", timedelta(days=3)),
    ("seven_days", timedelta(days=7)),
)
MAX_DELIVERY_ATTEMPTS = 5


def _utc(value: datetime | None = None) -> datetime:
    moment = value or datetime.now(tz=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _event_for(expiry: datetime, now: datetime) -> str | None:
    remaining = expiry - now
    if remaining.total_seconds() <= 0:
        return "expired"
    for event_type, window in EVENT_WINDOWS:
        if remaining <= window:
            return event_type
    return None


def _copy(event_type: str, expiry: datetime) -> tuple[str, str]:
    date = expiry.astimezone(timezone.utc).strftime("%d %B %Y at %H:%M UTC")
    if event_type == "expired":
        return (
            "Your SpreadBoard access has expired",
            f"Your prepaid access ended on {date}. The account remains available, but paid scanner and Research Pro features are paused until a new exact crypto invoice is paid.",
        )
    labels = {"seven_days": "seven days", "three_days": "three days", "one_day": "one day"}
    return (
        f"SpreadBoard access ends in {labels[event_type]}",
        f"Your prepaid access is scheduled to end on {date}. There is no automatic charge. Pay a new same-tier invoice before then if you want uninterrupted access.",
    )


def _discover(*, db_path: Path | str, now: datetime) -> int:
    """Persist the most urgent event currently due for each paid member."""
    connection = accounts._connect(db_path)
    discovered = 0
    now_iso = accounts._utc_iso(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT id, subscription_expires_at FROM users
               WHERE role != 'admin'
                 AND subscription_status IN ('active', 'trialing')
                 AND subscription_expires_at IS NOT NULL"""
        ).fetchall()
        for row in rows:
            expiry = _parse_iso(str(row["subscription_expires_at"] or ""))
            if expiry is None:
                continue
            event_type = _event_for(expiry, now)
            if event_type is None:
                continue
            cursor = connection.execute(
                """INSERT OR IGNORE INTO subscription_lifecycle_events (
                       user_id, subscription_expires_at, event_type, state,
                       attempts, created_at, updated_at
                   ) VALUES (?, ?, ?, 'pending', 0, ?, ?)""",
                (int(row["id"]), accounts._utc_iso(expiry), event_type, now_iso, now_iso),
            )
            discovered += int(cursor.rowcount > 0)
            if event_type == "expired":
                connection.execute(
                    """UPDATE users SET subscription_status = 'inactive', updated_at = ?
                       WHERE id = ? AND subscription_status IN ('active', 'trialing')""",
                    (now_iso, int(row["id"])),
                )
        connection.commit()
        return discovered
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _pending(*, db_path: Path | str, limit: int = 200) -> list[dict[str, Any]]:
    connection = accounts._connect(db_path)
    try:
        return [dict(row) for row in connection.execute(
            """SELECT e.*, u.email, u.display_name, t.chat_id AS telegram_chat_id
               FROM subscription_lifecycle_events e
               JOIN users u ON u.id = e.user_id
               LEFT JOIN telegram_links t ON t.user_id = e.user_id
               WHERE e.state IN ('pending', 'error') AND e.attempts < ?
               ORDER BY CASE e.event_type
                   WHEN 'expired' THEN 0 WHEN 'one_day' THEN 1
                   WHEN 'three_days' THEN 2 ELSE 3 END,
                   e.created_at
               LIMIT ?""",
            (MAX_DELIVERY_ATTEMPTS, max(1, min(1000, int(limit)))),
        ).fetchall()]
    finally:
        connection.close()


def _record(
    event: dict[str, Any], *, db_path: Path | str, state: str,
    notification_id: int | None, email_status: str, telegram_status: str,
    error: str = "",
) -> None:
    connection = accounts._connect(db_path)
    try:
        connection.execute(
            """UPDATE subscription_lifecycle_events SET
                   state = ?, attempts = attempts + 1,
                   in_app_notification_id = COALESCE(in_app_notification_id, ?),
                   email_status = ?, telegram_status = ?, last_error = ?, updated_at = ?
               WHERE user_id = ? AND subscription_expires_at = ? AND event_type = ?""",
            (
                state, notification_id, email_status, telegram_status,
                str(error or "")[:300] or None, accounts._utc_iso(),
                int(event["user_id"]), str(event["subscription_expires_at"]),
                str(event["event_type"]),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _deliver(event: dict[str, Any], *, db_path: Path | str) -> bool:
    expiry = _parse_iso(str(event["subscription_expires_at"])) or _utc()
    title, body = _copy(str(event["event_type"]), expiry)
    notification_id = event.get("in_app_notification_id")
    if not notification_id:
        notification = accounts.create_notification(
            int(event["user_id"]), title=title, body=body, db_path=db_path
        )
        notification_id = int(notification["id"])

    errors: list[str] = []
    email_status = str(event.get("email_status") or "not_configured")
    if email_status != "sent" and mailer.status()["configured"]:
        try:
            mailer.send_subscription_notice(
                recipient=str(event["email"]),
                display_name=str(event["display_name"]),
                subject=title,
                body=body,
                action_url=(
                    str(os.environ.get("SPREADBOARD_PUBLIC_URL", "")).rstrip("/")
                    + "/subscription"
                ),
            )
            email_status = "sent"
        except Exception as exc:  # noqa: BLE001 - persisted and retried.
            email_status = "error"
            errors.append(f"email:{type(exc).__name__}")

    telegram_status = str(event.get("telegram_status") or "not_linked")
    if telegram_status != "sent" and event.get("telegram_chat_id") is not None:
        try:
            from spreadboard import telegram_bot

            telegram_bot.send_direct_message(
                int(event["telegram_chat_id"]),
                f"{title}\n\n{body}\n\nOpen: "
                f"{str(os.environ.get('SPREADBOARD_PUBLIC_URL', '')).rstrip('/')}/subscription",
            )
            telegram_status = "sent"
        except Exception as exc:  # noqa: BLE001 - persisted and retried.
            telegram_status = "error"
            errors.append(f"telegram:{type(exc).__name__}")

    _record(
        event,
        db_path=db_path,
        state="error" if errors else "sent",
        notification_id=notification_id,
        email_status=email_status,
        telegram_status=telegram_status,
        error=", ".join(errors),
    )
    return not errors


def check_once(
    *, db_path: Path | str = accounts.DEFAULT_DB_PATH, now: datetime | None = None
) -> dict[str, int]:
    moment = _utc(now)
    discovered = _discover(db_path=db_path, now=moment)
    delivered = failed = 0
    for event in _pending(db_path=db_path):
        if _deliver(event, db_path=db_path):
            delivered += 1
        else:
            failed += 1
    return {"discovered": discovered, "delivered": delivered, "failed": failed}


def status(*, db_path: Path | str = accounts.DEFAULT_DB_PATH, now: datetime | None = None) -> dict[str, Any]:
    moment = _utc(now)
    now_iso = accounts._utc_iso(moment)
    seven_iso = accounts._utc_iso(moment + timedelta(days=7))
    connection = accounts._connect(db_path)
    try:
        rows = connection.execute(
            """SELECT subscription_tier, COUNT(*) AS count FROM users
               WHERE role != 'admin' AND subscription_status IN ('active', 'trialing')
                 AND (subscription_expires_at IS NULL OR subscription_expires_at > ?)
               GROUP BY subscription_tier""",
            (now_iso,),
        ).fetchall()
        active = {str(row["subscription_tier"]): int(row["count"]) for row in rows}
        expiring = int(connection.execute(
            """SELECT COUNT(*) FROM users WHERE role != 'admin'
               AND subscription_status IN ('active', 'trialing')
               AND subscription_expires_at > ? AND subscription_expires_at <= ?""",
            (now_iso, seven_iso),
        ).fetchone()[0])
        inconsistent_expired = int(connection.execute(
            """SELECT COUNT(*) FROM users WHERE role != 'admin'
               AND subscription_status IN ('active', 'trialing')
               AND subscription_expires_at IS NOT NULL AND subscription_expires_at <= ?""",
            (now_iso,),
        ).fetchone()[0])
        linked = int(connection.execute("SELECT COUNT(*) FROM telegram_links").fetchone()[0])
        errors = int(connection.execute(
            "SELECT COUNT(*) FROM subscription_lifecycle_events WHERE state = 'error'"
        ).fetchone()[0])
        return {
            "initialized": True,
            "active_total": sum(active.values()),
            "active_by_tier": active,
            "expiring_within_7_days": expiring,
            "expired_pending_reconciliation": inconsistent_expired,
            "telegram_linked_accounts": linked,
            "delivery_errors": errors,
        }
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        # Health endpoints must remain usable during first boot and in tooling
        # that intentionally supplies an empty accounts database.
        return {
            "initialized": False,
            "active_total": 0,
            "active_by_tier": {},
            "expiring_within_7_days": 0,
            "expired_pending_reconciliation": 0,
            "telegram_linked_accounts": 0,
            "delivery_errors": 0,
        }
    finally:
        connection.close()


class Worker:
    def __init__(
        self, *, db_path: Path | str = accounts.DEFAULT_DB_PATH,
        poll_seconds: float = 900.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.poll_seconds = max(60.0, float(poll_seconds))
        self.last_result: dict[str, int] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="spreadboard-subscription-lifecycle", daemon=True
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
        self._stop.wait(5.0)
        while not self._stop.is_set():
            try:
                self.last_result = check_once(db_path=self.db_path)
            except Exception as exc:  # noqa: BLE001 - worker must stay resident.
                self.last_result = {"discovered": 0, "delivered": 0, "failed": 1}
                print(f"spreadboard-subscription-lifecycle: {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(self.poll_seconds)
