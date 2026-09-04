from __future__ import annotations

import threading
import time
import urllib.parse

import pytest
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


def test_market_alert_delivery_receipt_is_not_returned_to_member_views(tmp_path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="receipt-privacy@example.test",
        display_name="Receipt Privacy",
        password="strong-receipt-password",
        subscription_status="active",
        db_path=db_path,
    )
    rule = accounts.add_market_alert_rule(
        user["id"],
        {
            "route_key": "OPENAI|CUSTOM|Mexc|Futures|IO-OAI_USDT|Hyperliquid|Futures|io:OAI",
            "symbol": "OPENAI",
            "type": "close_spread",
            "direction": "below",
            "threshold": 0.5,
            "delivery_priority": 2,
            "delivery_sound": "siren",
        },
        db_path=db_path,
    )
    connection = accounts._connect(db_path)
    try:
        connection.execute(
            "UPDATE market_alert_rules SET delivery_receipt = ? WHERE id = ?",
            ("opaque-provider-receipt", rule["id"]),
        )
        connection.commit()
    finally:
        connection.close()

    public = accounts.list_market_alert_rules(user["id"], db_path=db_path)[0]
    internal = accounts.list_market_alert_rules(
        user["id"], include_delivery_state=True, db_path=db_path
    )[0]

    assert "delivery_receipt" not in public
    assert "delivery_receipt_checked_at" not in public
    assert "delivery_acknowledged_at" not in public
    assert internal["delivery_receipt"] == "opaque-provider-receipt"


def test_normal_route_alert_fires_once_and_disables_itself(tmp_path, monkeypatch) -> None:
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
    saved = accounts.list_market_alert_rules(user["id"], db_path=db_path)[0]
    assert saved["enabled"] == 0
    assert saved["last_triggered_at"]
    assert worker.check_once()["triggered"] == 0
    row["open_spread_pct"] = 4.0
    worker.check_once()
    row["open_spread_pct"] = 6.0
    assert worker.check_once()["triggered"] == 0
    assert len(sent) == 1
    assert sent[0]["priority"] == 0


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
    assert sent[0]["priority"] == 0
    assert sent[0]["sound"] == "pushover"


def test_emergency_pushover_payload_repeats_until_acknowledged(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status":1,"receipt":"receipt-id"}'

    def fake_urlopen(request, *, timeout):
        captured.update(urllib.parse.parse_qs(request.data.decode("utf-8")))
        assert timeout == 10.0
        return Response()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    result = alerts.send_pushover_message(
        app_token="app",
        user_key="user",
        title="Urgent",
        message="Threshold crossed",
        sound="siren",
        priority=2,
        retry=10,
        expire=20000,
    )

    assert result["ok"] is True
    assert captured["priority"] == ["2"]
    assert captured["retry"] == ["30"]
    assert captured["expire"] == ["10800"]
    assert captured["sound"] == ["siren"]


def test_unresolved_custom_alert_quotes_run_in_parallel(tmp_path, monkeypatch) -> None:
    routes = {
        "CUSTOM:ONE": {"token": "ONE", "route_kind": "FUTURES"},
        "CUSTOM:TWO": {"token": "TWO", "route_kind": "FUTURES"},
    }
    barrier = threading.Barrier(2)
    monkeypatch.setattr(chart_catalog, "route_from_key", lambda key: routes.get(key))

    def quote(route):
        barrier.wait(timeout=1.0)
        return {**route, "displayed_open_spread_pct": 1.0, "spread_quote_current": True}

    monkeypatch.setattr(alerts, "_quote_custom_alert_route", quote)
    worker = alerts.UserMarketAlertWorker(
        board_path=tmp_path / "board.json",
        accounts_path=tmp_path / "accounts.sqlite3",
        poll_seconds=2,
    )

    rows = worker._custom_alert_rows(set(routes), [])

    assert set(rows) == set(routes)


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


def test_exact_route_exit_and_leg_price_alert_values_use_live_crossing_books() -> None:
    row = {
        "spread_quote_current": True,
        "notes": {
            "route_inputs": {
                "long": {"bid": 100.0, "ask": 101.0},
                "short": {"bid": 105.0, "ask": 106.0},
            },
            "relative_value": {"long_multiplier": 1, "short_multiplier": 1},
        },
    }

    assert alerts._rule_value(row, "close_spread_pct") == pytest.approx(6.0)
    assert alerts._rule_value(row, "long_leg_price") == 100.5
    assert alerts._rule_value(row, "short_leg_price") == 105.5


def test_openai_emergency_rule_keeps_its_per_rule_delivery_override(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="openai-alert@example.test",
        display_name="OpenAI Alert",
        password="strong-openai-password",
        subscription_status="active",
        db_path=db_path,
    )
    route_key = chart_catalog.custom_route_key(
        "OPENAI",
        {"venue": "Mexc", "market_type": "Futures", "symbol": "OPENAI/USDT:USDT"},
        {
            "venue": "Hyperliquid",
            "market_type": "Futures",
            "symbol": "IO-OAI/USDC:USDC",
        },
    )
    rule = accounts.add_market_alert_rule(
        user["id"],
        {
            "route_key": route_key,
            "symbol": "OPENAI",
            "type": "close_spread",
            "direction": "below",
            "threshold": 0.5,
            "delivery_priority": 2,
            "delivery_sound": "siren",
            "delivery_retry_seconds": 216,
            "delivery_expire_seconds": 10_800,
        },
        db_path=db_path,
    )

    assert rule["metric"] == "close_spread_pct"
    assert rule["delivery_priority"] == 2
    assert rule["delivery_sound"] == "siren"
    assert rule["delivery_retry_seconds"] == 216
    assert rule["delivery_expire_seconds"] == 10_800


def test_expired_unacknowledged_emergency_receipt_rearms_the_rule(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="repeat-alert@example.test",
        display_name="Repeat Alert",
        password="strong-repeat-password",
        subscription_status="active",
        db_path=db_path,
    )
    rule = accounts.add_market_alert_rule(
        user["id"],
        {
            "route_key": "OPENAI|Mexc|Futures|Hyperliquid|Futures",
            "symbol": "OPENAI",
            "type": "close_spread",
            "direction": "below",
            "threshold": 0.5,
            "stability_seconds": 0,
            "delivery_priority": 2,
        },
        db_path=db_path,
    )
    assert accounts.record_market_alert_evaluation(
        user["id"], rule["id"], value=0.4, title="t", body="b", db_path=db_path
    )
    active = accounts.list_market_alert_rules(
        user["id"], include_delivery_state=True, db_path=db_path
    )[0]
    assert active["enabled"] == 1
    accounts.record_market_alert_delivery_receipt(
        user["id"], rule["id"], "receipt-id", db_path=db_path
    )
    rearmed = accounts.record_market_alert_receipt_status(
        user["id"],
        rule["id"],
        acknowledged=False,
        expired=True,
        db_path=db_path,
    )

    assert rearmed["delivery_receipt"] is None
    assert rearmed["last_condition_met"] == 0
    assert accounts.record_market_alert_evaluation(
        user["id"], rule["id"], value=0.4, title="t", body="b", db_path=db_path
    )


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
