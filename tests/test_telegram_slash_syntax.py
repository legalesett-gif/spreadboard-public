"""Token-first lookups: ``GUA/``, ``GUA/funding``, ``GUA/calc 5000``.

You think of the token before you think of the question, so this lets you type
it in that order. On a phone ``GUA/f`` is far less work than ``/spread GUA``,
and it does not fight Telegram's own ``/`` autocomplete popup.

The bot runs with privacy mode off, which means it receives EVERY message in
the group. That makes the false-positive direction the dangerous one: "and/or",
"24/7", a pasted URL and a date all contain a slash, and a bot that answers
those is worse than one that answers nothing. Hence the guards here --
the pattern must open the message, the aspect must be one we recognise, and
anything URL-shaped is refused outright.
"""

from __future__ import annotations

import pytest
from spreadboard.telegram_queries import parse_query

from spreadboard import telegram_queries

# --------------------------------------------------------------------------
# It must not answer ordinary conversation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "and/or we could wait for the funding flip",
        "it runs 24/7 now",
        "https://spreadarbitrage.ink/markets?q=GUA",
        "see http://example.com/funding for the writeup",
        "spreadarbitrage.ink/markets",
        "12/05 was the last settlement",
        "w/ the short leg on Gate",
        "n/a",
        "I/we can take that one",
        "the ratio is 3/4 of the notional",
        "GUA / funding",  # spaced: ordinary prose, not the compact syntax
        "what about GUA/funding",  # mid-sentence, not an opening command
    ],
)
def test_ordinary_chat_is_never_answered(message: str) -> None:
    assert parse_query(message) is None, f"would have replied to: {message!r}"


def test_a_slash_alone_is_not_a_lookup() -> None:
    assert parse_query("/") is None
    assert parse_query("//") is None


# --------------------------------------------------------------------------
# The syntax itself
# --------------------------------------------------------------------------


def test_a_bare_token_slash_asks_for_the_spread() -> None:
    query = parse_query("GUA/")

    assert query is not None
    assert query.symbol == "GUA"
    assert query.kind == "spread"


def test_each_aspect_has_a_full_word_and_a_single_letter() -> None:
    """One letter is the whole point: this is typed on a phone, mid-conversation."""
    for text, expected in (
        ("GUA/funding", "funding"),
        ("GUA/f", "funding"),
        ("GUA/depth", "depth"),
        ("GUA/d", "depth"),
        ("GUA/transfer", "transfer"),
        ("GUA/t", "transfer"),
        ("GUA/spread", "spread"),
        ("GUA/s", "spread"),
    ):
        query = parse_query(text)
        assert query is not None, text
        assert query.kind == expected, f"{text} -> {query.kind}"
        assert query.symbol == "GUA"


def test_the_token_is_normalised_to_upper_case() -> None:
    query = parse_query("gua/f")

    assert query is not None
    assert query.symbol == "GUA"


def test_symbols_with_digits_and_separators_survive() -> None:
    for symbol in ("1INCH", "1000BONK", "BTC.B"):
        query = parse_query(f"{symbol}/f")
        assert query is not None, symbol
        assert query.symbol == symbol.upper()


def test_an_unrecognised_aspect_is_ignored_rather_than_guessed() -> None:
    """"and/or" is only safe because "or" is not an aspect."""
    assert parse_query("GUA/wen") is None
    assert parse_query("GUA/moon") is None


# --------------------------------------------------------------------------
# Sizing: the question that follows every good spread
# --------------------------------------------------------------------------


def test_calc_carries_the_capital_the_member_typed() -> None:
    query = parse_query("GUA/calc 5000")

    assert query is not None
    assert query.kind == "calc"
    assert query.symbol == "GUA"
    assert query.arg == "5000"


def test_calc_accepts_the_short_form_and_common_money_spellings() -> None:
    for text, expected in (
        ("GUA/c 5000", "5000"),
        ("GUA/c $5,000", "$5,000"),
        ("GUA/c 5k", "5k"),
    ):
        query = parse_query(text)
        assert query is not None, text
        assert query.kind == "calc", text
        assert query.arg == expected


def test_calc_without_an_amount_still_parses_so_it_can_explain_itself() -> None:
    query = parse_query("GUA/calc")

    assert query is not None
    assert query.kind == "calc"
    assert query.arg == ""


# --------------------------------------------------------------------------
# Board-wide commands, no token
# --------------------------------------------------------------------------


def test_the_board_wide_commands_need_no_token() -> None:
    for text, expected in (
        ("/top", "top"),
        ("/carry", "carry"),
        ("/deep", "deep"),
        ("/help", "help"),
        ("/status", "status"),
    ):
        query = parse_query(text)
        assert query is not None, text
        assert query.kind == expected, text
        assert query.symbol == ""


def test_group_commands_carrying_the_bot_suffix_still_parse() -> None:
    """Telegram appends @botname when several bots share a group."""
    query = parse_query("/top@spreadarbitragesubscription_bot")

    assert query is not None
    assert query.kind == "top"


def test_the_existing_cashtag_still_works() -> None:
    """The new syntax is an addition, not a replacement."""
    query = parse_query("$SIREN")

    assert query is not None
    assert query.symbol == "SIREN"
    assert query.kind == "spread"


def test_a_question_mark_asks_what_can_be_asked() -> None:
    query = parse_query("GUA/?")

    assert query is not None
    assert query.kind == "help"
    assert query.symbol == "GUA"


# --------------------------------------------------------------------------
# Rate limiting must see the argument
# --------------------------------------------------------------------------


def test_two_different_sizings_are_not_treated_as_a_repeat() -> None:
    """$1,000 and $50,000 are different questions about the same token."""
    telegram_queries.reset_cooldowns()
    first = parse_query("GUA/c 1000")
    second = parse_query("GUA/c 50000")
    assert first is not None and second is not None

    assert telegram_queries.allow(1, first) is True
    assert telegram_queries.allow(1, second) is True


def test_the_same_question_twice_is_still_rate_limited() -> None:
    telegram_queries.reset_cooldowns()
    query = parse_query("GUA/f")
    assert query is not None

    assert telegram_queries.allow(1, query) is True
    assert telegram_queries.allow(1, query) is False
