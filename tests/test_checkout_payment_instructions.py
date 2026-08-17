"""The payment message is where a buyer loses their money.

An exact amount and a 42-character address, both retyped by hand from a wrapped
paragraph, is the worst possible way to ask for a transfer. Telegram renders
<code> as monospace and copies it to the clipboard on tap, so each value the
buyer must reproduce exactly gets a line of its own that they never type.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from spreadboard import accounts, telegram_bot

RECEIVER = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
CHAT = 4242


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


def _invoice_reply(db):
    telegram_bot.handle_update(
        {"message": {"chat": {"id": CHAT, "type": "private"}, "text": "/subscribe"}},
        db_path=db,
    )
    for data in ("co:tier:research_pro", "co:period:30"):
        telegram_bot.handle_update(
            {"callback_query": {"data": data, "message": {"chat": {"id": CHAT, "type": "private"}, "message_id": 1}}},
            db_path=db,
        )
    telegram_bot.handle_update(
        {"message": {"chat": {"id": CHAT, "type": "private"}, "text": "buyer@example.test"}},
        db_path=db,
    )
    return telegram_bot.handle_update(
        {"callback_query": {"data": "co:confirm", "message": {"chat": {"id": CHAT, "type": "private"}, "message_id": 1}}},
        db_path=db,
    )


def test_the_amount_is_tap_to_copy_on_its_own(db) -> None:
    reply = _invoice_reply(db)

    assert "<code>149.00</code>" in reply["text"]


def test_the_address_is_tap_to_copy_on_its_own(db) -> None:
    reply = _invoice_reply(db)

    assert f"<code>{RECEIVER}</code>" in reply["text"]


def test_telegram_is_told_to_render_the_markup(db) -> None:
    """Without HTML mode the buyer just sees literal <code> tags."""
    reply = _invoice_reply(db)

    assert reply["parse_mode"] == "HTML"


def test_the_copyable_amount_carries_no_currency_symbol(db) -> None:
    """Pasting "$149.00" into a wallet amount field fails; "149.00" works."""
    reply = _invoice_reply(db)

    assert "<code>$" not in reply["text"]


def test_the_steps_are_numbered_so_nothing_is_skipped(db) -> None:
    reply = _invoice_reply(db)

    assert "1." in reply["text"] and "2." in reply["text"]


def test_the_network_is_named_next_to_the_address(db) -> None:
    """A correct address on the wrong chain is the commonest way to lose funds."""
    reply = _invoice_reply(db)

    assert "Arbitrum One" in reply["text"]


def test_the_accepted_tokens_are_stated(db) -> None:
    reply = _invoice_reply(db)

    assert "USDC" in reply["text"] and "USDT" in reply["text"]


def test_it_says_why_the_amount_must_be_exact(db) -> None:
    """Buyers round. Telling them the amount IS the identifier stops it."""
    reply = _invoice_reply(db)

    lowered = reply["text"].casefold()
    assert "exact" in lowered
    assert "identifies" in lowered or "recognise" in lowered or "recognize" in lowered


def test_it_says_what_happens_next_and_how_long(db) -> None:
    reply = _invoice_reply(db)

    lowered = reply["text"].casefold()
    assert "confirmation" in lowered
    assert "60 minutes" in lowered or "expires" in lowered


def test_the_status_button_survives_the_rewrite(db) -> None:
    reply = _invoice_reply(db)

    buttons = [b["callback_data"] for row in reply["reply_markup"]["inline_keyboard"] for b in row]
    assert "co:status" in buttons
