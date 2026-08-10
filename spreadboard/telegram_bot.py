"""Secure Telegram account linking, billing, and subscriber community access."""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from spreadboard import accounts, billing, crypto_billing, telegram_queries


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
    candidates = accounts.telegram_membership_candidates(db_path=db_path) if value.ready else []
    query_snapshot = telegram_queries.payload_status()
    return {
        "configured": value.ready,
        "bot_username": value.bot_username or None,
        "webhook_ready": bool(value.webhook_secret),
        "community_configured": community is not None,
        "community_title": community.get("title") if community else None,
        "linked_accounts": len(candidates),
        "membership_errors": sum(
            1 for candidate in candidates if candidate.get("membership_state") == "error"
        ),
        "public_feed_configured": bool(public_feed_chat_id()),
        "public_feed_outbound_ready": bool(public_feed_chat_id() and outbound_posting_enabled()),
        "query_snapshot_ready": query_snapshot["ready"],
        "query_snapshot_age_seconds": query_snapshot["age_seconds"],
        "query_snapshot_token_count": query_snapshot["token_count"],
        "query_snapshot_route_count": query_snapshot["route_count"],
    }


def public_feed_chat_id() -> str | None:
    value = os.environ.get("SPREADBOARD_TELEGRAM_PUBLIC_FEED_CHAT_ID", "").strip()
    if not value:
        return None
    if value.startswith("@") and all(char.isalnum() or char in "_@" for char in value):
        return value
    if value.lstrip("-").isdigit():
        return value
    return None


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
    public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip()
    # A bare "$" -- or "$so" with nothing exact behind it -- is a request for
    # suggestions. Telegram gives a bot no autocomplete hook, so this is the
    # closest thing: offer what is actually moving and let them tap.
    # Only a bare "$" asks for suggestions. "$SIREN" is a lookup and must stay
    # one -- matching any short cashtag here turned every token query into a
    # suggestion list.
    bare = text.strip() == "$"
    if bare and board_path is not None:
        prefix = ""
        if not telegram_queries.payload_status()["ready"]:
            return _reply(
                chat_id,
                "The live Telegram snapshot is warming. Try again shortly.",
                thread_id=thread_id,
            )
        if not telegram_queries.allow(chat_id, telegram_queries.Query(kind="spread", symbol=prefix or "$")):
            return _reply(
                chat_id,
                "I answered this lookup less than a minute ago. Tap the previous result or try again shortly.",
                thread_id=thread_id,
            )
        try:
            symbols = telegram_queries.suggestions(prefix, board_path=board_path)
        except Exception:  # noqa: BLE001 - a lookup failure must never break the webhook
            return None
        if not symbols:
            return _reply(chat_id, "No token matches that right now.", thread_id=thread_id)
        heading = f"Tokens matching <b>{prefix.upper()}</b>:" if prefix else "Biggest spreads right now — tap one:"
        return _reply(
            chat_id, heading, html=True, thread_id=thread_id,
            markup=telegram_queries.suggestion_keyboard(symbols, public_url=public_url),
        )
    query = telegram_queries.parse_query(text, bot_username=config().bot_username)
    if query is None:
        return None
    if board_path is None:
        return _reply(chat_id, "The live scanner is warming. Try again shortly.", thread_id=thread_id)
    if not telegram_queries.payload_status()["ready"]:
        return _reply(
            chat_id,
            "The live Telegram snapshot is warming. Try again shortly.",
            thread_id=thread_id,
        )
    if not telegram_queries.allow(chat_id, query):
        return _reply(
            chat_id,
            f"I answered ${query.symbol} {query.kind} less than a minute ago. Tap the previous result or try again shortly.",
            thread_id=thread_id,
        )
    try:
        body = telegram_queries.render(
            query, board_path=board_path, public_url=public_url
        )
    except Exception:  # noqa: BLE001 - a lookup failure must never break the webhook
        return None
    markup = telegram_queries.keyboard(query, public_url=public_url)
    if "no parsed routes right now" in body:
        # A dead end helps nobody: offer what is near it instead.
        try:
            near = telegram_queries.suggestions(query.symbol, board_path=board_path)
        except Exception:  # noqa: BLE001
            near = []
        if near:
            markup = telegram_queries.suggestion_keyboard(near, public_url=public_url)
    return _reply(
        chat_id, body, html=True, thread_id=thread_id, markup=markup,
    )


def _handle_callback(cb: dict[str, Any], *, db_path: Any, board_path: Any) -> dict[str, Any] | None:
    """A view button was pressed: re-render the same token in the chosen view."""
    data = str(cb.get("data") or "")
    message = cb.get("message") if isinstance(cb.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = int(chat.get("id") or 0)
    community = accounts.telegram_community(db_path=db_path)
    if community is None or int(community["chat_id"]) != chat_id:
        return None
    parts = data.split(":", 2)
    if len(parts) == 2 and parts[0] == "t":
        # A suggested token was tapped: show its spread, with the view buttons.
        query = telegram_queries.Query(kind="spread", symbol=parts[1])
    elif len(parts) == 3 and parts[0] == "v":
        query = telegram_queries.Query(kind=parts[1], symbol=parts[2])
    else:
        return None
    public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip()
    try:
        body = telegram_queries.render(query, board_path=board_path, public_url=public_url)
    except Exception:  # noqa: BLE001
        return None
    return {
        "method": "editMessageText",
        "chat_id": chat_id,
        "message_id": int(message.get("message_id") or 0),
        "text": body,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": telegram_queries.keyboard(query, public_url=public_url),
    }


def _handle_inline_query(
    iq: dict[str, Any], *, board_path: Any, db_path: Any
) -> dict[str, Any] | None:
    """Token autocomplete via inline mode (@bot SIREN) for active members."""
    sender = iq.get("from") if isinstance(iq.get("from"), dict) else {}
    user = accounts.user_for_telegram_chat(int(sender.get("id") or 0), db_path=db_path)
    if user is None or not user.subscription_active:
        return {
            "method": "answerInlineQuery",
            "inline_query_id": str(iq.get("id") or ""),
            "results": [],
            "cache_time": 5,
            "is_personal": True,
        }
    try:
        matches = telegram_queries.suggest(str(iq.get("query") or ""), board_path=board_path)
    except Exception:  # noqa: BLE001
        return None
    results = []
    for item in matches[:20]:
        token = item["token"]
        edge = item.get("best_edge_pct")
        edge_txt = f"{float(edge):+.2f}% best edge" if edge is not None else "no edge data"
        results.append({
            "type": "article",
            "id": f"tok-{token}"[:64],
            "title": token,
            "description": f"{edge_txt} · {item.get('route_count') or 0} routes",
            "input_message_content": {"message_text": f"${token}"},
        })
    return {
        "method": "answerInlineQuery",
        "inline_query_id": str(iq.get("id") or ""),
        "results": results,
        "cache_time": 30,
        "is_personal": True,
    }


def handle_update(
    update: dict[str, Any], *, db_path: Any, board_path: Any = None
) -> dict[str, Any] | None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        return _handle_callback(callback, db_path=db_path, board_path=board_path)
    inline = update.get("inline_query")
    if isinstance(inline, dict):
        return _handle_inline_query(inline, board_path=board_path, db_path=db_path)
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
        suffix = (
            " Use /access to request the Research Pro forum."
            if user.has_tier("research_pro")
            else (
                " Your Scanner access is active; upgrade to Research Pro for the private forum."
                if user.has_tier("scanner")
                else " Use /subscribe to activate access."
            )
        )
        return _reply(chat_id, f"Linked to {user.display_name}.{suffix}")
    if command == "/top":
        if board_path is None:
            return _reply(chat_id, "The live scanner is warming. Try /top again shortly.")
        return _reply(chat_id, render_public_digest(board_path=board_path), html=True)
    if command in {"/start", "/help"}:
        public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip().rstrip("/")
        text = (
            "SpreadBoard checks executable cross-venue routes, settled funding, and transfer rails.\n\n"
            "Use /top for a public research preview. In the subscriber forum, look up any exact token with "
            "$SIREN, /token SIREN, or @spreadarbitragesubscription_bot SIREN. "
            "Link your account on the website for /subscribe, /mysubscription and /access."
        )
        return _reply(chat_id, text, button=("Open SpreadBoard", f"{public_url}/telegram") if public_url else None)
    user = accounts.user_for_telegram_chat(chat_id, db_path=db_path)
    if user is None:
        return _reply(chat_id, "Link this chat from Account settings in SpreadBoard first.")
    query = telegram_queries.parse_query(text, bot_username=config().bot_username)
    if query is not None:
        if not user.subscription_active:
            return _reply(chat_id, "An active membership is required. Use /subscribe to activate access.")
        if board_path is None or not telegram_queries.payload_status()["ready"]:
            return _reply(chat_id, "The live Telegram snapshot is warming. Try again shortly.")
        if not telegram_queries.allow(chat_id, query):
            return _reply(
                chat_id,
                f"I answered ${query.symbol} {query.kind} less than a minute ago. Tap the previous result or try again shortly.",
            )
        try:
            body = telegram_queries.render(
                query,
                board_path=board_path,
                public_url=os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip(),
            )
        except Exception:  # noqa: BLE001 - a lookup failure must not break the webhook.
            return _reply(chat_id, "The live Telegram snapshot is warming. Try again shortly.")
        return _reply(
            chat_id,
            body,
            html=True,
            markup=telegram_queries.keyboard(
                query,
                public_url=os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip(),
            ),
        )
    if command == "/mysubscription":
        expiry = user.subscription_expires_at or "no fixed expiry"
        state = "active" if user.subscription_active else user.subscription_status
        tier = user.entitlement_tier.replace("_", " ").title()
        return _reply(chat_id, f"Subscription: {state}. Tier: {tier}. Valid until: {expiry}.")
    if command == "/subscribe":
        # Crypto is the way this is sold, so it leads. The payment page carries
        # the exact amount and address, and the chain watcher credits it -- a
        # link here rather than an address in chat, because the amount is what
        # identifies the invoice and it must not be retyped from memory.
        public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip().rstrip("/")
        crypto = crypto_billing.status()
        if crypto.get("checkout_ready") and public_url:
            tiers = crypto.get("tiers") or {}
            scanner = ((tiers.get("scanner") or {}).get("periods") or [{}])[0].get("label", "$49.00")
            research = ((tiers.get("research_pro") or {}).get("periods") or [{}])[0].get("label", "$149.00")
            return _reply(
                chat_id,
                "Pay in USDC or USDT on "
                f"{crypto.get('chain')}.\nScanner from {scanner} · Research Pro from {research}\n\n"
                "Open the page to get your exact amount and address. The amount "
                "is unique to your invoice, so send it exactly as shown.",
                button=("Get payment details", f"{public_url}/subscription"),
            )
        try:
            url = billing.create_checkout_session(user)
        except billing.BillingError:
            return _reply(chat_id, "Checkout is temporarily unavailable. Your account has not been charged.")
        return _reply(
            chat_id,
            f"Your secure {billing.config().plan_label} subscription link is ready.",
            button=("Open payment page", url),
        )
    if command == "/access":
        if not user.subscription_active:
            return _reply(chat_id, "An active membership is required. Use /subscribe to activate access.")
        if not user.has_tier("research_pro"):
            return _reply(
                chat_id,
                "The private subscriber forum is included with Research Pro. "
                "Your Scanner subscription remains active on the website; use /subscribe to view upgrade pricing.",
            )
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
        return _reply(chat_id, "Tap below, then request to join. Active Research Pro memberships are approved automatically.", button=("Request group access", invite))
    return _reply(
        chat_id,
        "Token lookup: $SIREN, /token SIREN, or "
        "@spreadarbitragesubscription_bot SIREN\n"
        "Commands: /top, /spread, /funding, /transfer, /subscribe, "
        "/mysubscription, /access",
    )


def render_public_digest(*, board_path: Any, limit: int = 5) -> str:
    """Small public research preview; no account data and no execution claims."""
    public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip().rstrip("/")
    rows = telegram_queries.suggest("", board_path=board_path, limit=max(1, min(8, limit)))
    lines = ["<b>SpreadBoard · live research preview</b>"]
    for index, item in enumerate(rows, 1):
        token = str(item.get("token") or "")
        edge = item.get("best_edge_pct")
        value = f"{float(edge):+.2f}%" if edge is not None else "—"
        venues = " / ".join(str(value) for value in (item.get("venues") or [])[:3])
        link = f"{public_url}/markets?q={quote(token)}&view=table" if public_url else ""
        title = f'<a href="{link}">{token}</a>' if link else token
        lines.append(f"{index}. <b>{title}</b> · {value} · {int(item.get('route_count') or 0)} routes · {venues}")
    if not rows:
        lines.append("No fresh routes matched the public safety filters.")
    lines.append("\n<i>Public market research, not a trade signal. Recheck depth, identity, fees and funding before acting.</i>")
    return "\n".join(lines)


class PublicFeedWorker:
    """Publish a deduplicated public preview only when a dedicated chat is configured."""

    def __init__(self, *, board_path: Any, poll_seconds: float = 900.0) -> None:
        self.board_path = board_path
        self.poll_seconds = max(300.0, float(poll_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="spreadboard-telegram-public-feed", daemon=True)
        self._last_text = ""

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        if public_feed_chat_id() and config().ready and not self.running:
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
                print(f"spreadboard-telegram-public-feed: {type(exc).__name__}: {exc}", flush=True)

    def check_once(self) -> dict[str, Any]:
        chat_id = public_feed_chat_id()
        if not chat_id:
            return {"status": "not_configured", "sent": 0}
        if not outbound_posting_enabled():
            return {"status": "outbound_disabled", "sent": 0}
        text = render_public_digest(board_path=self.board_path)
        if text == self._last_text:
            return {"status": "unchanged", "sent": 0}
        _api_call("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        self._last_text = text
        return {"status": "sent", "sent": 1, "at": int(time.time())}


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
    if user is None or not user.has_tier("research_pro"):
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
            return {"checked": 0, "removed": 0, "errors": 0}
        chat_id = int(community["chat_id"])
        checked = removed = errors = 0
        for candidate in accounts.telegram_membership_candidates(db_path=self.db_path):
            checked += 1
            telegram_user_id = int(candidate["telegram_user_id"])
            try:
                member = (_api_call(
                    "getChatMember", {"chat_id": chat_id, "user_id": telegram_user_id}
                ).get("result") or {})
            except TelegramBotError as exc:
                # One stale/invalid linked account must not abort checks for all
                # subscribers or fill the service log once a minute. Persist a
                # safe state for operators and continue with the next member.
                errors += 1
                accounts.record_telegram_membership(
                    int(candidate["user_id"]),
                    telegram_user_id=telegram_user_id,
                    community_chat_id=chat_id,
                    state="error",
                    error=str(exc)[:200],
                    db_path=self.db_path,
                )
                continue
            state = str(member.get("status") or "left")
            user = accounts.get_user_object(int(candidate["user_id"]), db_path=self.db_path)
            if state in {"creator", "administrator"}:
                accounts.record_telegram_membership(user.id, telegram_user_id=telegram_user_id, community_chat_id=chat_id, state="exempt", db_path=self.db_path)
                continue
            present = state == "member" or (state == "restricted" and bool(member.get("is_member")))
            if user is not None and user.has_tier("research_pro"):
                if present:
                    accounts.record_telegram_membership(user.id, telegram_user_id=telegram_user_id, community_chat_id=chat_id, state="active", db_path=self.db_path)
                continue
            if present:
                _api_call("banChatMember", {"chat_id": chat_id, "user_id": telegram_user_id, "revoke_messages": False})
                _api_call("unbanChatMember", {"chat_id": chat_id, "user_id": telegram_user_id, "only_if_banned": True})
                removed += 1
            if user is not None:
                accounts.record_telegram_membership(user.id, telegram_user_id=telegram_user_id, community_chat_id=chat_id, state="removed", db_path=self.db_path)
        return {"checked": checked, "removed": removed, "errors": errors}


def _create_join_request_link(chat_id: int) -> str:
    result = _api_call(
        "createChatInviteLink",
        {"chat_id": int(chat_id), "name": "SpreadBoard membership", "creates_join_request": True},
    ).get("result") or {}
    invite = str(result.get("invite_link") or "")
    if not invite.startswith("https://t.me/"):
        raise TelegramBotError("telegram_invite_link_unavailable")
    return invite


#: Telegram methods that only read. Everything else writes into a chat.
READ_ONLY_METHODS = frozenset({
    "getMe", "getUpdates", "getChat", "getChatMember", "getChatMemberCount",
    "getChatAdministrators", "getWebhookInfo", "getFile", "setWebhook", "deleteWebhook",
})


def outbound_posting_enabled() -> bool:
    """Whether this deployment may post into a chat at all.

    Off unless switched on, deliberately. The operator's own Telegram account
    is the one these messages come from, and a board that starts talking to a
    chat because a default flipped is not recoverable -- the message is already
    sent. Set SPREADBOARD_TELEGRAM_OUTBOUND=1 when a dedicated account exists.
    """
    return os.environ.get("SPREADBOARD_TELEGRAM_OUTBOUND", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }


class TelegramOutboundDisabled(TelegramBotError):
    """Raised instead of posting, so a caller cannot mistake it for delivery."""


def _api_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method not in READ_ONLY_METHODS and not outbound_posting_enabled():
        raise TelegramOutboundDisabled(
            f"telegram_outbound_disabled:{method}"
        )
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
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Telegram sends useful, non-secret JSON errors (for example an invalid
        # stale participant id). Preserve the category so health/audit output
        # can distinguish bad stored state from an unreachable API.
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            description = str(error_payload.get("description") or "telegram_http_error")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            description = "telegram_http_error"
        safe = "_".join(description.upper().replace(":", " ").split())[:120]
        raise TelegramBotError(f"telegram_api_error:{safe}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TelegramBotError("telegram_api_unavailable") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        description = str(payload.get("description") or "telegram_api_error") if isinstance(payload, dict) else "telegram_api_error"
        raise TelegramBotError(description[:200])
    return payload


def _reply(
    chat_id: int, text: str, *, button: tuple[str, str] | None = None, html: bool = False,
    thread_id: int | None = None, markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"method": "sendMessage", "chat_id": chat_id, "text": text}
    if markup:
        payload["reply_markup"] = markup
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if html:
        payload["parse_mode"] = "HTML"
        payload["disable_web_page_preview"] = True
    if button:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
    return payload


def send_group_message(chat_id: str | int, text: str) -> dict[str, Any]:
    """Push an unsolicited message to the subscriber group."""
    return _api_call("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})


def send_direct_message(chat_id: int, text: str) -> dict[str, Any]:
    """Send a service notice to one account-linked Telegram chat."""
    return _api_call(
        "sendMessage",
        {"chat_id": int(chat_id), "text": str(text)[:4000], "disable_web_page_preview": True},
    )


def parse_update(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramBotError("invalid_telegram_payload") from exc
    if not isinstance(payload, dict):
        raise TelegramBotError("invalid_telegram_payload")
    return payload
