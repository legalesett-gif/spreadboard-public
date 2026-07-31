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


def test_group_setup_requires_admin_and_records_community(tmp_path, monkeypatch) -> None:
    db_path = _linked_user(tmp_path)
    monkeypatch.setenv("SPREADBOARD_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SPREADBOARD_TELEGRAM_BOT_USERNAME", "spreadboard_test_bot")
    monkeypatch.setenv("SPREADBOARD_TELEGRAM_WEBHOOK_SECRET", "secret")

    def fake_api(method, params):
        if method == "getChatMember" and params["user_id"] == 42:
            return {"ok": True, "result": {"status": "creator"}}
        if method == "getMe":
            return {"ok": True, "result": {"id": 99}}
        if method == "getChatMember" and params["user_id"] == 99:
            return {"ok": True, "result": {"status": "administrator", "can_invite_users": True}}
        if method == "createChatInviteLink":
            return {"ok": True, "result": {"invite_link": "https://t.me/+join-request"}}
        raise AssertionError((method, params))

    monkeypatch.setattr(telegram_bot, "_api_call", fake_api)
    response = telegram_bot.handle_update(
        {"message": {
            "chat": {"id": -100123, "type": "supergroup", "title": "Subscribers"},
            "from": {"id": 42},
            "text": "/setupgroup",
        }},
        db_path=db_path,
    )
    assert "connected" in response["text"]
    assert accounts.telegram_community(db_path=db_path)["chat_id"] == -100123


def test_join_request_only_approves_active_linked_subscriber(tmp_path, monkeypatch) -> None:
    db_path = _linked_user(tmp_path)
    accounts.configure_telegram_community(
        -100123, title="Subscribers", configured_by_telegram_user_id=42, db_path=db_path
    )
    calls = []
    monkeypatch.setattr(
        telegram_bot,
        "_api_call",
        lambda method, params: calls.append((method, params)) or {"ok": True, "result": True},
    )
    telegram_bot.handle_update(
        {"chat_join_request": {"chat": {"id": -100123}, "from": {"id": 77}}},
        db_path=db_path,
    )
    assert calls[0][0] == "approveChatJoinRequest"
    assert accounts.telegram_membership_candidates(db_path=db_path)[0]["membership_state"] == "active"


def test_membership_worker_removes_expired_non_admin(tmp_path, monkeypatch) -> None:
    db_path = _linked_user(tmp_path)
    user = accounts.user_for_telegram_chat(77, db_path=db_path)
    accounts.update_subscription(user.id, status="cancelled", expires_at=None, db_path=db_path)
    accounts.configure_telegram_community(
        -100123, title="Subscribers", configured_by_telegram_user_id=42, db_path=db_path
    )
    calls = []

    def fake_api(method, params):
        calls.append(method)
        if method == "getChatMember":
            return {"ok": True, "result": {"status": "member"}}
        return {"ok": True, "result": True}

    monkeypatch.setattr(telegram_bot, "_api_call", fake_api)
    summary = telegram_bot.MembershipWorker(db_path=db_path).check_once()
    assert summary == {"checked": 1, "removed": 1}
    assert calls == ["getChatMember", "banChatMember", "unbanChatMember"]
