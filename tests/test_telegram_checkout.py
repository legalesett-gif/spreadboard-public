"""The whole purchase, inside Telegram, with no website step before payment.

This is the flow a buyer actually walks: land on the bot with no account, pick
a tier and a length, give an email, agree to the terms, get an exact amount and
address, pay, and be let in. Each of those is a seam where the old flow simply
handed over a website link instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from spreadboard import accounts, crypto_billing, telegram_bot, telegram_checkout

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
RECEIVER = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
USDT = "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"
CHAT = 4242
COMMUNITY = -100123


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_TELEGRAM_CHECKOUT", "1")
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadarbitrage.ink")
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", RECEIVER)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    # A live deployment has at least one chain whose watcher is working;
    # checkout deliberately offers nothing until one has proven itself.
    accounts.record_chain_scan("arbitrum", ok=True, db_path=path)
    return path


def _update(text: str) -> dict:
    return {"message": {"chat": {"id": CHAT, "type": "private"}, "text": text}}


def _callback(data: str) -> dict:
    return {
        "callback_query": {
            "data": data,
            "message": {"chat": {"id": CHAT, "type": "private"}, "message_id": 1},
        }
    }


def _pay(db, invoice, *, tx: str, now=NOW):
    return crypto_billing.record_transfer(
        token_address=USDT,
        raw_units=int(invoice["amount_cents"]) * 10**4,
        tx_hash=tx,
        log_index=0,
        from_address="0x2222222222222222222222222222222222222222",
        block_number=1000,
        db_path=db,
        now=now,
    )


def _walk_to_invoice(db, *, tier="research_pro", days=30, email="buyer@example.test"):
    telegram_bot.handle_update(_update("/subscribe"), db_path=db)
    telegram_bot.handle_update(_callback(f"co:tier:{tier}"), db_path=db)
    telegram_bot.handle_update(_callback(f"co:period:{days}"), db_path=db)
    telegram_bot.handle_update(_update(email), db_path=db)
    return telegram_bot.handle_update(_callback("co:confirm"), db_path=db)


# --------------------------------------------------------------------------
# Getting to an invoice
# --------------------------------------------------------------------------


def test_a_stranger_can_start_buying_without_an_account(db) -> None:
    """The old bot answered every unlinked chat with 'link on the website'."""
    reply = telegram_bot.handle_update(_update("/subscribe"), db_path=db)

    assert "Choose your plan" in reply["text"]
    buttons = [b["callback_data"] for row in reply["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == ["co:tier:scanner", "co:tier:research_pro"]


def test_first_contact_offers_a_subscribe_button(db) -> None:
    reply = telegram_bot.handle_update(_update("/start"), db_path=db)

    buttons = [b for row in reply["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons[0]["callback_data"] == "co:restart"


def test_both_tiers_are_offered_and_scanner_is_not_framed_as_a_refusal(db) -> None:
    reply = telegram_bot.handle_update(_update("/subscribe"), db_path=db)

    assert "board only" in reply["text"] or "board only" in str(reply["reply_markup"])
    assert "Scanner" in str(reply["reply_markup"])
    assert "cannot" not in reply["text"].casefold()


def test_each_tier_offers_its_own_three_lengths_and_prices(db) -> None:
    telegram_bot.handle_update(_update("/subscribe"), db_path=db)
    reply = telegram_bot.handle_update(_callback("co:tier:scanner"), db_path=db)

    labels = [b["text"] for row in reply["reply_markup"]["inline_keyboard"] for b in row]
    assert "30 days — $49.00" in labels
    assert "90 days — $135.00" in labels
    assert "365 days — $490.00" in labels


def test_a_mistyped_email_is_caught_before_anything_is_created(db) -> None:
    telegram_bot.handle_update(_update("/subscribe"), db_path=db)
    telegram_bot.handle_update(_callback("co:tier:research_pro"), db_path=db)
    telegram_bot.handle_update(_callback("co:period:30"), db_path=db)

    reply = telegram_bot.handle_update(_update("not-an-email"), db_path=db)

    assert "does not look like an email" in reply["text"]
    assert accounts.user_id_for_email("not-an-email", db_path=db) is None


def test_the_invoice_carries_an_exact_amount_and_the_receiving_address(db) -> None:
    reply = _walk_to_invoice(db)

    assert RECEIVER in reply["text"]
    assert "$149.00" in reply["text"]
    assert "exactly" in reply["text"]


def test_no_invoice_exists_without_a_recorded_consent(db) -> None:
    _walk_to_invoice(db)

    connection = accounts._connect(db)
    try:
        invoices = connection.execute("SELECT COUNT(*) FROM crypto_invoices").fetchone()[0]
        consents = connection.execute("SELECT COUNT(*) FROM subscription_consents").fetchone()[0]
    finally:
        connection.close()
    assert invoices == 1 and consents == 1


def test_an_unconfirmed_flow_never_issues_an_invoice(db) -> None:
    telegram_bot.handle_update(_update("/subscribe"), db_path=db)
    telegram_bot.handle_update(_callback("co:tier:research_pro"), db_path=db)
    telegram_bot.handle_update(_callback("co:period:30"), db_path=db)
    telegram_bot.handle_update(_update("buyer@example.test"), db_path=db)

    connection = accounts._connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM crypto_invoices").fetchone()[0] == 0
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Abuse and privacy
# --------------------------------------------------------------------------


def test_one_chat_cannot_exhaust_the_shared_amount_slots(db) -> None:
    """The slot pool is 100 and shared with the website; a bot is unauthenticated."""
    for index in range(accounts.MAX_OPEN_CHECKOUT_INVOICES_PER_CHAT + 3):
        _walk_to_invoice(db, email=f"buyer{index}@example.test")

    connection = accounts._connect(db)
    try:
        open_invoices = connection.execute(
            "SELECT COUNT(*) FROM crypto_invoices WHERE status = 'open'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert open_invoices == accounts.MAX_OPEN_CHECKOUT_INVOICES_PER_CHAT


def test_the_reply_is_identical_for_a_known_and_an_unknown_email(db) -> None:
    """Otherwise checkout is an account-enumeration oracle open to anyone."""
    accounts.create_user(
        email="known@example.test",
        display_name="Known",
        password="a-secure-existing-password",
        subscription_status="inactive",
        db_path=db,
    )

    telegram_bot.handle_update(_update("/subscribe"), db_path=db)
    telegram_bot.handle_update(_callback("co:tier:research_pro"), db_path=db)
    telegram_bot.handle_update(_callback("co:period:30"), db_path=db)
    known = telegram_bot.handle_update(_update("known@example.test"), db_path=db)

    accounts.clear_checkout_session(CHAT, db_path=db)
    telegram_bot.handle_update(_update("/subscribe"), db_path=db)
    telegram_bot.handle_update(_callback("co:tier:research_pro"), db_path=db)
    telegram_bot.handle_update(_callback("co:period:30"), db_path=db)
    unknown = telegram_bot.handle_update(_update("stranger@example.test"), db_path=db)

    assert known["text"].replace("known@", "X@") == unknown["text"].replace("stranger@", "X@")


def test_an_existing_account_is_topped_up_rather_than_duplicated(db) -> None:
    existing = accounts.create_user(
        email="known@example.test",
        display_name="Known",
        password="a-secure-existing-password",
        subscription_status="inactive",
        db_path=db,
    )
    _walk_to_invoice(db, email="known@example.test")

    connection = accounts._connect(db)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE email = 'known@example.test'"
        ).fetchone()[0]
        invoice_user = connection.execute(
            "SELECT user_id FROM crypto_invoices"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1
    assert invoice_user == existing["id"]


# --------------------------------------------------------------------------
# Payment and delivery
# --------------------------------------------------------------------------


def test_paying_grants_access_emails_a_link_and_opens_the_channel(db, monkeypatch) -> None:
    accounts.configure_telegram_community(
        COMMUNITY, title="Subscribers", configured_by_telegram_user_id=42, db_path=db
    )
    sent_mail, sent_chat, api = [], [], []
    monkeypatch.setattr(
        telegram_checkout.mailer,
        "send_subscription_notice",
        lambda **kwargs: sent_mail.append(kwargs),
    )
    monkeypatch.setattr(
        telegram_bot,
        "_api_call",
        lambda method, params: api.append((method, params))
        or {"ok": True, "result": {"invite_link": "https://t.me/+abc"}},
    )
    monkeypatch.setattr(
        telegram_bot,
        "send_direct_message",
        lambda chat_id, text, markup=None: sent_chat.append((chat_id, text, markup)),
    )

    _walk_to_invoice(db)
    invoice = crypto_billing.get_invoice(1, db_path=db)
    assert _pay(db, invoice, tx="0xaaa")["resolution"] == "settled"

    summary = telegram_checkout.Notifier(accounts_path=db).check_once()

    assert summary["delivered"] == 1
    # Entitlement
    user = accounts.get_user_object(
        accounts.user_id_for_email("buyer@example.test", db_path=db), db_path=db
    )
    assert user.subscription_active and user.entitlement_tier == "research_pro"
    # Email carries a single-use link, never a password
    assert "/set-password?token=" in sent_mail[0]["body"]
    assert "password" not in sent_mail[0]["body"].split("token=")[1]
    # The chat is linked, so the existing join-approval path works
    assert accounts.user_for_telegram_chat(CHAT, db_path=db).id == user.id
    # And the buyer is handed the channel
    assert "Join the private channel" in str(sent_chat[0][2])


def test_a_scanner_buyer_gets_an_account_but_no_channel_invite(db, monkeypatch) -> None:
    accounts.configure_telegram_community(
        COMMUNITY, title="Subscribers", configured_by_telegram_user_id=42, db_path=db
    )
    sent_chat = []
    monkeypatch.setattr(
        telegram_checkout.mailer, "send_subscription_notice", lambda **kwargs: None
    )
    monkeypatch.setattr(
        telegram_bot,
        "send_direct_message",
        lambda chat_id, text, markup=None: sent_chat.append((chat_id, text, markup)),
    )

    _walk_to_invoice(db, tier="scanner", email="scanner@example.test")
    invoice = crypto_billing.get_invoice(1, db_path=db)
    _pay(db, invoice, tx="0xbbb")
    telegram_checkout.Notifier(accounts_path=db).check_once()

    user = accounts.get_user_object(
        accounts.user_id_for_email("scanner@example.test", db_path=db), db_path=db
    )
    assert user.subscription_active and user.entitlement_tier == "scanner"
    assert sent_chat[0][2] is None
    assert "Research Pro" in sent_chat[0][1]


def test_a_confirmation_is_never_sent_twice(db, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(
        telegram_checkout.mailer, "send_subscription_notice", lambda **kwargs: None
    )
    monkeypatch.setattr(
        telegram_bot, "send_direct_message", lambda *a, **k: sent.append(a)
    )

    _walk_to_invoice(db, tier="scanner")
    _pay(db, crypto_billing.get_invoice(1, db_path=db), tx="0xccc")

    first = telegram_checkout.Notifier(accounts_path=db).check_once()
    second = telegram_checkout.Notifier(accounts_path=db).check_once()

    assert first["delivered"] == 1
    assert second == {"pending": 0, "delivered": 0, "failed": 0}
    assert len(sent) == 1


def test_a_failed_telegram_message_never_costs_paid_access(db, monkeypatch) -> None:
    monkeypatch.setattr(
        telegram_checkout.mailer, "send_subscription_notice", lambda **kwargs: None
    )
    monkeypatch.setattr(
        telegram_bot,
        "send_direct_message",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down")),
    )

    _walk_to_invoice(db, tier="scanner")
    _pay(db, crypto_billing.get_invoice(1, db_path=db), tx="0xddd")
    summary = telegram_checkout.Notifier(accounts_path=db).check_once()

    assert summary["failed"] == 1
    user = accounts.get_user_object(
        accounts.user_id_for_email("buyer@example.test", db_path=db), db_path=db
    )
    assert user.subscription_active, "entitlement must survive a delivery failure"


def test_status_tells_the_buyer_where_the_payment_stands(db) -> None:
    _walk_to_invoice(db)

    waiting = telegram_bot.handle_update(_callback("co:status"), db_path=db)
    assert "Not credited yet" in waiting["text"]

    _pay(db, crypto_billing.get_invoice(1, db_path=db), tx="0xeee")
    paid = telegram_bot.handle_update(_callback("co:status"), db_path=db)
    assert "Payment received" in paid["text"]


# --------------------------------------------------------------------------
# Flag and guard behaviour
# --------------------------------------------------------------------------


def test_the_flag_off_keeps_the_old_website_link(db, monkeypatch) -> None:
    monkeypatch.delenv("SPREADBOARD_TELEGRAM_CHECKOUT", raising=False)

    reply = telegram_bot.handle_update(_update("/subscribe"), db_path=db)

    assert "Link this chat" in reply["text"]


def test_buying_works_with_outbound_posting_disabled(db, monkeypatch) -> None:
    """Everything before payment is a webhook reply, not an API write."""
    monkeypatch.delenv("SPREADBOARD_TELEGRAM_OUTBOUND", raising=False)

    reply = _walk_to_invoice(db)

    assert RECEIVER in reply["text"]


def test_the_email_message_id_is_kept_as_delivery_evidence(db, monkeypatch) -> None:
    """"It wasn't sent" was unanswerable: success recorded nothing to check."""
    monkeypatch.setattr(
        telegram_checkout.mailer,
        "send_subscription_notice",
        lambda **kwargs: "resend-abc-123",
    )
    monkeypatch.setattr(telegram_bot, "send_direct_message", lambda *a, **k: None)

    _walk_to_invoice(db, tier="scanner", email="evidence@example.test")
    _pay(db, crypto_billing.get_invoice(1, db_path=db), tx="0xevid")
    telegram_checkout.Notifier(accounts_path=db).check_once()

    connection = accounts._connect(db)
    try:
        row = connection.execute(
            "SELECT email_message_id, email_recipient FROM telegram_checkout_invoices "
            "WHERE invoice_id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row["email_message_id"] == "resend-abc-123"
    assert row["email_recipient"] == "evidence@example.test"


# --------------------------------------------------------------------------
# Nothing is confirmed until the full amount has actually arrived
# --------------------------------------------------------------------------


def test_an_underpayment_grants_nothing_and_confirms_nothing(db, monkeypatch) -> None:
    """A cent short is not a purchase. No access, no email, no Telegram message."""
    mails, chats = [], []
    monkeypatch.setattr(
        telegram_checkout.mailer, "send_subscription_notice",
        lambda **kwargs: mails.append(kwargs) or "id",
    )
    monkeypatch.setattr(telegram_bot, "send_direct_message", lambda *a, **k: chats.append(a))

    _walk_to_invoice(db, tier="scanner", email="short@example.test")
    invoice = crypto_billing.get_invoice(1, db_path=db)
    short = dict(invoice)
    short["amount_cents"] = int(invoice["amount_cents"]) - 1
    assert _pay(db, short, tx="0xshort")["resolution"] != "settled"

    summary = telegram_checkout.Notifier(accounts_path=db).check_once()

    assert summary["delivered"] == 0
    assert mails == [] and chats == []
    assert crypto_billing.get_invoice(1, db_path=db)["status"] == "open"
    user = accounts.get_user_object(
        accounts.user_id_for_email("short@example.test", db_path=db), db_path=db
    )
    assert not user.subscription_active


def test_an_overpayment_is_parked_rather_than_guessed_into_a_tier(db, monkeypatch) -> None:
    mails = []
    monkeypatch.setattr(
        telegram_checkout.mailer, "send_subscription_notice",
        lambda **kwargs: mails.append(kwargs) or "id",
    )
    monkeypatch.setattr(telegram_bot, "send_direct_message", lambda *a, **k: None)

    _walk_to_invoice(db, tier="scanner", email="over@example.test")
    invoice = crypto_billing.get_invoice(1, db_path=db)
    over = dict(invoice)
    over["amount_cents"] = int(invoice["amount_cents"]) + 100
    assert _pay(db, over, tx="0xover")["resolution"] != "settled"

    telegram_checkout.Notifier(accounts_path=db).check_once()

    assert mails == []
    assert crypto_billing.get_invoice(1, db_path=db)["status"] == "open"


def test_the_exact_amount_is_what_confirms(db, monkeypatch) -> None:
    """The positive control for the two above."""
    mails = []
    monkeypatch.setattr(
        telegram_checkout.mailer, "send_subscription_notice",
        lambda **kwargs: mails.append(kwargs) or "id",
    )
    monkeypatch.setattr(telegram_bot, "send_direct_message", lambda *a, **k: None)

    _walk_to_invoice(db, tier="scanner", email="exact@example.test")
    assert _pay(db, crypto_billing.get_invoice(1, db_path=db), tx="0xexact")["resolution"] == "settled"
    telegram_checkout.Notifier(accounts_path=db).check_once()

    assert len(mails) == 1
    user = accounts.get_user_object(
        accounts.user_id_for_email("exact@example.test", db_path=db), db_path=db
    )
    assert user.subscription_active
