"""First-party affiliate attribution and manual weekly crypto payouts.

The browser receives an opaque referral token. Only its SHA-256 digest is
stored, and registration converts it into a durable user-to-partner
attribution. Commissions are derived server-side from settled crypto invoices;
the browser can never submit an amount or create an earning.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from . import accounts

REFERRAL_COOKIE = "spreadboard_ref"
DEFAULT_ATTRIBUTION_DAYS = 90
DEFAULT_DISCOUNT_BPS = 2_000
DEFAULT_COMMISSION_BPS = 5_000
DEFAULT_PAYOUT_HOLD_DAYS = 7
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _moment(value: datetime | None = None) -> datetime:
    return (value or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def public_url(slug: str) -> str:
    base = os.environ.get("SPREADBOARD_PUBLIC_URL", "https://spreadarbitrage.ink").rstrip("/")
    return f"{base}/r/{slug}"


def _slug_from_name(display_name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode()
    clean = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")
    if len(clean) < 3:
        clean = f"{clean or 'partner'}-channel"
    return clean[:64].rstrip("-")


def validate_partner(*, slug: str = "", display_name: str) -> tuple[str, str]:
    """Validate a partner name and generate a readable link slug when omitted."""
    clean_name = display_name.strip()
    if not clean_name or len(clean_name) > 100:
        raise ValueError("invalid_partner_name")
    clean_slug = slug.strip().casefold() or _slug_from_name(clean_name)
    if not _SLUG_RE.fullmatch(clean_slug):
        raise ValueError("invalid_partner_slug")
    return clean_slug, clean_name


def available_partner_slug(
    *, slug: str = "", display_name: str, db_path=accounts.DEFAULT_DB_PATH
) -> tuple[str, str]:
    """Return a unique public slug, suffixing auto-generated collisions."""
    clean_slug, clean_name = validate_partner(slug=slug, display_name=display_name)
    if slug.strip():
        if slug_exists(clean_slug, db_path=db_path):
            raise ValueError("partner_or_slug_already_exists")
        return clean_slug, clean_name
    base = clean_slug
    suffix = 1
    while slug_exists(clean_slug, db_path=db_path):
        suffix += 1
        marker = f"-{suffix}"
        clean_slug = f"{base[:64 - len(marker)].rstrip('-')}{marker}"
    return clean_slug, clean_name


def slug_exists(slug: str, *, db_path=accounts.DEFAULT_DB_PATH) -> bool:
    """Include paused and closed partners when enforcing unique public links."""
    connection = accounts._connect(db_path)
    try:
        return connection.execute(
            "SELECT 1 FROM affiliate_partners WHERE slug = ? COLLATE NOCASE",
            (slug.strip(),),
        ).fetchone() is not None
    finally:
        connection.close()


def create_partner(
    user_id: int,
    *,
    slug: str,
    display_name: str,
    discount_bps: int = DEFAULT_DISCOUNT_BPS,
    commission_bps: int = DEFAULT_COMMISSION_BPS,
    attribution_days: int = DEFAULT_ATTRIBUTION_DAYS,
    payout_hold_days: int = DEFAULT_PAYOUT_HOLD_DAYS,
    db_path=accounts.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    clean_slug, clean_name = validate_partner(slug=slug, display_name=display_name)
    if not 0 <= int(discount_bps) <= 10_000 or not 0 <= int(commission_bps) <= 10_000:
        raise ValueError("invalid_partner_rate")
    if not 1 <= int(attribution_days) <= 3650 or not 0 <= int(payout_hold_days) <= 365:
        raise ValueError("invalid_partner_window")
    now_iso = accounts._utc_iso(_moment())
    connection = accounts._connect(db_path)
    try:
        if connection.execute("SELECT 1 FROM users WHERE id = ?", (int(user_id),)).fetchone() is None:
            raise ValueError("user_not_found")
        try:
            cursor = connection.execute(
                """INSERT INTO affiliate_partners (
                       user_id, slug, display_name, status, discount_bps,
                       commission_bps, attribution_days, payout_hold_days,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
                (
                    int(user_id), clean_slug, clean_name, int(discount_bps),
                    int(commission_bps), int(attribution_days),
                    int(payout_hold_days), now_iso, now_iso,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("partner_or_slug_already_exists") from exc
        connection.commit()
        row = connection.execute(
            "SELECT * FROM affiliate_partners WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _partner_dict(row)
    finally:
        connection.close()


def _partner_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    result = dict(row)
    result["id"] = int(result["id"])
    result["user_id"] = int(result["user_id"])
    result["referral_url"] = public_url(str(result["slug"]))
    return result


def partner_for_slug(slug: str, *, db_path=accounts.DEFAULT_DB_PATH) -> dict[str, Any] | None:
    connection = accounts._connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM affiliate_partners WHERE slug = ? COLLATE NOCASE AND status = 'active'",
            (slug.strip(),),
        ).fetchone()
        return _partner_dict(row) if row is not None else None
    finally:
        connection.close()


def partner_for_user(user_id: int, *, db_path=accounts.DEFAULT_DB_PATH) -> dict[str, Any] | None:
    connection = accounts._connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM affiliate_partners WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        return _partner_dict(row) if row is not None else None
    finally:
        connection.close()


def update_partner_status(
    partner_id: int,
    *,
    status: str,
    db_path=accounts.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    clean_status = status.strip().casefold()
    if clean_status not in {"active", "paused", "closed"}:
        raise ValueError("invalid_partner_status")
    connection = accounts._connect(db_path)
    try:
        cursor = connection.execute(
            "UPDATE affiliate_partners SET status = ?, updated_at = ? WHERE id = ?",
            (clean_status, accounts._utc_iso(), int(partner_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("partner_not_found")
        connection.commit()
        row = connection.execute(
            "SELECT * FROM affiliate_partners WHERE id = ?", (int(partner_id),)
        ).fetchone()
        return _partner_dict(row)
    finally:
        connection.close()


def save_payout_profile(
    user_id: int,
    *,
    asset: str = "USDT",
    network: str,
    destination: str,
    db_path=accounts.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Save a partner-owned Arbitrum stablecoin payout destination."""
    requested_asset = asset.strip().upper() or "USDT"
    clean_asset = "USDT"
    clean_network = network.strip()
    clean_destination = destination.strip()
    if requested_asset != "USDT":
        raise ValueError("invalid_payout_asset")
    if clean_network.casefold() != "arbitrum":
        raise ValueError("invalid_payout_network")
    if not _EVM_ADDRESS_RE.fullmatch(clean_destination):
        raise ValueError("invalid_payout_destination")
    now_iso = accounts._utc_iso()
    connection = accounts._connect(db_path)
    try:
        cursor = connection.execute(
            """UPDATE affiliate_partners
               SET payout_asset = ?, payout_network = 'Arbitrum',
                   payout_destination = ?, payout_updated_at = ?, updated_at = ?
               WHERE user_id = ?""",
            (clean_asset, clean_destination, now_iso, now_iso, int(user_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("partner_account_required")
        connection.commit()
        row = connection.execute(
            "SELECT * FROM affiliate_partners WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        return _partner_dict(row)
    finally:
        connection.close()


def create_click(
    slug: str,
    *,
    landing_path: str = "/pricing",
    db_path=accounts.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    moment = _moment(now)
    connection = accounts._connect(db_path)
    try:
        partner = connection.execute(
            "SELECT * FROM affiliate_partners WHERE slug = ? COLLATE NOCASE AND status = 'active'",
            (slug.strip(),),
        ).fetchone()
        if partner is None:
            raise ValueError("partner_not_found")
        raw = secrets.token_urlsafe(32)
        safe_path = landing_path if landing_path.startswith("/") and not landing_path.startswith("//") else "/pricing"
        connection.execute(
            """INSERT INTO affiliate_clicks (
                   partner_id, token_hash, landing_path, clicked_at, expires_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                int(partner["id"]), _token_hash(raw), safe_path,
                accounts._utc_iso(moment),
                accounts._utc_iso(moment + timedelta(days=int(partner["attribution_days"]))),
            ),
        )
        connection.commit()
        return _partner_dict(partner), raw
    finally:
        connection.close()


def valid_click_token(
    raw_token: str,
    *,
    db_path=accounts.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> bool:
    """Whether a first-party referral cookie is still safe to preserve."""
    if not raw_token:
        return False
    connection = accounts._connect(db_path)
    try:
        return connection.execute(
            """SELECT 1 FROM affiliate_clicks c
               JOIN affiliate_partners p ON p.id = c.partner_id
               WHERE c.token_hash = ? AND c.expires_at > ? AND p.status = 'active'""",
            (_token_hash(raw_token), accounts._utc_iso(_moment(now))),
        ).fetchone() is not None
    finally:
        connection.close()


def attach_registration(
    user_id: int,
    raw_token: str,
    *,
    db_path=accounts.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not raw_token:
        return None
    moment = _moment(now)
    now_iso = accounts._utc_iso(moment)
    connection = accounts._connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT partner_id FROM affiliate_attributions WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        if existing is not None:
            connection.commit()
            row = connection.execute(
                "SELECT * FROM affiliate_partners WHERE id = ?", (existing["partner_id"],)
            ).fetchone()
            return _partner_dict(row) if row is not None else None
        click = connection.execute(
            """SELECT c.*, p.status AS partner_status
               FROM affiliate_clicks c
               JOIN affiliate_partners p ON p.id = c.partner_id
               WHERE c.token_hash = ? AND c.expires_at > ?""",
            (_token_hash(raw_token), now_iso),
        ).fetchone()
        if click is None or str(click["partner_status"]) != "active":
            connection.commit()
            return None
        connection.execute(
            """INSERT INTO affiliate_attributions (
                   user_id, partner_id, click_id, attributed_at
               ) VALUES (?, ?, ?, ?)""",
            (int(user_id), int(click["partner_id"]), int(click["id"]), now_iso),
        )
        connection.execute(
            """UPDATE affiliate_clicks
               SET registered_user_id = COALESCE(registered_user_id, ?),
                   registered_at = COALESCE(registered_at, ?)
               WHERE id = ?""",
            (int(user_id), now_iso, int(click["id"])),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM affiliate_partners WHERE id = ?", (click["partner_id"],)
        ).fetchone()
        return _partner_dict(row) if row is not None else None
    finally:
        connection.close()


def invoice_offer(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    tier: str,
    monthly_list_amount_cents: int,
) -> dict[str, int | str | None]:
    row = connection.execute(
        """SELECT p.* FROM affiliate_attributions a
           JOIN affiliate_partners p ON p.id = a.partner_id
           WHERE a.user_id = ?""",
        (int(user_id),),
    ).fetchone()
    if row is None:
        return {"partner_id": None, "discount_cents": 0, "partner_slug": None}
    used_or_reserved = connection.execute(
        """SELECT 1 FROM crypto_invoices
           WHERE user_id = ? AND (
             status = 'paid' OR (status = 'open' AND discount_cents > 0)
           ) LIMIT 1""",
        (int(user_id),),
    ).fetchone()
    discount = (
        0 if used_or_reserved is not None
        else _bps(int(monthly_list_amount_cents), int(row["discount_bps"]))
    )
    return {
        "partner_id": int(row["id"]),
        "discount_cents": discount,
        "partner_slug": str(row["slug"]),
    }


def _bps(amount_cents: int, bps: int) -> int:
    return (int(amount_cents) * int(bps) + 5_000) // 10_000


def record_settled_commission(
    connection: sqlite3.Connection,
    invoice: sqlite3.Row | dict[str, Any],
    *,
    settled_at: datetime,
) -> dict[str, Any] | None:
    partner_id = invoice["affiliate_partner_id"]
    if partner_id is None:
        return None
    partner = connection.execute(
        "SELECT * FROM affiliate_partners WHERE id = ?", (int(partner_id),)
    ).fetchone()
    if partner is None:
        return None
    list_amount = int(invoice["list_amount_cents"])
    discount = int(invoice["discount_cents"] or 0)
    base = max(0, list_amount - discount)
    commission = _bps(base, int(partner["commission_bps"]))
    earned = accounts._utc_iso(settled_at)
    available = accounts._utc_iso(
        settled_at + timedelta(days=int(partner["payout_hold_days"]))
    )
    connection.execute(
        """INSERT OR IGNORE INTO affiliate_commissions (
               partner_id, referred_user_id, invoice_id, subscription_tier,
               period_days, list_amount_cents, discount_cents,
               commission_base_cents, commission_bps, commission_cents,
               status, earned_at, available_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (
            int(partner_id), int(invoice["user_id"]), int(invoice["id"]),
            str(invoice["subscription_tier"]), int(invoice["period_days"]),
            list_amount, discount, base, int(partner["commission_bps"]),
            commission, earned, available,
        ),
    )
    connection.execute(
        "UPDATE affiliate_attributions SET first_payment_at = COALESCE(first_payment_at, ?) WHERE user_id = ?",
        (earned, int(invoice["user_id"])),
    )
    row = connection.execute(
        "SELECT * FROM affiliate_commissions WHERE invoice_id = ?", (int(invoice["id"]),)
    ).fetchone()
    return dict(row) if row is not None else None


def partner_summary(user_id: int, *, db_path=accounts.DEFAULT_DB_PATH, now: datetime | None = None) -> dict[str, Any] | None:
    moment = _moment(now)
    connection = accounts._connect(db_path)
    try:
        partner = connection.execute(
            "SELECT * FROM affiliate_partners WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        if partner is None:
            return None
        partner_id = int(partner["id"])
        now_iso = accounts._utc_iso(moment)
        counts = connection.execute(
            """SELECT
                 (SELECT COUNT(*) FROM affiliate_clicks WHERE partner_id = ?) AS clicks,
                 (SELECT COUNT(*) FROM affiliate_attributions WHERE partner_id = ?) AS registrations,
                 (SELECT COUNT(*) FROM affiliate_attributions WHERE partner_id = ? AND first_payment_at IS NOT NULL) AS customers,
                 COALESCE(SUM(CASE WHEN status != 'void' THEN commission_base_cents ELSE 0 END), 0) AS referred_revenue,
                 COALESCE(SUM(CASE WHEN status = 'pending' AND available_at <= ? THEN commission_cents ELSE 0 END), 0) AS payable,
                 COALESCE(SUM(CASE WHEN status = 'pending' AND available_at > ? THEN commission_cents ELSE 0 END), 0) AS on_hold,
                 COALESCE(SUM(CASE WHEN status = 'in_batch' THEN commission_cents ELSE 0 END), 0) AS batched,
                 COALESCE(SUM(CASE WHEN status = 'paid' THEN commission_cents ELSE 0 END), 0) AS paid
               FROM affiliate_commissions WHERE partner_id = ?""",
            (partner_id, partner_id, partner_id, now_iso, now_iso, partner_id),
        ).fetchone()
        commissions = [dict(row) for row in connection.execute(
            """SELECT id, invoice_id, subscription_tier, period_days,
                      list_amount_cents, discount_cents, commission_base_cents,
                      commission_cents, status, earned_at, available_at, paid_at
               FROM affiliate_commissions WHERE partner_id = ?
               ORDER BY earned_at DESC LIMIT 250""",
            (partner_id,),
        ).fetchall()]
        payouts = [dict(row) for row in connection.execute(
            "SELECT * FROM affiliate_payout_batches WHERE partner_id = ? ORDER BY created_at DESC LIMIT 100",
            (partner_id,),
        ).fetchall()]
        return {
            "partner": _partner_dict(partner),
            "metrics": dict(counts) if counts is not None else {},
            "commissions": commissions,
            "payouts": payouts,
        }
    finally:
        connection.close()


def partner_summary_for_id(
    partner_id: int,
    *,
    db_path=accounts.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Owner lookup without exposing the referred member's personal data."""
    connection = accounts._connect(db_path)
    try:
        row = connection.execute(
            "SELECT user_id FROM affiliate_partners WHERE id = ?", (int(partner_id),)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return partner_summary(int(row["user_id"]), db_path=db_path, now=now)


def list_partners(*, db_path=accounts.DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    connection = accounts._connect(db_path)
    try:
        rows = connection.execute(
            """SELECT p.*, u.email,
                      (SELECT COUNT(*) FROM affiliate_attributions a WHERE a.partner_id = p.id) AS registrations,
                      (SELECT COUNT(*) FROM affiliate_attributions a WHERE a.partner_id = p.id AND a.first_payment_at IS NOT NULL) AS customers,
                      (SELECT COALESCE(SUM(c.commission_cents), 0) FROM affiliate_commissions c WHERE c.partner_id = p.id AND c.status = 'pending') AS pending_cents,
                      (SELECT COALESCE(SUM(c.commission_cents), 0) FROM affiliate_commissions c WHERE c.partner_id = p.id AND c.status = 'paid') AS paid_cents,
                      (SELECT b.id FROM affiliate_payout_batches b WHERE b.partner_id = p.id AND b.status = 'draft' ORDER BY b.created_at DESC LIMIT 1) AS draft_batch_id,
                      (SELECT b.amount_cents FROM affiliate_payout_batches b WHERE b.partner_id = p.id AND b.status = 'draft' ORDER BY b.created_at DESC LIMIT 1) AS draft_batch_amount_cents
               FROM affiliate_partners p
               JOIN users u ON u.id = p.user_id
               ORDER BY p.created_at DESC"""
        ).fetchall()
        return [_partner_dict(row) | {"email": row["email"]} for row in rows]
    finally:
        connection.close()


def create_payout_batch(
    partner_id: int,
    *,
    db_path=accounts.DEFAULT_DB_PATH,
    now: datetime | None = None,
    note: str = "",
) -> dict[str, Any]:
    moment = _moment(now)
    now_iso = accounts._utc_iso(moment)
    connection = accounts._connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        partner = connection.execute(
            "SELECT * FROM affiliate_partners WHERE id = ?", (int(partner_id),)
        ).fetchone()
        if partner is None:
            raise ValueError("partner_not_found")
        if not str(partner["payout_destination"] or "").strip():
            raise ValueError("payout_profile_required")
        rows = connection.execute(
            """SELECT * FROM affiliate_commissions
               WHERE partner_id = ? AND status = 'pending' AND available_at <= ?
               ORDER BY earned_at, id""",
            (int(partner_id), now_iso),
        ).fetchall()
        if not rows:
            raise ValueError("no_payable_commissions")
        amount = sum(int(row["commission_cents"]) for row in rows)
        cursor = connection.execute(
            """INSERT INTO affiliate_payout_batches (
                   partner_id, period_start, period_end, amount_cents, status,
                   payout_asset, payout_network, payout_destination, note, created_at
               ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)""",
            (
                int(partner_id), str(rows[0]["earned_at"]),
                str(rows[-1]["earned_at"]), amount, str(partner["payout_asset"]),
                str(partner["payout_network"]), str(partner["payout_destination"]),
                note.strip()[:500], now_iso,
            ),
        )
        batch_id = int(cursor.lastrowid)
        placeholders = ",".join("?" for _ in rows)
        connection.execute(
            f"UPDATE affiliate_commissions SET status = 'in_batch', payout_batch_id = ? WHERE id IN ({placeholders})",  # noqa: S608 - placeholders only.
            (batch_id, *(int(row["id"]) for row in rows)),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM affiliate_payout_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        return dict(row) | {"commission_count": len(rows)}
    finally:
        connection.close()


def mark_payout_paid(
    batch_id: int,
    *,
    payment_reference: str,
    db_path=accounts.DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference = payment_reference.strip()
    if not reference or len(reference) > 200:
        raise ValueError("payment_reference_required")
    paid_at = accounts._utc_iso(_moment(now))
    connection = accounts._connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        batch = connection.execute(
            "SELECT * FROM affiliate_payout_batches WHERE id = ?", (int(batch_id),)
        ).fetchone()
        if batch is None:
            raise ValueError("payout_batch_not_found")
        if str(batch["status"]) == "paid":
            connection.commit()
            return dict(batch)
        if str(batch["status"]) != "draft":
            raise ValueError("payout_batch_not_payable")
        connection.execute(
            "UPDATE affiliate_payout_batches SET status = 'paid', payment_reference = ?, paid_at = ? WHERE id = ?",
            (reference, paid_at, int(batch_id)),
        )
        connection.execute(
            "UPDATE affiliate_commissions SET status = 'paid', paid_at = ? WHERE payout_batch_id = ? AND status = 'in_batch'",
            (paid_at, int(batch_id)),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM affiliate_payout_batches WHERE id = ?", (int(batch_id),)
        ).fetchone()
        return dict(row)
    finally:
        connection.close()


def void_commission(
    commission_id: int,
    *,
    reason: str,
    db_path=accounts.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    clean_reason = reason.strip()
    if not clean_reason or len(clean_reason) > 500:
        raise ValueError("void_reason_required")
    connection = accounts._connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM affiliate_commissions WHERE id = ?", (int(commission_id),)
        ).fetchone()
        if row is None:
            raise ValueError("commission_not_found")
        if str(row["status"]) != "pending":
            raise ValueError("commission_cannot_be_voided")
        connection.execute(
            "UPDATE affiliate_commissions SET status = 'void', void_reason = ? WHERE id = ?",
            (clean_reason, int(commission_id)),
        )
        connection.commit()
        updated = connection.execute(
            "SELECT * FROM affiliate_commissions WHERE id = ?", (int(commission_id),)
        ).fetchone()
        return dict(updated)
    finally:
        connection.close()
