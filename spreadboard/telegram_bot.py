"""Secure Telegram account linking, billing, and subscriber community access."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import os
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from spreadboard import accounts, billing, telegram_queries


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


def status(*, db_path: Any = accounts.DEFAULT_DB_PATH) -> dict[str, Any]:
    value = config()
    community = accounts.telegram_community(db_path=db_path) if value.ready else None
    return {
        "configured": value.ready,
        "bot_username": value.bot_username or None,
        "webhook_ready": bool(value.webhook_secret),
        "community_configured": community is not None,
        "community_title": community.get("title") if community else None,
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


def _handle_group_query(
    chat_id: int, text: str, *, db_path: Any, board_path: Any,
    thread_id: int | None = None,
) -> dict[str, Any] | None:
    """Answer $TOKEN lookups, but only inside the registered subscriber group.

    Membership is enforced at the door by join approval and expiry removal, so
    everyone here is a paying member. Any other group is ignored outright.
    """
    community = accounts.telegram_community(db_path=db_path)
    if community is None or int(community["chat_id"]) != int(chat_id):
        return None
    query = telegram_queries.parse_query(text)
    if query is None or not telegram_queries.allow(chat_id, query):
        return None
    if board_path is None:
        return None
    try:
        body = telegram_queries.render(
            query,
            board_path=board_path,
            public_url=os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip(),
        )
    except Exception:  # noqa: BLE001 - a lookup failure must never break the webhook
        return None
    return _reply(chat_id, body, html=True, thread_id=thread_id)


def handle_update(
    update: dict[str, Any], *, db_path: Any, board_path: Any = None
) -> dict[str, Any] | None:
    join_request = update.get("chat_join_request")
    if isinstance(join_request, dict):
        _handle_join_request(join_request, db_path=db_path)
        return {"ok": True}

    message = update.get("message") if isinstance(update.get("message"), dict) else None
    if not message:
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_id = int(chat.get("id") or 0)
    sender_id = int(sender.get("id") or 0)
    text = str(message.get("text") or "").strip()
    raw_thread = message.get("message_thread_id")
    thread_id = int(raw_thread) if isinstance(raw_thread, int) else None
    command, _, argument = text.partition(" ")
    command = command.split("@", 1)[0].casefold()

    if chat.get("type") in {"group", "supergroup"}:
        if command == "/setupgroup":
            _configure_group(chat, sender_id=sender_id, db_path=db_path)
            return _reply(chat_id, "SpreadBoard subscriber access is now connected to this group. Payments and account details remain private.")
        return _handle_group_query(
            chat_id, text, db_path=db_path, board_path=board_path,
            thread_id=thread_id,
        )
    if chat.get("type") != "private":
        return None

    if command == "/start" and argument.strip():
        try:
            user = accounts.bind_telegram_chat(argument.strip(), chat_id, db_path=db_path)
        except ValueError:
            return _reply(chat_id, "This link is invalid or expired. Generate a new link in your SpreadBoard account.")
        suffix = " Use /access to request the subscriber group." if user.subscription_active else " Use /subscribe to activate access."
        return _reply(chat_id, f"Linked to {user.display_name}.{suffix}")
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
            return _reply(chat_id, "Online checkout is temporarily unavailable. Your account has not been charged.")
        return _reply(
            chat_id,
            f"Your secure {billing.config().plan_label} subscription link is ready.",
            button=("Open payment page", url),
        )
    if command == "/access":
        if not user.subscription_active:
            return _reply(chat_id, "An active membership is required. Use /subscribe to activate access.")
        community = accounts.telegram_community(db_path=db_path)
        if community is None:
            return _reply(chat_id, "The subscriber group is being configured. Please try again shortly.")
        invite = _create_join_request_link(int(community["chat_id"]))
        accounts.record_telegram_membership(
            user.id,
            telegram_user_id=chat_id,
            community_chat_id=int(community["chat_id"]),
            state="pending",
            db_path=db_path,
        )
        return _reply(chat_id, "Tap below, then request to join. Active memberships are approved automatically.", button=("Request group access", invite))
    return _reply(chat_id, "Commands: /subscribe, /mysubscription, /access")


def _configure_group(chat: dict[str, Any], *, sender_id: int, db_path: Any) -> None:
    chat_id = int(chat.get("id") or 0)
    if not chat_id or not sender_id:
        raise TelegramBotError("invalid_group_setup")
    sender = _api_call("getChatMember", {"chat_id": chat_id, "user_id": sender_id}).get("result") or {}
    if sender.get("status") not in {"creator", "administrator"}:
        raise TelegramBotError("group_admin_required")
    bot_id = int(((_api_call("getMe", {}).get("result") or {}).get("id")) or 0)
    bot_member = _api_call("getChatMember", {"chat_id": chat_id, "user_id": bot_id}).get("result") or {}
    if bot_member.get("status") != "administrator" or not bot_member.get("can_invite_users"):
        raise TelegramBotError("bot_group_admin_with_invite_permission_required")
    invite = _create_join_request_link(chat_id)
    accounts.configure_telegram_community(
        chat_id,
        title=str(chat.get("title") or "SpreadBoard community"),
        configured_by_telegram_user_id=sender_id,
        invite_link=invite,
        db_path=db_path,
    )


def _handle_join_request(request: dict[str, Any], *, db_path: Any) -> None:
    chat = request.get("chat") if isinstance(request.get("chat"), dict) else {}
    sender = request.get("from") if isinstance(request.get("from"), dict) else {}
    chat_id = int(chat.get("id") or 0)
    sender_id = int(sender.get("id") or 0)
    community = accounts.telegram_community(db_path=db_path)
    if community is None or int(community["chat_id"]) != chat_id:
        return
    user = accounts.user_for_telegram_chat(sender_id, db_path=db_path)
    if user is None or not user.subscription_active:
        _api_call("declineChatJoinRequest", {"chat_id": chat_id, "user_id": sender_id})
        return
    _api_call("approveChatJoinRequest", {"chat_id": chat_id, "user_id": sender_id})
    accounts.record_telegram_membership(
        user.id,
        telegram_user_id=sender_id,
        community_chat_id=chat_id,
        state="active",
        db_path=db_path,
    )


class MembershipWorker:
    """Remove expired linked subscribers while leaving owners/admins untouched."""

    def __init__(self, *, db_path: Any = accounts.DEFAULT_DB_PATH, poll_seconds: float = 60.0) -> None:
        self.db_path = db_path
        self.poll_seconds = max(30.0, float(poll_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="spreadboard-telegram-membership", daemon=True)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        if config().ready and not self.running:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.running:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001
                print(f"spreadboard-telegram-membership: {type(exc).__name__}: {exc}", flush=True)

    def check_once(self) -> dict[str, int]:
        community = accounts.telegram_community(db_path=self.db_path)
        if community is None:
            return {"checked": 0, "removed": 0}
        chat_id = int(community["chat_id"])
        checked = removed = 0
        for candidate in accounts.telegram_membership_candidates(db_path=self.db_path):
            checked += 1
            telegram_user_id = int(candidate["telegram_user_id"])
            member = (_api_call("getChatMember", {"chat_id": chat_id, "user_id": telegram_user_id}).get("result") or {})
            state = str(member.get("status") or "left")
            user = accounts.get_user_object(int(candidate["user_id"]), db_path=self.db_path)
            if state in {"creator", "administrator"}:
                accounts.record_telegram_membership(user.id, telegram_user_id=telegram_user_id, community_chat_id=chat_id, state="exempt", db_path=self.db_path)
                continue
            present = state == "member" or (state == "restricted" and bool(member.get("is_member")))
            if user is not None and user.subscription_active:
                if present:
                    accounts.record_telegram_membership(user.id, telegram_user_id=telegram_user_id, community_chat_id=chat_id, state="active", db_path=self.db_path)
                continue
            if present:
                _api_call("banChatMember", {"chat_id": chat_id, "user_id": telegram_user_id, "revoke_messages": False})
                _api_call("unbanChatMember", {"chat_id": chat_id, "user_id": telegram_user_id, "only_if_banned": True})
                removed += 1
            if user is not None:
                accounts.record_telegram_membership(user.id, telegram_user_id=telegram_user_id, community_chat_id=chat_id, state="removed", db_path=self.db_path)
        return {"checked": checked, "removed": removed}


def _create_join_request_link(chat_id: int) -> str:
    result = _api_call(
        "createChatInviteLink",
        {"chat_id": int(chat_id), "name": "SpreadBoard membership", "creates_join_request": True},
    ).get("result") or {}
    invite = str(result.get("invite_link") or "")
    if not invite.startswith("https://t.me/"):
        raise TelegramBotError("telegram_invite_link_unavailable")
    return invite


def _api_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    token = config().bot_token
    if not token:
        raise TelegramBotError("telegram_bot_not_configured")
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed Telegram origin
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TelegramBotError("telegram_api_unavailable") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        description = str(payload.get("description") or "telegram_api_error") if isinstance(payload, dict) else "telegram_api_error"
        raise TelegramBotError(description[:200])
    return payload


def _reply(
    chat_id: int, text: str, *, button: tuple[str, str] | None = None, html: bool = False,
    thread_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"method": "sendMessage", "chat_id": chat_id, "text": text}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if html:
        payload["parse_mode"] = "HTML"
        payload["disable_web_page_preview"] = True
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
