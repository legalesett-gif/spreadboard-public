"""The group command menu is useful only when every tagged form works.

Telegram inserts "/funding@spreadarbitragesubscription_bot" when a member picks
a command from the "/" popup in a supergroup. The parser understands that form,
so removing the menu only made working commands undiscoverable.

Nothing is lost but the popup. Command registration only drives that menu; with
privacy mode off the bot receives every message in the group either way, so a
hand-typed "/funding" still arrives and is still answered.

Private chats keep their menu: there is only one bot in a DM, so nothing is
ever tagged there, and the menu is the only discovery surface a new buyer has.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from spreadboard import telegram_queries

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_telegram_webhook.py"


def _commands(scope: str) -> list[str]:
    source = SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        rf'"scope":\s*\{{"type":\s*"{scope}"\}},.*?"commands":\s*(\[.*?\])',
        source,
        re.DOTALL,
    )
    if match is None:
        return []
    return [entry["command"] for entry in ast.literal_eval(match.group(1))]


def _deleted(scope: str) -> bool:
    """Whether the script actually REMOVES this scope's menu.

    setMyCommands with an empty list leaves the previous list in place -- the
    live bot still listed all thirteen group commands after exactly that call.
    deleteMyCommands is the only thing that removes them.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    return bool(
        re.search(
            rf'deleteMyCommands",\s*\{{"scope":\s*\{{"type":\s*"{scope}"\}}\}}',
            source,
        )
    )


def test_the_group_menu_exposes_every_member_data_view() -> None:
    group = set(_commands("all_group_chats"))

    assert {
        "top", "spread", "funding", "radar", "deep", "carry", "token",
        "depth", "transfer", "calc", "help", "status",
    } <= group
    assert {"subscribe", "mysubscription", "access", "setupgroup"}.isdisjoint(group)


def test_private_chats_keep_their_menu() -> None:
    """A DM has one bot, so nothing is tagged, and buyers need the menu."""
    private = _commands("all_private_chats")

    assert "subscribe" in private
    assert "help" in private


def test_every_command_still_works_when_typed_by_hand() -> None:
    """Unregistering the menu must not unregister the behaviour.

    Command registration drives the popup only. The parser is what answers, and
    it has to keep answering the commands the menu used to advertise.
    """
    for text in ("/top", "/deep", "/carry", "/help", "/status", "/radar"):
        query = telegram_queries.parse_query(text)
        assert query is not None, f"{text} stopped working"

    for text in ("/funding GUA", "/spread GUA", "/depth GUA", "/transfer GUA"):
        query = telegram_queries.parse_query(text)
        assert query is not None, f"{text} stopped working"
        assert query.symbol == "GUA"


def test_the_tagged_form_is_still_understood() -> None:
    """Members who already have the tagged version in their history."""
    query = telegram_queries.parse_query("/top@spreadarbitragesubscription_bot")

    assert query is not None
    assert query.kind == "top"


def test_the_default_scope_stays_clear_so_scoped_menus_do_not_leak() -> None:
    assert _deleted("default"), "default scope menu is not removed"


def test_start_survives_the_default_clear() -> None:
    """It only lived in the default scope, and a DM still needs it."""
    assert "start" in _commands("all_private_chats")
