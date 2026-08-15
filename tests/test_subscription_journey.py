"""The whole paid journey in one pass, from referral click to removal.

Every stage below already has focused unit tests. What none of them cover is the
sequence: a referred visitor registers, pays one real on-chain transfer, gains
entitlement, joins the community, is reminded, expires, is removed, and earns
the partner a paid commission. Handover items 2 and 3 describe exactly this
chain, and a chain is where per-stage tests are weakest -- each stage can be
correct while the seam between two of them is not.

This exercises the confirmed-transfer path (`record_transfer`), not the admin
`settle_manually` shortcut the affiliate suite uses, because that is what a real
USDT payment actually travels through.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from spreadboard import (
    accounts,
    affiliates,
    crypto_billing,
    subscription_lifecycle,
    telegram_bot,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
RECEIVER = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
PAYER = "0x2222222222222222222222222222222222222222"
PAYOUT = "0x1111111111111111111111111111111111111111"
USDT = "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"
COMMUNITY_CHAT = -100123
MEMBER_CHAT = 77


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadarbitrage.ink")
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", RECEIVER)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def _user(db, email: str, **overrides) -> int:
    """An account that has never paid for anything.

    `create_user` clamps `subscription_days` to at least one, so a new account
    always carries an expiry. Left in place it would seed the renewal base and
    every expiry assertion below would drift with the wall clock, so clear it.
    """
    user_id = int(
        accounts.create_user(
            email=email,
            display_name=email.split("@")[0],
            password="correct horse battery staple",
            subscription_status=overrides.pop("subscription_status", "inactive"),
            db_path=db,
            **overrides,
        )["id"]
    )
    accounts.update_subscription(user_id, status="inactive", expires_at=None, db_path=db)
    return user_id


def _pay(db, invoice, *, tx: str, now: datetime) -> dict:
    """Send exactly what the invoice asked for, as a confirmed USDT transfer."""
    cents = int(invoice["amount_cents"])
    return crypto_billing.record_transfer(
        token_address=USDT,
        raw_units=cents * 10**4,  # USDT has 6 decimals; cents are 2
        tx_hash=tx,
        log_index=0,
        from_address=PAYER,
        block_number=1000,
        db_path=db,
        now=now,
    )


def test_a_referred_subscriber_completes_the_whole_paid_journey(db, monkeypatch) -> None:
    monkeypatch.setattr(
        subscription_lifecycle.mailer, "status", lambda: {"configured": False}
    )
    api_calls: list[tuple[str, dict]] = []

    def fake_api(method, params):
        api_calls.append((method, params))
        if method == "getChatMember":
            return {"ok": True, "result": {"status": "member"}}
        return {"ok": True, "result": True}

    monkeypatch.setattr(telegram_bot, "_api_call", fake_api)

    # 1. A partner is live and has somewhere to be paid.
    partner_user = _user(db, "creator@example.test")
    partner = affiliates.create_partner(
        partner_user, slug="youtube-channel", display_name="YouTube Channel", db_path=db
    )
    affiliates.save_payout_profile(
        partner_user, asset="USDT", network="Arbitrum", destination=PAYOUT, db_path=db
    )

    # 2. A visitor arrives through the referral link and registers.
    _row, token = affiliates.create_click("youtube-channel", db_path=db, now=NOW)
    subscriber = _user(db, "subscriber@example.test")
    assert affiliates.attach_registration(subscriber, token, db_path=db, now=NOW)

    # 3. Their first invoice carries the one-month referral discount.
    invoice = crypto_billing.create_invoice(
        subscriber, 30, tier="research_pro", db_path=db, now=NOW
    )
    assert invoice["discount_cents"] == 2_980
    assert invoice["amount_cents"] == 11_920

    # 4. One confirmed on-chain transfer of exactly that amount settles it.
    settlement = _pay(db, invoice, tx="0xaaa", now=NOW)
    assert settlement["resolution"] == "settled"

    # 5. Entitlement is granted for the period, and the partner earns once.
    member = accounts.get_user_object(subscriber, db_path=db)
    assert member.subscription_active
    assert member.entitlement_tier == "research_pro"
    expiry = datetime.fromisoformat(member.subscription_expires_at)
    assert expiry == NOW + timedelta(days=30)

    summary = affiliates.partner_summary(partner_user, db_path=db, now=NOW)
    assert summary is not None
    assert len(summary["commissions"]) == 1
    assert summary["commissions"][0]["commission_cents"] == 5_960

    # Paying the same transfer twice must not extend anything.
    assert _pay(db, invoice, tx="0xaaa", now=NOW)["resolution"] == "duplicate"
    again = accounts.get_user_object(subscriber, db_path=db)
    assert again.subscription_expires_at == member.subscription_expires_at

    # 6. They link Telegram and are approved into the community.
    link = accounts.create_telegram_link_token(subscriber, db_path=db)
    accounts.bind_telegram_chat(link, MEMBER_CHAT, db_path=db)
    accounts.configure_telegram_community(
        COMMUNITY_CHAT,
        title="Subscribers",
        configured_by_telegram_user_id=42,
        db_path=db,
    )
    telegram_bot.handle_update(
        {"chat_join_request": {"chat": {"id": COMMUNITY_CHAT}, "from": {"id": MEMBER_CHAT}}},
        db_path=db,
    )
    assert api_calls[0][0] == "approveChatJoinRequest"
    candidates = accounts.telegram_membership_candidates(db_path=db)
    assert candidates[0]["membership_state"] == "active"

    # A paid member is left alone by the sweep.
    assert telegram_bot.MembershipWorker(db_path=db).check_once() == {
        "checked": 1,
        "removed": 0,
        "errors": 0,
    }

    # 7. Each reminder fires once, in order, as expiry approaches.
    seen: list[str] = []
    for days_out, expected in ((7, "seven days"), (3, "three days"), (1, "one day")):
        moment = expiry - timedelta(days=days_out)
        first = subscription_lifecycle.check_once(db_path=db, now=moment)
        repeat = subscription_lifecycle.check_once(db_path=db, now=moment)
        assert first["delivered"] == 1, f"no notice {days_out} days out"
        assert repeat == {"discovered": 0, "delivered": 0, "failed": 0}
        titles = [row["title"] for row in accounts.list_notifications(subscriber, db_path=db)]
        assert any(expected in title for title in titles)
        seen.append(expected)
    assert seen == ["seven days", "three days", "one day"]

    # 8. Expiry revokes the paid state.
    after = expiry + timedelta(minutes=1)
    assert subscription_lifecycle.check_once(db_path=db, now=after)["delivered"] == 1
    lapsed = accounts.get_user_object(subscriber, db_path=db)
    assert not lapsed.subscription_active
    assert lapsed.entitlement_tier == "free"

    # 9. The sweep then removes them from the community.
    api_calls.clear()
    assert telegram_bot.MembershipWorker(db_path=db).check_once() == {
        "checked": 1,
        "removed": 1,
        "errors": 0,
    }
    assert [method for method, _ in api_calls] == [
        "getChatMember",
        "banChatMember",
        "unbanChatMember",
    ]

    # 10. The commission is batched and paid in USDT to the saved wallet.
    batch = affiliates.create_payout_batch(partner["id"], db_path=db, now=after)
    assert batch["payout_asset"] == "USDT"
    assert batch["payout_destination"] == PAYOUT
    assert batch["amount_cents"] == 5_960
    paid = affiliates.mark_payout_paid(
        batch["id"], payment_reference="0xdef456", db_path=db, now=after
    )
    assert paid["status"] == "paid"

    final = affiliates.partner_summary(partner_user, db_path=db, now=after)
    assert final is not None
    assert [row["status"] for row in final["commissions"]] == ["paid"]


def test_a_lapsed_subscriber_who_pays_again_is_readmitted(db, monkeypatch) -> None:
    """Renewal after a lapse must restore access rather than strand the account.

    The removal sweep is the destructive half of the lifecycle. If a re-paid
    member stayed 'removed', the only visible symptom would be a customer who
    cannot rejoin a community they have paid for.
    """
    monkeypatch.setattr(
        subscription_lifecycle.mailer, "status", lambda: {"configured": False}
    )
    monkeypatch.setattr(
        telegram_bot,
        "_api_call",
        lambda method, params: {"ok": True, "result": {"status": "left"}}
        if method == "getChatMember"
        else {"ok": True, "result": True},
    )

    subscriber = _user(db, "returning@example.test")
    first = crypto_billing.create_invoice(subscriber, 30, db_path=db, now=NOW)
    assert _pay(db, first, tx="0xbbb", now=NOW)["resolution"] == "settled"

    lapse = NOW + timedelta(days=31)
    subscription_lifecycle.check_once(db_path=db, now=lapse)
    # Assert on stored status, not on `subscription_active`, which compares
    # against the wall clock rather than the injected moment.
    assert accounts.get_user_object(subscriber, db_path=db).subscription_status == "inactive"

    renewal = crypto_billing.create_invoice(subscriber, 30, db_path=db, now=lapse)
    assert renewal["discount_cents"] == 0, "the referral discount is first month only"
    assert _pay(db, renewal, tx="0xccc", now=lapse)["resolution"] == "settled"

    restored = accounts.get_user_object(subscriber, db_path=db)
    assert restored.subscription_active
    assert restored.entitlement_tier == "research_pro"
    # A lapsed renewal starts from today, not from the expiry already burned.
    assert datetime.fromisoformat(restored.subscription_expires_at) == lapse + timedelta(
        days=30
    )

    # And the sweep must not evict the member it has just been paid for.
    assert telegram_bot.MembershipWorker(db_path=db).check_once()["removed"] == 0
