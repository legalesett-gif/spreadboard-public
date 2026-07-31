from __future__ import annotations

from cryptography.fernet import Fernet

from spreadboard import accounts, alerts


def test_pushover_key_is_encrypted_and_never_returned(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="alerts@example.test", display_name="Alerts",
        password="strong-alert-password", subscription_status="active", db_path=db_path,
    )
    key = "u" * 30
    public = accounts.save_notification_preferences(
        user["id"],
        {"pushover_user_key": key, "pushover_enabled": True, "pushover_sound": "siren"},
        db_path=db_path,
    )
    assert public["pushover_configured"] is True
    assert key not in str(public)
    connection = accounts._connect(db_path)
    try:
        encrypted = connection.execute(
            "SELECT pushover_user_key_encrypted FROM notification_preferences WHERE user_id = ?",
            (user["id"],),
        ).fetchone()[0]
    finally:
        connection.close()
    assert encrypted != key
    assert accounts.notification_delivery(user["id"], db_path=db_path)["user_key"] == key


def test_route_alert_sends_once_then_rearms(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "app-token")
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="route-alert@example.test", display_name="Route Alert",
        password="strong-route-password", subscription_status="active", db_path=db_path,
    )
    accounts.save_notification_preferences(
        user["id"],
        {"pushover_user_key": "k" * 30, "pushover_enabled": True},
        db_path=db_path,
    )
    accounts.add_market_alert_rule(
        user["id"],
        {
            "route_key": "COTI|FUTURES|Gate|Futures|Bybit|Futures",
            "symbol": "COTI",
            "type": "token_spread",
            "direction": "above",
            "threshold": 5,
            "stability_seconds": 0,
        },
        db_path=db_path,
    )
    row = {
        "route_key": "COTI|FUTURES|Gate|Futures|Bybit|Futures",
        "symbol": "COTI", "long_venue": "Gate", "short_venue": "Bybit",
        "open_spread_pct": 6.0,
    }
    monkeypatch.setattr(alerts.api_spreads, "load_spreads", lambda **kwargs: {"rows": [row]})
    sent = []
    monkeypatch.setattr(
        alerts,
        "send_pushover_message",
        lambda **kwargs: sent.append(kwargs) or {"ok": True, "status": 200},
    )
    worker = alerts.UserMarketAlertWorker(
        board_path=tmp_path / "board.json", accounts_path=db_path, poll_seconds=5
    )
    assert worker.check_once()["triggered"] == 1
    assert worker.check_once()["triggered"] == 0
    row["open_spread_pct"] = 4.0
    worker.check_once()
    row["open_spread_pct"] = 6.0
    assert worker.check_once()["triggered"] == 1
    assert len(sent) == 2
