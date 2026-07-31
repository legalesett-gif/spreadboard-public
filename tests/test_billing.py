from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3

import pytest

from spreadboard import accounts, billing


def _member(db_path: Path) -> accounts.User:
    accounts.initialize(db_path)
    created = accounts.create_user(
        email="member@example.com",
        display_name="Member",
        password="a-strong-test-password",
        subscription_status="inactive",
        db_path=db_path,
    )
    connection = accounts._connect(db_path)
    try:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (created["id"],)).fetchone()
        return accounts._user_from_row(row)
    finally:
        connection.close()


def _event(event_id: str, event_type: str, obj: dict) -> dict:
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


def test_webhook_signature_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPREADBOARD_STRIPE_WEBHOOK_SECRET", "whsec_test")
    payload = json.dumps(_event("evt_1", "invoice.paid", {}), separators=(",", ":")).encode()
    timestamp = 1_800_000_000
    digest = hmac.new(b"whsec_test", str(timestamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
    assert billing.verify_webhook(payload, f"t={timestamp},v1={digest}", now=timestamp)["id"] == "evt_1"
    with pytest.raises(billing.BillingError, match="invalid_webhook_signature"):
        billing.verify_webhook(payload, f"t={timestamp},v1=bad", now=timestamp)
    with pytest.raises(billing.BillingError, match="expired_webhook_signature"):
        billing.verify_webhook(payload, f"t={timestamp},v1={digest}", now=timestamp + 301)


def test_checkout_uses_recurring_price_and_user_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPREADBOARD_STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setenv("SPREADBOARD_STRIPE_PRICE_ID", "price_monthly")
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadboard.example")
    captured = {}
    def fake_post(path, params, *, idempotency_key):
        captured.update(path=path, params=params, idempotency_key=idempotency_key)
        return {"url": "https://checkout.stripe.com/c/pay/cs_test"}
    monkeypatch.setattr(billing, "_stripe_post", fake_post)
    user = accounts.User(7, "member@example.com", "Member", "member", "inactive", None, None)
    assert billing.create_checkout_session(user).startswith("https://checkout.stripe.com/")
    assert captured["params"]["mode"] == "subscription"
    assert captured["params"]["line_items[0][price]"] == "price_monthly"
    assert captured["params"]["subscription_data[metadata][spreadboard_user_id]"] == "7"


def test_subscription_event_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    user = _member(db_path)
    period_end = 1_900_000_000
    event = _event(
        "evt_subscription",
        "customer.subscription.updated",
        {
            "id": "sub_123",
            "customer": "cus_123",
            "status": "active",
            "current_period_end": period_end,
            "cancel_at_period_end": True,
            "metadata": {"spreadboard_user_id": str(user.id)},
        },
    )
    result = accounts.apply_billing_event(event, payload_sha256="abc", db_path=db_path)
    duplicate = accounts.apply_billing_event(event, payload_sha256="abc", db_path=db_path)
    updated = accounts.get_user(user.id, db_path=db_path)
    assert result["result"] == "subscription_active"
    assert duplicate["duplicate"] is True
    assert updated["subscription_status"] == "active"
    assert updated["billing_managed"] is True
    assert updated["subscription_cancel_at_period_end"] is True
    assert datetime.fromisoformat(updated["subscription_expires_at"].replace("Z", "+00:00")) == datetime.fromtimestamp(period_end, tz=timezone.utc)


def test_payment_failed_and_deleted_revoke_access(tmp_path: Path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    user = _member(db_path)
    linked = _event("evt_link", "checkout.session.completed", {
        "customer": "cus_456", "subscription": "sub_456", "client_reference_id": str(user.id)
    })
    accounts.apply_billing_event(linked, payload_sha256="1", db_path=db_path)
    accounts.apply_billing_event(
        _event("evt_fail", "invoice.payment_failed", {"customer": "cus_456", "subscription": "sub_456"}),
        payload_sha256="2", db_path=db_path,
    )
    assert accounts.get_user(user.id, db_path=db_path)["subscription_status"] == "past_due"
    accounts.apply_billing_event(
        _event("evt_delete", "customer.subscription.deleted", {"id": "sub_456", "customer": "cus_456", "status": "canceled"}),
        payload_sha256="3", db_path=db_path,
    )
    assert accounts.get_user(user.id, db_path=db_path)["subscription_status"] == "cancelled"


def test_customer_cannot_be_reassigned(tmp_path: Path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    first = _member(db_path)
    second = accounts.create_user(
        email="other@example.com", display_name="Other", password="another-strong-password",
        subscription_status="inactive", db_path=db_path,
    )
    accounts.apply_billing_event(
        _event("evt_first", "checkout.session.completed", {"customer": "cus_same", "client_reference_id": str(first.id)}),
        payload_sha256="1", db_path=db_path,
    )
    with pytest.raises(ValueError, match="billing_customer_conflict"):
        accounts.apply_billing_event(
            _event("evt_second", "checkout.session.completed", {"customer": "cus_same", "client_reference_id": str(second["id"])}),
            payload_sha256="2", db_path=db_path,
        )


def test_unrelated_invoice_cannot_activate_membership(tmp_path: Path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    user = _member(db_path)
    accounts.apply_billing_event(
        _event("evt_linked", "checkout.session.completed", {
            "customer": "cus_owner", "subscription": "sub_spreadboard", "client_reference_id": str(user.id)
        }), payload_sha256="1", db_path=db_path,
    )
    result = accounts.apply_billing_event(
        _event("evt_other_invoice", "invoice.paid", {
            "customer": "cus_owner", "subscription": "sub_unrelated"
        }), payload_sha256="2", db_path=db_path,
    )
    assert result["result"] == "ignored_subscription_mismatch"
    assert accounts.get_user(user.id, db_path=db_path)["subscription_status"] == "inactive"


def test_initialize_migrates_existing_users_table(tmp_path: Path) -> None:
    db_path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, display_name TEXT, password_hash TEXT, role TEXT, subscription_status TEXT, subscription_expires_at TEXT, monthly_capital_usd REAL, created_at TEXT, updated_at TEXT, last_login_at TEXT)")
    connection.commit()
    connection.close()
    accounts.initialize(db_path)
    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    finally:
        connection.close()
    assert {"billing_customer_id", "billing_subscription_id", "subscription_cancel_at_period_end"} <= columns
    connection = sqlite3.connect(db_path)
    try:
        alert_columns = {row[1] for row in connection.execute("PRAGMA table_info(position_alert_rules)")}
    finally:
        connection.close()
    assert "last_condition_met" in alert_columns
