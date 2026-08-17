"""Buy a subscription without leaving Telegram.

`/subscribe` used to hand the buyer a link to the website and stop there. This
carries the whole purchase in the chat: tier, length, email, consent, the exact
amount and address to send, and the confirmation once the chain watcher credits
it. The website is touched once afterwards, to set a password, because choosing
a password is not something a chat should carry.

Every step up to and including the invoice is a synchronous webhook reply, so
none of it depends on outbound posting being switched on. Only the settlement
message is an unsolicited push, and that runs in its own worker so a Telegram
outage can never complicate a payment that already succeeded.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spreadboard import accounts, crypto_billing, mailer

TERMS_VERSION = "2026-08-12"
CALLBACK_PREFIX = "co"
TIER_LABELS = {"scanner": "Scanner", "research_pro": "Research Pro"}
# Deliberately not RFC 5322. A buyer who mistypes should be told now, in the
# chat, rather than after paying when the invite email silently goes nowhere.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class CheckoutError(RuntimeError):
    """A step could not be completed for a reason the buyer should be told."""


def enabled() -> bool:
    """Off unless switched on, so /subscribe keeps its old behaviour until asked."""
    return os.environ.get("SPREADBOARD_TELEGRAM_CHECKOUT", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _public_url() -> str:
    return os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip().rstrip("/")


def _terms_url() -> str:
    base = _public_url()
    return f"{base}/terms" if base else "the terms on the website"


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _periods(tier: str) -> dict[int, int]:
    return dict(crypto_billing.TIER_PERIODS.get(tier) or {})


def rehearsal_price_cents(tier: str, chat_id: int | None) -> int | None:
    """A rehearsal price for named chats, valid only until a stated deadline.

    Proving the payment path end to end needs a real transfer, and the list
    price is a lot to spend on a button. The risk is not the discount but a
    discount that outlives the rehearsal: this bot is public, so a forgotten
    cheap tier is a permanent hole in the pricing.

    Every branch therefore fails closed. No deadline, a deadline in the past,
    an unreadable deadline, an empty chat list, a chat that was not named, or
    the wrong tier all return None and the published price stands.
    """
    raw_cents = os.environ.get("SPREADBOARD_CHECKOUT_TEST_PRICE_CENTS", "").strip()
    deadline = os.environ.get("SPREADBOARD_CHECKOUT_TEST_PRICE_UNTIL", "").strip()
    chats = os.environ.get("SPREADBOARD_CHECKOUT_TEST_PRICE_CHATS", "").strip()
    only_tier = (
        os.environ.get("SPREADBOARD_CHECKOUT_TEST_PRICE_TIER", "").strip()
        or "research_pro"
    )
    if not raw_cents or not deadline or not chats or chat_id is None:
        return None
    if str(tier) != only_tier:
        return None
    allowed = {part.strip() for part in chats.split(",") if part.strip()}
    # "*" opens the price to every chat. It has to be typed explicitly: a blank
    # or missing list stays nobody, so "everyone" can never happen by accident.
    if "*" not in allowed and str(int(chat_id)) not in allowed:
        return None
    try:
        expires = datetime.fromisoformat(deadline)
        cents = int(raw_cents)
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if cents <= 0 or expires <= datetime.now(tz=UTC):
        return None
    return cents


def _priced_periods(tier: str, chat_id: int | None) -> dict[int, int]:
    """What this chat will actually be charged, so the buttons cannot mislead."""
    periods = _periods(tier)
    override = rehearsal_price_cents(tier, chat_id)
    if override is None:
        return periods
    return dict.fromkeys(periods, override)


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def tier_prompt(chat_id: int | None = None) -> dict[str, Any]:
    """Both tiers are real choices: some buyers want the board, not the group."""
    rows = []
    for tier, label in TIER_LABELS.items():
        cheapest = min(_priced_periods(tier, chat_id).values())
        included = (
            "board + private channel" if tier == "research_pro" else "board only"
        )
        rows.append(
            [
                {
                    "text": f"{label} — from {_money(cheapest)} · {included}",
                    "callback_data": f"{CALLBACK_PREFIX}:tier:{tier}",
                }
            ]
        )
    text = (
        "Choose your plan.\n\n"
        "Scanner — the full web board and alerts.\n"
        "Research Pro — everything in Scanner, plus the private subscriber "
        "channel and exact-size research quoting.\n\n"
        "Payment is USDC or USDT on Arbitrum One. No card, no recurring charge."
    )
    return {"text": text, "markup": {"inline_keyboard": rows}}


def welcome(
    *,
    username: str = "",
    first_name: str = "",
    chat_id: int | None = None,
) -> dict[str, Any]:
    """Greet a newcomer and say what is actually for sale.

    Someone arriving here has usually never seen the product. The old reply
    explained how to look a token up inside a subscriber forum they cannot
    enter yet -- an answer to a question they had not asked.

    Prices come from the same function that prices the invoice, so a welcome
    can never quote a figure the checkout will not honour.
    """
    handle = f"@{username.lstrip('@')}" if username.strip() else (first_name.strip() or "there")
    scanner = min(_priced_periods("scanner", chat_id).values())
    research = min(_priced_periods("research_pro", chat_id).values())
    text = (
        f"Welcome, {handle}.\n\n"
        "SpreadBoard finds cross-venue price and funding gaps that can actually "
        "be executed — matched live order books, real deposit and withdrawal "
        "rails, and settled funding rather than quoted funding.\n\n"
        f"Scanner — from {_money(scanner)}\n"
        "• the full live board, every route\n"
        "• spread, funding, price and rail alerts\n\n"
        f"Research Pro — from {_money(research)}\n"
        "• everything in Scanner\n"
        "• exact-size quoting from $10 to $100,000\n"
        "• the private subscriber channel\n\n"
        "Prepaid 30, 90 or 365 days in USDC or USDT on Arbitrum One. "
        "No card and no recurring charge."
    )
    return {
        "text": text,
        "markup": {
            "inline_keyboard": [
                [{"text": "See plans", "callback_data": f"{CALLBACK_PREFIX}:restart"}]
            ]
        },
    }


def period_prompt(tier: str, chat_id: int | None = None) -> dict[str, Any]:
    periods = _priced_periods(tier, chat_id)
    if not periods:
        raise CheckoutError("unknown_subscription_tier")
    rows = [
        [
            {
                "text": f"{days} days — {_money(cents)}",
                "callback_data": f"{CALLBACK_PREFIX}:period:{days}",
            }
        ]
        for days, cents in sorted(periods.items())
    ]
    rows.append([{"text": "← Back", "callback_data": f"{CALLBACK_PREFIX}:restart"}])
    return {
        "text": f"{TIER_LABELS[tier]} — how long?",
        "markup": {"inline_keyboard": rows},
    }


def email_prompt(tier: str, period_days: int, chat_id: int | None = None) -> dict[str, Any]:
    cents = _priced_periods(tier, chat_id)[period_days]
    return {
        "text": (
            f"{TIER_LABELS[tier]}, {period_days} days — {_money(cents)}\n\n"
            "Send the email address for your account.\n\n"
            "This is where your sign-in link goes after payment, so use one you "
            "can open. Nothing is sent to it until you have paid."
        ),
        "markup": None,
    }


def confirm_prompt(tier: str, period_days: int, email: str, chat_id: int | None = None) -> dict[str, Any]:
    cents = _priced_periods(tier, chat_id)[period_days]
    extra = (
        "\nIncludes the private subscriber channel."
        if tier == "research_pro"
        else "\nWeb board and alerts. The private channel is Research Pro only."
    )
    return {
        "text": (
            f"{TIER_LABELS[tier]} · {period_days} days · {_money(cents)}\n"
            f"Account email: {email}{extra}\n\n"
            f"By continuing you accept the terms: {_terms_url()}"
        ),
        "markup": {
            "inline_keyboard": [
                [
                    {
                        "text": "Agree and get payment details",
                        "callback_data": f"{CALLBACK_PREFIX}:confirm",
                    }
                ],
                [{"text": "← Start over", "callback_data": f"{CALLBACK_PREFIX}:restart"}],
            ]
        },
    }


def invoice_message(invoice: dict[str, Any]) -> dict[str, Any]:
    """Two values the buyer must reproduce exactly, each one tap away.

    Telegram copies the contents of a <code> block to the clipboard when it is
    tapped, so the amount and the address each get a line of their own and are
    never retyped. The amount is deliberately bare -- pasting "$149.00" into a
    wallet's amount field fails, "149.00" does not.
    """
    cents = int(invoice["amount_cents"])
    bare_amount = f"{cents / 100:.2f}"
    address = str(invoice["receiving_address"])
    tokens = " or ".join(invoice.get("tokens") or ["USDC", "USDT"])
    confirmations = crypto_billing.config().confirmations
    text = (
        "<b>Almost there — two things to copy.</b>\n\n"
        # The figure appears twice on purpose: with the currency so a human
        # reads it correctly, and bare so it can be pasted into a wallet's
        # amount field, which rejects a leading symbol.
        f"<b>1.</b> Send exactly {_money(cents)} — tap to copy:\n"
        f"<code>{bare_amount}</code>\n\n"
        f"<b>2.</b> To this address on <b>{crypto_billing.CHAIN_NAME}</b> "
        "(tap to copy):\n"
        f"<code>{address}</code>\n\n"
        f"<b>{tokens} only.</b> Another token, or the same address on another "
        "network, cannot be credited and cannot be recovered.\n\n"
        "The amount is how we recognise your payment, so it has to match to "
        "the cent. Sending a rounded figure leaves it unmatched until an "
        "admin sorts it out.\n\n"
        f"You will get a message here automatically after {confirmations} "
        "confirmations, usually a couple of minutes. This invoice expires in "
        "60 minutes."
    )
    return {
        "text": text,
        "html": True,
        "markup": {
            "inline_keyboard": [
                [
                    {
                        "text": "Check payment status",
                        "callback_data": f"{CALLBACK_PREFIX}:status",
                    }
                ]
            ]
        },
    }


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


def begin(chat_id: int, *, db_path: Path | str) -> dict[str, Any]:
    accounts.save_checkout_session(chat_id, step="tier", db_path=db_path)
    return tier_prompt(chat_id)


def choose_tier(chat_id: int, tier: str, *, db_path: Path | str) -> dict[str, Any]:
    if tier not in TIER_LABELS:
        raise CheckoutError("unknown_subscription_tier")
    accounts.save_checkout_session(chat_id, step="period", tier=tier, db_path=db_path)
    return period_prompt(tier, chat_id)


def choose_period(chat_id: int, period_days: int, *, db_path: Path | str) -> dict[str, Any]:
    session = accounts.get_checkout_session(chat_id, db_path=db_path)
    if session is None or not session.get("tier"):
        return begin(chat_id, db_path=db_path)
    tier = str(session["tier"])
    if period_days not in _periods(tier):
        raise CheckoutError("unknown_period")
    accounts.save_checkout_session(
        chat_id, step="email", tier=tier, period_days=period_days, db_path=db_path
    )
    return email_prompt(tier, period_days, chat_id)


def submit_email(chat_id: int, email: str, *, db_path: Path | str) -> dict[str, Any]:
    session = accounts.get_checkout_session(chat_id, db_path=db_path)
    if session is None or session.get("step") != "email":
        return begin(chat_id, db_path=db_path)
    clean = str(email or "").strip()
    if len(clean) > 254 or not EMAIL_RE.match(clean):
        return {
            "text": "That does not look like an email address. Send it again, for example name@example.com.",
            "markup": None,
        }
    accounts.save_checkout_session(
        chat_id,
        step="confirm",
        tier=str(session["tier"]),
        period_days=int(session["period_days"]),
        email=clean,
        db_path=db_path,
    )
    return confirm_prompt(str(session["tier"]), int(session["period_days"]), clean, chat_id)


def confirm(chat_id: int, *, db_path: Path | str) -> dict[str, Any]:
    """Record consent, provision the account, and issue the invoice."""
    session = accounts.get_checkout_session(chat_id, db_path=db_path)
    if session is None or session.get("step") != "confirm":
        return begin(chat_id, db_path=db_path)

    open_invoices = accounts.open_checkout_invoices(chat_id, db_path=db_path)
    if len(open_invoices) >= accounts.MAX_OPEN_CHECKOUT_INVOICES_PER_CHAT:
        newest = crypto_billing.get_invoice(int(open_invoices[0]["id"]), db_path=db_path)
        if newest is not None:
            return invoice_message(newest)
        raise CheckoutError("too_many_open_invoices")

    tier = str(session["tier"])
    period_days = int(session["period_days"])
    email = str(session["email"])

    existing_id = accounts.user_id_for_email(email, db_path=db_path)
    new_account = existing_id is None
    if new_account:
        created, _token = accounts.create_invited_user(
            email=email,
            display_name=email.split("@", 1)[0][:100] or "Member",
            subscription_status="inactive",
            subscription_tier=tier,
            subscription_days=1,
            db_path=db_path,
        )
        # An invited account starts with a day of access it has not paid for.
        accounts.update_subscription(
            int(created["id"]), status="inactive", expires_at=None, db_path=db_path
        )
        user_id = int(created["id"])
    else:
        user_id = int(existing_id)

    accounts.record_subscription_consent(
        user_id,
        terms_version=TERMS_VERSION,
        immediate_access=True,
        user_agent="telegram-bot-checkout",
        db_path=db_path,
    )
    invoice = crypto_billing.create_invoice(
        user_id,
        period_days,
        tier=tier,
        list_amount_cents=rehearsal_price_cents(tier, chat_id),
        db_path=db_path,
    )
    accounts.link_checkout_invoice(
        int(invoice["id"]),
        chat_id=chat_id,
        tier=tier,
        new_account=new_account,
        db_path=db_path,
    )
    accounts.save_checkout_session(
        chat_id,
        step="invoice",
        tier=tier,
        period_days=period_days,
        email=email,
        invoice_id=int(invoice["id"]),
        db_path=db_path,
    )
    return invoice_message(invoice)


def status(chat_id: int, *, db_path: Path | str) -> dict[str, Any]:
    session = accounts.get_checkout_session(chat_id, db_path=db_path)
    if session is None or not session.get("invoice_id"):
        return {"text": "No invoice is open. Send /subscribe to start.", "markup": None}
    invoice = crypto_billing.get_invoice(int(session["invoice_id"]), db_path=db_path)
    if invoice is None:
        return {"text": "No invoice is open. Send /subscribe to start.", "markup": None}
    state = str(invoice.get("status") or "")
    if state == "paid":
        return {
            "text": "Payment received. Your confirmation and sign-in link are on the way.",
            "markup": None,
        }
    if state in {"expired", "cancelled"}:
        return {
            "text": "That invoice expired before payment arrived. Send /subscribe to get a fresh one.",
            "markup": None,
        }
    return {
        "text": (
            "Not credited yet. An exact-amount transfer is credited after "
            f"{crypto_billing.config().confirmations} confirmations. If you have "
            "just sent it, give it a couple of minutes and check again."
        ),
        "markup": invoice_message(invoice)["markup"],
    }


def handle_callback(chat_id: int, data: str, *, db_path: Path | str) -> dict[str, Any] | None:
    """Route a `co:*` button press. Returns None for anything not ours."""
    parts = str(data or "").split(":")
    if not parts or parts[0] != CALLBACK_PREFIX:
        return None
    action = parts[1] if len(parts) > 1 else ""
    try:
        if action == "tier" and len(parts) > 2:
            return choose_tier(chat_id, parts[2], db_path=db_path)
        if action == "period" and len(parts) > 2:
            return choose_period(chat_id, int(parts[2]), db_path=db_path)
        if action == "confirm":
            return confirm(chat_id, db_path=db_path)
        if action == "status":
            return status(chat_id, db_path=db_path)
        if action == "restart":
            return begin(chat_id, db_path=db_path)
    except CheckoutError as exc:
        return {"text": _friendly(str(exc)), "markup": None}
    except crypto_billing.CryptoBillingError as exc:
        return {"text": _friendly(str(exc)), "markup": None}
    return None


def awaiting_email(chat_id: int, *, db_path: Path | str) -> bool:
    session = accounts.get_checkout_session(chat_id, db_path=db_path)
    return bool(session and session.get("step") == "email")


def _friendly(code: str) -> str:
    return {
        "crypto_billing_not_configured": "Crypto checkout is temporarily unavailable. Nothing has been charged.",
        "unknown_subscription_tier": "That plan is not available. Send /subscribe to start again.",
        "unknown_period": "That length is not available. Send /subscribe to start again.",
        "too_many_open_invoices": "You already have invoices open. Pay one of those, or wait for them to expire.",
        "invoice_slots_exhausted": "Checkout is busy right now. Please try again in a few minutes.",
    }.get(code, "Something went wrong and nothing was charged. Send /subscribe to try again.")


# --------------------------------------------------------------------------
# Settlement notifier
# --------------------------------------------------------------------------


def deliver_one(item: dict[str, Any], *, db_path: Path | str) -> dict[str, Any]:
    """Tell one buyer their payment landed, and let them in.

    Entitlement is already granted by the chain watcher before this runs, so a
    failure here delays a message; it never costs someone access they paid for.
    """
    from spreadboard import telegram_bot

    invoice_id = int(item["invoice_id"])
    chat_id = int(item["chat_id"])
    user_id = int(item["user_id"])
    tier = str(item.get("tier") or item.get("subscription_tier") or "research_pro")
    user = accounts.get_user_object(user_id, db_path=db_path)
    if user is None:
        raise CheckoutError("account_missing")

    purpose = "invite" if int(item.get("new_account") or 0) else "reset"
    token = accounts.create_password_token(user_id, purpose=purpose, db_path=db_path)
    base = _public_url()
    link = f"{base}/set-password?token={token}"

    mailer.send_subscription_notice(
        recipient=user.email,
        display_name=user.display_name,
        subject="Your SpreadBoard access is ready",
        body=(
            f"Your payment is confirmed and {TIER_LABELS.get(tier, tier)} access is "
            "active.\n\n"
            "Set your password with this single-use link, then sign in with this "
            "email address:\n\n"
            f"{link}"
        ),
        action_url=link,
    )

    accounts.bind_telegram_chat_direct(user_id, chat_id, db_path=db_path)

    lines = [
        "Payment confirmed. Your access is active.",
        "",
        f"We have emailed {user.email} a single-use link to set your password.",
    ]
    if tier == "research_pro":
        lines += ["", "Tap below to join the private subscriber channel."]
        telegram_bot.send_direct_message(
            chat_id,
            "\n".join(lines),
            markup=_channel_markup(user_id, chat_id, db_path=db_path),
        )
    else:
        lines += [
            "",
            (
                "Scanner covers the web board and alerts. The private channel "
                "is part of Research Pro if you ever want it."
            ),
        ]
        telegram_bot.send_direct_message(chat_id, "\n".join(lines))
    return {"invoice_id": invoice_id, "delivered": True}


def _channel_markup(
    user_id: int, chat_id: int, *, db_path: Path | str
) -> dict[str, Any] | None:
    from spreadboard import telegram_bot

    community = accounts.telegram_community(db_path=db_path)
    if community is None:
        return None
    invite = telegram_bot.create_join_request_link(int(community["chat_id"]))
    # In a private chat the chat id is the user's Telegram id, which is what
    # the join-request handler and the membership sweep both match on.
    accounts.record_telegram_membership(
        user_id,
        telegram_user_id=chat_id,
        community_chat_id=int(community["chat_id"]),
        state="pending",
        db_path=db_path,
    )
    return {"inline_keyboard": [[{"text": "Join the private channel", "url": invite}]]}


class Notifier:
    """Announce settled Telegram checkouts, with bounded retries."""

    def __init__(
        self,
        *,
        accounts_path: Path | str = accounts.DEFAULT_DB_PATH,
        poll_seconds: float = 10.0,
    ) -> None:
        self.accounts_path = Path(accounts_path)
        self.poll_seconds = max(5.0, float(poll_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="spreadboard-telegram-checkout", daemon=True
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
                self.check_once()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"spreadboard-telegram-checkout: {type(exc).__name__}: {exc}",
                    flush=True,
                )
            self._stop.wait(self.poll_seconds)

    def check_once(self) -> dict[str, int]:
        pending = accounts.pending_checkout_notifications(db_path=self.accounts_path)
        delivered = failed = 0
        for item in pending:
            try:
                deliver_one(item, db_path=self.accounts_path)
                accounts.record_checkout_notification(
                    int(item["invoice_id"]), delivered=True, db_path=self.accounts_path
                )
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not stall the rest.
                accounts.record_checkout_notification(
                    int(item["invoice_id"]),
                    delivered=False,
                    error=f"{type(exc).__name__}: {exc}",
                    db_path=self.accounts_path,
                )
                failed += 1
        return {"pending": len(pending), "delivered": delivered, "failed": failed}


def status_snapshot(*, db_path: Path | str = accounts.DEFAULT_DB_PATH) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "enabled": enabled(),
        "pending_notifications": len(
            accounts.pending_checkout_notifications(db_path=db_path)
        ),
    }
