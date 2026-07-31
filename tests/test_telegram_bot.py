from __future__ import annotations

import pytest

from spreadboard import accounts, billing, telegram_bot


def _linked_user(tmp_path):
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    created = accounts.create_user(
        email="member@example.test", display_name="Member",
        password="secure-member-password", subscription_status="active", db_path=db_path,
    )
    token = accounts.create_telegram_link_token(created["id"], db_path=db_path)
    accounts.bind_telegram_chat(token, 77, db_path=db_path)
    return db_path


def test_webhook_secret_is_required(monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_TELEGRAM_WEBHOOK_SECRET", "secret-token")
    telegram_bot.verify_webhook("secret-token")
    with pytest.raises(telegram_bot.TelegramBotError, match="invalid_telegram_webhook_secret"):
        telegram_bot.verify_webhook("forged")


def test_subscription_command_uses_linked_account_checkout(tmp_path, monkeypatch) -> None:
    db_path = _linked_user(tmp_path)
    monkeypatch.setattr(billing, "create_checkout_session", lambda user: f"https://checkout.stripe.com/{user.id}")
    response = telegram_bot.handle_update(
        {"message": {"chat": {"id": 77, "type": "private"}, "text": "/subscribe"}},
        db_path=db_path,
    )
    assert response["method"] == "sendMessage"
    assert response["reply_markup"]["inline_keyboard"][0][0]["url"].startswith("https://checkout.stripe.com/")


def test_unlinked_chat_cannot_read_subscription(tmp_path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    response = telegram_bot.handle_update(
        {"message": {"chat": {"id": 88, "type": "private"}, "text": "/mysubscription"}},
        db_path=db_path,
    )
    assert "Link this chat" in response["text"]


def test_group_messages_are_ignored(tmp_path) -> None:
    db_path = _linked_user(tmp_path)
    response = telegram_bot.handle_update(
        {"message": {"chat": {"id": -100123, "type": "supergroup"}, "text": "/subscribe"}},
        db_path=db_path,
    )
    assert response is None
