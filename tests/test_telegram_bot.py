from __future__ import annotations

import pytest

from spreadboard import accounts, billing, telegram_bot


def _linked_user(tmp_path, *, tier="research_pro"):
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    created = accounts.create_user(
        email="member@example.test", display_name="Member",
        password="secure-member-password", subscription_status="active",
        subscription_tier=tier, db_path=db_path,
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


def test_private_help_lists_every_exact_token_lookup_form(tmp_path) -> None:
    db_path = _linked_user(tmp_path)

    response = telegram_bot.handle_update(
        {"message": {"chat": {"id": 77, "type": "private"}, "text": "/help"}},
        db_path=db_path,
    )

    assert "$SIREN" in response["text"]
    assert "/token SIREN" in response["text"]
    assert "@spreadarbitragesubscription_bot SIREN" in response["text"]


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


def test_scanner_member_cannot_request_or_join_the_research_pro_forum(
    tmp_path, monkeypatch
) -> None:
    db_path = _linked_user(tmp_path, tier="scanner")
    accounts.configure_telegram_community(
        -100123, title="Subscribers", configured_by_telegram_user_id=42, db_path=db_path
    )
    calls = []
    monkeypatch.setattr(
        telegram_bot,
        "_api_call",
        lambda method, params: calls.append((method, params)) or {"ok": True, "result": True},
    )

    response = telegram_bot.handle_update(
        {"message": {"chat": {"id": 77, "type": "private"}, "text": "/access"}},
        db_path=db_path,
    )
    assert "Research Pro" in response["text"]
    assert calls == [], "a Scanner account must not receive an invite link"

    telegram_bot.handle_update(
        {"chat_join_request": {"chat": {"id": -100123}, "from": {"id": 77}}},
        db_path=db_path,
    )
    assert calls[0][0] == "declineChatJoinRequest"


def test_membership_worker_removes_an_active_scanner_member(tmp_path, monkeypatch) -> None:
    db_path = _linked_user(tmp_path, tier="scanner")
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

    assert summary == {"checked": 1, "removed": 1, "errors": 0}
    assert calls == ["getChatMember", "banChatMember", "unbanChatMember"]


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
    assert summary == {"checked": 1, "removed": 1, "errors": 0}
    assert calls == ["getChatMember", "banChatMember", "unbanChatMember"]


def test_membership_worker_records_one_invalid_link_and_continues(tmp_path, monkeypatch) -> None:
    db_path = _linked_user(tmp_path)
    accounts.configure_telegram_community(
        -100123, title="Subscribers", configured_by_telegram_user_id=42, db_path=db_path
    )
    monkeypatch.setattr(
        telegram_bot,
        "_api_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            telegram_bot.TelegramBotError("telegram_api_error:BAD_REQUEST_PARTICIPANT_ID_INVALID")
        ),
    )

    summary = telegram_bot.MembershipWorker(db_path=db_path).check_once()

    assert summary == {"checked": 1, "removed": 0, "errors": 1}
    candidate = accounts.telegram_membership_candidates(db_path=db_path)[0]
    assert candidate["membership_state"] == "error"


def test_public_digest_has_deep_links_without_claiming_tradeability(monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadarbitrage.ink")
    monkeypatch.setattr(
        telegram_bot.telegram_queries,
        "payload_status",
        lambda: {
            "ready": True,
            "age_seconds": 125.0,
            "token_count": 691,
            "route_count": 2_511,
        },
    )
    monkeypatch.setattr(telegram_bot.telegram_queries, "suggest", lambda *_args, **_kwargs: [
        {"token": "SIREN", "best_edge_pct": 2.5, "route_count": 4, "venues": ["Gate", "OKX DEX"]}
    ])
    text = telegram_bot.render_public_digest(board_path="ignored")
    assert "SIREN" in text and "+2.50%" in text
    assert "view=table" in text and "research" in text.lower()
    assert "latest completed research snapshot" in text.lower()
    assert "Updated 2m ago · 691 tokens · 2,511 routes" in text


def test_public_digest_reports_snapshot_warming_instead_of_no_routes(monkeypatch) -> None:
    """A cold process has no market answer yet, not evidence of no opportunity."""
    monkeypatch.setattr(
        telegram_bot.telegram_queries,
        "payload_status",
        lambda: {
            "ready": False,
            "age_seconds": None,
            "token_count": 0,
            "route_count": 0,
        },
    )
    monkeypatch.setattr(
        telegram_bot.telegram_queries,
        "suggest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a cold snapshot must not be queried")
        ),
    )

    text = telegram_bot.render_public_digest(board_path="ignored")

    assert "snapshot is still warming" in text.lower()
    assert "try /top again shortly" in text.lower()
    assert "no fresh routes matched" not in text.lower()


def test_public_digest_escapes_upstream_labels_before_telegram_html(monkeypatch) -> None:
    """Exchange metadata is display data, never trusted Telegram markup."""
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadarbitrage.ink")
    monkeypatch.setattr(
        telegram_bot.telegram_queries,
        "payload_status",
        lambda: {
            "ready": True,
            "age_seconds": 5.0,
            "token_count": 1,
            "route_count": 1,
        },
    )
    monkeypatch.setattr(
        telegram_bot.telegram_queries,
        "suggest",
        lambda *_args, **_kwargs: [
            {
                "token": '<b>SIREN & "friends"</b>',
                "best_edge_pct": 1.0,
                "route_count": "not-a-count",
                "venues": ["Gate & Co", "<i>Not markup</i>"],
            }
        ],
    )

    text = telegram_bot.render_public_digest(board_path="ignored")

    assert "Updated 5s ago · 1 token · 1 route" in text
    assert "· 0 routes · Gate &amp; Co" in text
    assert '&lt;b&gt;SIREN &amp; "friends"&lt;/b&gt;' in text
    assert "Gate &amp; Co / &lt;i&gt;Not markup&lt;/i&gt;" in text
    assert "<b><b>SIREN" not in text
    assert "q=%3Cb%3ESIREN%20%26%20%22friends%22%3C%2Fb%3E" in text


def test_top_command_is_available_before_account_linking(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    monkeypatch.setattr(telegram_bot, "render_public_digest", lambda **_: "Top routes preview")
    response = telegram_bot.handle_update(
        {"message": {"chat": {"id": 88, "type": "private"}, "text": "/top"}},
        db_path=db_path, board_path="board",
    )
    assert response["text"] == "Top routes preview"


def test_top_command_does_not_mislabel_a_cold_snapshot_as_no_routes(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    monkeypatch.setattr(
        telegram_bot.telegram_queries,
        "payload_status",
        lambda: {
            "ready": False,
            "age_seconds": None,
            "token_count": 0,
            "route_count": 0,
        },
    )

    response = telegram_bot.handle_update(
        {"message": {"chat": {"id": 88, "type": "private"}, "text": "/top"}},
        db_path=db_path,
        board_path="board",
    )

    assert "snapshot is still warming" in response["text"].lower()
    assert "no fresh routes matched" not in response["text"].lower()


def test_public_feed_worker_requires_an_explicit_chat(monkeypatch) -> None:
    monkeypatch.delenv("SPREADBOARD_TELEGRAM_PUBLIC_FEED_CHAT_ID", raising=False)
    worker = telegram_bot.PublicFeedWorker(board_path="board", poll_seconds=300)
    assert worker.check_once() == {"status": "not_configured", "sent": 0}
