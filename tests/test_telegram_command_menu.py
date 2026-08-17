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


def test_the_group_menu_leads_with_what_members_actually_ask() -> None:
    """"What is worth looking at" is the first question, so it is the first item."""
    source = SCRIPT.read_text(encoding="utf-8")
    group = re.search(
        r'"scope":\s*\{"type":\s*"all_group_chats"\},.*?"commands":\s*(\[.*?\])',
        source,
        re.DOTALL,
    )
    assert group is not None
    commands = [entry["command"] for entry in ast.literal_eval(group.group(1))]
    assert commands[0] == "top"
    # The operator-only plumbing must not sit above the member commands.
    assert commands.index("setupgroup") > commands.index("help")


def test_the_group_can_reach_every_board_wide_view() -> None:
    """These answer without a token and are the reason the menu is useful."""
    group = _advertised()["all_group_chats"]
    assert {"top", "deep", "carry", "help", "status"} <= group


def test_help_is_advertised_because_the_shortcuts_are_invisible_otherwise() -> None:
    """Nothing in Telegram hints that "GUA/f" is a thing you can type."""
    for scope, commands in _advertised().items():
        assert "help" in commands, scope
