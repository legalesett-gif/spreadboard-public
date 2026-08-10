"""Token lookups in the subscriber Telegram group."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spreadboard import accounts, telegram_bot, telegram_queries

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
def board_file(tmp_path, monkeypatch, request):
    """Patch the real feed loader; the returned path is unused but kept for the API."""
    def fake_load_spreads(*, q=None, **kwargs):
        # The bot must never be more permissive than the site. It now passes
        # these explicitly -- same values as the site's defaults -- so that it
        # shares one warm cache entry with the alert worker instead of building
        # its own board on every lookup, which cost 26s and timed the webhook
        # out. What matters is the value, not whether the kwarg is present.
        assert kwargs.get('include_stale', False) is False, 'bot must not be laxer than the site'
        assert kwargs.get('include_unverified', False) is False, 'bot must not be laxer than the site'
        assert kwargs.get('require_deliverable', False) is True, 'bot must match the member board'
        assert 'max_age_min' not in kwargs, 'bot must not bypass freshness filters'
        # One warm payload carrying the whole board; the bot filters it itself
        # rather than asking for a per-token query it would have to build.
        return {"groups": [
            {"token": "SIREN", "routes": list(SIREN_ROUTES)},
            {"token": "GUA", "routes": [
                route("GUA", "FUTURES", "MEXC", "KuCoin", 0.74, -0.021, -7.6, 51_000)]},
        ]}

    monkeypatch.setattr(telegram_queries.api_spreads, "load_spreads", fake_load_spreads)
    path = tmp_path / "unused.jsonl"
    telegram_queries.reset_payload()
    telegram_queries.refresh_payload(path)
    request.addfinalizer(telegram_queries.reset_payload)
    return path


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
        ("@spreadarbitragesubscription_bot SIREN", "spread", "SIREN"),
        ("@spreadarbitragesubscription_bot funding SIREN", "funding", "SIREN"),
        ("@spreadarbitragesubscription_bot SIREN rails", "transfer", "SIREN"),
        ("$S", "spread", "S"),
        ("$4", "spread", "4"),
        ("$1INCH", "spread", "1INCH"),
        ("$1000000BABYDOGE", "spread", "1000000BABYDOGE"),
        ("$龙虾", "spread", "龙虾"),
        ("@spreadarbitragesubscription_bot 4", "spread", "4"),
        ("@spreadarbitragesubscription_bot 龙虾", "spread", "龙虾"),
        ("/token 4", "spread", "4"),
        ("/token 1000000BABYDOGE", "spread", "1000000BABYDOGE"),
        ("/token 龙虾", "spread", "龙虾"),
        (
            "@spreadarbitragesubscription_bot 1000000BABYDOGE",
            "spread",
            "1000000BABYDOGE",
        ),
    ],
)
def test_recognised_triggers(text, kind, symbol):
    query = telegram_queries.parse_query(
        text, bot_username="spreadarbitragesubscription_bot"
    )
    assert query is not None
    assert (query.kind, query.symbol) == (kind, symbol)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "GUA looks interesting",          # bare ticker in conversation
        "I made $500 today",              # dollar amount, not a cashtag
        "I paid $4 today",                # numeric token syntax only wins alone
        "good morning everyone",
        "SKY is the limit",
        "/spread",                        # command with no token
        "https://example.com/$FOO",       # no leading whitespace boundary
    ],
)
def test_ordinary_chat_is_not_a_query(text):
    assert telegram_queries.parse_query(text) is None


def test_radar_command_needs_no_token():
    assert telegram_queries.parse_query("/radar") == telegram_queries.Query(
        kind="radar", symbol=""
    )


@pytest.mark.parametrize(
    "raw",
    ["../../etc/passwd", "<script>alert(1)</script>", "SIREN'; DROP TABLE users;--", "A" * 50],
)
def test_symbol_is_sanitised(raw):
    """Whatever a member types becomes a bounded, inert token string."""
    query = telegram_queries.parse_query(f"/spread {raw}")
    assert query is not None
    assert len(query.symbol) <= telegram_queries.MAX_SYMBOL_LENGTH
    assert all(character.isalnum() or character in "._-" for character in query.symbol)


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def test_repeat_question_is_suppressed_within_cooldown():
    query = telegram_queries.Query(kind="spread", symbol="SIREN")
    assert telegram_queries.allow(GROUP_ID, query, now=1000.0) is True
    assert telegram_queries.allow(GROUP_ID, query, now=1005.0) is False


def test_repeat_group_question_gets_an_explanation_instead_of_silence(
    db, board_file
):
    accounts.configure_telegram_community(
        GROUP_ID, title="Subscribers", configured_by_telegram_user_id=1,
        invite_link="https://t.me/+abc", db_path=db,
    )
    telegram_queries.reset_cooldowns()
    first = telegram_bot.handle_update(
        message(GROUP_ID, "$SIREN"), db_path=db, board_path=board_file
    )
    second = telegram_bot.handle_update(
        message(GROUP_ID, "$SIREN"), db_path=db, board_path=board_file
    )
    assert first is not None
    assert second is not None and "less than a minute" in second["text"]


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
    assert "SIREN" in body and "3 routes" in body
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
    assert "query recognised" in body and "no parsed routes" in body


def test_unknown_token_links_to_explicit_audit_filter(board_file):
    body = telegram_queries.render(
        telegram_queries.Query("spread", "NOTATOKEN"),
        board_path=board_file,
        public_url="https://spreadarbitrage.ink",
    )
    assert "include_unverified=1" in body


def test_cooled_token_falls_back_to_the_historical_funding_radar(board_file, monkeypatch):
    monkeypatch.setattr(telegram_queries, "_rows_for", lambda *_args: [])
    monkeypatch.setattr(
        telegram_queries.funding_radar,
        "routes_for",
        lambda _symbol: [{
            "token": "GUA",
            "route_key": "GUA|Mexc|Spot|Aster|Futures",
            "long_venue": "Mexc",
            "short_venue": "Aster",
            "executable_spread_pct": -0.12,
            "radar_last_seen_age_min": 30,
            "radar_windows": {"1d": 1.1, "7d": 2.4, "30d": 5.9},
        }],
    )

    body = telegram_queries.render(
        telegram_queries.Query("spread", "GUA"),
        board_path=board_file,
        public_url="https://spreadarbitrage.ink",
    )

    assert "historical funding radar" in body
    assert "No client-visible route" in body
    assert "+1.10%" in body and "+2.40%" in body and "+5.90%" in body
    assert "last basis -0.12%" in body
    assert "not a current entry quote" in body


def test_radar_command_lists_retained_leaders(board_file, monkeypatch):
    monkeypatch.setattr(
        telegram_queries.funding_radar,
        "routes_for",
        lambda *_args, **_kwargs: [{
            "token": "GUA",
            "radar_windows": {"1d": 1.1, "7d": 2.4, "30d": 5.9},
        }],
    )

    body = telegram_queries.render(
        telegram_queries.Query("radar", ""),
        board_path=board_file,
        public_url="https://spreadarbitrage.ink",
    )

    assert "Funding radar" in body and "GUA" in body
    assert "+1.10%" in body and "+5.90%" in body
    assert "/funding?rank=1d" in body


def test_server_full_client_universe_atomically_replaces_bot_snapshot(monkeypatch):
    from spreadboard import server

    seen = []
    monkeypatch.setattr(telegram_queries, "replace_payload", lambda value: seen.append(value) or value)
    payload = {
        "filters": {
            "q": None,
            "funding_only": False,
            "include_stale": False,
            "include_unverified": False,
            "sort": "edge",
            "direction": "desc",
        },
        "pagination": {"offset": 0, "limit": 500},
        "groups": [{"token": "GUA", "routes": []}],
    }

    assert server._sync_telegram_client_universe(payload) is payload
    assert seen == [payload]


def test_server_does_not_replace_snapshot_from_a_filtered_page(monkeypatch):
    from spreadboard import server

    seen = []
    monkeypatch.setattr(telegram_queries, "replace_payload", lambda value: seen.append(value) or value)
    payload = {
        "filters": {"q": "GUA", "include_stale": False, "include_unverified": False},
        "pagination": {"offset": 0, "limit": 500},
        "groups": [{"token": "GUA", "routes": []}],
    }

    assert server._sync_telegram_client_universe(payload) is payload
    assert seen == []


def test_reply_carries_a_risk_note_and_site_link(board_file):
    body = telegram_queries.render(
        telegram_queries.Query("spread", "SIREN"),
        board_path=board_file, public_url="https://spreadarbitrage.ink",
    )
    assert "not advice" in body
    assert "https://spreadarbitrage.ink/markets?q=SIREN&amp;view=table" in body


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


def test_missing_snapshot_fails_fast_with_a_visible_warming_reply(db, monkeypatch):
    accounts.configure_telegram_community(
        GROUP_ID, title="Subscribers", configured_by_telegram_user_id=1,
        invite_link="https://t.me/+abc", db_path=db,
    )
    telegram_queries.reset_payload()
    monkeypatch.setattr(
        telegram_queries.api_spreads,
        "load_spreads",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("webhook rebuilt board")),
    )

    reply = telegram_bot.handle_update(
        message(GROUP_ID, "$SIREN"), db_path=db, board_path="board"
    )

    assert reply is not None and "warming" in reply["text"]


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


def test_tagging_the_bot_then_typing_a_token_is_answered(db, board_file, monkeypatch):
    monkeypatch.setenv(
        "SPREADBOARD_TELEGRAM_BOT_USERNAME", "spreadarbitragesubscription_bot"
    )
    accounts.configure_telegram_community(
        GROUP_ID, title="Subscribers", configured_by_telegram_user_id=1,
        invite_link="https://t.me/+abc", db_path=db,
    )

    reply = telegram_bot.handle_update(
        message(GROUP_ID, "@spreadarbitragesubscription_bot SIREN"),
        db_path=db,
        board_path=board_file,
    )

    assert reply is not None and "SIREN" in reply["text"]


def test_linked_active_member_can_query_in_private_chat(db, board_file):
    user = accounts.create_user(
        email="private-member@example.test",
        display_name="Private Member",
        password="a-secure-member-password",
        subscription_status="active",
        subscription_days=30,
        db_path=db,
    )
    token = accounts.create_telegram_link_token(user["id"], db_path=db)
    accounts.bind_telegram_chat(token, 4242, db_path=db)
    telegram_queries.reset_cooldowns()

    reply = telegram_bot.handle_update(
        message(4242, "/token SIREN", chat_type="private"),
        db_path=db,
        board_path=board_file,
    )

    assert reply is not None and reply["parse_mode"] == "HTML"
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


def test_unverified_routes_are_shown_but_marked(board_file):
    """Big spreads are real; hide nothing, but flag unconfirmed identity."""
    body = telegram_queries.render(
        telegram_queries.Query("spread", "SIREN"), board_path=board_file
    )
    assert "Ghost" in body, "an unverified route must still reach the member"
    assert "Ghost&gt;Phantom?" in body, "and must carry the ? identity marker"
    assert "identity unverified" in body
    assert "3 routes" in body


@pytest.mark.parametrize(
    "text,kind",
    [
        ("$Vanry /funding", "funding"),      # the exact message that failed
        ("$SIREN funding", "funding"),
        ("$SIREN /transfer", "transfer"),
        ("$SIREN rails", "transfer"),
        ("/funding $SIREN", "funding"),
        ("$SIREN", "spread"),
        ("$SIREN spread", "spread"),
        ("what is $siren funding like", "funding"),
    ],
)
def test_intent_word_wins_regardless_of_order(text, kind):
    query = telegram_queries.parse_query(text)
    assert query is not None
    assert query.symbol == "SIREN" or query.symbol == "VANRY"
    assert query.kind == kind


# --------------------------------------------------------------------------
# View buttons and inline suggestions
# --------------------------------------------------------------------------


def test_answer_offers_the_other_views_as_buttons(db, board_file):
    accounts.configure_telegram_community(
        GROUP_ID, title="Spread", configured_by_telegram_user_id=1,
        invite_link="https://t.me/+abc", db_path=db,
    )
    reply = telegram_bot.handle_update(
        message(GROUP_ID, "$SIREN"), db_path=db, board_path=board_file
    )
    rows = reply["reply_markup"]["inline_keyboard"]
    labels = [b["text"] for row in rows for b in row]
    assert "Funding" in labels
    assert "Deposits / Withdrawals" in labels
    assert "Spread" not in labels, "the current view should not be offered again"


def test_pressing_a_view_button_edits_in_place(db, board_file):
    accounts.configure_telegram_community(
        GROUP_ID, title="Spread", configured_by_telegram_user_id=1,
        invite_link="https://t.me/+abc", db_path=db,
    )
    update = {"callback_query": {
        "id": "1", "data": "v:funding:SIREN",
        "message": {"message_id": 55, "chat": {"id": GROUP_ID, "type": "supergroup"}},
    }}
    reply = telegram_bot.handle_update(update, db_path=db, board_path=board_file)
    assert reply["method"] == "editMessageText"
    assert reply["message_id"] == 55
    assert "funding" in reply["text"]
    assert "Spread" in [b["text"] for r in reply["reply_markup"]["inline_keyboard"] for b in r]


def test_callback_from_an_unregistered_chat_is_ignored(db, board_file):
    update = {"callback_query": {
        "id": "1", "data": "v:funding:SIREN",
        "message": {"message_id": 55, "chat": {"id": -999, "type": "supergroup"}},
    }}
    assert telegram_bot.handle_update(update, db_path=db, board_path=board_file) is None


def test_inline_query_suggests_tokens(db, board_file, monkeypatch):
    user = accounts.create_user(
        email="inline@example.test", display_name="Inline", password="a-secure-password",
        subscription_status="active", subscription_days=30, db_path=db,
    )
    token = accounts.create_telegram_link_token(user["id"], db_path=db)
    accounts.bind_telegram_chat(token, 42, db_path=db)
    monkeypatch.setattr(telegram_queries, "_warm_payload", lambda *_args: {"groups": [
        {"token": "SIREN", "best_edge_pct": 1.7, "route_count": 3, "venues": ["Bybit"]},
        {"token": "SILVER", "best_edge_pct": 2.9, "route_count": 2, "venues": ["Xt"]},
        {"token": "GUA", "best_edge_pct": 0.7, "route_count": 1, "venues": ["MEXC"]},
    ]})
    reply = telegram_bot.handle_update(
        {"inline_query": {"id": "q1", "from": {"id": 42}, "query": "SI"}},
        db_path=db, board_path=board_file,
    )
    assert reply["method"] == "answerInlineQuery"
    titles = [r["title"] for r in reply["results"]]
    assert titles == ["SILVER", "SIREN"], "prefix filtered, ranked by best edge"
    assert reply["results"][0]["input_message_content"]["message_text"] == "$SILVER"


def test_inline_query_with_empty_prefix_returns_top_tokens(db, board_file, monkeypatch):
    user = accounts.create_user(
        email="inline-empty@example.test", display_name="Inline", password="a-secure-password",
        subscription_status="active", subscription_days=30, db_path=db,
    )
    token = accounts.create_telegram_link_token(user["id"], db_path=db)
    accounts.bind_telegram_chat(token, 42, db_path=db)
    monkeypatch.setattr(
        telegram_queries, "_warm_payload",
        lambda *_args: {"groups": [{"token": "AAA", "best_edge_pct": 0.5, "route_count": 1}]},
    )
    reply = telegram_bot.handle_update(
        {"inline_query": {"id": "q1", "from": {"id": 42}, "query": ""}},
        db_path=db, board_path=board_file,
    )
    assert [r["title"] for r in reply["results"]] == ["AAA"]


def test_inline_query_is_empty_for_an_unlinked_user(db, board_file):
    reply = telegram_bot.handle_update(
        {"inline_query": {"id": "q1", "from": {"id": 999}, "query": "SIREN"}},
        db_path=db, board_path=board_file,
    )
    assert reply["results"] == []
    assert reply["is_personal"] is True


def test_a_bare_dollar_offers_tokens_to_tap(monkeypatch, tmp_path) -> None:
    """`$` on its own used to parse to nothing and the member got silence.

    Telegram gives a bot no autocomplete hook, so offering what is actually
    moving is the closest thing to the autocomplete they expected.
    """
    from spreadboard import telegram_queries as q

    monkeypatch.setattr(
        q.api_spreads, "load_spreads",
        lambda **kwargs: {"groups": [
            {"token": "SIREN", "best_edge_pct": 12.0},
            {"token": "GUA", "best_edge_pct": 8.0},
            {"token": "SOL", "best_edge_pct": 1.0},
        ]},
    )
    q.reset_payload()
    q.refresh_payload(tmp_path / "board.json")

    picks = q.suggestions("", board_path=tmp_path / "board.json")

    assert picks == ["SIREN", "GUA", "SOL"]
    kb = q.suggestion_keyboard(picks)
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert labels == ["SIREN", "GUA", "SOL"]
    assert all(b["callback_data"].startswith("t:") for row in kb["inline_keyboard"] for b in row)


def test_a_prefix_puts_matches_first(monkeypatch, tmp_path) -> None:
    from spreadboard import telegram_queries as q

    monkeypatch.setattr(
        q.api_spreads, "load_spreads",
        lambda **kwargs: {"groups": [
            {"token": "RESOLV", "best_edge_pct": 30.0},
            {"token": "SOL", "best_edge_pct": 2.0},
            {"token": "SOSO", "best_edge_pct": 1.0},
        ]},
    )
    q.reset_payload()
    q.refresh_payload(tmp_path / "board.json")

    picks = q.suggestions("so", board_path=tmp_path / "board.json")

    # Starts-with wins over a bigger edge that merely contains it.
    assert picks[0] in {"SOL", "SOSO"}
    assert "RESOLV" in picks


def test_a_named_token_is_still_a_lookup_not_a_suggestion() -> None:
    """Matching any short cashtag turned every token query into a list."""
    import inspect

    from spreadboard import telegram_bot

    source = inspect.getsource(telegram_bot._handle_group_query)
    assert 'text.strip() == "$"' in source


def test_tapping_a_suggested_token_renders_it() -> None:
    import inspect

    from spreadboard import telegram_bot

    source = inspect.getsource(telegram_bot._handle_callback)
    assert 'parts[0] == "t"' in source
    assert 'kind="spread"' in source
