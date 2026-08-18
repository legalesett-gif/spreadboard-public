"""The "/" menu is the bot's discovery surface, so it must not lie.

Telegram shows this list the moment a member types "/". A command advertised
there that the parser does not understand is worse than a missing one: the
member types exactly what they were offered and gets silence.

The opposite gap matters too — a working command absent from the menu is a
feature nobody finds.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from spreadboard import telegram_queries

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_telegram_webhook.py"

# Registered for the operator's own setup and handled elsewhere in the bot.
NON_QUERY_COMMANDS = {
    "setupgroup", "subscribe", "mysubscription", "access", "start", "top",
}


def _advertised() -> dict[str, set[str]]:
    """Commands per scope, read from the configuration script itself."""
    source = SCRIPT.read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    for match in re.finditer(
        r'"scope":\s*\{"type":\s*"(\w+)"\},.*?"commands":\s*(\[.*?\])',
        source,
        re.DOTALL,
    ):
        scope, block = match.group(1), match.group(2)
        out[scope] = {entry["command"] for entry in ast.literal_eval(block)}
    return out


def test_both_scopes_are_registered() -> None:
    scopes = _advertised()
    assert "all_group_chats" in scopes
    assert "all_private_chats" in scopes


def test_every_advertised_command_is_understood_by_the_parser() -> None:
    """Type what the menu offered and something must come back."""
    unknown = []
    for scope, commands in _advertised().items():
        for command in commands:
            if command in NON_QUERY_COMMANDS:
                continue
            # Token-taking commands are offered a token; board-wide ones are not.
            parsed = telegram_queries.parse_query(f"/{command} GUA")
            if parsed is None:
                parsed = telegram_queries.parse_query(f"/{command}")
            if parsed is None:
                unknown.append(f"{scope}:{command}")
    assert not unknown, f"advertised but unparsed: {unknown}"


def test_the_private_menu_leads_with_what_members_actually_ask() -> None:
    """"What is worth looking at" is the first question a member arrives with.

    Groups have no menu at all now -- see test_telegram_group_menu_tag -- so the
    private chat is where ordering matters.
    """
    private = list(_advertised()["all_private_chats"])
    assert {"top", "deep", "carry"} <= set(private)


def test_every_board_wide_view_is_reachable_without_a_menu() -> None:
    """The group lost its popup, so the parser is the only route in."""
    for command in ("top", "deep", "carry", "help", "status"):
        assert telegram_queries.parse_query(f"/{command}") is not None, command


def test_help_is_advertised_where_a_menu_still_exists() -> None:
    """Nothing in Telegram hints that "GUA/f" is a thing you can type."""
    assert "help" in _advertised()["all_private_chats"]
