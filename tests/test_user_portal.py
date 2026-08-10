from __future__ import annotations

import http.client
import json
import sqlite3
import threading

import pytest

from spreadboard import accounts, billing, portfolio
from spreadboard.server import TERMS_VERSION, SpreadBoardHandler, SpreadBoardServer


def test_authenticated_http_boundary_and_csrf(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("SPREADBOARD_ADMIN_PASSWORD", "correct-horse-battery-staple")
    server = SpreadBoardServer(
        ("127.0.0.1", 0),
        SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=db_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request("GET", "/api/board")
        response = connection.getresponse()
        assert response.status == 401
        response.read()

        connection.request("POST", "/api/billing/webhook", body="{}", headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "billing_webhook_not_configured"

        monkeypatch.setenv("SPREADBOARD_TELEGRAM_BOT_TOKEN", "123:test-token")
        monkeypatch.setenv("SPREADBOARD_TELEGRAM_BOT_USERNAME", "spreadboard_test_bot")
        monkeypatch.setenv("SPREADBOARD_TELEGRAM_WEBHOOK_SECRET", "webhook-secret")
        telegram_update = json.dumps(
            {"update_id": 1, "message": {"chat": {"id": 99, "type": "private"}, "text": "/mysubscription"}}
        )
        connection.request(
            "POST", "/api/telegram/webhook", body=telegram_update,
            headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": "forged"},
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "invalid_telegram_webhook_secret"

        connection.request(
            "POST", "/api/telegram/webhook", body=telegram_update,
            headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert "Link this chat" in json.loads(response.read())["text"]

        connection.request(
            "POST", "/api/register",
            body=json.dumps({"display_name": "New Member", "email": "new@example.test", "password": "new-member-password"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 201
        member_cookie = response.getheader("Set-Cookie")
        assert json.loads(response.read())["next"] == "/subscription"
        connection.request("GET", "/api/board", headers={"Cookie": member_cookie})
        response = connection.getresponse()
        assert response.status == 402
        assert json.loads(response.read())["error"] == "subscription_required"

        connection.request("GET", "/pricing")
        response = connection.getresponse()
        pricing_page = response.read().decode("utf-8")
        assert response.status == 200
        assert "$149" in pricing_page
        assert "Futures-DEX" in pricing_page
        assert "OKX DEX" in pricing_page
        assert "Create account" in pricing_page
        assert "uacryptoinvest" not in pricing_page.lower()

        for path, expected in (
            ("/terms", "Terms of Service"),
            ("/privacy", "Privacy Notice"),
            ("/refunds", "Refund Policy"),
        ):
            connection.request("GET", path)
            response = connection.getresponse()
            page = response.read().decode("utf-8")
            assert response.status == 200
            assert expected in page
            assert "uacryptoinvest" not in page.lower()

        connection.request(
            "POST",
            "/api/login",
            body=json.dumps({"email": "admin@example.test", "password": "correct-horse-battery-staple"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        cookie = response.getheader("Set-Cookie")
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
        login = json.loads(response.read())

        monkeypatch.setattr(billing, "create_checkout_session", lambda user: "https://checkout.stripe.com/test-session")
        connection.request(
            "POST", "/api/billing/checkout", body="{}",
            headers={"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": login["csrf_token"]},
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "subscription_consent_required"

        connection.request(
            "POST", "/api/billing/checkout",
            body=json.dumps({"terms_accepted": True, "immediate_access_consent": True}),
            headers={"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": login["csrf_token"]},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["url"] == "https://checkout.stripe.com/test-session"
        with sqlite3.connect(db_path) as database:
            consent = database.execute(
                "SELECT terms_version, immediate_access, ip_address, user_agent FROM subscription_consents"
            ).fetchone()
        assert consent is not None
        assert consent[0] == TERMS_VERSION
        assert consent[1] == 1
        assert consent[2]

        connection.request("POST", "/api/positions", body="{}", headers={"Cookie": cookie})
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "invalid_csrf_token"

        payload = {
            "token": "BTC",
            "long_venue": "Binance",
            "long_market_type": "Spot",
            "long_symbol": "BTC/USDT",
            "long_quantity": 0.01,
            "long_entry_price": 100,
            "short_venue": "Hyperliquid",
            "short_market_type": "Futures",
            "short_symbol": "BTC/USDC:USDC",
            "short_quantity": 0.01,
            "short_entry_price": 110,
        }
        connection.request(
            "POST",
            "/api/positions",
            body=json.dumps(payload),
            headers={"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": login["csrf_token"]},
        )
        response = connection.getresponse()
        assert response.status == 201
        assert json.loads(response.read())["position"]["token"] == "BTC"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_portfolio_separates_price_funding_and_fees(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="member@example.test",
        display_name="Member",
        password="this-is-a-long-password",
        subscription_status="active",
        subscription_days=30,
        db_path=db_path,
    )
    accounts.update_account_settings(user["id"], display_name="Member", monthly_capital_usd=1000, db_path=db_path)
    position = accounts.create_position(
        user["id"],
        {
            "token": "TEST", "long_venue": "A", "long_market_type": "Spot",
            "long_quantity": 2, "long_entry_price": 100,
            "short_venue": "B", "short_market_type": "Futures",
            "short_quantity": 2, "short_entry_price": 110,
            "entry_fees_usd": 2, "capital_usd": 400,
        },
        db_path=db_path,
    )
    accounts.add_funding_cashflow(user["id"], position["id"], {"venue": "B", "amount_usd": 5}, db_path=db_path)
    monkeypatch.setattr(
        portfolio.api_spreads,
        "load_spreads",
        lambda **_: {"rows": [{
            "route_key": position["route_key"], "token": "TEST",
            "long_bid": 105, "long_ask": 106, "short_bid": 101, "short_ask": 102,
        }]},
    )
    current, _ = accounts.login("member@example.test", "this-is-a-long-password", db_path=db_path)
    snapshot = portfolio.portfolio_snapshot(current, board_path=tmp_path / "board", accounts_path=db_path)
    marked = snapshot["positions"][0]
    assert marked["long_price_pnl_usd"] == 10
    assert marked["short_price_pnl_usd"] == 16
    assert marked["price_pnl_usd"] == 26
    assert marked["funding_income_usd"] == 5
    assert marked["fees_usd"] == 2
    assert marked["total_pnl_usd"] == 29
    assert snapshot["summary"]["monthly_return_pct"] == pytest.approx(2.9)


def test_portfolio_return_falls_back_to_tracked_position_capital() -> None:
    summary = portfolio._portfolio_totals(
        [{"status": "open", "total_pnl_usd": 25, "funding_income_usd": 5, "capital_usd": 500}],
        None,
    )
    assert summary["monthly_capital_usd"] == 500
    assert summary["capital_basis"] == "tracked_positions"
    assert summary["monthly_return_pct"] == pytest.approx(5)


def test_background_position_alerts_trigger_once_and_rearm(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="alerts@example.test", display_name="Alerts",
        password="alerts-password-strong", subscription_status="active",
        subscription_days=30, db_path=db_path,
    )
    position = accounts.create_position(
        user["id"],
        {
            "token": "TEST", "long_venue": "A", "long_market_type": "Spot",
            "long_quantity": 1, "long_entry_price": 100,
            "short_venue": "B", "short_market_type": "Futures",
            "short_quantity": 1, "short_entry_price": 110,
        }, db_path=db_path,
    )
    accounts.add_alert_rule(
        user["id"], position["id"],
        {"metric": "pnl_usd", "operator": "gte", "threshold": 10},
        db_path=db_path,
    )
    market = {"long_bid": 105, "long_ask": 106, "short_bid": 101, "short_ask": 102}
    def rows(**_):
        return {"rows": [{
            "route_key": position["route_key"], "token": "TEST",
            "long_venue": "A", "long_market_type": "Spot",
            "short_venue": "B", "short_market_type": "Futures", **market,
        }]}
    monkeypatch.setattr(portfolio.api_spreads, "load_spreads", rows)
    worker = portfolio.PositionAlertWorker(
        board_path=tmp_path / "board", accounts_path=db_path, poll_seconds=10,
    )
    worker.check_once()
    worker.check_once()
    assert len(accounts.list_notifications(user["id"], db_path=db_path)) == 1

    market.update(long_bid=100, short_ask=110)
    worker.check_once()
    market.update(long_bid=105, short_ask=102)
    worker.check_once()
    assert len(accounts.list_notifications(user["id"], db_path=db_path)) == 2
    assert accounts.mark_notifications_read(user["id"], db_path=db_path) == 2
    assert all(item["read_at"] for item in accounts.list_notifications(user["id"], db_path=db_path))
