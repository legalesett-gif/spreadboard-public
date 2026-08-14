from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from spreadboard import accounts


def _database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "accounts.sqlite3"
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SPREADBOARD_ADMIN_PASSWORD", raising=False)
    accounts.initialize(path)
    return path


def test_initialize_migrates_existing_positions_with_lifecycle_cost_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    cost_columns = {
        "borrow_costs_usd",
        "gas_costs_usd",
        "transfer_costs_usd",
        "slippage_costs_usd",
    }
    with sqlite3.connect(path) as connection:
        for column in cost_columns:
            connection.execute(f"ALTER TABLE positions DROP COLUMN {column}")

    accounts.initialize(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(positions)")}
    assert cost_columns <= columns


def test_password_hash_is_salted_and_verifiable() -> None:
    first = accounts.hash_password("correct horse battery staple")
    second = accounts.hash_password("correct horse battery staple")
    assert first != second
    assert accounts.verify_password("correct horse battery staple", first)
    assert not accounts.verify_password("wrong password value", first)


def test_login_uses_opaque_session_and_subscription_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    user = accounts.create_user(
        email="member@example.com",
        display_name="Member",
        password="member-password-strong",
        subscription_status="trialing",
        subscription_days=30,
        db_path=path,
    )
    signed_in, token = accounts.login("member@example.com", "member-password-strong", db_path=path)
    assert signed_in.id == user["id"]
    assert token and "member@example.com" not in token
    assert accounts.user_for_session(token, path).subscription_active

    expired = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
    accounts.update_subscription(user["id"], status="active", expires_at=expired, db_path=path)
    assert not accounts.user_for_session(token, path).subscription_active
    accounts.logout(token, path)
    assert accounts.user_for_session(token, path) is None


def test_page_analytics_store_only_aggregate_path_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    when = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    accounts.record_page_view("/pricing", at=when, db_path=path)
    accounts.record_page_view("/pricing", at=when, db_path=path)
    accounts.record_page_view("/free", at=when, db_path=path)

    summary = accounts.page_view_summary(days=365, db_path=path)
    assert summary["privacy"] == "aggregate_path_counts_only"
    assert summary["total_views"] == 3
    assert summary["paths"][0] == {"path": "/pricing", "views": 2}

    import sqlite3

    connection = sqlite3.connect(path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(daily_page_views)")}
    finally:
        connection.close()
    assert columns == {"day", "path", "view_count"}


def test_position_funding_and_alert_records_are_user_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    first = accounts.create_user(
        email="first@example.com",
        display_name="First",
        password="first-password-strong",
        subscription_status="active",
        db_path=path,
    )
    second = accounts.create_user(
        email="second@example.com",
        display_name="Second",
        password="second-password-strong",
        subscription_status="active",
        db_path=path,
    )
    position = accounts.create_position(
        first["id"],
        {
            "token": "COTI",
            "long_venue": "Gate",
            "long_market_type": "Spot",
            "long_symbol": "COTI/USDT",
            "long_quantity": 1000,
            "long_entry_price": 0.04,
            "short_venue": "Bybit",
            "short_market_type": "Futures",
            "short_symbol": "COTI/USDT:USDT",
            "short_quantity": 1000,
            "short_entry_price": 0.05,
            "capital_usd": 100,
        },
        db_path=path,
    )
    accounts.add_funding_cashflow(
        first["id"], position["id"], {"venue": "Bybit", "amount_usd": 1.25}, db_path=path
    )
    accounts.add_alert_rule(
        first["id"],
        position["id"],
        {"metric": "exit_spread_pct", "operator": "gte", "threshold": -1},
        db_path=path,
    )
    hydrated = accounts.list_positions(first["id"], db_path=path)[0]
    assert hydrated["funding_cashflows"][0]["amount_usd"] == 1.25
    assert hydrated["alert_rules"][0]["threshold"] == -1
    assert accounts.list_positions(second["id"], db_path=path) == []
    with pytest.raises(ValueError, match="position_not_found"):
        accounts.add_alert_rule(
            second["id"],
            position["id"],
            {"metric": "pnl_usd", "operator": "gte", "threshold": 10},
            db_path=path,
        )


def test_telegram_link_is_one_time_and_chat_cannot_be_reassigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    first = accounts.create_user(
        email="telegram-first@example.com",
        display_name="First",
        password="first-secure-password",
        subscription_status="active",
        db_path=path,
    )
    second = accounts.create_user(
        email="telegram-second@example.com",
        display_name="Second",
        password="second-secure-password",
        subscription_status="active",
        db_path=path,
    )
    token = accounts.create_telegram_link_token(first["id"], db_path=path)
    linked = accounts.bind_telegram_chat(token, 12345, db_path=path)
    assert linked.id == first["id"]
    assert accounts.telegram_link_status(first["id"], db_path=path)["linked"] is True
    with pytest.raises(ValueError, match="invalid_or_expired_telegram_link"):
        accounts.bind_telegram_chat(token, 12345, db_path=path)
    second_token = accounts.create_telegram_link_token(second["id"], db_path=path)
    with pytest.raises(ValueError, match="telegram_chat_already_linked"):
        accounts.bind_telegram_chat(second_token, 12345, db_path=path)


def test_position_corrections_are_owner_scoped_and_recompute_route(tmp_path, monkeypatch) -> None:
    path = _database(tmp_path, monkeypatch)
    owner = accounts.create_user(
        email="position-owner@example.test",
        display_name="Owner",
        password="position-owner-password",
        subscription_status="active",
        db_path=path,
    )
    other = accounts.create_user(
        email="position-other@example.test",
        display_name="Other",
        password="position-other-password",
        subscription_status="active",
        db_path=path,
    )
    created = accounts.create_position(
        owner["id"],
        {
            "token": "BTW",
            "long_venue": "Mexc",
            "long_market_type": "Spot",
            "long_symbol": "BTW/USDC",
            "long_quantity": 13530,
            "long_entry_price": 0.2026227273,
            "short_venue": "Aster",
            "short_market_type": "Futures",
            "short_symbol": "BTW/USDT:USDT",
            "short_quantity": 13530,
            "short_entry_price": 0.2017316364,
        },
        db_path=path,
    )
    payload = {
        "token": "BTW",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_symbol": "BTW/USDT",
        "long_quantity": 13530,
        "long_entry_price": 0.2017316364,
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "short_symbol": "BTW/USDT:USDT",
        "short_quantity": 13530,
        "short_entry_price": 0.2026227273,
        "capital_usd": 2750,
        "entry_fees_usd": 1.25,
        "borrow_costs_usd": 2.5,
        "gas_costs_usd": 3.75,
        "transfer_costs_usd": 4.0,
        "slippage_costs_usd": 5.25,
        "opened_at": "2026-08-11T23:19:58Z",
        "notes": "Corrected from venue fills",
    }
    corrected = accounts.update_position(owner["id"], created["id"], payload, db_path=path)
    assert corrected["long_symbol"] == "BTW/USDT"
    assert corrected["long_entry_price"] == 0.2017316364
    assert corrected["short_entry_price"] == 0.2026227273
    assert corrected["route_key"] == "BTW|Mexc|Spot|Aster|Futures"
    assert corrected["entry_spread_pct"] > 0
    assert corrected["opened_at"] == "2026-08-11T23:19:58Z"
    assert corrected["entry_fees_usd"] == 1.25
    assert corrected["borrow_costs_usd"] == 2.5
    assert corrected["gas_costs_usd"] == 3.75
    assert corrected["transfer_costs_usd"] == 4.0
    assert corrected["slippage_costs_usd"] == 5.25

    closed = accounts.close_position(
        owner["id"],
        created["id"],
        {
            "long_exit_price": 0.23,
            "short_exit_price": 0.231,
            "exit_fees_usd": 1.5,
            "closed_at": "2026-08-12T01:00:00Z",
        },
        db_path=path,
    )
    corrected_closed = accounts.update_position(
        owner["id"],
        created["id"],
        {
            **payload,
            "status": "closed",
            "closed_at": closed["closed_at"],
            "long_exit_price": 0.229,
            "short_exit_price": 0.2305,
            "exit_fees_usd": 1.25,
        },
        db_path=path,
    )
    assert corrected_closed["status"] == "closed"
    assert corrected_closed["long_exit_price"] == 0.229
    assert corrected_closed["short_exit_price"] == 0.2305
    assert corrected_closed["exit_fees_usd"] == 1.25

    reopened = accounts.update_position(
        owner["id"],
        created["id"],
        {**payload, "status": "open"},
        db_path=path,
    )
    assert reopened["status"] == "open"
    assert reopened["closed_at"] is None
    assert reopened["long_exit_price"] is None
    assert reopened["short_exit_price"] is None
    assert reopened["exit_fees_usd"] == 0
    with pytest.raises(ValueError, match="position_not_found"):
        accounts.update_position(other["id"], created["id"], payload, db_path=path)


def _alert_user(tmp_path, monkeypatch):
    path = _database(tmp_path, monkeypatch)
    user = accounts.create_user(
        email="a@b.c",
        display_name="A",
        password="member-password-strong",
        subscription_status="active",
        subscription_days=30,
        db_path=path,
    )
    return path, user


def test_a_member_can_edit_and_delete_their_own_alert(tmp_path, monkeypatch) -> None:
    """Members create alerts against a route and must be able to change the
    threshold, the stability window, turn one off, or remove it."""
    db, user = _alert_user(tmp_path, monkeypatch)
    rule = accounts.add_market_alert_rule(
        user["id"] if isinstance(user, dict) else user.id,
        {
            "route_key": "SIREN|Kucoin|Spot|Gate|Futures",
            "symbol": "SIREN",
            "type": "token_spread",
            "direction": "above",
            "threshold": 32.0,
            "stability_seconds": 10,
            "enabled": True,
        },
        db_path=db,
    )
    uid = user["id"] if isinstance(user, dict) else user.id

    updated = accounts.update_market_alert_rule(
        uid, rule["id"], {"threshold": 45.0, "stability_seconds": 21, "enabled": False}, db_path=db
    )
    assert updated["threshold"] == 45.0
    assert updated["stability_seconds"] == 21
    assert updated["enabled"] == 0

    assert accounts.delete_market_alert_rule(uid, rule["id"], db_path=db) is True
    assert accounts.get_market_alert_rule(uid, rule["id"], db_path=db) is None


def test_editing_an_alert_rearms_it(tmp_path, monkeypatch) -> None:
    """A rule edited while its condition was already met would otherwise stay
    silent until it lapsed and re-armed itself."""
    db, user = _alert_user(tmp_path, monkeypatch)
    uid = user["id"] if isinstance(user, dict) else user.id
    rule = accounts.add_market_alert_rule(
        uid,
        {
            "route_key": "X|A|Spot|B|Futures",
            "symbol": "X",
            "type": "token_spread",
            "direction": "above",
            "threshold": 5.0,
            "stability_seconds": 0,
            "enabled": True,
        },
        db_path=db,
    )
    accounts.record_market_alert_evaluation(
        uid, rule["id"], value=9.0, title="t", body="b", db_path=db
    )
    assert accounts.get_market_alert_rule(uid, rule["id"], db_path=db)["last_condition_met"] == 1
    updated = accounts.update_market_alert_rule(uid, rule["id"], {"threshold": 6.0}, db_path=db)
    assert updated["last_condition_met"] == 0 and updated["condition_since"] is None


def test_a_member_cannot_touch_someone_elses_alert(tmp_path, monkeypatch) -> None:
    db, user = _alert_user(tmp_path, monkeypatch)
    uid = user["id"] if isinstance(user, dict) else user.id
    rule = accounts.add_market_alert_rule(
        uid,
        {
            "route_key": "X|A|Spot|B|Futures",
            "symbol": "X",
            "type": "token_spread",
            "direction": "above",
            "threshold": 5.0,
            "stability_seconds": 0,
            "enabled": True,
        },
        db_path=db,
    )
    other = accounts.create_user(
        email="c@d.e",
        display_name="C",
        password="member-password-strong",
        subscription_status="active",
        subscription_days=30,
        db_path=db,
    )
    other_id = other["id"] if isinstance(other, dict) else other.id
    assert (
        accounts.update_market_alert_rule(other_id, rule["id"], {"threshold": 1.0}, db_path=db)
        is None
    )
    assert accounts.delete_market_alert_rule(other_id, rule["id"], db_path=db) is False


def test_filter_presets_and_watchlist_are_account_scoped(tmp_path, monkeypatch) -> None:
    db = _database(tmp_path, monkeypatch)
    first = accounts.create_user(
        email="preset-first@example.com",
        display_name="First",
        password="first-secure-password",
        subscription_status="active",
        db_path=db,
    )
    second = accounts.create_user(
        email="preset-second@example.com",
        display_name="Second",
        password="second-secure-password",
        subscription_status="active",
        db_path=db,
    )
    preset = accounts.save_filter_preset(
        first["id"],
        {
            "name": "Persistent DEX farms",
            "query": {"kind": "DEX-FUTURES", "min_spread_pct": "0.5", "funding_only": "1"},
        },
        db_path=db,
    )
    assert preset["name"] == "Persistent DEX farms"
    assert preset["query"]["kind"] == "DEX-FUTURES"
    assert accounts.list_filter_presets(second["id"], db_path=db) == []

    tokens = accounts.replace_watchlist(first["id"], ["siren", "ESPORTS", "SIREN"], db_path=db)
    assert tokens == ["SIREN", "ESPORTS"]
    assert accounts.list_watchlist(first["id"], db_path=db) == ["SIREN", "ESPORTS"]
    assert accounts.list_watchlist(second["id"], db_path=db) == []

    assert accounts.delete_filter_preset(second["id"], preset["id"], db_path=db) is False
    assert accounts.delete_filter_preset(first["id"], preset["id"], db_path=db) is True


def test_filter_presets_reject_unknown_query_fields(tmp_path, monkeypatch) -> None:
    db = _database(tmp_path, monkeypatch)
    user = accounts.create_user(
        email="preset-validation@example.com",
        display_name="Member",
        password="member-secure-password",
        subscription_status="active",
        db_path=db,
    )
    with pytest.raises(ValueError, match="invalid_filter_field"):
        accounts.save_filter_preset(
            user["id"], {"name": "Unsafe", "query": {"redirect": "https://example.com"}}, db_path=db
        )
