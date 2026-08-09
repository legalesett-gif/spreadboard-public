"""HTTP boundary for crypto checkout.

The subtle failure this guards against: members buying access have no active
subscription yet, so if the invoice routes sit behind the subscription gate,
nobody can ever pay. That is a silent, total revenue outage.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from spreadboard.server import SpreadBoardHandler, SpreadBoardServer


RECEIVER = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
CONSENT = {"terms_accepted": True, "immediate_access_consent": True, "period_days": 30}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("SPREADBOARD_ADMIN_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", RECEIVER)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    SpreadBoardHandler._login_attempts.clear()
    server = SpreadBoardServer(
        ("127.0.0.1", 0), SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl", config={},
        accounts_path=tmp_path / "accounts.sqlite3",
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        yield connection
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        SpreadBoardHandler._login_attempts.clear()


def register(connection, email: str) -> tuple[str, str]:
    connection.request(
        "POST", "/api/register",
        body=json.dumps({"display_name": "Member", "email": email, "password": "new-member-password"}),
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    assert response.status == 201, response.read()
    cookie = response.getheader("Set-Cookie")
    response.read()

    connection.request("GET", "/api/session", headers={"Cookie": cookie})
    response = connection.getresponse()
    csrf = json.loads(response.read())["csrf_token"]
    return cookie, csrf


def post(connection, path, cookie, csrf, body):
    connection.request(
        "POST", path, body=json.dumps(body),
        headers={"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": csrf},
    )
    return connection.getresponse()


def test_member_without_subscription_can_still_create_an_invoice(client):
    cookie, csrf = register(client, "buyer@example.test")

    client.request("GET", "/api/board", headers={"Cookie": cookie})
    response = client.getresponse()
    assert response.status == 402, "precondition: this member has no access yet"
    response.read()

    response = post(client, "/api/billing/crypto/invoice", cookie, csrf, CONSENT)
    assert response.status == 201, "checkout must not sit behind the subscription gate"
    invoice = json.loads(response.read())["invoice"]
    assert invoice["amount_cents"] == 14_900
    assert invoice["receiving_address"] == RECEIVER
    assert invoice["chain_id"] == 42161


def test_invoice_polling_is_reachable_without_a_subscription(client):
    cookie, csrf = register(client, "buyer@example.test")
    response = post(client, "/api/billing/crypto/invoice", cookie, csrf, CONSENT)
    invoice_id = json.loads(response.read())["invoice"]["id"]

    client.request("GET", f"/api/billing/crypto/invoice/{invoice_id}", headers={"Cookie": cookie})
    response = client.getresponse()
    assert response.status == 200
    assert json.loads(response.read())["invoice"]["id"] == invoice_id


def test_exact_token_qr_is_reachable_without_a_subscription(client):
    cookie, csrf = register(client, "qr-buyer@example.test")
    response = post(client, "/api/billing/crypto/invoice", cookie, csrf, CONSENT)
    invoice = json.loads(response.read())["invoice"]

    client.request(
        "GET", f"/api/billing/crypto/qr/{invoice['id']}?token=USDC",
        headers={"Cookie": cookie},
    )
    response = client.getresponse()
    payload = response.read()
    assert response.status == 200
    assert response.getheader("Content-Type").startswith("image/svg+xml")
    assert payload.startswith(b"<svg")
    assert len(payload) > 500


def test_qr_rejects_unknown_token_and_another_members_invoice(client):
    first_cookie, first_csrf = register(client, "qr-first@example.test")
    response = post(client, "/api/billing/crypto/invoice", first_cookie, first_csrf, CONSENT)
    invoice_id = json.loads(response.read())["invoice"]["id"]

    client.request(
        "GET", f"/api/billing/crypto/qr/{invoice_id}?token=ETH",
        headers={"Cookie": first_cookie},
    )
    response = client.getresponse()
    assert response.status >= 400
    response.read()

    second_cookie, _ = register(client, "qr-second@example.test")
    client.request(
        "GET", f"/api/billing/crypto/qr/{invoice_id}?token=USDC",
        headers={"Cookie": second_cookie},
    )
    response = client.getresponse()
    assert response.status >= 400
    response.read()


def test_a_member_cannot_poll_someone_elses_invoice(client):
    first_cookie, first_csrf = register(client, "first@example.test")
    response = post(client, "/api/billing/crypto/invoice", first_cookie, first_csrf, CONSENT)
    invoice_id = json.loads(response.read())["invoice"]["id"]

    second_cookie, _ = register(client, "second@example.test")
    client.request("GET", f"/api/billing/crypto/invoice/{invoice_id}", headers={"Cookie": second_cookie})
    response = client.getresponse()
    assert response.status >= 400
    response.read()


def test_consent_is_required_before_an_invoice_is_issued(client):
    cookie, csrf = register(client, "buyer@example.test")
    response = post(client, "/api/billing/crypto/invoice", cookie, csrf, {"period_days": 30})
    assert response.status >= 400
    assert "consent" in response.read().decode()


def test_invalid_period_is_rejected(client):
    cookie, csrf = register(client, "buyer@example.test")
    response = post(
        client, "/api/billing/crypto/invoice", cookie, csrf,
        {"terms_accepted": True, "immediate_access_consent": True, "period_days": 45},
    )
    assert response.status >= 400
    response.read()


def test_admin_only_queue_is_closed_to_members(client):
    cookie, _ = register(client, "buyer@example.test")
    client.request("GET", "/api/billing/crypto/pending", headers={"Cookie": cookie})
    response = client.getresponse()
    assert response.status >= 400
    response.read()


def test_health_reports_crypto_provider(client):
    client.request("GET", "/api/health")
    response = client.getresponse()
    payload = json.loads(response.read())
    crypto = payload["crypto_billing"]
    assert crypto["provider"] == "crypto"
    assert crypto["chain_id"] == 42161
    assert crypto["recurring"] is False
    assert sorted(crypto["tokens"]) == ["USDC", "USDT"]


def test_public_status_exposes_setup_gaps_without_claiming_all_operational(client):
    client.request("GET", "/api/status")
    response = client.getresponse()
    payload = json.loads(response.read())
    assert response.status == 200
    assert payload["components"]["crypto_checkout"]["status"] == "operational"
    assert payload["components"]["email_recovery"]["status"] == "setup_needed"
    assert payload["ok"] is False


def test_subscription_page_offers_crypto_checkout(client):
    cookie, _ = register(client, "buyer@example.test")
    client.request("GET", "/subscription", headers={"Cookie": cookie})
    response = client.getresponse()
    page = response.read().decode()
    assert response.status == 200
    assert "Pay with crypto" in page
    assert "Arbitrum One" in page
    assert "USDC" in page and "USDT" in page
    # all three prepaid periods must be offered
    for label in ("$149.00", "$375.00", "$1,365.00"):
        assert label in page, f"missing period {label}"
    for days in ("30", "90", "365"):
        assert f'data-crypto-period="{days}"' in page
    # the wrong-chain warning must be present, not buried
    assert "cannot be credited" in page
    assert "no auto-renewal" in page.lower()
    assert "data-crypto-token-picker" in page
    assert "data-crypto-contract" in page
    assert "/api/billing/crypto/qr/" in page


def test_checkout_panel_fails_closed_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("SPREADBOARD_CRYPTO_RPC_URL", raising=False)
    cookie, _ = register(client, "buyer2@example.test")
    client.request("GET", "/subscription", headers={"Cookie": cookie})
    page = client.getresponse().read().decode()
    assert "No payment can be taken yet" in page
    assert "data-crypto-period" not in page


def test_guide_page_is_public_and_covers_all_four_lanes(client):
    """The tutorial must be reachable without an account -- it is a conversion page."""
    client.request("GET", "/guide")
    response = client.getresponse()
    page = response.read().decode()
    assert response.status == 200
    for lane in ("Futures / Futures", "Futures / Spot", "Spot / Spot", "Futures / DEX"):
        assert lane in page, f"missing lane: {lane}"
    # the two ideas a beginner must not miss
    assert "delta neutral" in page
    assert "SHUT" in page, "closed transfer rails must be explained"
    # large spreads are shown deliberately, and must carry the caveat
    assert "Very large spreads" in page
    assert "financial advice" in page.lower()
    assert "research tool" in page.lower()
