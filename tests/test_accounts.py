from __future__ import annotations

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
    signed_in, token = accounts.login(
        "member@example.com", "member-password-strong", db_path=path
    )
    assert signed_in.id == user["id"]
    assert token and "member@example.com" not in token
    assert accounts.user_for_session(token, path).subscription_active

    expired = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
    accounts.update_subscription(user["id"], status="active", expires_at=expired, db_path=path)
    assert not accounts.user_for_session(token, path).subscription_active
    accounts.logout(token, path)
    assert accounts.user_for_session(token, path) is None


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
        email="telegram-first@example.com", display_name="First",
        password="first-secure-password", subscription_status="active", db_path=path,
    )
    second = accounts.create_user(
        email="telegram-second@example.com", display_name="Second",
        password="second-secure-password", subscription_status="active", db_path=path,
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
