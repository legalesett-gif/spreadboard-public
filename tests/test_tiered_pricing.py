from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timezone

import pytest

from spreadboard import accounts, billing, crypto_billing, server


def test_existing_paid_members_default_to_research_pro_and_inactive_users_are_free(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    paid = accounts.create_user(
        email="paid@example.test",
        display_name="Paid",
        password="paid-research-password",
        subscription_status="active",
        db_path=db_path,
    )
    free = accounts.create_user(
        email="free@example.test",
        display_name="Free",
        password="free-research-password",
        subscription_status="inactive",
        db_path=db_path,
    )
    assert paid["subscription_tier"] == "research_pro"
    assert free["subscription_tier"] == "free"

    scanner = accounts.update_subscription(
        free["id"], status="active", expires_at="2099-01-01T00:00:00Z",
        tier="scanner", db_path=db_path
    )
    scanner_user = accounts.get_user_object(scanner["id"], db_path=db_path)
    assert scanner_user.has_tier("scanner")
    assert not scanner_user.has_tier("research_pro")


def test_scanner_stripe_checkout_uses_its_own_price_and_metadata(monkeypatch):
    monkeypatch.setenv("SPREADBOARD_STRIPE_SECRET_KEY", "sk_live_example")
    monkeypatch.setenv("SPREADBOARD_STRIPE_WEBHOOK_SECRET", "whsec_example")
    monkeypatch.setenv("SPREADBOARD_STRIPE_PRICE_ID", "price_research")
    monkeypatch.setenv("SPREADBOARD_STRIPE_SCANNER_PRICE_ID", "price_scanner")
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadboard.example")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"url": "https://checkout.stripe.com/session"}).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = request.data.decode()
        return Response()

    monkeypatch.setattr(billing, "urlopen", fake_urlopen)
    user = accounts.User(7, "member@example.test", "Member", "member", "inactive", None, None)
    assert billing.create_checkout_session(user, tier="scanner").startswith(
        "https://checkout.stripe.com/"
    )
    assert "price_scanner" in captured["body"]
    assert "spreadboard_tier%5D=scanner" in captured["body"]
    assert billing.status()["tiers"]["scanner"]["checkout_ready"] is True


def test_crypto_invoice_carries_tier_through_settlement(tmp_path, monkeypatch):
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://rpc.example.test")
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="crypto-tier@example.test",
        display_name="Crypto Tier",
        password="crypto-tier-password",
        subscription_status="inactive",
        db_path=db_path,
    )
    invoice = crypto_billing.create_invoice(
        user["id"], 30, tier="scanner", db_path=db_path
    )
    assert invoice["subscription_tier"] == "scanner"
    assert invoice["list_amount_cents"] == 4_900
    settled = crypto_billing.settle_manually(invoice["id"], db_path=db_path)
    assert settled["resolution"] == "settled"
    assert settled["subscription_tier"] == "scanner"
    assert settled["period_days"] == 30
    assert accounts.get_user(user["id"], db_path=db_path)["subscription_tier"] == "scanner"


def test_exact_on_chain_amount_activates_only_the_invoice_tier(tmp_path, monkeypatch):
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://rpc.example.test")
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)

    scanner_user = accounts.create_user(
        email="scanner@example.test", display_name="Scanner",
        password="scanner-tier-password", subscription_status="inactive", db_path=db_path,
    )
    pro_user = accounts.create_user(
        email="pro@example.test", display_name="Pro",
        password="research-tier-password", subscription_status="inactive", db_path=db_path,
    )
    scanner_invoice = crypto_billing.create_invoice(
        scanner_user["id"], 30, tier="scanner", db_path=db_path
    )
    pro_invoice = crypto_billing.create_invoice(
        pro_user["id"], 30, tier="research_pro", db_path=db_path
    )

    scanner_result = crypto_billing.record_transfer(
        token_address=next(iter(crypto_billing.TOKENS)), raw_units=49_000_000,
        tx_hash="0xscanner", log_index=0, from_address="0x" + "2" * 40,
        block_number=1, db_path=db_path,
    )
    pro_result = crypto_billing.record_transfer(
        token_address=next(iter(crypto_billing.TOKENS)), raw_units=149_000_000,
        tx_hash="0xpro", log_index=0, from_address="0x" + "3" * 40,
        block_number=2, db_path=db_path,
    )

    assert scanner_result["invoice_id"] == scanner_invoice["id"]
    assert scanner_result["subscription_tier"] == "scanner"
    assert pro_result["invoice_id"] == pro_invoice["id"]
    assert pro_result["subscription_tier"] == "research_pro"
    assert accounts.get_user_object(scanner_user["id"], db_path=db_path).entitlement_tier == "scanner"
    assert accounts.get_user_object(pro_user["id"], db_path=db_path).entitlement_tier == "research_pro"


def test_different_tier_cannot_relabel_an_active_prepaid_term(tmp_path, monkeypatch):
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://rpc.example.test")
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="active-scanner@example.test", display_name="Active Scanner",
        password="active-scanner-password", subscription_status="inactive", db_path=db_path,
    )
    accounts.update_subscription(
        user["id"], status="active", expires_at="2026-08-02T00:00:00Z",
        tier="scanner", db_path=db_path,
    )
    before_expiry = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(
        crypto_billing.CryptoBillingError,
        match="tier_change_available_after_current_term",
    ):
        crypto_billing.create_invoice(
            user["id"], 30, tier="research_pro", db_path=db_path, now=before_expiry
        )

    # Same-tier renewal is exact and remains available.
    assert crypto_billing.create_invoice(
        user["id"], 30, tier="scanner", db_path=db_path, now=before_expiry
    )["subscription_tier"] == "scanner"

    # Once the old term is over, a new tier can be bought normally.
    assert crypto_billing.create_invoice(
        user["id"], 30, tier="research_pro", db_path=db_path,
        now=datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc),
    )["subscription_tier"] == "research_pro"


def test_pricing_page_compares_all_three_tiers_and_entitlements():
    html = server.render_pricing_page()
    assert "Free" in html
    assert "Scanner" in html and "$49" in html
    assert "Research Pro" in html and "$149" in html
    assert "Full market and funding scanners" in html
    assert "complete evidence and intelligence workspace" in html
    assert "USDC or USDT on Arbitrum" in html
    assert "No card, no automatic renewal" in html
    assert "What you get &mdash; and how to start" in html
    assert "Pay the exact crypto invoice" in html
    assert "Research Pro also unlocks the private Telegram forum" in html


def test_scanner_is_server_side_blocked_from_research_pro_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    accounts.create_user(
        email="scanner-route@example.test", display_name="Scanner Route",
        password="scanner-route-password", subscription_status="active",
        subscription_tier="scanner", db_path=db_path,
    )
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0), server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl", config={}, accounts_path=db_path,
    )
    threading.Thread(target=app.serve_forever, daemon=True).start()
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        client.request(
            "POST", "/api/login",
            body=json.dumps({"email": "scanner-route@example.test", "password": "scanner-route-password"}),
            headers={"Content-Type": "application/json"},
        )
        login = client.getresponse()
        cookie = login.getheader("Set-Cookie")
        assert login.status == 200
        login.read()

        for path in ("/fair", "/intel"):
            client.request("GET", path, headers={"Cookie": cookie})
            response = client.getresponse()
            assert response.status == 303
            assert response.getheader("Location") == "/pricing?upgrade=research_pro"
            response.read()
        client.request("GET", "/api/intel", headers={"Cookie": cookie})
        response = client.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["error"] == "research_pro_required"

        client.request("GET", "/api/session", headers={"Cookie": cookie})
        response = client.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["user"]["subscription_tier"] == "scanner"
    finally:
        client.close()
        app.shutdown()
        app.server_close()
