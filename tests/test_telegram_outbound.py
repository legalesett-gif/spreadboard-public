"""Nothing posts into a chat unless it has been switched on.

The operator's own Telegram account is the one these messages come from. A
board that starts talking to a chat because a default flipped is not
recoverable -- the message is already sent -- so the switch is off unless set,
and every write goes through one place that checks it.
"""

from __future__ import annotations

import pytest

from spreadboard import telegram_bot


def test_posting_is_off_unless_explicitly_switched_on(monkeypatch) -> None:
    monkeypatch.delenv("SPREADBOARD_TELEGRAM_OUTBOUND", raising=False)
    assert telegram_bot.outbound_posting_enabled() is False

    for value in ("0", "", "no", "off", "false"):
        monkeypatch.setenv("SPREADBOARD_TELEGRAM_OUTBOUND", value)
        assert telegram_bot.outbound_posting_enabled() is False

    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("SPREADBOARD_TELEGRAM_OUTBOUND", value)
        assert telegram_bot.outbound_posting_enabled() is True


def test_a_send_raises_instead_of_posting(monkeypatch) -> None:
    """It must raise, not return quietly: a caller cannot mistake this for delivery."""
    monkeypatch.delenv("SPREADBOARD_TELEGRAM_OUTBOUND", raising=False)

    called = []
    monkeypatch.setattr(
        telegram_bot,
        "config",
        lambda: type("C", (), {"bot_token": "t"})(),
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("posted")),
    )

    with pytest.raises(telegram_bot.TelegramOutboundDisabled):
        telegram_bot._api_call("sendMessage", {"chat_id": 1, "text": "hi"})

    assert not called, "a request left the process"


def test_every_write_method_is_covered(monkeypatch) -> None:
    """Not just sendMessage -- edits and replies write into a chat too."""
    monkeypatch.delenv("SPREADBOARD_TELEGRAM_OUTBOUND", raising=False)
    monkeypatch.setattr(
        telegram_bot, "config", lambda: type("C", (), {"bot_token": "t"})()
    )

    for method in (
        "sendMessage",
        "sendPhoto",
        "editMessageText",
        "answerCallbackQuery",
        "createChatInviteLink",
        "banChatMember",
    ):
        with pytest.raises(telegram_bot.TelegramOutboundDisabled):
            telegram_bot._api_call(method, {})


def test_reads_still_work_so_membership_checks_do_not_break(monkeypatch) -> None:
    """Verifying who is in the group is not posting."""
    monkeypatch.delenv("SPREADBOARD_TELEGRAM_OUTBOUND", raising=False)
    monkeypatch.setattr(
        telegram_bot, "config", lambda: type("C", (), {"bot_token": "t"})()
    )

    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true, "result": {"status": "member"}}'

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        return Response()

    monkeypatch.setattr("spreadboard.telegram_bot.urlopen", fake_urlopen)

    payload = telegram_bot._api_call("getChatMember", {"chat_id": 1, "user_id": 2})

    assert payload["ok"] is True
    assert "getChatMember" in seen["url"]


def test_group_broadcast_is_blocked(monkeypatch) -> None:
    """The unsolicited path into the subscriber group."""
    monkeypatch.delenv("SPREADBOARD_TELEGRAM_OUTBOUND", raising=False)
    monkeypatch.setattr(
        telegram_bot, "config", lambda: type("C", (), {"bot_token": "t"})()
    )

    with pytest.raises(telegram_bot.TelegramOutboundDisabled):
        telegram_bot.send_group_message(-100123, "anything")
