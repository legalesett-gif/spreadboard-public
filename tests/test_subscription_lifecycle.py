from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from spreadboard import accounts, subscription_lifecycle


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _member(db: Path, *, email: str = "member@example.test", days: float = 6) -> int:
    user = accounts.create_user(
        email=email,
        display_name=email.split("@", 1)[0],
        password="a-secure-member-password",
        subscription_status="active",
        subscription_tier="research_pro",
        db_path=db,
    )
    accounts.update_subscription(
        user["id"],
        status="active",
        expires_at=(NOW + timedelta(days=days)).isoformat(),
        tier="research_pro",
        db_path=db,
    )
    return int(user["id"])


def test_due_notice_is_persisted_and_deduplicated(tmp_path, monkeypatch) -> None:
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    user_id = _member(db, days=6)
    monkeypatch.setattr(subscription_lifecycle.mailer, "status", lambda: {"configured": False})

    first = subscription_lifecycle.check_once(db_path=db, now=NOW)
    second = subscription_lifecycle.check_once(db_path=db, now=NOW)

    assert first == {"discovered": 1, "delivered": 1, "failed": 0}
    assert second == {"discovered": 0, "delivered": 0, "failed": 0}
    notices = accounts.list_notifications(user_id, db_path=db)
    assert len(notices) == 1 and "seven days" in notices[0]["title"]


def test_expiry_revokes_paid_state_and_records_notice(tmp_path, monkeypatch) -> None:
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    user_id = _member(db, days=-1)
    monkeypatch.setattr(subscription_lifecycle.mailer, "status", lambda: {"configured": False})

    result = subscription_lifecycle.check_once(db_path=db, now=NOW)

    assert result == {"discovered": 1, "delivered": 1, "failed": 0}
    user = accounts.get_user_object(user_id, db_path=db)
    assert user is not None and user.subscription_status == "inactive"
    assert user.subscription_active is False
    assert "expired" in accounts.list_notifications(user_id, db_path=db)[0]["title"].lower()


def test_email_and_linked_telegram_are_sent_once(tmp_path, monkeypatch) -> None:
    from spreadboard import telegram_bot

    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    user_id = _member(db, days=0.5)
    token = accounts.create_telegram_link_token(user_id, db_path=db)
    accounts.bind_telegram_chat(token, 12345, db_path=db)
    emails: list[dict] = []
    messages: list[tuple[int, str]] = []
    monkeypatch.setattr(subscription_lifecycle.mailer, "status", lambda: {"configured": True})
    monkeypatch.setattr(
        subscription_lifecycle.mailer,
        "send_subscription_notice",
        lambda **kwargs: emails.append(kwargs),
    )
    monkeypatch.setattr(
        telegram_bot,
        "send_direct_message",
        lambda chat_id, text: messages.append((chat_id, text)) or {"ok": True},
    )

    subscription_lifecycle.check_once(db_path=db, now=NOW)
    subscription_lifecycle.check_once(db_path=db, now=NOW)

    assert len(emails) == 1
    assert messages and messages[0][0] == 12345
    assert "one day" in emails[0]["subject"]


def test_retry_does_not_repeat_a_channel_that_already_succeeded(tmp_path, monkeypatch) -> None:
    from spreadboard import telegram_bot

    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    user_id = _member(db, days=0.5)
    token = accounts.create_telegram_link_token(user_id, db_path=db)
    accounts.bind_telegram_chat(token, 12345, db_path=db)
    emails: list[dict] = []
    telegram_attempts = 0

    monkeypatch.setattr(subscription_lifecycle.mailer, "status", lambda: {"configured": True})
    monkeypatch.setattr(
        subscription_lifecycle.mailer,
        "send_subscription_notice",
        lambda **kwargs: emails.append(kwargs),
    )

    def flaky_telegram(_chat_id: int, _text: str) -> dict:
        nonlocal telegram_attempts
        telegram_attempts += 1
        if telegram_attempts == 1:
            raise RuntimeError("temporary_telegram_error")
        return {"ok": True}

    monkeypatch.setattr(telegram_bot, "send_direct_message", flaky_telegram)

    first = subscription_lifecycle.check_once(db_path=db, now=NOW)
    second = subscription_lifecycle.check_once(db_path=db, now=NOW)

    assert first["failed"] == 1
    assert second["delivered"] == 1
    assert len(emails) == 1
    assert telegram_attempts == 2


def test_status_and_worker_path_handle_one_hundred_subscribers(tmp_path, monkeypatch) -> None:
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    connection = accounts._connect(db)
    try:
        now_iso = accounts._utc_iso(NOW)
        expiry = accounts._utc_iso(NOW + timedelta(days=6))
        password_hash = accounts.hash_password("a-secure-member-password")
        connection.executemany(
            """INSERT INTO users (
                   email, display_name, password_hash, role, subscription_status,
                   subscription_expires_at, subscription_tier, created_at, updated_at
               ) VALUES (?, ?, ?, 'member', 'active', ?, 'research_pro', ?, ?)""",
            [
                (f"member-{index}@example.test", f"Member {index}", password_hash, expiry, now_iso, now_iso)
                for index in range(100)
            ],
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(subscription_lifecycle.mailer, "status", lambda: {"configured": False})

    before = subscription_lifecycle.status(db_path=db, now=NOW)
    result = subscription_lifecycle.check_once(db_path=db, now=NOW)
    after = subscription_lifecycle.status(db_path=db, now=NOW)

    assert before["active_total"] == 100
    assert before["expiring_within_7_days"] == 100
    assert result == {"discovered": 100, "delivered": 100, "failed": 0}
    assert after["delivery_errors"] == 0
