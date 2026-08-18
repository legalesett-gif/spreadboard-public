"""Asking without repeating yourself, and without the /menu.

The reported failure, exactly: a member typed "ESports", then typed "/f" and
picked "/funding" from Telegram's popup. The popup REPLACES the whole compose
box, so what actually got sent was the single word "/funding" -- no token -- and
the bot said nothing at all.

Two things follow from that.

The command must not be a dead end without a token. A chat that was just
talking about ESPORTS means ESPORTS, so the last token seen in that chat
answers it.

And the "/" menu should be avoidable entirely. Picking from it in a supergroup
makes Telegram paste "/funding@spreadarbitragesubscription_bot", which the
operator does not want to look at and which no server-side setting can prevent.
So plain words work too: "esports funding" needs no slash, so no popup appears
and nothing gets tagged.
"""

from __future__ import annotations

import pytest
from spreadboard.telegram_queries import Query, parse_query

from spreadboard import telegram_queries

CHAT = -1004373383074


@pytest.fixture(autouse=True)
def _known(monkeypatch):
    """A board carrying the tokens these tests name."""
    payload = {
        "groups": [
            {"token": "ESPORTS", "routes": [{"token": "ESPORTS"}]},
            {"token": "GUA", "routes": [{"token": "GUA"}]},
            {"token": "BTW", "routes": [{"token": "BTW"}]},
            {"token": "4", "routes": [{"token": "4"}]},
        ]
    }
    monkeypatch.setattr(telegram_queries, "client_visible_payload", lambda: payload)
    monkeypatch.setattr(telegram_queries, "_warm_payload", lambda *_a, **_k: payload)
    telegram_queries.reset_context()
    yield
    telegram_queries.reset_context()


# --------------------------------------------------------------------------
# A bare command remembers what the chat was talking about
# --------------------------------------------------------------------------


def test_a_bare_command_uses_the_token_the_chat_just_mentioned() -> None:
    """The reported flow: "ESports", then "/funding"."""
    telegram_queries.remember_token(CHAT, "ESPORTS")

    query = telegram_queries.resolve(parse_query("/funding"), chat_id=CHAT)

    assert query is not None
    assert query.symbol == "ESPORTS"
    assert query.kind == "funding"


def test_the_bot_suffix_telegram_pastes_does_not_break_it() -> None:
    """Picking from the menu sends "/funding@thebot"."""
    telegram_queries.remember_token(CHAT, "ESPORTS")

    query = telegram_queries.resolve(
        parse_query("/funding@spreadarbitragesubscription_bot"), chat_id=CHAT
    )

    assert query is not None
    assert query.symbol == "ESPORTS"
    assert query.kind == "funding"


def test_a_bare_token_message_is_what_sets_the_context() -> None:
    """"ESports" on its own answers nothing, but it is remembered."""
    assert parse_query("ESports") is None

    telegram_queries.note_message(CHAT, "ESports")

    query = telegram_queries.resolve(parse_query("/funding"), chat_id=CHAT)
    assert query is not None
    assert query.symbol == "ESPORTS"


def test_ordinary_prose_never_becomes_the_context() -> None:
    """Only a message that is exactly a listed token counts."""
    telegram_queries.note_message(CHAT, "the funding on that one looked good")
    telegram_queries.note_message(CHAT, "worth a look")

    assert telegram_queries.resolve(parse_query("/funding"), chat_id=CHAT) is None


def test_an_unlisted_word_is_not_remembered() -> None:
    telegram_queries.note_message(CHAT, "NOTATOKEN")

    assert telegram_queries.resolve(parse_query("/funding"), chat_id=CHAT) is None


def test_context_is_per_chat() -> None:
    telegram_queries.remember_token(CHAT, "ESPORTS")

    assert telegram_queries.resolve(parse_query("/funding"), chat_id=-999) is None


def test_a_command_that_names_its_own_token_ignores_the_context() -> None:
    telegram_queries.remember_token(CHAT, "ESPORTS")

    query = telegram_queries.resolve(parse_query("GUA/f"), chat_id=CHAT)

    assert query is not None
    assert query.symbol == "GUA"


def test_board_wide_commands_are_untouched_by_context() -> None:
    telegram_queries.remember_token(CHAT, "ESPORTS")

    query = telegram_queries.resolve(parse_query("/top"), chat_id=CHAT)

    assert query is not None
    assert query.kind == "top"
    assert query.symbol == ""


def test_stale_context_expires_rather_than_answering_yesterdays_token() -> None:
    telegram_queries.remember_token(CHAT, "ESPORTS", now=1000.0)

    resolved = telegram_queries.resolve(
        parse_query("/funding"),
        chat_id=CHAT,
        now=1000.0 + telegram_queries.CONTEXT_TTL_SECONDS + 1,
    )

    assert resolved is None


# --------------------------------------------------------------------------
# No slash at all, so Telegram never pastes a tag
# --------------------------------------------------------------------------


def test_a_token_and_an_aspect_need_no_slash() -> None:
    query = telegram_queries.resolve(parse_query("esports funding"), chat_id=CHAT)

    assert query is not None
    assert query.symbol == "ESPORTS"
    assert query.kind == "funding"


def test_the_two_words_work_in_either_order() -> None:
    query = telegram_queries.resolve(parse_query("funding esports"), chat_id=CHAT)

    assert query is not None
    assert query.symbol == "ESPORTS"
    assert query.kind == "funding"


def test_a_plain_pair_only_counts_when_the_token_is_on_the_board() -> None:
    """Otherwise every two-word sentence ending in "depth" becomes a lookup."""
    assert parse_query("some depth") is None
    assert parse_query("more funding") is None


def test_a_sentence_that_merely_contains_both_words_is_left_alone() -> None:
    """"BTW funding is great" is someone talking, not someone asking."""
    assert parse_query("BTW funding is great") is None
    assert parse_query("i think esports funding looks good") is None


def test_sizing_still_takes_its_amount_without_a_slash() -> None:
    query = telegram_queries.resolve(parse_query("esports calc 5000"), chat_id=CHAT)

    assert query is not None
    assert query.kind == "calc"
    assert query.symbol == "ESPORTS"
    assert query.arg == "5000"


# --------------------------------------------------------------------------
# Never a dead end
# --------------------------------------------------------------------------


def test_a_bare_command_with_no_context_is_answerable_rather_than_silent() -> None:
    """Silence reads as a broken bot. It must say what it needs."""
    resolved = telegram_queries.resolve(parse_query("/funding"), chat_id=CHAT)

    assert resolved is None  # nothing to answer with...
    prompt = telegram_queries.needs_token_prompt(Query(kind="funding", symbol=""))
    assert "funding" in prompt.casefold()
    assert "/" in prompt


def test_help_illustrates_with_a_token_that_is_actually_on_the_board() -> None:
    """A fixed example reads as the only thing that works.

    Every one of the board's tokens answers these, but help that always says
    "GUA/" invites exactly the conclusion that GUA is special.
    """
    body = telegram_queries.render(
        Query(kind="help", symbol=""), board_path="", public_url=""
    )

    assert "GUA/" not in body or "ESPORTS/" in body
    # Whatever it picked must be a real listed token, not a placeholder.
    shown = {token for token in telegram_queries.known_tokens() if f"{token}/" in body}
    assert shown, "help showed no live token as its example"


def test_help_says_the_slash_is_optional() -> None:
    """The tag the operator dislikes only appears via the "/" popup."""
    body = telegram_queries.render(
        Query(kind="help", symbol=""), board_path="", public_url=""
    )

    assert "no slash" in body.casefold()
