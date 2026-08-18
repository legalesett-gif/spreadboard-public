"""No group command menu, because the menu is what pastes the @bot tag.

Telegram inserts "/funding@spreadarbitragesubscription_bot" when a member picks
a command from the "/" popup in a supergroup. That insertion is done by the
client and no API setting changes how it writes the command -- but the popup
itself is populated by `setMyCommands` for the group scope. Register nothing
there and the popup cannot offer, and therefore cannot tag, anything.

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
    assert match is not None, f"no {scope} registration found"
    return [entry["command"] for entry in ast.literal_eval(match.group(1))]


def test_the_group_menu_is_empty_so_nothing_can_be_tagged() -> None:
    """The popup is the only thing that writes "@botname" into a message."""
    assert _commands("all_group_chats") == []


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


def test_the_default_scope_is_cleared_too() -> None:
    """An empty group scope falls back to default, and the popup returns.

    Telegram resolves a group's menu down the scope chain. Leaving commands in
    the default scope would put the popup -- and its "@botname" insertion --
    straight back into the group.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r'"scope":\s*\{"type":\s*"default"\},\s*"commands":\s*(\[\s*\])',
        source,
        re.DOTALL,
    )
    assert match is not None, "default scope still carries commands"


def test_start_survives_the_default_clear() -> None:
    """It only lived in the default scope, and a DM still needs it."""
    assert "start" in _commands("all_private_chats")
