"""Durable first-party affiliate attribution for manual crypto renewals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from spreadboard import accounts, affiliates, crypto_billing, server


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
RECEIVER = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
PAYOUT = "0x1111111111111111111111111111111111111111"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadarbitrage.ink")
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", RECEIVER)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def _user(db, email: str) -> int:
    return int(accounts.create_user(
        email=email,
        display_name=email.split("@")[0],
        password="correct horse battery staple",
        subscription_status="inactive",
        subscription_days=0,
        db_path=db,
    )["id"])


def _partner(db, slug: str = "youtube-channel") -> tuple[int, dict]:
    user_id = _user(db, f"{slug}@example.test")
    return user_id, affiliates.create_partner(
        user_id, slug=slug, display_name="YouTube Channel", db_path=db
    )


def _attribute(db, partner_slug: str = "youtube-channel") -> tuple[int, int, str]:
    partner_user_id, partner = _partner(db, partner_slug)
    _partner_row, token = affiliates.create_click(partner_slug, db_path=db, now=NOW)
    referred_user_id = _user(db, f"viewer-{partner_slug}@example.test")
    attached = affiliates.attach_registration(
        referred_user_id, token, db_path=db, now=NOW
    )
    assert attached and attached["id"] == partner["id"]
    return partner_user_id, referred_user_id, token


def test_referral_token_is_opaque_hashed_and_expires(db) -> None:
    _partner(db)
    _partner_row, token = affiliates.create_click("youtube-channel", db_path=db, now=NOW)

    connection = sqlite3.connect(db)
    stored = connection.execute("SELECT token_hash FROM affiliate_clicks").fetchone()[0]
    connection.close()

    assert token not in stored
    assert affiliates.valid_click_token(token, db_path=db, now=NOW + timedelta(days=89))
    assert not affiliates.valid_click_token(token, db_path=db, now=NOW + timedelta(days=91))


def test_first_qualifying_registration_is_fixed(db) -> None:
    _, first = _partner(db, "first-channel")
    _, second = _partner(db, "second-channel")
    _, first_token = affiliates.create_click("first-channel", db_path=db, now=NOW)
    _, second_token = affiliates.create_click("second-channel", db_path=db, now=NOW)
    viewer = _user(db, "viewer@example.test")

    affiliates.attach_registration(viewer, first_token, db_path=db, now=NOW)
    attached = affiliates.attach_registration(viewer, second_token, db_path=db, now=NOW)

    assert attached and attached["id"] == first["id"]
    assert attached["id"] != second["id"]


@pytest.mark.parametrize(
    "tier,period_days,expected_discount",
    [("scanner", 30, 980), ("scanner", 365, 980), ("research_pro", 30, 2_980), ("research_pro", 365, 2_980)],
)
def test_first_invoice_discount_is_one_month_only(
    db, tier: str, period_days: int, expected_discount: int
) -> None:
    _partner_user, viewer, _token = _attribute(db, f"{tier.replace('_', '-')}-{period_days}")

    invoice = crypto_billing.create_invoice(
        viewer, period_days, tier=tier, db_path=db, now=NOW
    )

    assert invoice["discount_cents"] == expected_discount
    assert invoice["amount_cents"] == invoice["list_amount_cents"] - expected_discount


def test_settlement_creates_one_commission_and_later_renewals_stay_attributed(db) -> None:
    partner_user, viewer, _token = _attribute(db)
    first = crypto_billing.create_invoice(
        viewer, 30, tier="research_pro", db_path=db, now=NOW
    )
    crypto_billing.settle_manually(first["id"], db_path=db, now=NOW)
    crypto_billing.settle_manually(first["id"], db_path=db, now=NOW)

    later = NOW + timedelta(days=10)
    second = crypto_billing.create_invoice(
        viewer, 30, tier="research_pro", db_path=db, now=later
    )
    crypto_billing.settle_manually(second["id"], db_path=db, now=later)
    summary = affiliates.partner_summary(partner_user, db_path=db, now=later)

    assert summary is not None
    commissions = sorted(summary["commissions"], key=lambda row: row["invoice_id"])
    assert len(commissions) == 2
    assert commissions[0]["discount_cents"] == 2_980
    assert commissions[0]["commission_base_cents"] == 11_920
    assert commissions[0]["commission_cents"] == 5_960
    assert commissions[1]["discount_cents"] == 0
    assert commissions[1]["commission_cents"] == 7_450


def test_only_one_open_invoice_can_reserve_the_first_month_discount(db) -> None:
    _partner_user, viewer, _token = _attribute(db)

    first = crypto_billing.create_invoice(viewer, 30, db_path=db, now=NOW)
    alternate = crypto_billing.create_invoice(viewer, 90, db_path=db, now=NOW)

    assert first["discount_cents"] == 2_980
    assert alternate["discount_cents"] == 0


def test_existing_attribution_survives_a_paused_public_link(db) -> None:
    partner_user, viewer, _token = _attribute(db)
    connection = accounts._connect(db)
    connection.execute("UPDATE affiliate_partners SET status = 'paused'")
    connection.commit()
    connection.close()

    invoice = crypto_billing.create_invoice(viewer, 30, db_path=db, now=NOW)
    crypto_billing.settle_manually(invoice["id"], db_path=db, now=NOW)

    assert len(affiliates.partner_summary(partner_user, db_path=db)["commissions"]) == 1


def test_owner_can_pause_new_clicks_without_losing_the_ledger(db) -> None:
    partner_user, viewer, _token = _attribute(db)
    partner = affiliates.partner_for_user(partner_user, db_path=db)
    invoice = crypto_billing.create_invoice(viewer, 30, db_path=db, now=NOW)
    crypto_billing.settle_manually(invoice["id"], db_path=db, now=NOW)

    affiliates.update_partner_status(partner["id"], status="paused", db_path=db)

    with pytest.raises(ValueError, match="partner_not_found"):
        affiliates.create_click(partner["slug"], db_path=db, now=NOW)
    owner_summary = affiliates.partner_summary_for_id(partner["id"], db_path=db)
    assert owner_summary is not None
    assert owner_summary["partner"]["status"] == "paused"
    assert len(owner_summary["commissions"]) == 1


def test_weekly_payout_requires_and_snapshots_the_partner_wallet(db) -> None:
    partner_user, viewer, _token = _attribute(db)
    partner = affiliates.partner_for_user(partner_user, db_path=db)
    invoice = crypto_billing.create_invoice(viewer, 30, db_path=db, now=NOW)
    crypto_billing.settle_manually(invoice["id"], db_path=db, now=NOW)

    with pytest.raises(ValueError, match="payout_profile_required"):
        affiliates.create_payout_batch(
            partner["id"], db_path=db, now=NOW + timedelta(days=8)
        )

    affiliates.save_payout_profile(
        partner_user,
        asset="USDC",
        network="Arbitrum",
        destination=PAYOUT,
        db_path=db,
    )
    batch = affiliates.create_payout_batch(
        partner["id"], db_path=db, now=NOW + timedelta(days=8)
    )
    paid = affiliates.mark_payout_paid(
        batch["id"], payment_reference="0xabc123", db_path=db, now=NOW + timedelta(days=8)
    )

    assert batch["payout_asset"] == "USDC"
    assert batch["payout_network"] == "Arbitrum"
    assert batch["payout_destination"] == PAYOUT
    assert paid["status"] == "paid"
    summary = affiliates.partner_summary(partner_user, db_path=db, now=NOW + timedelta(days=8))
    assert summary["metrics"]["paid"] == 5_960


def test_referral_offer_and_affiliate_terms_are_visible() -> None:
    pricing = server.render_pricing_page({"referred": ["1"]})
    terms = server.render_legal_page("affiliate")

    assert "Your channel discount is saved" in pricing
    assert "20% off the first 30-day" in pricing
    assert "50% of subscription plan revenue" in terms
    assert "in the video and in the description near the link" in terms
