from __future__ import annotations

import time
import urllib.parse

from cryptography.fernet import Fernet

from spreadboard import accounts, alerts, chart_catalog, warm_query_projection


def test_pushover_key_is_encrypted_and_never_returned(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="alerts@example.test",
        display_name="Alerts",
        password="strong-alert-password",
        subscription_status="active",
        db_path=db_path,
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
        email="route-alert@example.test",
        display_name="Route Alert",
        password="strong-route-password",
        subscription_status="active",
        db_path=db_path,
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
        "symbol": "COTI",
        "long_venue": "Gate",
        "short_venue": "Bybit",
        "open_spread_pct": 6.0,
        "age_min": 0.1,
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


def test_route_alert_uses_resident_live_universe_without_board_rebuild(
    tmp_path, monkeypatch
) -> None:
    """A healthy production alert poll must never parse the full discovery board."""

    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "app-token")
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="resident-alert@example.test",
        display_name="Resident Alert",
        password="strong-resident-password",
        subscription_status="active",
        db_path=db_path,
    )
    accounts.save_notification_preferences(
        user["id"],
        {"pushover_user_key": "k" * 30, "pushover_enabled": True},
        db_path=db_path,
    )
    route_key = "COTI|FUTURES|Gate|Futures|Bybit|Futures"
    accounts.add_market_alert_rule(
        user["id"],
        {
            "route_key": route_key,
            "symbol": "COTI",
            "type": "token_spread",
            "direction": "above",
            "threshold": 5,
            "stability_seconds": 0,
        },
        db_path=db_path,
    )
    structural = {
        "route_key": route_key,
        "token": "COTI",
        "symbol": "COTI",
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "displayed_open_spread_pct": 6.0,
        "deliverable": True,
    }
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({route_key: structural}, template={"ok": True})
    monkeypatch.setattr(
        warm_query_projection.api_spreads,
        "live_route_updates_for",
        lambda *_args, **_kwargs: {
            route_key: (6.0, 0.1, int(time.time() * 1_000_000), "matched_vwap")
        },
    )
    universe.refresh()
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)
    monkeypatch.setattr(
        alerts.api_spreads,
        "load_spreads",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full board rebuild")),
    )
    sent = []
    monkeypatch.setattr(
        alerts,
        "send_pushover_message",
        lambda **kwargs: sent.append(kwargs) or {"ok": True, "status": 200},
    )

    result = alerts.UserMarketAlertWorker(
        board_path=tmp_path / "board.json",
        accounts_path=db_path,
        poll_seconds=5,
    ).check_once()

    assert result == {"evaluated": 1, "triggered": 1, "delivered": 1}
    assert len(sent) == 1


def test_custom_chart_alert_resolves_to_matching_live_board_route(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://example.test")
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="custom-alert@example.test",
        display_name="Custom Alert",
        password="strong-custom-password",
        subscription_status="active",
        db_path=db_path,
    )
    accounts.save_notification_preferences(
        user["id"],
        {"pushover_user_key": "k" * 30, "pushover_enabled": True},
        db_path=db_path,
    )
    route_key = chart_catalog.custom_route_key(
        "X",
        {"venue": "Gate", "market_type": "Spot", "symbol": "X/USDT"},
        {"venue": "Bybit", "market_type": "Futures", "symbol": "X/USDT:USDT"},
    )
    accounts.add_market_alert_rule(
        user["id"],
        {
            "route_key": route_key,
            "symbol": "X",
            "type": "token_spread",
            "direction": "above",
            "threshold": 5,
            "stability_seconds": 0,
        },
        db_path=db_path,
    )
    board_row = {
        "route_key": "X|SPOT-FUTURES|Gate|Spot|Bybit|Futures",
        "token": "X",
        "symbol": "X",
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "long_market_symbol": "X/USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "X/USDT:USDT",
        "displayed_open_spread_pct": 6.0,
        "spread_quote_current": True,
    }
    monkeypatch.setattr(alerts.api_spreads, "load_spreads", lambda **kwargs: {"rows": [board_row]})
    sent = []
    monkeypatch.setattr(
        alerts,
        "send_pushover_message",
        lambda **kwargs: sent.append(kwargs) or {"ok": True, "status": 200},
    )
    result = alerts.UserMarketAlertWorker(
        board_path=tmp_path / "board.json",
        accounts_path=db_path,
        poll_seconds=5,
    ).check_once()
    assert result == {"evaluated": 1, "triggered": 1, "delivered": 1}
    assert len(sent) == 1
    assert sent[0]["url"].endswith(urllib.parse.quote(route_key, safe=""))


def test_funding_alert_accepts_current_projection_when_settled_is_missing() -> None:
    assert alerts._rule_value({"funding_projected_24h_pct": 0.42}, "funding_24h_pct") == 0.42


def test_custom_chart_alert_quotes_noncanonical_dex_route(tmp_path, monkeypatch) -> None:
    route_key = chart_catalog.custom_route_key(
        "GUA",
        {
            "venue": "OKX DEX 56",
            "market_type": "Spot",
            "symbol": "GUA",
            "dex_chain": "56",
            "dex_contract": "0xgua",
        },
        {"venue": "Bybit", "market_type": "Futures", "symbol": "GUA/USDT:USDT"},
    )
    quoted = {
        "token": "GUA",
        "displayed_open_spread_pct": 4.2,
        "funding_projected_24h_pct": 0.8,
        "spread_quote_current": True,
    }
    seen = []
    monkeypatch.setattr(
        chart_catalog,
        "dex_market_entries",
        lambda: [
            {
                "token": "GUA",
                "venue": "OKX DEX 56",
                "market_type": "Spot",
                "symbol": "GUA",
                "dex_chain": "56",
                "dex_contract": "0xgua",
            }
        ],
    )
    monkeypatch.setattr(
        alerts,
        "_quote_custom_alert_route",
        lambda route: seen.append(route) or quoted,
    )
    worker = alerts.UserMarketAlertWorker(
        board_path=tmp_path / "board.json",
        accounts_path=tmp_path / "accounts.sqlite3",
        poll_seconds=5,
    )

    rows = worker._custom_alert_rows({route_key}, [])

    assert rows[route_key]["route_key"] == route_key
    assert rows[route_key]["displayed_open_spread_pct"] == 4.2
    assert seen[0]["route_kind"] == "DEX-FUTURES"


def test_cooled_standard_chart_alert_gets_exact_quote(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="cooled-alert@example.test",
        display_name="Cooled Alert",
        password="strong-cooled-password",
        subscription_status="active",
        db_path=db_path,
    )
    route_key = "GUA|Mexc|Spot|Aster|Futures"
    accounts.add_market_alert_rule(
        user["id"],
        {
            "route_key": route_key,
            "symbol": "GUA",
            "type": "token_spread",
            "direction": "above",
            "threshold": 999999,
            "stability_seconds": 0,
        },
        db_path=db_path,
    )
    structural = {
        "route_key": route_key,
        "token": "GUA",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_market_symbol": "GUA/USDT",
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
    }

    def load_spreads(**kwargs):
        return {"rows": [structural] if kwargs.get("include_unverified") else []}

    monkeypatch.setattr(alerts.api_spreads, "load_spreads", load_spreads)
    monkeypatch.setattr(
        alerts,
        "_quote_custom_alert_route",
        lambda route: {
            **route,
            "displayed_open_spread_pct": 1.25,
            "spread_quote_current": True,
        },
    )

    result = alerts.UserMarketAlertWorker(
        board_path=tmp_path / "board.json",
        accounts_path=db_path,
        poll_seconds=5,
    ).check_once()

    assert result == {"evaluated": 1, "triggered": 0, "delivered": 0}
    rule = accounts.list_market_alert_rules(user["id"], db_path=db_path)[0]
    assert rule["last_value"] == 1.25
