"""Partner operations must stay recoverable and truthful at the browser boundary.

The production Partner audit found two async handlers reading
``event.currentTarget`` after an ``await``.  Browsers clear ``currentTarget``
when event dispatch ends, so creating a partner succeeded in the database but
finished with ``Cannot read properties of null (reading 'reset')``; Copy Link
failed the same way.  The same pass found that a draft payout could be created
but never cancelled, leaving its commissions permanently stuck ``in_batch``
without a direct database edit.
"""

from __future__ import annotations

import http.client
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from spreadboard import accounts, affiliates, crypto_billing, server

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
PAYOUT = "0x1111111111111111111111111111111111111111"


def _user(db, email: str) -> int:
    return int(
        accounts.create_user(
            email=email,
            display_name=email.split("@", 1)[0],
            password="correct horse battery staple",
            subscription_status="inactive",
            subscription_days=1,
            db_path=db,
        )["id"]
    )


def _payable_partner(db, monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict, dict]:
    monkeypatch.setenv(
        "SPREADBOARD_CRYPTO_RECEIVING_ADDRESS",
        "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937",
    )
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    partner_user = _user(db, "partner@example.test")
    partner = affiliates.create_partner(
        partner_user,
        slug="partner-channel",
        display_name="Partner Channel",
        db_path=db,
    )
    affiliates.save_payout_profile(
        partner_user,
        network="Arbitrum",
        destination=PAYOUT,
        db_path=db,
    )
    _partner, token = affiliates.create_click("partner-channel", db_path=db, now=NOW)
    customer = _user(db, "customer@example.test")
    affiliates.attach_registration(customer, token, db_path=db, now=NOW)
    invoice = crypto_billing.create_invoice(
        customer,
        30,
        tier="research_pro",
        db_path=db,
        now=NOW,
    )
    crypto_billing.settle_manually(invoice["id"], db_path=db, now=NOW)
    return partner_user, partner, invoice


def test_async_partner_controls_keep_a_stable_element_reference(tmp_path) -> None:
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    partner_user = _user(db, "cabinet@example.test")
    affiliates.create_partner(
        partner_user,
        slug="cabinet-channel",
        display_name="Cabinet Channel",
        db_path=db,
    )
    user = accounts.get_user_object(partner_user, db_path=db)
    assert user is not None

    admin_script = server.render_partner_admin_script()
    partner_page = server.render_partner_page(user, db)
    connection = accounts._connect(db)
    connection.execute(
        "UPDATE users SET role = 'admin', subscription_status = 'active' WHERE id = ?",
        (partner_user,),
    )
    connection.commit()
    connection.close()
    admin_user = accounts.get_user_object(partner_user, db_path=db)
    assert admin_user is not None
    admin_page = server.render_partner_page(admin_user, db)

    assert "const form=event.currentTarget" in admin_script
    assert "form.reset()" in admin_script
    assert "event.currentTarget.reset()" not in admin_script
    assert "const button=event.currentTarget" in partner_page
    assert "button.textContent='Copied'" in partner_page
    assert "event.currentTarget.textContent='Copied'" not in partner_page
    assert 'pattern="[a-z0-9](?:[a-z0-9]|-){2,63}"' in admin_page


def test_partner_admin_never_turns_a_failed_load_into_no_partners() -> None:
    script = server.render_partner_admin_script()

    assert "if(!response.ok)throw new Error" in script
    assert "Could not load partners" in script
    assert "Nothing payable" in script
    assert "No commissions have completed the seven-day hold yet." in script


def test_draft_payout_can_be_cancelled_without_losing_commissions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    partner_user, partner, _invoice = _payable_partner(db, monkeypatch)
    payable_at = NOW + timedelta(days=8)
    first = affiliates.create_payout_batch(partner["id"], db_path=db, now=payable_at)

    cancelled = affiliates.cancel_payout_batch(
        first["id"],
        reason="operator caught a wrong destination before transfer",
        db_path=db,
        now=payable_at,
    )
    restored = affiliates.partner_summary(partner_user, db_path=db, now=payable_at)

    assert cancelled["status"] == "cancelled"
    assert "wrong destination" in cancelled["note"]
    assert [row["status"] for row in restored["commissions"]] == ["pending"]
    assert restored["metrics"]["payable"] == 5_960
    second = affiliates.create_payout_batch(partner["id"], db_path=db, now=payable_at)
    assert second["id"] != first["id"]
    assert second["amount_cents"] == 5_960


def test_admin_list_separates_pending_hold_from_payable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    _partner_user, _partner, _invoice = _payable_partner(db, monkeypatch)

    held = affiliates.list_partners(db_path=db, now=NOW)
    payable = affiliates.list_partners(db_path=db, now=NOW + timedelta(days=8))

    assert held[0]["pending_cents"] == 5_960
    assert held[0]["payable_cents"] == 0
    assert payable[0]["pending_cents"] == 5_960
    assert payable[0]["payable_cents"] == 5_960


def test_partner_earnings_are_labelled_usdt_without_lifetime_overclaim(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    partner_user, _partner, _invoice = _payable_partner(db, monkeypatch)
    user = accounts.get_user_object(partner_user, db_path=db)
    assert user is not None

    page = server.render_partner_page(user, db)
    admin = server.render_partner_page(
        accounts.User(
            id=999,
            email="admin@example.test",
            display_name="Admin",
            role="admin",
            subscription_status="active",
            subscription_expires_at=None,
            subscription_tier="research_pro",
            billing_customer_id=None,
            billing_subscription_id=None,
            subscription_cancel_at_period_end=False,
            monthly_capital_usd=None,
            csrf_token="csrf",
        ),
        db,
    )

    assert "5,960" not in page
    assert "59.60 USDT" in page
    assert "Recurring 50%" in admin
    assert "Lifetime 50%" not in admin
    admin_script = server.render_partner_admin_script()
    assert "const usdt=value=>" in admin_script
    assert "${usdt(item.pending_cents)} pending" in admin_script
    assert "Open referral link" not in admin_script
    assert "Open single-use password setup link" not in admin_script
    assert "data-copy-partner-link" in admin_script


def test_partner_grid_contains_wide_ledgers_on_mobile(tmp_path) -> None:
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    partner_user = _user(db, "mobile-partner@example.test")
    affiliates.create_partner(
        partner_user,
        slug="mobile-partner",
        display_name="Mobile Partner",
        db_path=db,
    )
    user = accounts.get_user_object(partner_user, db_path=db)
    assert user is not None

    server.render_partner_page(user, db)

    assert ".partner-page>* { min-width:0; }" in server.APP_CSS
    assert (
        ".partner-table-wrap { width:100%; min-width:0; max-width:100%; overflow-x:auto; }"
        in server.APP_CSS
    )


def test_only_an_admin_can_cancel_a_draft_through_the_http_boundary(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("SPREADBOARD_ADMIN_PASSWORD", "correct-horse-battery-staple")
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=db,
    )
    partner_user, partner, _invoice = _payable_partner(db, monkeypatch)
    payable_at = NOW + timedelta(days=8)
    batch = affiliates.create_payout_batch(partner["id"], db_path=db, now=payable_at)
    accounts.update_subscription(
        partner_user,
        status="active",
        expires_at=(NOW + timedelta(days=30)).isoformat(),
        db_path=db,
    )
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)

    def login(email: str, password: str) -> tuple[str, str]:
        connection.request(
            "POST",
            "/api/login",
            body=json.dumps({"email": email, "password": password}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        cookie = response.getheader("Set-Cookie")
        payload = json.loads(response.read())
        return cookie, payload["csrf_token"]

    def cancel(cookie: str, csrf: str):
        connection.request(
            "POST",
            f"/api/admin/payouts/{batch['id']}/cancel",
            body=json.dumps({"reason": "audit reversal"}),
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
            },
        )
        return connection.getresponse()

    try:
        member_cookie, member_csrf = login("partner@example.test", "correct horse battery staple")
        response = cancel(member_cookie, member_csrf)
        assert response.status == 403
        assert json.loads(response.read())["error"] == "admin_required"

        admin_cookie, admin_csrf = login("admin@example.test", "correct-horse-battery-staple")
        response = cancel(admin_cookie, admin_csrf)
        assert response.status == 200
        assert json.loads(response.read())["batch"]["status"] == "cancelled"
    finally:
        connection.close()
        app.shutdown()
        app.server_close()
        thread.join(timeout=5)
        server.SpreadBoardHandler._login_attempts.clear()


def test_failed_partner_creation_removes_the_unclaimed_invite(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("SPREADBOARD_ADMIN_PASSWORD", "correct-horse-battery-staple")
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=db,
    )
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        connection.request(
            "POST",
            "/api/login",
            body=json.dumps(
                {
                    "email": "admin@example.test",
                    "password": "correct-horse-battery-staple",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        cookie = response.getheader("Set-Cookie")
        payload = json.loads(response.read())
        assert response.status == 200

        def fail_partner(*_args, **_kwargs):
            raise ValueError("simulated_partner_race")

        monkeypatch.setattr(affiliates, "create_partner", fail_partner)
        connection.request(
            "POST",
            "/api/admin/partners",
            body=json.dumps(
                {
                    "email": "orphan@example.test",
                    "display_name": "Orphan Audit",
                    "slug": "orphan-audit",
                }
            ),
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-CSRF-Token": payload["csrf_token"],
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "simulated_partner_race"
        assert accounts.user_id_for_email("orphan@example.test", db_path=db) is None
    finally:
        connection.close()
        app.shutdown()
        app.server_close()
        thread.join(timeout=5)
        server.SpreadBoardHandler._login_attempts.clear()


def test_failed_invite_token_creation_does_not_leave_an_orphan_user(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)

    def fail_token(*_args, **_kwargs):
        raise RuntimeError("simulated token failure")

    monkeypatch.setattr(accounts, "create_password_token", fail_token)
    with pytest.raises(RuntimeError, match="simulated token failure"):
        accounts.create_invited_user(
            email="token-orphan@example.test",
            display_name="Token Orphan",
            subscription_status="inactive",
            subscription_tier="free",
            subscription_days=1,
            db_path=db,
        )

    assert accounts.user_id_for_email("token-orphan@example.test", db_path=db) is None
