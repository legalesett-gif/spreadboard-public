"""A cheap price for a live rehearsal, that cannot outlive the rehearsal.

Walking the real payment path end to end needs a real transfer, and $149 is a
lot to spend proving a button works. The danger is not the discount, it is a
discount that is still live next week: the bot is public, so a forgotten $5
Research Pro is a permanent hole in the pricing.

So the override fails closed in every direction. No deadline, no override. A
deadline in the past, no override. A chat that was not named, no override. It
only ever cheapens the Telegram path; the website is untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from spreadboard import accounts, crypto_billing, telegram_bot, telegram_checkout

RECEIVER = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
CHAT = 1969819583
OTHER_CHAT = 5555


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_TELEGRAM_CHECKOUT", "1")
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadarbitrage.ink")
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", RECEIVER)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def _arm(monkeypatch, *, cents=500, hours=12, chats=str(CHAT), tier="research_pro"):
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_CENTS", str(cents))
    monkeypatch.setenv(
        "SPREADBOARD_CHECKOUT_TEST_PRICE_UNTIL",
        (datetime.now(tz=UTC) + timedelta(hours=hours)).isoformat(),
    )
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_CHATS", chats)
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_TIER", tier)


def _buy(db, chat=CHAT, *, tier="research_pro", days=30, email="trial@example.test"):
    telegram_bot.handle_update(
        {"message": {"chat": {"id": chat, "type": "private"}, "text": "/subscribe"}},
        db_path=db,
    )
    for data in (f"co:tier:{tier}", f"co:period:{days}"):
        telegram_bot.handle_update(
            {"callback_query": {"data": data, "message": {"chat": {"id": chat, "type": "private"}, "message_id": 1}}},
            db_path=db,
        )
    telegram_bot.handle_update(
        {"message": {"chat": {"id": chat, "type": "private"}, "text": email}}, db_path=db
    )
    return telegram_bot.handle_update(
        {"callback_query": {"data": "co:confirm", "message": {"chat": {"id": chat, "type": "private"}, "message_id": 1}}},
        db_path=db,
    )


def test_the_named_chat_is_charged_the_test_price(db, monkeypatch) -> None:
    _arm(monkeypatch)

    reply = _buy(db)

    assert "$5.00" in reply["text"]
    invoice = crypto_billing.get_invoice(1, db_path=db)
    assert invoice["amount_cents"] == 500


def test_the_keyboard_shows_the_price_that_will_actually_be_charged(db, monkeypatch) -> None:
    """A $149 button that issues a $5 invoice is how a buyer sends the wrong amount."""
    _arm(monkeypatch)

    prompt = telegram_checkout.period_prompt("research_pro", chat_id=CHAT)

    labels = [b["text"] for row in prompt["markup"]["inline_keyboard"] for b in row]
    assert "30 days — $5.00" in labels


def test_any_other_chat_pays_full_price(db, monkeypatch) -> None:
    _arm(monkeypatch)

    reply = _buy(db, OTHER_CHAT, email="stranger@example.test")

    assert "$149.00" in reply["text"]


def test_an_expired_deadline_restores_full_price(db, monkeypatch) -> None:
    _arm(monkeypatch, hours=-1)

    reply = _buy(db)

    assert "$149.00" in reply["text"]


def test_a_missing_deadline_is_not_an_open_ended_discount(db, monkeypatch) -> None:
    """The failure mode that matters: a cheap price nobody remembers setting."""
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_CENTS", "500")
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_CHATS", str(CHAT))
    monkeypatch.delenv("SPREADBOARD_CHECKOUT_TEST_PRICE_UNTIL", raising=False)

    reply = _buy(db)

    assert "$149.00" in reply["text"]


def test_an_unparseable_deadline_restores_full_price(db, monkeypatch) -> None:
    _arm(monkeypatch)
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_UNTIL", "next tuesday")

    reply = _buy(db)

    assert "$149.00" in reply["text"]


def test_an_empty_chat_list_does_not_mean_everyone(db, monkeypatch) -> None:
    _arm(monkeypatch, chats="")

    reply = _buy(db)

    assert "$149.00" in reply["text"]


def test_only_the_named_tier_is_cheapened(db, monkeypatch) -> None:
    _arm(monkeypatch, tier="research_pro")

    reply = _buy(db, tier="scanner", email="scanner@example.test")

    assert "$49.00" in reply["text"]


def test_the_website_price_is_never_touched(db, monkeypatch) -> None:
    """The override lives in the Telegram path; create_invoice defaults unchanged."""
    _arm(monkeypatch)
    user = accounts.create_user(
        email="web@example.test",
        display_name="Web",
        password="a-secure-website-password",
        subscription_status="inactive",
        db_path=db,
    )

    invoice = crypto_billing.create_invoice(user["id"], 30, tier="research_pro", db_path=db)

    assert invoice["amount_cents"] == 14_900


def test_with_nothing_armed_the_price_is_the_list_price(db, monkeypatch) -> None:
    for name in (
        "SPREADBOARD_CHECKOUT_TEST_PRICE_CENTS",
        "SPREADBOARD_CHECKOUT_TEST_PRICE_UNTIL",
        "SPREADBOARD_CHECKOUT_TEST_PRICE_CHATS",
        "SPREADBOARD_CHECKOUT_TEST_PRICE_TIER",
    ):
        monkeypatch.delenv(name, raising=False)

    reply = _buy(db)

    assert "$149.00" in reply["text"]
