"""Prepaid subscription billing settled in USDC/USDT on Arbitrum One.

Members pay a fixed amount to a receiving address the operator controls. The
server is strictly WATCH-ONLY: it never holds a key, a seed, or anything that
can spend. It observes confirmed ERC-20 ``Transfer`` logs and activates access
when one matches an open invoice.

Attribution is by amount. Because a payment settles within a tolerance band,
two concurrent invoices must never have overlapping bands -- otherwise a single
transfer could plausibly belong to either member and we would have to guess.
``SLOT_STEP_CENTS`` is therefore strictly greater than twice the tolerance, and
allocation enforces that separation against every open invoice.

The design rule throughout: when attribution is uncertain, activate nobody and
surface it for a human. Granting the wrong account is worse than a short delay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import sqlite3
from typing import Any

from . import accounts


class CryptoBillingError(RuntimeError):
    """Raised when an invoice cannot be created or settled."""


# Arbitrum One. Tokens are matched by contract address, never by symbol --
# anyone can deploy a contract that calls itself USDC.
CHAIN_ID = 42161
CHAIN_NAME = "Arbitrum One"
TOKENS: dict[str, dict[str, Any]] = {
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": {"symbol": "USDC", "decimals": 6},
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": {"symbol": "USDT", "decimals": 6},
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# period_days -> list price in cents. PERIODS remains the Research Pro alias for
# integrations which display the primary plan without a tier selector.
TIER_PERIODS: dict[str, dict[int, int]] = {
    "scanner": {30: 4_900, 90: 13_500, 365: 49_000},
    "research_pro": {30: 14_900, 90: 37_500, 365: 136_500},
}
PERIODS: dict[int, int] = TIER_PERIODS["research_pro"]

# The QR and wallet URI encode raw token units, so the received transfer can and
# should match the invoice exactly.  A fee-deducted or mistyped transfer is
# parked for admin review instead of being guessed into a tier.  One-cent slots
# keep simultaneous invoices unique without making later customers pay dollars
# more merely because another checkout is open.
TOLERANCE_CENTS = 0
SLOT_STEP_CENTS = 1
MAX_SLOTS = 100
INVOICE_WINDOW_SECONDS = 3600  # 60 minutes, then the amount slot is reusable

assert SLOT_STEP_CENTS > 2 * TOLERANCE_CENTS, "slot step must exceed the full tolerance band"


@dataclass(frozen=True)
class CryptoConfig:
    receiving_address: str
    rpc_url: str
    confirmations: int
    poll_seconds: float

    @property
    def configured(self) -> bool:
        return bool(self.receiving_address) and bool(self.rpc_url)


def config() -> CryptoConfig:
    address = os.environ.get("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", "").strip()
    if address and not _is_address(address):
        address = ""
    try:
        confirmations = int(os.environ.get("SPREADBOARD_CRYPTO_CONFIRMATIONS", "6"))
    except ValueError:
        confirmations = 6
    try:
        poll_seconds = float(os.environ.get("SPREADBOARD_CRYPTO_POLL_SECONDS", "30"))
    except ValueError:
        poll_seconds = 30.0
    return CryptoConfig(
        receiving_address=address.lower(),
        rpc_url=os.environ.get("SPREADBOARD_CRYPTO_RPC_URL", "").strip(),
        confirmations=max(1, confirmations),
        poll_seconds=max(5.0, poll_seconds),
    )


def status() -> dict[str, Any]:
    value = config()
    return {
        "provider": "crypto",
        "chain": CHAIN_NAME,
        "chain_id": CHAIN_ID,
        "tokens": sorted(token["symbol"] for token in TOKENS.values()),
        "configured": value.configured,
        "checkout_ready": value.configured,
        "watcher_ready": value.configured,
        "recurring": False,
        "confirmations": value.confirmations,
        "periods": [
            {"days": days, "amount_cents": cents, "label": format_amount(cents)}
            for days, cents in sorted(PERIODS.items())
        ],
        "tiers": {
            tier: {
                "periods": [
                    {"days": days, "amount_cents": cents, "label": format_amount(cents)}
                    for days, cents in sorted(periods.items())
                ]
            }
            for tier, periods in TIER_PERIODS.items()
        },
    }


def _is_address(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def format_amount(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def units_to_cents(raw_units: int, decimals: int) -> int:
    """Convert raw token units to whole cents, truncating sub-cent dust.

    Truncation is deterministic and loses under one cent, which is immaterial
    against a $2 tolerance.
    """
    if decimals < 2:
        raise CryptoBillingError("unsupported_token_decimals")
    return int(raw_units) // (10 ** (decimals - 2))


def payment_options(amount_cents: int, receiving_address: str) -> list[dict[str, Any]]:
    """Return exact ERC-681 token transfers for a checkout amount.

    A generic ``ethereum:<receiver>`` URI asks compatible wallets to prepare a
    native ETH transfer.  That is the wrong asset for this checkout.  Each
    option therefore targets the allowlisted token contract and carries the
    exact raw token amount in the ERC-20 ``transfer`` call.
    """
    if int(amount_cents) <= 0:
        raise CryptoBillingError("invalid_invoice_amount")
    if not _is_address(receiving_address):
        raise CryptoBillingError("invalid_receiving_address")

    options = []
    for contract, token in TOKENS.items():
        decimals = int(token["decimals"])
        amount_raw = int(amount_cents) * (10 ** (decimals - 2))
        options.append({
            "symbol": str(token["symbol"]),
            "contract_address": contract,
            "decimals": decimals,
            "amount_raw": str(amount_raw),
            "wallet_uri": (
                f"ethereum:{contract}@{CHAIN_ID}/transfer"
                f"?address={receiving_address.lower()}&uint256={amount_raw}"
            ),
        })
    return sorted(options, key=lambda option: option["symbol"])


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


def _open_invoice_rows(connection: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM crypto_invoices WHERE status = 'open' AND expires_at > ? "
            "ORDER BY expected_amount_cents",
            (now_iso,),
        )
    )


def _allocate_amount(taken: list[int], list_amount_cents: int) -> tuple[int, int]:
    """Pick the lowest slot whose tolerance band clears every open invoice.

    Two bands overlap when their centres are within ``2 * TOLERANCE_CENTS``, so
    a candidate is only safe once it is strictly further than that from every
    amount already in flight.
    """
    for slot in range(MAX_SLOTS):
        candidate = list_amount_cents + slot * SLOT_STEP_CENTS
        if all(abs(candidate - other) > 2 * TOLERANCE_CENTS for other in taken):
            return slot, candidate
    raise CryptoBillingError("no_free_amount_slot")


def expire_stale_invoices(*, db_path=accounts.DEFAULT_DB_PATH, now: datetime | None = None) -> int:
    now_iso = accounts._utc_iso(now)
    connection = accounts._connect(db_path)
    try:
        cursor = connection.execute(
            "UPDATE crypto_invoices SET status = 'expired' WHERE status = 'open' AND expires_at <= ?",
            (now_iso,),
        )
        connection.commit()
        return int(cursor.rowcount or 0)
    finally:
        connection.close()


def create_invoice(
    user_id: int,
    period_days: int,
    *,
    tier: str = "research_pro",
    db_path=accounts.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    periods = TIER_PERIODS.get(str(tier))
    if periods is None:
        raise CryptoBillingError("unknown_subscription_tier")
    if period_days not in periods:
        raise CryptoBillingError("unknown_period")
    settings = config()
    if not settings.configured:
        raise CryptoBillingError("crypto_billing_not_configured")

    moment = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    now_iso = accounts._utc_iso(moment)
    expires_iso = accounts._utc_iso(moment + timedelta(seconds=INVOICE_WINDOW_SECONDS))
    list_amount = periods[period_days]

    connection = accounts._connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        member = connection.execute(
            """SELECT role, subscription_status, subscription_expires_at, subscription_tier
               FROM users WHERE id = ?""",
            (int(user_id),),
        ).fetchone()
        if member is None:
            raise CryptoBillingError("user_not_found")
        expiry = None
        try:
            expiry = datetime.fromisoformat(
                str(member["subscription_expires_at"] or "").replace("Z", "+00:00")
            )
        except ValueError:
            expiry = None
        active_now = (
            str(member["role"]) == "admin"
            or (
                str(member["subscription_status"]) in {"active", "trialing"}
                and (expiry is None or expiry > moment)
            )
        )
        if active_now and str(member["subscription_tier"]) != tier:
            # A user row stores one tier and one expiry. Applying a different
            # tier now would silently re-label the unused part of the current
            # prepaid term. Keep renewal exact until queued grants exist.
            raise CryptoBillingError("tier_change_available_after_current_term")
        connection.execute(
            "UPDATE crypto_invoices SET status = 'expired' WHERE status = 'open' AND expires_at <= ?",
            (now_iso,),
        )
        open_rows = _open_invoice_rows(connection, now_iso)

        # Reuse this member's own live invoice for the same period rather than
        # burning a second amount slot on someone who simply reloaded the page.
        for row in open_rows:
            if (
                int(row["user_id"]) == int(user_id)
                and int(row["period_days"]) == period_days
                and str(row["subscription_tier"]) == tier
            ):
                connection.commit()
                return _invoice_dict(row)

        taken = [int(row["expected_amount_cents"]) for row in open_rows]
        slot, expected = _allocate_amount(taken, list_amount)
        cursor = connection.execute(
            "INSERT INTO crypto_invoices (user_id, period_days, subscription_tier, list_amount_cents, slot_index, "
            "expected_amount_cents, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (user_id, period_days, tier, list_amount, slot, expected, now_iso, expires_iso),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM crypto_invoices WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _invoice_dict(row)
    finally:
        connection.close()


def _invoice_dict(row: sqlite3.Row) -> dict[str, Any]:
    settings = config()
    amount_cents = int(row["expected_amount_cents"])
    return {
        "id": int(row["id"]),
        "user_id": int(row["user_id"]),
        "period_days": int(row["period_days"]),
        "subscription_tier": str(row["subscription_tier"]),
        "status": str(row["status"]),
        "amount_cents": amount_cents,
        "amount_display": f"{amount_cents / 100:.2f}",
        "list_amount_cents": int(row["list_amount_cents"]),
        "slot_index": int(row["slot_index"]),
        "tolerance_display": f"{TOLERANCE_CENTS / 100:.2f}",
        "receiving_address": settings.receiving_address,
        "chain": CHAIN_NAME,
        "chain_id": CHAIN_ID,
        "tokens": sorted(token["symbol"] for token in TOKENS.values()),
        "payment_options": payment_options(amount_cents, settings.receiving_address),
        "created_at": str(row["created_at"]),
        "expires_at": str(row["expires_at"]),
        "settled_at": row["settled_at"],
        "tx_hash": row["tx_hash"],
    }


def get_invoice(invoice_id: int, *, db_path=accounts.DEFAULT_DB_PATH) -> dict[str, Any] | None:
    connection = accounts._connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM crypto_invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        return _invoice_dict(row) if row else None
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


def _next_expiry(user: Any, period_days: int, moment: datetime) -> str:
    """Extend from the member's current expiry when it is still in the future.

    Renewing early must never cost someone the days they already paid for.
    """
    base = moment
    if isinstance(user, dict):
        current = user.get("subscription_expires_at")
    else:
        current = getattr(user, "subscription_expires_at", None)
    if current:
        try:
            parsed = datetime.fromisoformat(str(current).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed > moment:
                base = parsed
        except ValueError:
            base = moment
    return accounts._utc_iso(base + timedelta(days=period_days))


def record_transfer(
    *,
    token_address: str,
    raw_units: int,
    tx_hash: str,
    log_index: int,
    from_address: str,
    block_number: int,
    db_path=accounts.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one confirmed on-chain transfer. Idempotent per (tx_hash, log_index)."""
    token = TOKENS.get(str(token_address).lower())
    if token is None:
        return {"resolution": "ignored", "reason": "token_not_allowlisted"}

    moment = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    now_iso = accounts._utc_iso(moment)
    amount_cents = units_to_cents(raw_units, int(token["decimals"]))
    tx_hash = str(tx_hash).lower()
    from_address = str(from_address).lower()

    connection = accounts._connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        seen = connection.execute(
            "SELECT resolution, invoice_id FROM crypto_payments WHERE tx_hash = ? AND log_index = ?",
            (tx_hash, log_index),
        ).fetchone()
        if seen is not None:
            connection.rollback()
            return {
                "resolution": "duplicate",
                "previous": str(seen["resolution"]),
                "invoice_id": seen["invoice_id"],
            }

        connection.execute(
            "UPDATE crypto_invoices SET status = 'expired' WHERE status = 'open' AND expires_at <= ?",
            (now_iso,),
        )
        open_rows = _open_invoice_rows(connection, now_iso)

        exact = [
            r for r in open_rows
            if int(r["expected_amount_cents"]) == amount_cents
        ]
        if len(exact) == 1:
            chosen, note = exact[0], ""
        elif len(exact) > 1:
            chosen, note = None, "multiple open invoices matched this amount"
        else:
            chosen, note = None, "no open invoice matched the exact amount"

        if chosen is None:
            resolution = "ambiguous" if len(exact) > 1 else "unmatched"
            connection.execute(
                "INSERT INTO crypto_payments (tx_hash, log_index, token, from_address, amount_cents, "
                "block_number, invoice_id, resolution, note, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (tx_hash, log_index, token["symbol"], from_address, amount_cents,
                 block_number, resolution, note, now_iso),
            )
            connection.commit()
            return {"resolution": resolution, "amount_cents": amount_cents, "note": note}

        invoice_id = int(chosen["id"])
        user_id = int(chosen["user_id"])
        user = connection.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            raise CryptoBillingError("invoice_user_missing")
        expires_at = _next_expiry(dict(user), int(chosen["period_days"]), moment)
        connection.execute(
            "UPDATE crypto_invoices SET status = 'paid', token = ?, tx_hash = ?, from_address = ?, "
            "paid_amount_cents = ?, block_number = ?, settled_at = ? WHERE id = ?",
            (token["symbol"], tx_hash, from_address, amount_cents, block_number, now_iso, invoice_id),
        )
        connection.execute(
            "INSERT INTO crypto_payments (tx_hash, log_index, token, from_address, amount_cents, "
            "block_number, invoice_id, resolution, note, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'settled', ?, ?)",
            (tx_hash, log_index, token["symbol"], from_address, amount_cents,
             block_number, invoice_id, note, now_iso),
        )
        connection.execute(
            "UPDATE users SET subscription_status = 'active', subscription_expires_at = ?, "
            "subscription_tier = ?, updated_at = ? WHERE id = ?",
            (expires_at, str(chosen["subscription_tier"]), now_iso, user_id),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "resolution": "settled",
        "invoice_id": invoice_id,
        "user_id": user_id,
        "amount_cents": amount_cents,
        "period_days": int(chosen["period_days"]),
        "subscription_tier": str(chosen["subscription_tier"]),
        "expires_at": expires_at,
        "note": note,
    }


def settle_manually(
    invoice_id: int,
    *,
    db_path=accounts.DEFAULT_DB_PATH,
    now: datetime | None = None,
    note: str = "manual admin settlement",
) -> dict[str, Any]:
    """Grant an invoice's period by hand, for payments the watcher parked."""
    moment = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    now_iso = accounts._utc_iso(moment)
    connection = accounts._connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM crypto_invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        if row is None:
            raise CryptoBillingError("invoice_not_found")
        if str(row["status"]) == "paid":
            return {"resolution": "already_paid", "invoice_id": invoice_id}
        user = connection.execute(
            "SELECT * FROM users WHERE id = ?", (int(row["user_id"]),)
        ).fetchone()
        if user is None:
            raise CryptoBillingError("invoice_user_missing")
        expires_at = _next_expiry(dict(user), int(row["period_days"]), moment)
        connection.execute(
            "UPDATE crypto_invoices SET status = 'paid', settled_at = ? WHERE id = ?",
            (now_iso, invoice_id),
        )
        connection.execute(
            "UPDATE users SET subscription_status = 'active', subscription_expires_at = ?, "
            "subscription_tier = ?, updated_at = ? WHERE id = ?",
            (expires_at, str(row["subscription_tier"]), now_iso, int(row["user_id"])),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "resolution": "settled",
        "invoice_id": invoice_id,
        "user_id": int(row["user_id"]),
        "period_days": int(row["period_days"]),
        "subscription_tier": str(row["subscription_tier"]),
        "expires_at": expires_at,
        "note": note,
    }


def pending_payments(*, db_path=accounts.DEFAULT_DB_PATH, limit: int = 100) -> list[dict[str, Any]]:
    """Transfers the watcher could not attribute, newest first, for the admin queue."""
    connection = accounts._connect(db_path)
    try:
        rows = connection.execute(
            "SELECT * FROM crypto_payments WHERE resolution IN ('unmatched', 'ambiguous') "
            "ORDER BY observed_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {
                "tx_hash": str(r["tx_hash"]),
                "log_index": int(r["log_index"]),
                "token": str(r["token"]),
                "from_address": str(r["from_address"]),
                "amount_cents": int(r["amount_cents"]),
                "amount_display": format_amount(int(r["amount_cents"])),
                "block_number": int(r["block_number"]),
                "resolution": str(r["resolution"]),
                "note": str(r["note"]),
                "observed_at": str(r["observed_at"]),
            }
            for r in rows
        ]
    finally:
        connection.close()
