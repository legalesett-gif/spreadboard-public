from __future__ import annotations

import http.client
import json
import threading

import pytest

from spreadboard import margin_planner, server
from spreadboard.server import SpreadBoardHandler, SpreadBoardServer


def test_isolated_margin_uses_exact_tier_costs_and_public_stress() -> None:
    result = margin_planner.calculate(
        {
            "account_mode": "isolated",
            "position_notional_usd": 10_000,
            "leverage": 3,
            "maintenance_margin_pct": 1.0,
            "stress_move_pct": 20.0,
            "allocated_collateral_usd": 4_000,
            "entry_fee_pct": 0.1,
            "exit_fee_pct": 0.1,
            "exit_slippage_pct": 0.2,
            "adverse_funding_pct": 0.1,
        }
    )
    assert result["initial_margin_usd"] == 3333.33
    assert result["maintenance_margin_usd"] == 100.0
    assert result["public_stress_loss_usd"] == 2000.0
    assert result["entered_costs_usd"] == 50.0
    assert result["stress_headroom_usd"] == 1850.0
    assert result["verdict"] == "within_entered_stress"
    assert result["method"] == "account_input_margin_stress_v1"


def test_cross_margin_deducts_other_positions_and_protected_cash() -> None:
    result = margin_planner.calculate(
        {
            "account_mode": "cross",
            "position_notional_usd": 5_000,
            "leverage": 5,
            "maintenance_margin_pct": 0.5,
            "stress_move_pct": 25,
            "account_equity_usd": 2_000,
            "other_positions_reserve_usd": 500,
            "cash_reserve_usd": 300,
        }
    )
    assert result["available_collateral_usd"] == 1200.0
    assert result["survival_requirement_usd"] == 1275.0
    assert result["verdict"] == "stress_shortfall"


def test_margin_planner_refuses_to_invent_maintenance_tier() -> None:
    with pytest.raises(ValueError, match="maintenance_margin_pct_required"):
        margin_planner.calculate(
            {
                "account_mode": "isolated",
                "position_notional_usd": 1000,
                "leverage": 2,
                "stress_move_pct": 25,
                "allocated_collateral_usd": 700,
            }
        )


def test_watchlist_exposes_private_transient_margin_form() -> None:
    page = server.render_watchlist_page(server.board.DEFAULT_BOARD_PATH, {}, {})
    assert 'id="margin-planner"' in page
    assert "Exact maintenance margin" in page
    assert "Inputs are calculated transiently and are not stored" in page
    assert "/api/margin-plan" in page
    assert ".margin-plan-form [hidden] { display:none !important; }" in server.APP_CSS


def test_margin_endpoint_requires_login_and_csrf(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("SPREADBOARD_ADMIN_PASSWORD", "correct-horse-battery-staple")
    app = SpreadBoardServer(
        ("127.0.0.1", 0),
        SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=tmp_path / "accounts.sqlite3",
    )
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    payload = {
        "account_mode": "isolated",
        "position_notional_usd": 1000,
        "leverage": 2,
        "maintenance_margin_pct": 1,
        "stress_move_pct": 20,
        "allocated_collateral_usd": 700,
    }
    try:
        client.request(
            "POST", "/api/margin-plan", body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = client.getresponse()
        assert response.status == 401
        assert json.loads(response.read())["error"] == "authentication_required"

        client.request(
            "POST", "/api/login",
            body=json.dumps({
                "email": "admin@example.test",
                "password": "correct-horse-battery-staple",
            }),
            headers={"Content-Type": "application/json"},
        )
        response = client.getresponse()
        assert response.status == 200
        cookie = response.getheader("Set-Cookie")
        login = json.loads(response.read())

        client.request(
            "POST", "/api/margin-plan", body=json.dumps(payload),
            headers={"Cookie": cookie, "Content-Type": "application/json"},
        )
        response = client.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "invalid_csrf_token"

        client.request(
            "POST", "/api/margin-plan", body=json.dumps(payload),
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-CSRF-Token": login["csrf_token"],
            },
        )
        response = client.getresponse()
        result = json.loads(response.read())
        assert response.status == 200
        assert result["method"] == "account_input_margin_stress_v1"
        assert result["available_collateral_usd"] == 700.0
    finally:
        client.close()
        app.shutdown()
        app.server_close()
        thread.join(timeout=5)
