"""Crypto (Arbitrum USDC/USDT) prepaid billing.

The invariant that matters most: a transfer must never activate the wrong
member. Attribution is by amount within a tolerance band, so these tests lean
hard on band separation, ambiguity, replay, and boundary conditions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spreadboard import accounts, crypto_billing  # noqa: E402


RECEIVER = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDT = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def usdc(dollars: float) -> int:
    """Dollars -> raw 6-decimal token units."""
    return int(round(dollars * 1_000_000))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", RECEIVER)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def make_user(db, email: str) -> int:
    created = accounts.create_user(
        email=email,
        display_name=email.split("@")[0],
        password="correct horse battery staple",
        subscription_status="inactive",
        subscription_days=0,
        db_path=db,
    )
    user_id = int(created["id"])
    accounts.update_subscription(user_id, status="inactive", expires_at=None, db_path=db)
    return user_id


def pay(db, invoice_amount_dollars: float, *, tx: str, log_index: int = 0, token: str = USDC, now=NOW):
    return crypto_billing.record_transfer(
        token_address=token,
        raw_units=usdc(invoice_amount_dollars),
        tx_hash=tx,
        log_index=log_index,
        from_address="0x1111111111111111111111111111111111111111",
        block_number=1000,
        db_path=db,
        now=now,
    )


# --------------------------------------------------------------------------
# Configuration and invariants
# --------------------------------------------------------------------------


def test_slot_step_exceeds_full_tolerance_band():
    # Two bands of +/-TOLERANCE overlap unless centres are more than 2x apart.
    assert crypto_billing.SLOT_STEP_CENTS > 2 * crypto_billing.TOLERANCE_CENTS


def test_prices_match_the_agreed_ladder():
    assert crypto_billing.PERIODS == {30: 18_000, 90: 45_000, 365: 165_000}


def test_receiving_address_and_rpc_come_from_env(monkeypatch):
    monkeypatch.delenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", raising=False)
    monkeypatch.delenv("SPREADBOARD_CRYPTO_RPC_URL", raising=False)
    settings = crypto_billing.config()
    assert settings.receiving_address == ""
    assert settings.rpc_url == ""
    assert settings.configured is False


def test_source_contains_no_hardcoded_receiving_address():
    source = Path(crypto_billing.__file__).read_text(encoding="utf-8")
    assert RECEIVER not in source.lower()


# --------------------------------------------------------------------------
# Invoice allocation
# --------------------------------------------------------------------------


def test_first_invoice_is_the_exact_list_price(db):
    invoice = crypto_billing.create_invoice(make_user(db, "a@example.com"), 30, db_path=db, now=NOW)
    assert invoice["amount_cents"] == 18_000
    assert invoice["slot_index"] == 0


def test_invoice_has_exact_token_specific_wallet_options(db):
    invoice = crypto_billing.create_invoice(make_user(db, "a@example.com"), 30, db_path=db, now=NOW)
    options = {option["symbol"]: option for option in invoice["payment_options"]}

    assert sorted(options) == ["USDC", "USDT"]
    assert options["USDC"]["contract_address"] == USDC.lower()
    assert options["USDC"]["amount_raw"] == "180000000"
    assert options["USDC"]["wallet_uri"] == (
        f"ethereum:{USDC.lower()}@42161/transfer"
        f"?address={RECEIVER.lower()}&uint256=180000000"
    )
    assert options["USDT"]["wallet_uri"].startswith(
        f"ethereum:{USDT.lower()}@42161/transfer?"
    )


def test_concurrent_invoices_never_share_an_overlapping_band(db):
    amounts = []
    for index in range(6):
        user_id = make_user(db, f"user{index}@example.com")
        invoice = crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
        amounts.append(invoice["amount_cents"])

    assert len(set(amounts)) == len(amounts)
    for i, first in enumerate(amounts):
        for second in amounts[i + 1:]:
            assert abs(first - second) > 2 * crypto_billing.TOLERANCE_CENTS


def test_reloading_checkout_reuses_the_same_invoice(db):
    user_id = make_user(db, "a@example.com")
    first = crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    second = crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    assert first["id"] == second["id"]
    assert first["amount_cents"] == second["amount_cents"]


def test_expired_invoice_releases_its_amount_slot(db):
    first_user = make_user(db, "a@example.com")
    first = crypto_billing.create_invoice(first_user, 30, db_path=db, now=NOW)
    later = NOW + timedelta(seconds=crypto_billing.INVOICE_WINDOW_SECONDS + 1)
    second = crypto_billing.create_invoice(make_user(db, "b@example.com"), 30, db_path=db, now=later)
    assert second["amount_cents"] == first["amount_cents"]


def test_unknown_period_is_rejected(db):
    with pytest.raises(crypto_billing.CryptoBillingError):
        crypto_billing.create_invoice(make_user(db, "a@example.com"), 45, db_path=db, now=NOW)


def test_invoice_creation_fails_closed_when_unconfigured(db, monkeypatch):
    monkeypatch.delenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", raising=False)
    with pytest.raises(crypto_billing.CryptoBillingError):
        crypto_billing.create_invoice(make_user(db, "a@example.com"), 30, db_path=db, now=NOW)


# --------------------------------------------------------------------------
# Tolerance boundaries
# --------------------------------------------------------------------------


def test_exact_payment_activates_for_the_period(db):
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    result = pay(db, 180.00, tx="0xaaa")
    assert result["resolution"] == "settled"
    user = accounts.get_user_object(user_id, db_path=db)
    assert user.subscription_status == "active"
    assert user.subscription_active is True
    assert user.subscription_expires_at.startswith("2026-08-31")


def test_underpayment_within_two_dollars_still_settles(db):
    """An exchange withdrawal fee must not cost the member their access."""
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    assert pay(db, 178.00, tx="0xaaa")["resolution"] == "settled"
    assert accounts.get_user_object(user_id, db_path=db).subscription_status == "active"


def test_underpayment_beyond_tolerance_parks_for_admin(db):
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    result = pay(db, 177.99, tx="0xaaa")
    assert result["resolution"] == "unmatched"
    assert accounts.get_user_object(user_id, db_path=db).subscription_status == "inactive"
    assert len(crypto_billing.pending_payments(db_path=db)) == 1


def test_clear_overpayment_is_honoured_when_unambiguous(db):
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    result = pay(db, 250.00, tx="0xaaa")
    assert result["resolution"] == "settled"
    assert "overpaid" in result["note"]
    assert accounts.get_user_object(user_id, db_path=db).subscription_status == "active"


def test_overpayment_matching_two_invoices_activates_nobody(db):
    first = make_user(db, "a@example.com")
    second = make_user(db, "b@example.com")
    crypto_billing.create_invoice(first, 30, db_path=db, now=NOW)
    crypto_billing.create_invoice(second, 90, db_path=db, now=NOW)
    result = pay(db, 900.00, tx="0xaaa")
    assert result["resolution"] == "ambiguous"
    for user_id in (first, second):
        assert accounts.get_user_object(user_id, db_path=db).subscription_status == "inactive"


def test_unsolicited_payment_with_no_open_invoice_parks(db):
    result = pay(db, 180.00, tx="0xaaa")
    assert result["resolution"] == "unmatched"
    assert len(crypto_billing.pending_payments(db_path=db)) == 1


# --------------------------------------------------------------------------
# Token and chain allowlisting
# --------------------------------------------------------------------------


def test_usdt_is_accepted(db):
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    assert pay(db, 180.00, tx="0xaaa", token=USDT)["resolution"] == "settled"


def test_impostor_token_contract_is_ignored(db):
    """A token calling itself USDC must not buy access."""
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    result = pay(db, 180.00, tx="0xaaa", token="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    assert result["resolution"] == "ignored"
    assert accounts.get_user_object(user_id, db_path=db).subscription_status == "inactive"
    assert crypto_billing.pending_payments(db_path=db) == []


# --------------------------------------------------------------------------
# Replay and idempotency
# --------------------------------------------------------------------------


def test_replaying_the_same_transfer_grants_nothing_extra(db):
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    first = pay(db, 180.00, tx="0xaaa")
    expiry = accounts.get_user_object(user_id, db_path=db).subscription_expires_at

    second = pay(db, 180.00, tx="0xaaa")
    assert first["resolution"] == "settled"
    assert second["resolution"] == "duplicate"
    assert accounts.get_user_object(user_id, db_path=db).subscription_expires_at == expiry


def test_distinct_logs_in_one_transaction_are_separate_payments(db):
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    assert pay(db, 180.00, tx="0xaaa", log_index=0)["resolution"] == "settled"
    assert pay(db, 180.00, tx="0xaaa", log_index=1)["resolution"] == "unmatched"


# --------------------------------------------------------------------------
# Expiry arithmetic
# --------------------------------------------------------------------------


def test_early_renewal_extends_from_existing_expiry(db):
    """Renewing with time left must not forfeit the days already paid for."""
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    pay(db, 180.00, tx="0xaaa")
    first_expiry = accounts.get_user_object(user_id, db_path=db).subscription_expires_at

    ten_days_later = NOW + timedelta(days=10)
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=ten_days_later)
    pay(db, 180.00, tx="0xbbb", now=ten_days_later)

    second = accounts.get_user_object(user_id, db_path=db).subscription_expires_at
    assert second.startswith("2026-09-30")  # 31 Aug + 30 days, not 11 Aug + 30
    assert second > first_expiry


def test_renewal_after_lapse_starts_from_today(db):
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    pay(db, 180.00, tx="0xaaa")

    much_later = NOW + timedelta(days=200)
    crypto_billing.create_invoice(user_id, 30, db_path=db, now=much_later)
    pay(db, 180.00, tx="0xbbb", now=much_later)
    expiry = accounts.get_user_object(user_id, db_path=db).subscription_expires_at
    assert expiry.startswith("2027-03-19")  # 1 Aug + 200d = 17 Feb, + 30d


def test_annual_period_grants_a_year(db):
    user_id = make_user(db, "a@example.com")
    crypto_billing.create_invoice(user_id, 365, db_path=db, now=NOW)
    assert pay(db, 1650.00, tx="0xaaa")["resolution"] == "settled"
    assert accounts.get_user_object(user_id, db_path=db).subscription_expires_at.startswith("2027-08-01")


# --------------------------------------------------------------------------
# Admin resolution
# --------------------------------------------------------------------------


def test_admin_can_settle_a_parked_payment(db):
    user_id = make_user(db, "a@example.com")
    invoice = crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    pay(db, 100.00, tx="0xaaa")
    assert accounts.get_user_object(user_id, db_path=db).subscription_status == "inactive"

    result = crypto_billing.settle_manually(invoice["id"], db_path=db, now=NOW)
    assert result["resolution"] == "settled"
    assert accounts.get_user_object(user_id, db_path=db).subscription_status == "active"


def test_manual_settlement_is_idempotent(db):
    user_id = make_user(db, "a@example.com")
    invoice = crypto_billing.create_invoice(user_id, 30, db_path=db, now=NOW)
    crypto_billing.settle_manually(invoice["id"], db_path=db, now=NOW)
    expiry = accounts.get_user_object(user_id, db_path=db).subscription_expires_at
    again = crypto_billing.settle_manually(invoice["id"], db_path=db, now=NOW)
    assert again["resolution"] == "already_paid"
    assert accounts.get_user_object(user_id, db_path=db).subscription_expires_at == expiry


# --------------------------------------------------------------------------
# Unit conversion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dollars,expected_cents",
    [(180.0, 18_000), (0.01, 1), (1650.0, 165_000), (178.0, 17_800)],
)
def test_units_to_cents(dollars, expected_cents):
    assert crypto_billing.units_to_cents(usdc(dollars), 6) == expected_cents
