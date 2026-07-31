"""Secure Telegram account linking and subscription commands."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import os
from typing import Any
from urllib.parse import quote

from spreadboard import accounts, billing


class TelegramBotError(RuntimeError):
    """A safe Telegram integration failure."""


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    bot_username: str
    webhook_secret: str

    @property
    def ready(self) -> bool:
        return bool(self.bot_token and self.bot_username and self.webhook_secret)


def config() -> TelegramConfig:
    username = os.environ.get("SPREADBOARD_TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    if username and not all(character.isalnum() or character == "_" for character in username):
        username = ""
    return TelegramConfig(
        bot_token=os.environ.get("SPREADBOARD_TELEGRAM_BOT_TOKEN", "").strip(),
        bot_username=username,
        webhook_secret=os.environ.get("SPREADBOARD_TELEGRAM_WEBHOOK_SECRET", "").strip(),
    )


def status() -> dict[str, Any]:
    value = config()
    return {
        "configured": value.ready,
        "bot_username": value.bot_username or None,
        "webhook_ready": bool(value.webhook_secret),
    }


def link_url(token: str) -> str:
    username = config().bot_username
    if not username:
        raise TelegramBotError("telegram_bot_not_configured")
    return f"https://t.me/{username}?start={quote(token, safe='')}"


def verify_webhook(secret_header: str) -> None:
    expected = config().webhook_secret
    if not expected:
        raise TelegramBotError("telegram_webhook_not_configured")
    if not hmac.compare_digest(expected, str(secret_header or "")):
        raise TelegramBotError("invalid_telegram_webhook_secret")


def handle_update(update: dict[str, Any], *, db_path: Any) -> dict[str, Any] | None:
    message = update.get("message") if isinstance(update.get("message"), dict) else None
    if not message:
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    if chat.get("type") != "private":
        return _reply(int(chat.get("id") or 0), "Please use this bot in a private chat.")
    chat_id = int(chat.get("id") or 0)
    text = str(message.get("text") or "").strip()
    command, _, argument = text.partition(" ")
    command = command.split("@", 1)[0].casefold()
    if command == "/start" and argument.strip():
        try:
            user = accounts.bind_telegram_chat(argument.strip(), chat_id, db_path=db_path)
        except ValueError:
            return _reply(chat_id, "This link is invalid or expired. Generate a new link in your SpreadBoard account.")
        return _reply(chat_id, f"Linked to {user.display_name}. Use /subscribe or /mysubscription.")
    user = accounts.user_for_telegram_chat(chat_id, db_path=db_path)
    if user is None:
        return _reply(chat_id, "Link this chat from Account settings in SpreadBoard first.")
    if command == "/mysubscription":
        expiry = user.subscription_expires_at or "no fixed expiry"
        state = "active" if user.subscription_active else user.subscription_status
        return _reply(chat_id, f"Subscription: {state}. Valid until: {expiry}.")
    if command == "/subscribe":
        try:
            url = billing.create_checkout_session(user)
        except billing.BillingError:
            return _reply(chat_id, "Online checkout is not configured yet. Your account has not been charged.")
        return _reply(chat_id, "Your secure monthly subscription link is ready.", button=("Open payment page", url))
    return _reply(chat_id, "Commands: /subscribe, /mysubscription")


def _reply(chat_id: int, text: str, *, button: tuple[str, str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"method": "sendMessage", "chat_id": chat_id, "text": text}
    if button:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
    return payload


def parse_update(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramBotError("invalid_telegram_payload") from exc
    if not isinstance(payload, dict):
        raise TelegramBotError("invalid_telegram_payload")
    return payload
