"""Token lookups in the subscriber Telegram group."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spreadboard import accounts, telegram_bot, telegram_queries  # noqa: E402


GROUP_ID = -1002222222222


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    telegram_queries.reset_cooldowns()
    yield
    telegram_queries.reset_cooldowns()


def route(token, kind, long_venue, short_venue, edge, funding, apr, depth,
          *, rails=(None, None, None, None), guarded=False):
    """One route dict in the shape api_spreads.load_spreads actually returns."""
    ld, lw, sd, sw = rails
    return {
        "token": token, "route_kind": kind,
        "long_venue": long_venue, "short_venue": short_venue,
        "executable_spread_pct": edge,
        "displayed_open_spread_pct": edge,
        "funding_daily_pct": funding, "funding_spread_pct": funding,
        "funding_apr_pct": apr, "depth_usd": depth,
        "long_deposit_enabled": ld, "long_withdraw_enabled": lw,
        "short_deposit_enabled": sd, "short_withdraw_enabled": sw,
        "mirage_guarded": guarded, "freshness": "fresh", "age_min": 0.2,
    }


SIREN_ROUTES = [
    route("SIREN", "DEX-FUTURES", "OKX DEX", "Bybit", 1.70, 0.051, 18.6, 142_000,
          rails=(True, True, True, False)),
    route("SIREN", "SPOT-FUTURES", "Gate", "Bybit", 1.10, 0.020, 7.3, 88_000,
          rails=(False, True, True, True)),
    # A guarded mirage must never reach a member.
    route("SIREN", "FUTURES", "Ghost", "Phantom", 9999.0, 0.0, 0.0, None, guarded=True),
]


@pytest.fixture()
def board_file(tmp_path, monkeypatch):
    """Patch the real feed loader; the returned path is unused but kept for the API."""
    def fake_load_spreads(*, q=None, **kwargs):
        assert 'include_stale' not in kwargs, 'bot must not be laxer than the site'
        assert 'max_age_min' not in kwargs, 'bot must not bypass freshness filters'
        token = str(q or "").upper()
        groups = []
        if token == "SIREN":
            groups = [{"token": "SIREN", "routes": list(SIREN_ROUTES)}]
        elif token == "GUA":
            groups = [{"token": "GUA", "routes": [
                route("GUA", "FUTURES", "MEXC", "KuCoin", 0.74, -0.021, -7.6, 51_000)]}]
        return {"groups": groups}

    monkeypatch.setattr(telegram_queries.api_spreads, "load_spreads", fake_load_spreads)
    return tmp_path / "unused.jsonl"


# --------------------------------------------------------------------------
# Trigger parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,kind,symbol",
    [
        ("$SIREN", "spread", "SIREN"),
        ("what about $siren today?", "spread", "SIREN"),
        ("/spread SIREN", "spread", "SIREN"),
        ("/funding SIREN", "funding", "SIREN"),
        ("/transfer SIREN", "transfer", "SIREN"),
        ("/spread@spreadbot SIREN", "spread", "SIREN"),
        ("/funding $siren", "funding", "SIREN"),
    ],
)
def test_recognised_triggers(text, kind, symbol):
    query = telegram_queries.parse_query(text)
    assert query is not None
    assert (query.kind, query.symbol) == (kind, symbol)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "GUA looks interesting",          # bare ticker in conversation
        "I made $500 today",              # dollar amount, not a cashtag
        "good morning everyone",
        "SKY is the limit",
        "/spread",                        # command with no token
        "https://example.com/$FOO",       # no leading whitespace boundary
    ],
)
def test_ordinary_chat_is_not_a_query(text):
    assert telegram_queries.parse_query(text) is None


@pytest.mark.parametrize(
    "raw",
    ["../../etc/passwd", "<script>alert(1)</script>", "SIREN'; DROP TABLE users;--", "A" * 50],
)
def test_symbol_is_sanitised(raw):
    """Whatever a member types becomes a bounded, inert token string."""
    query = telegram_queries.parse_query(f"/spread {raw}")
    assert query is not None
    assert len(query.symbol) <= 12
    assert set(query.symbol) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def test_repeat_question_is_suppressed_within_cooldown():
    query = telegram_queries.Query(kind="spread", symbol="SIREN")
    assert telegram_queries.allow(GROUP_ID, query, now=1000.0) is True
    assert telegram_queries.allow(GROUP_ID, query, now=1005.0) is False


def test_cooldown_expires():
    query = telegram_queries.Query(kind="spread", symbol="SIREN")
    assert telegram_queries.allow(GROUP_ID, query, now=1000.0) is True
    later = 1000.0 + telegram_queries.COOLDOWN_SECONDS + 1
    assert telegram_queries.allow(GROUP_ID, query, now=later) is True


def test_cooldown_is_per_token_and_per_kind():
    assert telegram_queries.allow(GROUP_ID, telegram_queries.Query("spread", "SIREN"), now=1000.0)
    assert telegram_queries.allow(GROUP_ID, telegram_queries.Query("spread", "GUA"), now=1000.0)
    assert telegram_queries.allow(GROUP_ID, telegram_queries.Query("funding", "SIREN"), now=1000.0)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_spread_reply_lists_every_route_best_first(board_file):
    body = telegram_queries.render(
        telegram_queries.Query("spread", "SIREN"), board_path=board_file
    )
    assert "SIREN" in body and "2 routes" in body
    assert "OKX DEX&gt;Bybit" in body and "Gate&gt;Bybit" in body
    assert body.index("+1.70%") < body.index("+1.10%"), "best edge must come first"
    assert "$142K" in body


def test_funding_reply_shows_net_and_apr(board_file):
    body = telegram_queries.render(
        telegram_queries.Query("funding", "SIREN"), board_path=board_file
    )
    assert "funding" in body
    assert "+0.051%" in body and "+18.6%" in body


def test_transfer_reply_shows_deposit_and_withdraw_state(board_file):
    body = telegram_queries.render(
        telegram_queries.Query("transfer", "SIREN"), board_path=board_file
    )
    assert "DEPOSIT" in body and "WITHDRAW" in body
    assert "SHUT" in body, "a closed rail must be visible, not silently blank"
    assert "Gate" in body and "OKX DEX" in body


def test_unknown_token_answers_plainly(board_file):
    body = telegram_queries.render(
        telegram_queries.Query("spread", "NOTATOKEN"), board_path=board_file
    )
    assert "no parsed routes" in body


def test_reply_carries_a_risk_note_and_site_link(board_file):
    body = telegram_queries.render(
        telegram_queries.Query("spread", "SIREN"),
        board_path=board_file, public_url="https://spreadarbitrage.ink",
    )
    assert "not advice" in body
    assert "https://spreadarbitrage.ink/markets?q=SIREN" in body


def test_injected_markup_never_reaches_the_reply(board_file):
    """A hostile 'token' must not smuggle tags into the HTML-parsed message."""
    body = telegram_queries.render(
        telegram_queries.Query("spread", "<b>X</b><a href=evil>"), board_path=board_file
    )
    assert "<a href" not in body and "<script" not in body
    # render() normalises independently of parse_query, so the angle brackets
    # are gone entirely rather than merely escaped.
    assert "<b>X" not in body
    assert body.count("<b>") == 1 and body.count("</b>") == 1


# --------------------------------------------------------------------------
# Group gating through the bot
# --------------------------------------------------------------------------


def message(chat_id: int, text: str, chat_type: str = "supergroup") -> dict:
    return {"message": {"chat": {"id": chat_id, "type": chat_type},
                        "from": {"id": 42}, "text": text}}


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def test_query_is_ignored_in_an_unregistered_group(db, board_file):
    reply = telegram_bot.handle_update(
        message(GROUP_ID, "$SIREN"), db_path=db, board_path=board_file
    )
    assert reply is None, "the bot must stay silent in groups it does not serve"


def test_query_is_answered_in_the_registered_group(db, board_file):
    accounts.configure_telegram_community(
        GROUP_ID, title="Subscribers", configured_by_telegram_user_id=1,
        invite_link="https://t.me/+abc", db_path=db,
    )
    reply = telegram_bot.handle_update(
        message(GROUP_ID, "$SIREN"), db_path=db, board_path=board_file
    )
    assert reply is not None
    assert reply["parse_mode"] == "HTML"
    assert reply["chat_id"] == GROUP_ID
    assert "SIREN" in reply["text"]


def test_chatter_in_the_registered_group_is_ignored(db, board_file):
    accounts.configure_telegram_community(
        GROUP_ID, title="Subscribers", configured_by_telegram_user_id=1,
        invite_link="https://t.me/+abc", db_path=db,
    )
    assert telegram_bot.handle_update(
        message(GROUP_ID, "morning all"), db_path=db, board_path=board_file
    ) is None


def test_reply_stays_in_the_forum_topic_it_was_asked_in(db, board_file):
    """Forum groups deliver messages per topic; answering in General is wrong."""
    accounts.configure_telegram_community(
        GROUP_ID, title="Spread", configured_by_telegram_user_id=1,
        invite_link="https://t.me/+abc", db_path=db,
    )
    update = message(GROUP_ID, "$SIREN")
    update["message"]["message_thread_id"] = 77
    reply = telegram_bot.handle_update(update, db_path=db, board_path=board_file)
    assert reply is not None
    assert reply["message_thread_id"] == 77


def test_non_forum_reply_omits_thread_id(db, board_file):
    accounts.configure_telegram_community(
        GROUP_ID, title="Spread", configured_by_telegram_user_id=1,
        invite_link="https://t.me/+abc", db_path=db,
    )
    reply = telegram_bot.handle_update(
        message(GROUP_ID, "$SIREN"), db_path=db, board_path=board_file
    )
    assert reply is not None and "message_thread_id" not in reply


def test_mirage_guarded_routes_are_never_shown(board_file):
    """The guard exists to stop members chasing dislocation traps."""
    body = telegram_queries.render(
        telegram_queries.Query("spread", "SIREN"), board_path=board_file
    )
    assert "Ghost" not in body and "9999" not in body
    assert "2 routes" in body, "only the two real routes should count"
