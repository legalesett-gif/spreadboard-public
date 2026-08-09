from __future__ import annotations

import json

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
    assert accounts.get_user(user["id"], db_path=db_path)["subscription_tier"] == "scanner"


def test_pricing_page_compares_all_three_tiers_and_entitlements():
    html = server.render_pricing_page()
    assert "Free" in html
    assert "Scanner" in html and "$49" in html
    assert "Research Pro" in html and "$180" in html
    assert "Full market and funding scanners" in html
    assert "complete evidence and intelligence workspace" in html
    assert "USDC or USDT on Arbitrum" in html
    assert "No card, no automatic renewal" in html
    assert "What you get &mdash; and how to start" in html
    assert "Pay the exact crypto invoice" in html
