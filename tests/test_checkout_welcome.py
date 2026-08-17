"""First contact should say what is on offer, not list commands.

Someone arriving at the bot has usually never seen the product. The old reply
explained how to look a token up inside a subscriber forum they cannot enter
yet, which answers a question they have not asked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from spreadboard import accounts, telegram_bot, telegram_checkout

RECEIVER = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
CHAT = 8650235482


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


def _start(db, *, username=None, first_name=None):
    sender = {"id": CHAT}
    if username:
        sender["username"] = username
    if first_name:
        sender["first_name"] = first_name
    return telegram_bot.handle_update(
        {
            "message": {
                "chat": {"id": CHAT, "type": "private"},
                "from": sender,
                "text": "/start",
            }
        },
        db_path=db,
    )


def test_a_newcomer_is_greeted_by_their_handle(db) -> None:
    reply = _start(db, username="tradername")

    assert "@tradername" in reply["text"]


def test_a_handle_less_newcomer_is_greeted_by_first_name(db) -> None:
    reply = _start(db, first_name="Sam")

    assert "Sam" in reply["text"]
    assert "@" not in reply["text"].splitlines()[0]


def test_an_anonymous_newcomer_is_still_greeted(db) -> None:
    reply = _start(db)

    assert reply["text"].strip()
    assert "None" not in reply["text"]


def test_the_welcome_says_what_each_tier_buys(db) -> None:
    reply = _start(db, username="tradername")

    assert "Scanner" in reply["text"]
    assert "Research Pro" in reply["text"]
    assert "private" in reply["text"].casefold()
    assert "Arbitrum" in reply["text"]


def test_the_welcome_offers_the_subscribe_button(db) -> None:
    reply = _start(db, username="tradername")

    buttons = [b for row in reply["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons[0]["callback_data"] == "co:restart"


def test_the_welcome_quotes_the_price_this_chat_will_be_charged(db, monkeypatch) -> None:
    """A welcome saying $149 above a $5 keyboard is how a buyer sends the wrong amount."""
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_CENTS", "500")
    monkeypatch.setenv(
        "SPREADBOARD_CHECKOUT_TEST_PRICE_UNTIL",
        (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat(),
    )
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_CHATS", str(CHAT))
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_TIER", "research_pro")

    reply = _start(db, username="tradername")

    assert "$5" in reply["text"]
    assert "$149" not in reply["text"]


def test_an_existing_member_keeps_the_command_reference(db) -> None:
    """The welcome is for people who have not bought; members need the commands."""
    user = accounts.create_user(
        email="member@example.test",
        display_name="Member",
        password="a-secure-member-password",
        subscription_status="active",
        db_path=db,
    )
    token = accounts.create_telegram_link_token(user["id"], db_path=db)
    accounts.bind_telegram_chat(token, CHAT, db_path=db)

    reply = _start(db, username="tradername")

    assert "$SIREN" in reply["text"]


def test_help_still_lists_commands_for_a_newcomer(db) -> None:
    reply = telegram_bot.handle_update(
        {"message": {"chat": {"id": CHAT, "type": "private"}, "text": "/help"}},
        db_path=db,
    )

    assert "$SIREN" in reply["text"]


# --------------------------------------------------------------------------
# Opening the rehearsal price to everyone
# --------------------------------------------------------------------------


def _arm(monkeypatch, chats: str) -> None:
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_CENTS", "500")
    monkeypatch.setenv(
        "SPREADBOARD_CHECKOUT_TEST_PRICE_UNTIL",
        (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat(),
    )
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_CHATS", chats)
    monkeypatch.setenv("SPREADBOARD_CHECKOUT_TEST_PRICE_TIER", "research_pro")


def test_a_star_opens_the_price_to_every_chat(monkeypatch) -> None:
    _arm(monkeypatch, "*")

    assert telegram_checkout.rehearsal_price_cents("research_pro", 111) == 500
    assert telegram_checkout.rehearsal_price_cents("research_pro", 222) == 500


def test_the_wildcard_still_obeys_the_deadline(monkeypatch) -> None:
    """Open to everyone is exactly when an expiry matters most."""
    _arm(monkeypatch, "*")
    monkeypatch.setenv(
        "SPREADBOARD_CHECKOUT_TEST_PRICE_UNTIL",
        (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat(),
    )

    assert telegram_checkout.rehearsal_price_cents("research_pro", 111) is None


def test_the_wildcard_still_obeys_the_tier(monkeypatch) -> None:
    _arm(monkeypatch, "*")

    assert telegram_checkout.rehearsal_price_cents("scanner", 111) is None


def test_an_empty_list_is_still_nobody(monkeypatch) -> None:
    """Only an explicit star means everyone; blank must never widen to all."""
    _arm(monkeypatch, "")

    assert telegram_checkout.rehearsal_price_cents("research_pro", 111) is None
