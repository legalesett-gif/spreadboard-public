"""Authenticated SpreadBoard accounts and user-owned portfolio records."""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import statistics
import threading
from typing import Any, Iterator

from .dex_identity import identity_key


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_DB_PATH = RUNTIME_DIR / "spreadboard_accounts.sqlite3"
SESSION_COOKIE = "spreadboard_session"
SESSION_DAYS = 30
SESSION_TOUCH_SECONDS = 300
RESEARCH_CONSENT_VERSION = "portfolio_research_v2"
_REQUEST_STATE = threading.local()
_PAGE_VIEW_LOCK = threading.Lock()
_PAGE_VIEW_PENDING: dict[tuple[str, str, str], int] = {}
_DATABASE_PRAGMA_LOCK = threading.Lock()
_DATABASE_PRAGMA_READY: set[str] = set()


def _member_manager_emails() -> set[str]:
    """Accounts allowed to see and operate the subscriber ledger.

    This is intentionally independent of the broad ``admin`` role.  Billing,
    deployment, and subscriber operations are different powers; granting one
    must not silently grant the others.
    """
    configured = os.environ.get(
        "SPREADBOARD_MEMBER_MANAGER_EMAILS",
        "alex@spreadarbitrage.ink,anatolij@spreadarbitrage.ink",
    )
    return {item.strip().casefold() for item in configured.split(",") if item.strip()}


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str
    role: str
    subscription_status: str
    subscription_expires_at: str | None
    monthly_capital_usd: float | None
    subscription_tier: str = "research_pro"
    billing_customer_id: str | None = None
    billing_subscription_id: str | None = None
    subscription_cancel_at_period_end: bool = False
    csrf_token: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_manage_members(self) -> bool:
        return self.email.strip().casefold() in _member_manager_emails()

    @property
    def subscription_active(self) -> bool:
        if self.is_admin:
            return True
        if self.subscription_status not in {"active", "trialing"}:
            return False
        if not self.subscription_expires_at:
            return self.subscription_status == "active"
        try:
            expires = datetime.fromisoformat(self.subscription_expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return expires > datetime.now(tz=timezone.utc)

    @property
    def entitlement_tier(self) -> str:
        if self.is_admin:
            return "research_pro"
        return self.subscription_tier if self.subscription_active else "free"

    def has_tier(self, minimum: str) -> bool:
        order = {"free": 0, "scanner": 1, "research_pro": 2}
        return order.get(self.entitlement_tier, 0) >= order.get(str(minimum), 99)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "is_admin": self.is_admin,
            "can_manage_members": self.can_manage_members,
            "subscription_status": self.subscription_status,
            "subscription_expires_at": self.subscription_expires_at,
            "subscription_active": self.subscription_active,
            "subscription_tier": self.entitlement_tier,
            "monthly_capital_usd": self.monthly_capital_usd,
            "billing_managed": bool(self.billing_customer_id),
            "subscription_cancel_at_period_end": self.subscription_cancel_at_period_end,
        }


def auth_required() -> bool:
    return os.environ.get("SPREADBOARD_AUTH_REQUIRED", "0").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def initialize(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    connection = _connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
                subscription_status TEXT NOT NULL DEFAULT 'inactive'
                    CHECK (subscription_status IN ('inactive', 'trialing', 'active', 'past_due', 'cancelled')),
                subscription_expires_at TEXT,
                subscription_tier TEXT NOT NULL DEFAULT 'research_pro'
                    CHECK (subscription_tier IN ('free', 'scanner', 'research_pro')),
                monthly_capital_usd REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS password_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                purpose TEXT NOT NULL CHECK (purpose IN ('invite', 'reset')),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                csrf_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                user_agent_hash TEXT,
                ip_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token TEXT NOT NULL,
                route_key TEXT,
                long_venue TEXT NOT NULL,
                long_market_type TEXT NOT NULL,
                long_symbol TEXT,
                long_quantity REAL NOT NULL CHECK (long_quantity > 0),
                long_entry_price REAL NOT NULL CHECK (long_entry_price > 0),
                short_venue TEXT NOT NULL,
                short_market_type TEXT NOT NULL,
                short_symbol TEXT,
                short_quantity REAL NOT NULL CHECK (short_quantity > 0),
                short_entry_price REAL NOT NULL CHECK (short_entry_price > 0),
                entry_spread_pct REAL,
                capital_usd REAL CHECK (capital_usd IS NULL OR capital_usd >= 0),
                entry_fees_usd REAL NOT NULL DEFAULT 0,
                exit_fees_usd REAL NOT NULL DEFAULT 0,
                borrow_costs_usd REAL NOT NULL DEFAULT 0,
                gas_costs_usd REAL NOT NULL DEFAULT 0,
                transfer_costs_usd REAL NOT NULL DEFAULT 0,
                slippage_costs_usd REAL NOT NULL DEFAULT 0,
                transfer_chain TEXT,
                transfer_contract TEXT,
                transfer_started_at TEXT,
                transfer_credited_at TEXT,
                research_costs_complete INTEGER NOT NULL DEFAULT 0,
                research_cost_consent INTEGER NOT NULL DEFAULT 0,
                research_transfer_consent INTEGER NOT NULL DEFAULT 0,
                research_matched_notional_usd REAL,
                research_consent_version TEXT,
                research_consented_at TEXT,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                long_exit_price REAL,
                short_exit_price REAL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS funding_cashflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
                venue TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                occurred_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS position_alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
                metric TEXT NOT NULL CHECK (metric IN ('exit_spread_pct', 'open_spread_pct', 'pnl_usd', 'funding_usd')),
                operator TEXT NOT NULL CHECK (operator IN ('lte', 'gte')),
                threshold REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_triggered_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS in_app_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                alert_rule_id INTEGER REFERENCES position_alert_rules(id) ON DELETE SET NULL,
                position_id INTEGER REFERENCES positions(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT
            );
            CREATE TABLE IF NOT EXISTS billing_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                result TEXT NOT NULL,
                processed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscription_consents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                terms_version TEXT NOT NULL,
                immediate_access INTEGER NOT NULL CHECK (immediate_access IN (0, 1)),
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                accepted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_links (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL UNIQUE,
                linked_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_link_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS telegram_communities (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                chat_id INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                invite_link TEXT,
                configured_by_telegram_user_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_memberships (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                telegram_user_id INTEGER NOT NULL,
                community_chat_id INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'active', 'removed', 'exempt', 'error')),
                last_checked_at TEXT,
                last_error TEXT,
                joined_at TEXT,
                removed_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                pushover_user_key_encrypted TEXT,
                pushover_device TEXT NOT NULL DEFAULT '',
                pushover_sound TEXT NOT NULL DEFAULT 'pushover',
                pushover_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exchange_connections (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                venue TEXT NOT NULL,
                credential_encrypted TEXT NOT NULL,
                credential_fields_json TEXT NOT NULL DEFAULT '[]',
                terms_version TEXT NOT NULL,
                read_only_confirmed_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                last_sync_at TEXT,
                last_status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, venue)
            );
            CREATE TABLE IF NOT EXISTS market_alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                route_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                metric TEXT NOT NULL CHECK (metric IN (
                    'open_spread_pct', 'funding_24h_pct',
                    -- Per token rather than per route: "tell me when DOGE
                    -- trades above X", or when anything on this token pays.
                    'token_price', 'token_funding_24h_pct',
                    -- Route-operability rules.  These keep a watched pair on
                    -- the member's radar when a rail closes or its quote ages.
                    'route_deliverable', 'quote_age_seconds'
                )),
                operator TEXT NOT NULL CHECK (operator IN ('lte', 'gte')),
                threshold REAL NOT NULL,
                stability_seconds INTEGER NOT NULL DEFAULT 10,
                enabled INTEGER NOT NULL DEFAULT 1,
                condition_since TEXT,
                last_condition_met INTEGER NOT NULL DEFAULT 0,
                last_triggered_at TEXT,
                last_value REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crypto_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                period_days INTEGER NOT NULL,
                subscription_tier TEXT NOT NULL DEFAULT 'research_pro'
                    CHECK (subscription_tier IN ('scanner', 'research_pro')),
                list_amount_cents INTEGER NOT NULL,
                slot_index INTEGER NOT NULL,
                expected_amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'paid', 'expired', 'cancelled')),
                token TEXT,
                tx_hash TEXT UNIQUE,
                from_address TEXT,
                paid_amount_cents INTEGER,
                block_number INTEGER,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                settled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crypto_payments (
                tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL,
                token TEXT NOT NULL,
                from_address TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                invoice_id INTEGER REFERENCES crypto_invoices(id) ON DELETE SET NULL,
                resolution TEXT NOT NULL
                    CHECK (resolution IN ('settled', 'unmatched', 'ambiguous', 'manual')),
                note TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                PRIMARY KEY (tx_hash, log_index)
            );
            CREATE TABLE IF NOT EXISTS affiliate_partners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'paused', 'closed')),
                discount_bps INTEGER NOT NULL DEFAULT 2000
                    CHECK (discount_bps BETWEEN 0 AND 10000),
                commission_bps INTEGER NOT NULL DEFAULT 5000
                    CHECK (commission_bps BETWEEN 0 AND 10000),
                attribution_days INTEGER NOT NULL DEFAULT 90
                    CHECK (attribution_days BETWEEN 1 AND 3650),
                payout_hold_days INTEGER NOT NULL DEFAULT 7
                    CHECK (payout_hold_days BETWEEN 0 AND 365),
                payout_asset TEXT NOT NULL DEFAULT 'USDT',
                payout_network TEXT NOT NULL DEFAULT 'Arbitrum',
                payout_destination TEXT NOT NULL DEFAULT '',
                payout_updated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS affiliate_clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id INTEGER NOT NULL REFERENCES affiliate_partners(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                landing_path TEXT NOT NULL DEFAULT '/',
                clicked_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                registered_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                registered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS affiliate_attributions (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                partner_id INTEGER NOT NULL REFERENCES affiliate_partners(id) ON DELETE RESTRICT,
                click_id INTEGER REFERENCES affiliate_clicks(id) ON DELETE SET NULL,
                attributed_at TEXT NOT NULL,
                first_payment_at TEXT
            );
            CREATE TABLE IF NOT EXISTS affiliate_payout_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id INTEGER NOT NULL REFERENCES affiliate_partners(id) ON DELETE RESTRICT,
                period_start TEXT,
                period_end TEXT,
                amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'paid', 'cancelled')),
                payment_reference TEXT NOT NULL DEFAULT '',
                payout_asset TEXT NOT NULL DEFAULT 'USDT',
                payout_network TEXT NOT NULL DEFAULT 'Arbitrum',
                payout_destination TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                paid_at TEXT
            );
            CREATE TABLE IF NOT EXISTS affiliate_commissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id INTEGER NOT NULL REFERENCES affiliate_partners(id) ON DELETE RESTRICT,
                referred_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                invoice_id INTEGER NOT NULL UNIQUE REFERENCES crypto_invoices(id) ON DELETE RESTRICT,
                subscription_tier TEXT NOT NULL,
                period_days INTEGER NOT NULL,
                list_amount_cents INTEGER NOT NULL,
                discount_cents INTEGER NOT NULL DEFAULT 0,
                commission_base_cents INTEGER NOT NULL,
                commission_bps INTEGER NOT NULL,
                commission_cents INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_batch', 'paid', 'void')),
                earned_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                payout_batch_id INTEGER REFERENCES affiliate_payout_batches(id) ON DELETE SET NULL,
                paid_at TEXT,
                void_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS saved_charts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                route_key TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                -- Some pairs are the same asset at a fixed ratio: SKHY and SKHX
                -- on Hyperliquid are 10:1, so their spread only reads correctly
                -- once one side is scaled.
                ratio REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                UNIQUE (user_id, route_key)
            );
            CREATE INDEX IF NOT EXISTS saved_charts_user ON saved_charts(user_id, created_at);
            CREATE TABLE IF NOT EXISTS filter_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                query_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, name)
            );
            CREATE TABLE IF NOT EXISTS watchlist_tokens (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, symbol)
            );
            CREATE INDEX IF NOT EXISTS filter_presets_user ON filter_presets(user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS watchlist_tokens_user ON watchlist_tokens(user_id, position);
            CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_push_deliveries (
                notification_id INTEGER NOT NULL REFERENCES in_app_notifications(id) ON DELETE CASCADE,
                subscription_id INTEGER NOT NULL REFERENCES web_push_subscriptions(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'permanent_failure')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (notification_id, subscription_id)
            );
            CREATE INDEX IF NOT EXISTS web_push_subscriptions_user ON web_push_subscriptions(user_id, active);
            CREATE INDEX IF NOT EXISTS web_push_deliveries_status ON web_push_deliveries(status, attempts, updated_at);
            CREATE TABLE IF NOT EXISTS daily_page_views (
                day TEXT NOT NULL,
                path TEXT NOT NULL,
                view_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, path)
            );
            CREATE TABLE IF NOT EXISTS subscription_lifecycle_events (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                subscription_expires_at TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('seven_days', 'three_days', 'one_day', 'expired')
                ),
                state TEXT NOT NULL DEFAULT 'pending' CHECK (
                    state IN ('pending', 'sent', 'error')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                in_app_notification_id INTEGER REFERENCES in_app_notifications(id) ON DELETE SET NULL,
                email_status TEXT,
                telegram_status TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, subscription_expires_at, event_type)
            );
            """
        )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS crypto_watcher_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_scanned_block INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_token_hash ON sessions(token_hash);
            CREATE INDEX IF NOT EXISTS sessions_user_expiry ON sessions(user_id, expires_at);
            CREATE INDEX IF NOT EXISTS positions_user_status ON positions(user_id, status, opened_at DESC);
            CREATE INDEX IF NOT EXISTS funding_position_time ON funding_cashflows(position_id, occurred_at);
            CREATE INDEX IF NOT EXISTS alert_rules_user_position ON position_alert_rules(user_id, position_id, enabled);
            CREATE INDEX IF NOT EXISTS notifications_user_time ON in_app_notifications(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS subscription_consents_user_time ON subscription_consents(user_id, accepted_at DESC);
            CREATE INDEX IF NOT EXISTS telegram_link_tokens_user ON telegram_link_tokens(user_id, expires_at);
            CREATE INDEX IF NOT EXISTS telegram_memberships_state ON telegram_memberships(state, updated_at);
            CREATE INDEX IF NOT EXISTS market_alert_rules_user ON market_alert_rules(user_id, enabled, updated_at);
            CREATE INDEX IF NOT EXISTS crypto_invoices_open ON crypto_invoices(status, expected_amount_cents, expires_at);
            CREATE INDEX IF NOT EXISTS crypto_invoices_user ON crypto_invoices(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS crypto_payments_resolution ON crypto_payments(resolution, observed_at DESC);
            CREATE INDEX IF NOT EXISTS affiliate_clicks_partner_time ON affiliate_clicks(partner_id, clicked_at DESC);
            CREATE INDEX IF NOT EXISTS affiliate_attributions_partner_time ON affiliate_attributions(partner_id, attributed_at DESC);
            CREATE INDEX IF NOT EXISTS affiliate_commissions_partner_status ON affiliate_commissions(partner_id, status, available_at);
            CREATE INDEX IF NOT EXISTS affiliate_payouts_partner_time ON affiliate_payout_batches(partner_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS subscription_lifecycle_state
                ON subscription_lifecycle_events(state, attempts, updated_at);
            CREATE INDEX IF NOT EXISTS exchange_connections_enabled
                ON exchange_connections(enabled, venue, updated_at);
            """
        )
        _ensure_columns(
            connection,
            "users",
            {
                "billing_provider": "TEXT",
                "billing_customer_id": "TEXT",
                "billing_subscription_id": "TEXT",
                "subscription_cancel_at_period_end": "INTEGER NOT NULL DEFAULT 0",
                "billing_updated_at": "TEXT",
                "subscription_tier": "TEXT NOT NULL DEFAULT 'research_pro'",
            },
        )
        _ensure_columns(
            connection,
            "crypto_invoices",
            {
                "subscription_tier": "TEXT NOT NULL DEFAULT 'research_pro'",
                "discount_cents": "INTEGER NOT NULL DEFAULT 0",
                "affiliate_partner_id": "INTEGER",
            },
        )
        _ensure_columns(
            connection,
            "affiliate_partners",
            {
                "payout_asset": "TEXT NOT NULL DEFAULT 'USDT'",
                "payout_network": "TEXT NOT NULL DEFAULT 'Arbitrum'",
                "payout_destination": "TEXT NOT NULL DEFAULT ''",
                "payout_updated_at": "TEXT",
            },
        )
        _ensure_columns(
            connection,
            "affiliate_payout_batches",
            {
                "payout_asset": "TEXT NOT NULL DEFAULT 'USDT'",
                "payout_network": "TEXT NOT NULL DEFAULT 'Arbitrum'",
                "payout_destination": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _ensure_columns(
            connection,
            "positions",
            {
                "borrow_costs_usd": "REAL NOT NULL DEFAULT 0",
                "gas_costs_usd": "REAL NOT NULL DEFAULT 0",
                "transfer_costs_usd": "REAL NOT NULL DEFAULT 0",
                "slippage_costs_usd": "REAL NOT NULL DEFAULT 0",
                "transfer_chain": "TEXT",
                "transfer_contract": "TEXT",
                "transfer_started_at": "TEXT",
                "transfer_credited_at": "TEXT",
                "research_costs_complete": "INTEGER NOT NULL DEFAULT 0",
                "research_cost_consent": "INTEGER NOT NULL DEFAULT 0",
                "research_transfer_consent": "INTEGER NOT NULL DEFAULT 0",
                "research_matched_notional_usd": "REAL",
                "research_consent_version": "TEXT",
                "research_consented_at": "TEXT",
            },
        )
        _ensure_columns(
            connection,
            "position_alert_rules",
            {
                "last_condition_met": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        _widen_market_alert_metrics(connection)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_billing_customer ON users(billing_customer_id) WHERE billing_customer_id IS NOT NULL"
        )
        connection.commit()
    finally:
        connection.close()
    _bootstrap_admin(db_path)


def _bootstrap_admin(db_path: Path | str) -> None:
    email = os.environ.get("SPREADBOARD_ADMIN_EMAIL", "").strip().casefold()
    password = os.environ.get("SPREADBOARD_ADMIN_PASSWORD", "")
    if not email or not password:
        return
    connection = _connect(db_path)
    try:
        exists = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if exists:
            return
        now = _utc_iso()
        connection.execute(
            """
            INSERT INTO users (
                email, display_name, password_hash, role, subscription_status,
                subscription_expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'admin', 'active', NULL, ?, ?)
            """,
            (
                email,
                os.environ.get("SPREADBOARD_ADMIN_NAME", "SpreadBoard Admin").strip()
                or "SpreadBoard Admin",
                hash_password(password),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password_must_be_at_least_12_characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
        )
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def login(
    email: str,
    password: str,
    *,
    user_agent: str = "",
    ip_address: str = "",
    db_path: Path | str = DEFAULT_DB_PATH,
) -> tuple[User, str]:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip(),)
        ).fetchone()
        if row is None or not verify_password(password, str(row["password_hash"])):
            raise ValueError("invalid_credentials")
        now = datetime.now(tz=timezone.utc)
        expires = now + timedelta(days=SESSION_DAYS)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        connection.execute(
            """
            INSERT INTO sessions (
                user_id, token_hash, csrf_token, created_at, expires_at,
                last_seen_at, user_agent_hash, ip_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                _token_hash(token),
                csrf,
                _utc_iso(now),
                _utc_iso(expires),
                _utc_iso(now),
                _privacy_hash(user_agent),
                _privacy_hash(ip_address),
            ),
        )
        connection.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (_utc_iso(now), _utc_iso(now), row["id"]),
        )
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (_utc_iso(now),))
        connection.commit()
        user = _user_from_row(row, csrf_token=csrf)
        return user, token
    finally:
        connection.close()


def user_for_session(token: str, db_path: Path | str = DEFAULT_DB_PATH) -> User | None:
    if not token:
        return None
    connection = _connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT u.*, s.csrf_token, s.id AS session_id,
                   s.last_seen_at AS session_last_seen_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (_token_hash(token), _utc_iso()),
        ).fetchone()
        if row is None:
            return None
        now = datetime.now(tz=timezone.utc)
        touch_cutoff = _utc_iso(now - timedelta(seconds=SESSION_TOUCH_SECONDS))
        if str(row["session_last_seen_at"] or "") < touch_cutoff:
            connection.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                (_utc_iso(now), row["session_id"]),
            )
            connection.commit()
        return _user_from_row(row, csrf_token=str(row["csrf_token"]))
    finally:
        connection.close()


def logout(token: str, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    connection = _connect(db_path)
    try:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
        connection.commit()
    finally:
        connection.close()


def create_user(
    *,
    email: str,
    display_name: str,
    password: str,
    subscription_status: str = "trialing",
    subscription_tier: str | None = None,
    subscription_days: int = 30,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    normalized_email = email.strip().casefold()
    if (
        len(normalized_email) > 254
        or normalized_email.count("@") != 1
        or not all(normalized_email.split("@", 1))
        or "." not in normalized_email.split("@", 1)[1]
    ):
        raise ValueError("invalid_email")
    clean_name = display_name.strip()
    if not clean_name or len(clean_name) > 100:
        raise ValueError("invalid_display_name")
    status = (
        subscription_status
        if subscription_status in {"inactive", "trialing", "active"}
        else "trialing"
    )
    tier = str(subscription_tier or ("free" if status == "inactive" else "research_pro"))
    if tier not in {"free", "scanner", "research_pro"}:
        raise ValueError("invalid_subscription_tier")
    now = datetime.now(tz=timezone.utc)
    expires = now + timedelta(days=max(1, min(3660, int(subscription_days))))
    connection = _connect(db_path)
    try:
        try:
            cursor = connection.execute(
                """
                INSERT INTO users (
                    email, display_name, password_hash, role, subscription_status,
                    subscription_expires_at, subscription_tier, created_at, updated_at
                ) VALUES (?, ?, ?, 'member', ?, ?, ?, ?, ?)
                """,
                (
                    normalized_email,
                    clean_name,
                    hash_password(password),
                    status,
                    _utc_iso(expires),
                    tier,
                    _utc_iso(now),
                    _utc_iso(now),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("email_already_registered") from exc
        connection.commit()
        return get_user(int(cursor.lastrowid), db_path=db_path) or {}
    finally:
        connection.close()


def create_invited_user(
    *,
    email: str,
    display_name: str,
    role: str = "member",
    subscription_status: str = "trialing",
    subscription_tier: str = "research_pro",
    subscription_days: int = 30,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> tuple[dict[str, Any], str]:
    """Create an account whose password is known only to its owner.

    A random, undisclosed placeholder satisfies the password column; the
    returned one-time invite token immediately replaces it when the person sets
    their password. Only an authenticated administrator can reach the HTTP
    endpoint that calls this function.
    """
    if role not in {"admin", "member"}:
        raise ValueError("invalid_role")
    created = create_user(
        email=email,
        display_name=display_name,
        password=secrets.token_urlsafe(48),
        subscription_status=subscription_status,
        subscription_tier=subscription_tier,
        subscription_days=subscription_days,
        db_path=db_path,
    )
    if role == "admin":
        connection = _connect(db_path)
        try:
            connection.execute(
                "UPDATE users SET role = 'admin', subscription_status = 'active', subscription_expires_at = NULL, updated_at = ? WHERE id = ?",
                (_utc_iso(), int(created["id"])),
            )
            connection.commit()
        finally:
            connection.close()
        created = get_user(int(created["id"]), db_path=db_path) or created
    token = create_password_token(int(created["id"]), purpose="invite", db_path=db_path)
    return created, token


def get_user(user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    connection = _connect(db_path)
    try:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_from_row(row).public_dict() if row is not None else None
    finally:
        connection.close()


def get_user_object(user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH) -> User | None:
    connection = _connect(db_path)
    try:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_from_row(row) if row is not None else None
    finally:
        connection.close()


def user_id_for_email(email: str, *, db_path: Path | str = DEFAULT_DB_PATH) -> int | None:
    """Resolve an account for password recovery without returning user data."""
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT id FROM users WHERE email = ? COLLATE NOCASE",
            (str(email or "").strip(),),
        ).fetchone()
        return int(row["id"]) if row is not None else None
    finally:
        connection.close()


def record_page_view(
    path: str,
    *,
    at: datetime | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Queue an aggregate path/day count without writing on the request path."""
    clean_path = "/" + str(path or "/").strip().lstrip("/")
    if len(clean_path) > 180 or not clean_path.startswith("/"):
        clean_path = "/other"
    day = (at or datetime.now(tz=timezone.utc)).astimezone(timezone.utc).date().isoformat()
    key = (str(Path(db_path)), day, clean_path)
    with _PAGE_VIEW_LOCK:
        _PAGE_VIEW_PENDING[key] = _PAGE_VIEW_PENDING.get(key, 0) + 1


def flush_page_views(db_path: Path | str | None = None) -> int:
    """Persist queued aggregate analytics in one transaction per database."""

    wanted = str(Path(db_path)) if db_path is not None else None
    with _PAGE_VIEW_LOCK:
        selected = {
            key: count
            for key, count in _PAGE_VIEW_PENDING.items()
            if wanted is None or key[0] == wanted
        }
        for key in selected:
            _PAGE_VIEW_PENDING.pop(key, None)
    if not selected:
        return 0

    grouped: dict[str, list[tuple[str, str, int]]] = {}
    for (path, day, page), count in selected.items():
        grouped.setdefault(path, []).append((day, page, count))
    written = 0
    for path, rows in grouped.items():
        connection = _connect(path)
        try:
            connection.executemany(
                """INSERT INTO daily_page_views (day, path, view_count)
                   VALUES (?, ?, ?)
                   ON CONFLICT(day, path)
                   DO UPDATE SET view_count = view_count + excluded.view_count""",
                rows,
            )
            connection.commit()
            written += sum(count for _, _, count in rows)
        except Exception:
            with _PAGE_VIEW_LOCK:
                for day, page, count in rows:
                    key = (path, day, page)
                    _PAGE_VIEW_PENDING[key] = _PAGE_VIEW_PENDING.get(key, 0) + count
            raise
        finally:
            connection.close()
    return written


class PageViewWorker(threading.Thread):
    """Flush aggregate analytics periodically, away from page rendering."""

    def __init__(
        self,
        *,
        db_path: Path | str = DEFAULT_DB_PATH,
        interval_seconds: float = 5.0,
    ) -> None:
        super().__init__(name="page-view-flush", daemon=True)
        self.db_path = Path(db_path)
        self.interval_seconds = max(1.0, interval_seconds)
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                flush_page_views(self.db_path)
            except Exception:
                continue

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=5.0)
        with suppress(Exception):
            flush_page_views(self.db_path)


def page_view_summary(*, days: int = 30, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Return privacy-safe aggregate traffic for the owner dashboard."""
    flush_page_views(db_path)
    bounded_days = max(1, min(365, int(days)))
    since = (datetime.now(tz=timezone.utc).date() - timedelta(days=bounded_days - 1)).isoformat()
    connection = _connect(db_path)
    try:
        daily = [
            dict(row)
            for row in connection.execute(
                """SELECT day, SUM(view_count) AS views
               FROM daily_page_views WHERE day >= ? GROUP BY day ORDER BY day""",
                (since,),
            ).fetchall()
        ]
        paths = [
            dict(row)
            for row in connection.execute(
                """SELECT path, SUM(view_count) AS views
               FROM daily_page_views WHERE day >= ?
               GROUP BY path ORDER BY views DESC, path LIMIT 25""",
                (since,),
            ).fetchall()
        ]
        return {
            "days": bounded_days,
            "since": since,
            "total_views": sum(int(row["views"] or 0) for row in daily),
            "daily": daily,
            "paths": paths,
            "privacy": "aggregate_path_counts_only",
        }
    finally:
        connection.close()


def create_telegram_link_token(
    user_id: int,
    *,
    ttl_minutes: int = 10,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> str:
    token = secrets.token_urlsafe(18)
    now = datetime.now(tz=timezone.utc)
    expires = now + timedelta(minutes=max(1, min(60, int(ttl_minutes))))
    connection = _connect(db_path)
    try:
        if connection.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise ValueError("user_not_found")
        connection.execute(
            "DELETE FROM telegram_link_tokens WHERE user_id = ? OR expires_at <= ?",
            (user_id, _utc_iso(now)),
        )
        connection.execute(
            "INSERT INTO telegram_link_tokens (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), user_id, _utc_iso(now), _utc_iso(expires)),
        )
        connection.commit()
        return token
    finally:
        connection.close()


def bind_telegram_chat(
    token: str,
    chat_id: int,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> User:
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM telegram_link_tokens WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
            (_token_hash(token), _utc_iso()),
        ).fetchone()
        if row is None:
            raise ValueError("invalid_or_expired_telegram_link")
        user_id = int(row["user_id"])
        owner = connection.execute(
            "SELECT user_id FROM telegram_links WHERE chat_id = ?", (int(chat_id),)
        ).fetchone()
        if owner is not None and int(owner["user_id"]) != user_id:
            raise ValueError("telegram_chat_already_linked")
        now = _utc_iso()
        connection.execute(
            "INSERT INTO telegram_links (user_id, chat_id, linked_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id, updated_at = excluded.updated_at",
            (user_id, int(chat_id), now, now),
        )
        connection.execute(
            "UPDATE telegram_link_tokens SET used_at = ? WHERE token_hash = ?",
            (now, row["token_hash"]),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    user = get_user_object(user_id, db_path=db_path)
    if user is None:
        raise ValueError("user_not_found")
    return user


def user_for_telegram_chat(chat_id: int, *, db_path: Path | str = DEFAULT_DB_PATH) -> User | None:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT u.* FROM telegram_links t JOIN users u ON u.id = t.user_id WHERE t.chat_id = ?",
            (int(chat_id),),
        ).fetchone()
        return _user_from_row(row) if row is not None else None
    finally:
        connection.close()


def telegram_link_status(user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT linked_at, updated_at FROM telegram_links WHERE user_id = ?", (user_id,)
        ).fetchone()
        return {
            "linked": row is not None,
            "linked_at": row["linked_at"] if row is not None else None,
            "updated_at": row["updated_at"] if row is not None else None,
        }
    finally:
        connection.close()


def unlink_telegram_chat(user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    connection = _connect(db_path)
    try:
        cursor = connection.execute("DELETE FROM telegram_links WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM telegram_link_tokens WHERE user_id = ?", (user_id,))
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def configure_telegram_community(
    chat_id: int,
    *,
    title: str,
    configured_by_telegram_user_id: int,
    invite_link: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        connection.execute(
            """INSERT INTO telegram_communities (
                   id, chat_id, title, invite_link, configured_by_telegram_user_id,
                   active, created_at, updated_at
               ) VALUES (1, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET chat_id = excluded.chat_id,
                   title = excluded.title, invite_link = excluded.invite_link,
                   configured_by_telegram_user_id = excluded.configured_by_telegram_user_id,
                   active = 1, updated_at = excluded.updated_at""",
            (
                int(chat_id),
                str(title or "SpreadBoard community")[:255],
                str(invite_link or "")[:1000] or None,
                int(configured_by_telegram_user_id),
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE telegram_memberships SET state = 'pending', updated_at = ? WHERE community_chat_id != ?",
            (now, int(chat_id)),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM telegram_communities WHERE id = 1").fetchone()
        return dict(row)
    finally:
        connection.close()


def telegram_community(*, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM telegram_communities WHERE id = 1 AND active = 1"
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def record_telegram_membership(
    user_id: int,
    *,
    telegram_user_id: int,
    community_chat_id: int,
    state: str,
    error: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if state not in {"pending", "active", "removed", "exempt", "error"}:
        raise ValueError("invalid_telegram_membership_state")
    now = _utc_iso()
    joined_at = now if state == "active" else None
    removed_at = now if state == "removed" else None
    connection = _connect(db_path)
    try:
        connection.execute(
            """INSERT INTO telegram_memberships (
                   user_id, telegram_user_id, community_chat_id, state,
                   last_checked_at, last_error, joined_at, removed_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   telegram_user_id = excluded.telegram_user_id,
                   community_chat_id = excluded.community_chat_id,
                   state = excluded.state,
                   last_checked_at = excluded.last_checked_at,
                   last_error = excluded.last_error,
                   joined_at = COALESCE(telegram_memberships.joined_at, excluded.joined_at),
                   removed_at = excluded.removed_at,
                   updated_at = excluded.updated_at""",
            (
                int(user_id),
                int(telegram_user_id),
                int(community_chat_id),
                state,
                now,
                str(error or "")[:500] or None,
                joined_at,
                removed_at,
                now,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM telegram_memberships WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        return dict(row)
    finally:
        connection.close()


def telegram_membership_candidates(
    *, db_path: Path | str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT u.id AS user_id, u.role, u.subscription_status,
                      u.subscription_expires_at, t.chat_id AS telegram_user_id,
                      m.state AS membership_state
               FROM users u
               JOIN telegram_links t ON t.user_id = u.id
               LEFT JOIN telegram_memberships m ON m.user_id = u.id
               ORDER BY u.id"""
            ).fetchall()
        ]
    finally:
        connection.close()


def notification_preferences(
    user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[str, Any]:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM notification_preferences WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        return {
            "pushover_configured": bool(row and row["pushover_user_key_encrypted"]),
            "pushover_device": str(row["pushover_device"] or "") if row else "",
            "pushover_sound": str(row["pushover_sound"] or "pushover") if row else "pushover",
            "pushover_enabled": bool(row["pushover_enabled"]) if row else False,
            "updated_at": row["updated_at"] if row else None,
        }
    finally:
        connection.close()


def notification_credentials(
    user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    """Internal decrypted delivery configuration, never an API response.

    ``notification_delivery`` deliberately ignores disabled rows.  Saving a
    changed device or re-enabling an existing key still needs to validate the
    already encrypted key before mutating the row, so this narrower helper
    returns it regardless of the enabled flag.  Callers must never serialize
    the result.
    """
    from spreadboard import field_crypto

    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM notification_preferences WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if row is None or not row["pushover_user_key_encrypted"]:
            return None
        return {
            "user_key": field_crypto.decrypt(str(row["pushover_user_key_encrypted"])),
            "device": str(row["pushover_device"] or ""),
            "sound": str(row["pushover_sound"] or "pushover"),
            "enabled": bool(row["pushover_enabled"]),
        }
    finally:
        connection.close()


def save_notification_preferences(
    user_id: int,
    payload: dict[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    from spreadboard import field_crypto

    key = str(payload.get("pushover_user_key") or "").strip()
    if key and (len(key) != 30 or not key.isalnum()):
        raise ValueError("invalid_pushover_user_key")
    device = str(payload.get("pushover_device") or "").strip()
    if len(device) > 25 or any(not (char.isalnum() or char in "-_") for char in device):
        raise ValueError("invalid_pushover_device")
    sound = str(payload.get("pushover_sound") or "pushover").strip()
    allowed_sounds = {"default", "pushover", "siren", "magic", "cashregister", "vibrate"}
    if sound not in allowed_sounds:
        raise ValueError("invalid_pushover_sound")
    enabled = bool(payload.get("pushover_enabled"))
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        existing = connection.execute(
            "SELECT pushover_user_key_encrypted, created_at FROM notification_preferences WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        encrypted = (
            field_crypto.encrypt(key)
            if key
            else (str(existing["pushover_user_key_encrypted"] or "") if existing else "")
        )
        if enabled and not encrypted:
            raise ValueError("pushover_user_key_required")
        connection.execute(
            """INSERT INTO notification_preferences (
                   user_id, pushover_user_key_encrypted, pushover_device,
                   pushover_sound, pushover_enabled, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   pushover_user_key_encrypted = excluded.pushover_user_key_encrypted,
                   pushover_device = excluded.pushover_device,
                   pushover_sound = excluded.pushover_sound,
                   pushover_enabled = excluded.pushover_enabled,
                   updated_at = excluded.updated_at""",
            (int(user_id), encrypted or None, device, sound, int(enabled), now, now),
        )
        connection.commit()
    finally:
        connection.close()
    return notification_preferences(user_id, db_path=db_path)


def notification_delivery(
    user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    from spreadboard import field_crypto

    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM notification_preferences WHERE user_id = ? AND pushover_enabled = 1",
            (int(user_id),),
        ).fetchone()
        if row is None or not row["pushover_user_key_encrypted"]:
            return None
        return {
            "user_key": field_crypto.decrypt(str(row["pushover_user_key_encrypted"])),
            "device": str(row["pushover_device"] or ""),
            "sound": str(row["pushover_sound"] or "pushover"),
        }
    finally:
        connection.close()


#: Rules that watch an asset rather than one venue pair.
TOKEN_METRICS = frozenset({"token_price", "token_funding_24h_pct"})

#: The prefix that marks a synthetic, token-wide alert target.
TOKEN_ALERT_PREFIX = "TOKEN:"


def token_alert_key(symbol: str) -> str:
    return f"{TOKEN_ALERT_PREFIX}{str(symbol or '').strip().upper()}"


def token_from_alert_key(route_key: str) -> str | None:
    text = str(route_key or "")
    if not text.startswith(TOKEN_ALERT_PREFIX):
        return None
    return text[len(TOKEN_ALERT_PREFIX) :].strip().upper() or None


def add_market_alert_rule(
    user_id: int,
    payload: dict[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    route_key = str(payload.get("route_key") or "").strip()[:1000]
    symbol = str(payload.get("symbol") or "").strip().upper()[:80]
    alert_type = str(payload.get("type") or "token_spread")
    metric = {
        "funding": "funding_24h_pct",
        "price": "token_price",
        "token_funding": "token_funding_24h_pct",
        "dw_tracking": "route_deliverable",
        "freshness": "quote_age_seconds",
    }.get(alert_type, "open_spread_pct")
    operator = "lte" if str(payload.get("direction") or "above") == "below" else "gte"
    threshold = float(payload.get("threshold"))
    stability = max(0, min(3600, int(payload.get("stability_seconds") or 0)))
    if not symbol:
        raise ValueError("alert_requires_a_token")
    if metric in TOKEN_METRICS:
        # A token rule watches the asset, not one venue pair, so it carries a
        # synthetic key instead of a route. Same table, same worker, same
        # de-duplication.
        route_key = token_alert_key(symbol)
    elif not route_key:
        raise ValueError("route_alert_requires_exact_route")
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            """INSERT INTO market_alert_rules (
                   user_id, route_key, symbol, metric, operator, threshold,
                   stability_seconds, enabled, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id),
                route_key,
                symbol,
                metric,
                operator,
                threshold,
                stability,
                int(payload.get("enabled") is not False),
                now,
                now,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM market_alert_rules WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        connection.close()


def update_market_alert_rule(
    user_id: int,
    rule_id: int,
    payload: dict[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Edit a member's own alert: threshold, direction, stability, on/off.

    Changing what an alert watches resets its trigger state, otherwise a rule
    edited while already met would stay silent until it lapsed and re-armed.
    """
    fields: list[str] = []
    values: list[Any] = []
    if "threshold" in payload:
        fields.append("threshold = ?")
        values.append(float(payload["threshold"]))
    if "direction" in payload:
        fields.append("operator = ?")
        values.append("lte" if str(payload["direction"]) == "below" else "gte")
    if "stability_seconds" in payload:
        fields.append("stability_seconds = ?")
        values.append(max(0, min(3600, int(payload["stability_seconds"] or 0))))
    if "enabled" in payload:
        fields.append("enabled = ?")
        values.append(int(bool(payload["enabled"])))
    if not fields:
        return get_market_alert_rule(user_id, rule_id, db_path=db_path)
    fields.extend(["condition_since = NULL", "last_condition_met = 0", "updated_at = ?"])
    values.append(_utc_iso())
    connection = _connect(db_path)
    try:
        connection.execute(
            f"UPDATE market_alert_rules SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
            (*values, int(rule_id), int(user_id)),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM market_alert_rules WHERE id = ? AND user_id = ?",
            (int(rule_id), int(user_id)),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def get_market_alert_rule(
    user_id: int, rule_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM market_alert_rules WHERE id = ? AND user_id = ?",
            (int(rule_id), int(user_id)),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def delete_market_alert_rule(
    user_id: int, rule_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> bool:
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            "DELETE FROM market_alert_rules WHERE id = ? AND user_id = ?",
            (int(rule_id), int(user_id)),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def list_market_alert_rules(
    user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM market_alert_rules WHERE user_id = ? ORDER BY updated_at DESC",
                (int(user_id),),
            ).fetchall()
        ]
    finally:
        connection.close()


def list_market_alert_user_ids(*, db_path: Path | str = DEFAULT_DB_PATH) -> list[int]:
    connection = _connect(db_path)
    try:
        return [
            int(row["user_id"])
            for row in connection.execute(
                "SELECT DISTINCT user_id FROM market_alert_rules WHERE enabled = 1 ORDER BY user_id"
            ).fetchall()
        ]
    finally:
        connection.close()


def list_pushover_user_ids(*, db_path: Path | str = DEFAULT_DB_PATH) -> list[int]:
    """Everyone who asked for Pushover delivery and still has an active subscription.

    Rail reopens are announced to members individually rather than to one group,
    because the window is short and a push reaches a phone.
    """
    connection = _connect(db_path)
    try:
        return [
            int(row["user_id"])
            for row in connection.execute(
                """SELECT n.user_id FROM notification_preferences n
                   WHERE n.pushover_enabled = 1
                     AND n.pushover_user_key_encrypted IS NOT NULL
                     AND n.pushover_user_key_encrypted != ''
                   ORDER BY n.user_id"""
            ).fetchall()
        ]
    finally:
        connection.close()


def record_market_alert_evaluation(
    user_id: int,
    rule_id: int,
    *,
    value: float,
    title: str,
    body: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    now_dt = datetime.now(tz=timezone.utc)
    now = _utc_iso(now_dt)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM market_alert_rules WHERE id = ? AND user_id = ? AND enabled = 1",
            (int(rule_id), int(user_id)),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        condition = (
            value <= float(row["threshold"])
            if row["operator"] == "lte"
            else value >= float(row["threshold"])
        )
        condition_since = row["condition_since"]
        should_trigger = False
        if condition:
            if not condition_since:
                condition_since = now
            try:
                started = datetime.fromisoformat(str(condition_since).replace("Z", "+00:00"))
                stable = (now_dt - started).total_seconds() >= int(row["stability_seconds"])
            except ValueError:
                stable = False
            should_trigger = stable and not bool(row["last_condition_met"])
        else:
            condition_since = None
        notification = None
        if should_trigger:
            cursor = connection.execute(
                """INSERT INTO in_app_notifications (
                       user_id, alert_rule_id, position_id, title, body, created_at
                   ) VALUES (?, NULL, NULL, ?, ?, ?)""",
                (int(user_id), title[:160], body[:1000], now),
            )
            notification = dict(
                connection.execute(
                    "SELECT * FROM in_app_notifications WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
            )
        connection.execute(
            """UPDATE market_alert_rules SET condition_since = ?, last_condition_met = ?,
                   last_triggered_at = CASE WHEN ? THEN ? ELSE last_triggered_at END,
                   last_value = ?, updated_at = ? WHERE id = ?""",
            (
                condition_since,
                int(condition and (should_trigger or bool(row["last_condition_met"]))),
                int(should_trigger),
                now,
                value,
                now,
                int(rule_id),
            ),
        )
        connection.commit()
        return notification
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_alert_user_ids(*, db_path: Path | str = DEFAULT_DB_PATH) -> list[int]:
    connection = _connect(db_path)
    try:
        return [
            int(row["user_id"])
            for row in connection.execute(
                """SELECT DISTINCT p.user_id FROM positions p
               JOIN position_alert_rules r ON r.position_id = p.id AND r.user_id = p.user_id
               WHERE p.status = 'open' AND r.enabled = 1 ORDER BY p.user_id"""
            ).fetchall()
        ]
    finally:
        connection.close()


def list_users(*, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        rows = connection.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        return [_user_from_row(row).public_dict() for row in rows]
    finally:
        connection.close()


def update_subscription(
    user_id: int,
    *,
    status: str,
    expires_at: str | None,
    tier: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    if status not in {"inactive", "trialing", "active", "past_due", "cancelled"}:
        raise ValueError("invalid_subscription_status")
    normalized_expiry = _normalize_iso(expires_at) if expires_at else None
    if tier is not None and tier not in {"free", "scanner", "research_pro"}:
        raise ValueError("invalid_subscription_tier")
    connection = _connect(db_path)
    try:
        if tier is None:
            connection.execute(
                "UPDATE users SET subscription_status = ?, subscription_expires_at = ?, updated_at = ? WHERE id = ?",
                (status, normalized_expiry, _utc_iso(), user_id),
            )
        else:
            connection.execute(
                "UPDATE users SET subscription_status = ?, subscription_expires_at = ?, subscription_tier = ?, updated_at = ? WHERE id = ?",
                (status, normalized_expiry, tier, _utc_iso(), user_id),
            )
        connection.commit()
    finally:
        connection.close()
    return get_user(user_id, db_path=db_path)


def apply_billing_event(
    event: dict[str, Any],
    *,
    payload_sha256: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Apply a verified Stripe event exactly once."""

    event_id = str(event.get("id") or "")[:255]
    event_type = str(event.get("type") or "")[:255]
    obj = (event.get("data") or {}).get("object") or {}
    if not event_id or not event_type or not isinstance(obj, dict):
        raise ValueError("invalid_billing_event")
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT result FROM billing_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if prior is not None:
            connection.rollback()
            return {"ok": True, "duplicate": True, "result": prior["result"]}

        customer_id = _stripe_id(obj.get("customer"), "cus_")
        raw_subscription = obj.get("id")
        if event_type.startswith("checkout."):
            raw_subscription = obj.get("subscription")
        elif event_type.startswith("invoice."):
            raw_subscription = obj.get("subscription") or (
                (((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription"))
                if isinstance(obj.get("parent"), dict)
                else None
            )
        subscription_id = _stripe_id(raw_subscription, "sub_")
        user_id = _billing_user_id(connection, obj, customer_id)
        billing_tier = _billing_tier(obj)
        result = "ignored_no_user"
        if user_id is not None:
            _assert_customer_owner(connection, user_id, customer_id)
            now = _utc_iso()
            explicit_user = bool(
                (
                    isinstance(obj.get("metadata"), dict)
                    and obj["metadata"].get("spreadboard_user_id")
                )
                or obj.get("client_reference_id")
            )
            stored = connection.execute(
                "SELECT billing_subscription_id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            stored_subscription = stored["billing_subscription_id"] if stored else None
            mismatched_subscription = bool(
                subscription_id and stored_subscription and subscription_id != stored_subscription
            )
            if event_type == "checkout.session.completed":
                connection.execute(
                    """UPDATE users SET billing_provider = 'stripe', billing_customer_id = COALESCE(?, billing_customer_id),
                       billing_subscription_id = COALESCE(?, billing_subscription_id),
                       subscription_tier = COALESCE(?, subscription_tier), billing_updated_at = ?, updated_at = ? WHERE id = ?""",
                    (customer_id, subscription_id, billing_tier, now, now, user_id),
                )
                result = "customer_linked"
            elif (
                event_type.startswith("customer.subscription.")
                and mismatched_subscription
                and not explicit_user
            ):
                result = "ignored_subscription_mismatch"
            elif event_type.startswith("customer.subscription."):
                status = _subscription_status(str(obj.get("status") or ""), event_type)
                expiry = _stripe_period_end(obj)
                connection.execute(
                    """UPDATE users SET billing_provider = 'stripe', billing_customer_id = COALESCE(?, billing_customer_id),
                       billing_subscription_id = COALESCE(?, billing_subscription_id), subscription_status = ?,
                       subscription_expires_at = ?, subscription_tier = COALESCE(?, subscription_tier),
                       subscription_cancel_at_period_end = ?, billing_updated_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        customer_id,
                        subscription_id,
                        status,
                        expiry,
                        billing_tier,
                        int(bool(obj.get("cancel_at_period_end"))),
                        now,
                        now,
                        user_id,
                    ),
                )
                result = f"subscription_{status}"
            elif event_type.startswith("invoice.") and (
                not subscription_id or mismatched_subscription
            ):
                result = "ignored_subscription_mismatch"
            elif event_type == "invoice.payment_failed":
                connection.execute(
                    "UPDATE users SET subscription_status = 'past_due', billing_updated_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, user_id),
                )
                result = "subscription_past_due"
            elif event_type == "invoice.paid":
                connection.execute(
                    "UPDATE users SET subscription_status = 'active', billing_updated_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, user_id),
                )
                result = "subscription_active"
            else:
                result = "ignored_event_type"
        connection.execute(
            "INSERT INTO billing_events (event_id, event_type, payload_sha256, result, processed_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, event_type, payload_sha256, result, _utc_iso()),
        )
        connection.commit()
        return {"ok": True, "duplicate": False, "result": result}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


#: How long a set-password link stays usable. An invite is handed over by the
#: admin and may sit in a chat for a day or two; a reset is a live credential
#: and should not.
INVITE_TOKEN_DAYS = 7
RESET_TOKEN_HOURS = 2


def create_password_token(
    user_id: int,
    *,
    purpose: str = "invite",
    db_path: Path | str = DEFAULT_DB_PATH,
) -> str:
    """Mint a single-use link that lets someone set their own password.

    There was no way to recover an account at all: no reset, no forgot-password,
    and no mail sender configured. A member who lost their password was locked
    out permanently and a new member could only be created by an admin typing a
    password and then transmitting it -- which means the admin knows it.

    The raw token is returned once and never stored; only its hash is kept, the
    same way session tokens are handled.
    """
    if purpose not in {"invite", "reset"}:
        raise ValueError("unknown_token_purpose")
    token = secrets.token_urlsafe(32)
    now = datetime.now(tz=timezone.utc)
    expires = now + (
        timedelta(days=INVITE_TOKEN_DAYS)
        if purpose == "invite"
        else timedelta(hours=RESET_TOKEN_HOURS)
    )
    connection = _connect(db_path)
    try:
        # One live link per user: minting a new one retires the old.
        connection.execute(
            "UPDATE password_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (_utc_iso(), int(user_id)),
        )
        connection.execute(
            """INSERT INTO password_tokens (user_id, token_hash, purpose, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (int(user_id), _token_hash(token), purpose, now.isoformat(), expires.isoformat()),
        )
        connection.commit()
    finally:
        connection.close()
    return token


def consume_password_token(
    token: str,
    new_password: str,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Set a password from a link, once.

    Every existing session for that user is revoked: if the link was needed
    because the password leaked, leaving old sessions alive defeats it.
    """
    text = str(token or "").strip()
    if not text:
        return None
    encoded = hash_password(new_password)  # raises if too short, before any writes
    now = datetime.now(tz=timezone.utc)
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM password_tokens WHERE token_hash = ?", (_token_hash(text),)
        ).fetchone()
        if row is None or row["used_at"]:
            return None
        try:
            expires = datetime.fromisoformat(str(row["expires_at"]))
        except ValueError:
            return None
        if expires <= now:
            return None
        connection.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (encoded, _utc_iso(), int(row["user_id"])),
        )
        connection.execute(
            "UPDATE password_tokens SET used_at = ? WHERE id = ?", (_utc_iso(), int(row["id"]))
        )
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (int(row["user_id"]),))
        connection.commit()
        user = connection.execute(
            "SELECT id, email, display_name FROM users WHERE id = ?", (int(row["user_id"]),)
        ).fetchone()
        return dict(user) if user else None
    finally:
        connection.close()


def password_token_status(
    token: str, *, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    """Whether a link is still good, without spending it."""
    text = str(token or "").strip()
    if not text:
        return None
    connection = _connect(db_path)
    try:
        row = connection.execute(
            """SELECT p.purpose, p.expires_at, p.used_at, u.email, u.display_name
               FROM password_tokens p JOIN users u ON u.id = p.user_id
               WHERE p.token_hash = ?""",
            (_token_hash(text),),
        ).fetchone()
        if row is None or row["used_at"]:
            return None
        try:
            if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(tz=timezone.utc):
                return None
        except ValueError:
            return None
        return dict(row)
    finally:
        connection.close()


def update_account_settings(
    user_id: int,
    *,
    display_name: str | None = None,
    monthly_capital_usd: float | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT display_name FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "UPDATE users SET display_name = ?, monthly_capital_usd = ?, updated_at = ? WHERE id = ?",
            (
                (display_name or str(row["display_name"])).strip(),
                monthly_capital_usd,
                _utc_iso(),
                user_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_user(user_id, db_path=db_path)


def list_positions(user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        positions = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM positions WHERE user_id = ? ORDER BY status DESC, opened_at DESC",
                (user_id,),
            ).fetchall()
        ]
        for position in positions:
            position["funding_cashflows"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM funding_cashflows WHERE user_id = ? AND position_id = ? ORDER BY occurred_at",
                    (user_id, position["id"]),
                ).fetchall()
            ]
            position["alert_rules"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM position_alert_rules WHERE user_id = ? AND position_id = ? ORDER BY created_at",
                    (user_id, position["id"]),
                ).fetchall()
            ]
        return positions
    finally:
        connection.close()


def research_route_signature(row: dict[str, Any]) -> str:
    """Return a case-insensitive route identity without account or fill data."""

    def market_type(value: Any) -> str:
        normalized = str(value or "").strip().casefold()
        if normalized in {"future", "futures", "perp", "perpetual", "swap"}:
            return "futures"
        if normalized == "dex":
            return "dex"
        if normalized == "spot":
            return "spot"
        return normalized

    return "|".join(
        (
            str(row.get("token") or "").strip().casefold(),
            str(row.get("long_venue") or "").strip().casefold(),
            market_type(row.get("long_market_type")),
            str(row.get("short_venue") or "").strip().casefold(),
            market_type(row.get("short_market_type")),
        )
    )


def anonymized_research_evidence(
    *,
    as_of: datetime | float | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Aggregate explicitly contributed closed-position evidence.

    The return value intentionally contains no user/position identifiers,
    quantities, fills, notionals, or event timestamps. Lifecycle slippage is
    included here because public route gross edge does not contain a member's
    realised entry/exit slippage; portfolio PnL still does not deduct it twice.
    """

    if isinstance(as_of, datetime):
        cutoff = as_of.astimezone(timezone.utc)
    elif as_of is None:
        cutoff = datetime.now(tz=timezone.utc)
    else:
        cutoff = datetime.fromtimestamp(float(as_of), tz=timezone.utc)
    connection = _connect(db_path)
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT token, long_venue, long_market_type, short_venue,
                          short_market_type, status, closed_at,
                          entry_fees_usd, exit_fees_usd, borrow_costs_usd,
                          gas_costs_usd, transfer_costs_usd, slippage_costs_usd,
                          transfer_chain, transfer_contract,
                          transfer_started_at, transfer_credited_at,
                          research_costs_complete, research_cost_consent,
                          research_transfer_consent, research_matched_notional_usd,
                          research_consent_version
                   FROM positions
                   WHERE status = 'closed'
                     AND (research_cost_consent = 1 OR research_transfer_consent = 1)"""
            ).fetchall()
        ]
    finally:
        connection.close()

    grouped: dict[tuple[str, str, str], dict[str, list[Any]]] = {}
    for row in rows:
        # A wider evidence payload requires fresh, version-matched consent.
        # Never reinterpret a legacy total-only opt-in as permission to emit
        # the component breakdown introduced in portfolio_research_v2.
        if str(row.get("research_consent_version") or "") != RESEARCH_CONSENT_VERSION:
            continue
        closed_at = _optional_normalized_iso(row.get("closed_at"))
        if not closed_at:
            continue
        if datetime.fromisoformat(closed_at.replace("Z", "+00:00")) > cutoff:
            continue
        signature = research_route_signature(row)
        is_dex = "dex" in {
            str(row.get("long_market_type") or "").strip().casefold(),
            str(row.get("short_market_type") or "").strip().casefold(),
        }
        chain, contract = identity_key(
            row.get("transfer_chain"), row.get("transfer_contract")
        )
        if is_dex and not (chain and contract):
            continue
        bucket = grouped.setdefault((signature, chain, contract), {"costs": [], "transfers": []})
        if row.get("research_cost_consent") and row.get("research_costs_complete"):
            notional = _optional_nonnegative_float(row.get("research_matched_notional_usd"))
            if notional:
                components_usd = {
                    "fee_pct": float(row.get("entry_fees_usd") or 0.0)
                    + float(row.get("exit_fees_usd") or 0.0),
                    "borrow_pct": float(row.get("borrow_costs_usd") or 0.0),
                    "gas_pct": float(row.get("gas_costs_usd") or 0.0),
                    "transfer_pct": float(row.get("transfer_costs_usd") or 0.0),
                    "measured_slippage_pct": float(row.get("slippage_costs_usd") or 0.0),
                }
                component_pct = {
                    name: amount / notional * 100.0
                    for name, amount in components_usd.items()
                }
                bucket["costs"].append(
                    {
                        **component_pct,
                        "round_trip_cost_pct": sum(component_pct.values()),
                    }
                )
        if row.get("research_transfer_consent"):
            started_at = _optional_normalized_iso(row.get("transfer_started_at"))
            credited_at = _optional_normalized_iso(row.get("transfer_credited_at"))
            if started_at and credited_at:
                duration = (
                    datetime.fromisoformat(credited_at.replace("Z", "+00:00"))
                    - datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                ).total_seconds()
                if 0 <= duration <= 30 * 24 * 3600:
                    bucket["transfers"].append(duration)

    output: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for (signature, chain, contract), values in grouped.items():
        route = output.setdefault(signature, {"costs": [], "transfers": []})
        if values["costs"]:
            component_names = (
                "fee_pct",
                "borrow_pct",
                "gas_pct",
                "transfer_pct",
                "measured_slippage_pct",
                "round_trip_cost_pct",
            )
            route["costs"].append(
                {
                    "chain": chain or None,
                    "contract": contract or None,
                    **{
                        name: round(
                            statistics.median(item[name] for item in values["costs"]),
                            8,
                        )
                        for name in component_names
                    },
                    "sample_count": len(values["costs"]),
                    "source": "opt_in_completed_positions",
                    "includes": "fees_borrow_gas_transfer_and_measured_slippage",
                    "consent_version": RESEARCH_CONSENT_VERSION,
                }
            )
        if values["transfers"]:
            route["transfers"].append(
                {
                    "chain": chain,
                    "contract": contract,
                    "transfer_time_seconds": round(statistics.median(values["transfers"]), 3),
                    "sample_count": len(values["transfers"]),
                    "source": "opt_in_successful_transfers",
                }
            )
    return output


def _position_values(payload: dict[str, Any]) -> dict[str, Any]:
    required_text = ("token", "long_venue", "long_market_type", "short_venue", "short_market_type")
    text = {key: str(payload.get(key) or "").strip() for key in required_text}
    if not all(text.values()):
        raise ValueError("position_route_fields_required")
    numeric = {
        key: _positive_float(payload.get(key), key)
        for key in ("long_quantity", "long_entry_price", "short_quantity", "short_entry_price")
    }
    opened_at = _normalize_iso(str(payload.get("opened_at") or _utc_iso()))
    entry_spread = (numeric["short_entry_price"] / numeric["long_entry_price"] - 1) * 100
    token = text["token"].upper()
    route_key = "|".join(
        (
            token,
            text["long_venue"],
            text["long_market_type"],
            text["short_venue"],
            text["short_market_type"],
        )
    )
    return {
        **text,
        **numeric,
        "token": token,
        "route_key": route_key,
        "long_symbol": str(payload.get("long_symbol") or "").strip() or None,
        "short_symbol": str(payload.get("short_symbol") or "").strip() or None,
        "entry_spread_pct": entry_spread,
        "capital_usd": _optional_nonnegative_float(payload.get("capital_usd")),
        "entry_fees_usd": _optional_nonnegative_float(payload.get("entry_fees_usd")) or 0.0,
        "borrow_costs_usd": _optional_nonnegative_float(payload.get("borrow_costs_usd")) or 0.0,
        "gas_costs_usd": _optional_nonnegative_float(payload.get("gas_costs_usd")) or 0.0,
        "transfer_costs_usd": _optional_nonnegative_float(payload.get("transfer_costs_usd")) or 0.0,
        "slippage_costs_usd": _optional_nonnegative_float(payload.get("slippage_costs_usd")) or 0.0,
        "opened_at": opened_at,
        "notes": str(payload.get("notes") or "")[:2000],
    }


def create_position(
    user_id: int, payload: dict[str, Any], *, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[str, Any]:
    values = _position_values(payload)
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO positions (
                user_id, token, route_key, long_venue, long_market_type, long_symbol,
                long_quantity, long_entry_price, short_venue, short_market_type,
                short_symbol, short_quantity, short_entry_price, entry_spread_pct,
                capital_usd, entry_fees_usd, borrow_costs_usd, gas_costs_usd,
                transfer_costs_usd, slippage_costs_usd, opened_at, notes,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                values["token"],
                values["route_key"],
                values["long_venue"],
                values["long_market_type"],
                values["long_symbol"],
                values["long_quantity"],
                values["long_entry_price"],
                values["short_venue"],
                values["short_market_type"],
                values["short_symbol"],
                values["short_quantity"],
                values["short_entry_price"],
                values["entry_spread_pct"],
                values["capital_usd"],
                values["entry_fees_usd"],
                values["borrow_costs_usd"],
                values["gas_costs_usd"],
                values["transfer_costs_usd"],
                values["slippage_costs_usd"],
                values["opened_at"],
                values["notes"],
                now,
                now,
            ),
        )
        connection.commit()
        position_id = int(cursor.lastrowid)
    finally:
        connection.close()
    return next(
        item for item in list_positions(user_id, db_path=db_path) if item["id"] == position_id
    )


def update_position(
    user_id: int,
    position_id: int,
    payload: dict[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Correct a user-owned journal entry without touching live venues."""

    connection = _connect(db_path)
    try:
        existing = connection.execute(
            "SELECT * FROM positions WHERE id = ? AND user_id = ?",
            (position_id, user_id),
        ).fetchone()
        if existing is None:
            raise ValueError("position_not_found")
        values = _position_values(payload)
        status = str(payload.get("status") or existing["status"] or "open").casefold()
        if status not in {"open", "closed"}:
            raise ValueError("invalid_position_status")
        if status == "closed":
            closed_at = _normalize_iso(
                str(payload.get("closed_at") or existing["closed_at"] or "")
            )
            if not closed_at:
                raise ValueError("closed_at_required")
            if datetime.fromisoformat(closed_at.replace("Z", "+00:00")) < datetime.fromisoformat(
                values["opened_at"].replace("Z", "+00:00")
            ):
                raise ValueError("closed_at_before_opened_at")
            long_exit_price = _positive_float(
                payload.get("long_exit_price", existing["long_exit_price"]),
                "long_exit_price",
            )
            short_exit_price = _positive_float(
                payload.get("short_exit_price", existing["short_exit_price"]),
                "short_exit_price",
            )
            exit_fees_usd = (
                _optional_nonnegative_float(payload.get("exit_fees_usd"))
                if "exit_fees_usd" in payload
                else float(existing["exit_fees_usd"] or 0.0)
            ) or 0.0
        else:
            # Reopening corrects an accidental journal close only. It never
            # sends an order or changes either venue.
            closed_at = None
            long_exit_price = None
            short_exit_price = None
            exit_fees_usd = 0.0
        transfer_chain = str(
            payload.get("transfer_chain", existing["transfer_chain"] or "") or ""
        ).strip()[:120] or None
        transfer_contract = str(
            payload.get("transfer_contract", existing["transfer_contract"] or "") or ""
        ).strip()[:240] or None
        transfer_started_at = _optional_normalized_iso(
            payload.get("transfer_started_at", existing["transfer_started_at"])
        )
        transfer_credited_at = _optional_normalized_iso(
            payload.get("transfer_credited_at", existing["transfer_credited_at"])
        )
        if bool(transfer_started_at) != bool(transfer_credited_at):
            raise ValueError("transfer_timestamps_must_be_complete")
        if transfer_started_at and transfer_credited_at:
            started = datetime.fromisoformat(transfer_started_at.replace("Z", "+00:00"))
            credited = datetime.fromisoformat(transfer_credited_at.replace("Z", "+00:00"))
            if credited < started:
                raise ValueError("transfer_credited_before_started")

        research_costs_complete = _payload_bool(
            payload,
            "research_costs_complete",
            default=bool(existing["research_costs_complete"]),
        )
        research_cost_consent = _payload_bool(
            payload,
            "research_cost_consent",
            default=bool(existing["research_cost_consent"]),
        )
        research_transfer_consent = _payload_bool(
            payload,
            "research_transfer_consent",
            default=bool(existing["research_transfer_consent"]),
        )
        research_matched_notional_usd = (
            _optional_nonnegative_float(payload.get("research_matched_notional_usd"))
            if "research_matched_notional_usd" in payload
            else _optional_nonnegative_float(existing["research_matched_notional_usd"])
        )
        if status != "closed":
            research_costs_complete = False
            research_cost_consent = False
            research_transfer_consent = False
        if research_cost_consent and not research_costs_complete:
            raise ValueError("complete_lifecycle_costs_before_research_consent")
        if research_cost_consent and not research_matched_notional_usd:
            raise ValueError("matched_notional_required_for_research_consent")
        is_dex = any(
            str(value or "").casefold() == "dex"
            for value in (values["long_market_type"], values["short_market_type"])
        )
        if research_cost_consent and is_dex and not (transfer_chain and transfer_contract):
            raise ValueError("exact_dex_identity_required_for_research_consent")
        if research_transfer_consent:
            if not is_dex:
                raise ValueError("transfer_research_requires_dex_route")
            if not (transfer_chain and transfer_contract):
                raise ValueError("exact_dex_identity_required_for_transfer_research")
            if not (transfer_started_at and transfer_credited_at):
                raise ValueError("transfer_timestamps_required_for_research_consent")
        consent_active = research_cost_consent or research_transfer_consent
        was_consented = bool(
            existing["research_cost_consent"] or existing["research_transfer_consent"]
        )
        research_consent_version = RESEARCH_CONSENT_VERSION if consent_active else None
        same_consent_version = (
            was_consented
            and str(existing["research_consent_version"] or "")
            == RESEARCH_CONSENT_VERSION
        )
        submitted_consent_version = str(
            payload.get("research_consent_version") or ""
        ).strip()
        if (
            consent_active
            and not same_consent_version
            and submitted_consent_version != RESEARCH_CONSENT_VERSION
        ):
            raise ValueError("current_research_consent_required")
        if consent_active and same_consent_version:
            research_consented_at = str(existing["research_consented_at"] or "") or _utc_iso()
        elif consent_active:
            research_consented_at = _utc_iso()
        else:
            research_consented_at = None
        cursor = connection.execute(
            """
            UPDATE positions SET
                token = ?, route_key = ?, long_venue = ?, long_market_type = ?,
                long_symbol = ?, long_quantity = ?, long_entry_price = ?,
                short_venue = ?, short_market_type = ?, short_symbol = ?,
                short_quantity = ?, short_entry_price = ?, entry_spread_pct = ?,
                capital_usd = ?, entry_fees_usd = ?, borrow_costs_usd = ?,
                gas_costs_usd = ?, transfer_costs_usd = ?, slippage_costs_usd = ?,
                transfer_chain = ?, transfer_contract = ?, transfer_started_at = ?,
                transfer_credited_at = ?, research_costs_complete = ?,
                research_cost_consent = ?, research_transfer_consent = ?,
                research_matched_notional_usd = ?, research_consent_version = ?,
                research_consented_at = ?,
                opened_at = ?, notes = ?,
                status = ?, closed_at = ?, long_exit_price = ?, short_exit_price = ?,
                exit_fees_usd = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                values["token"],
                values["route_key"],
                values["long_venue"],
                values["long_market_type"],
                values["long_symbol"],
                values["long_quantity"],
                values["long_entry_price"],
                values["short_venue"],
                values["short_market_type"],
                values["short_symbol"],
                values["short_quantity"],
                values["short_entry_price"],
                values["entry_spread_pct"],
                values["capital_usd"],
                values["entry_fees_usd"],
                values["borrow_costs_usd"],
                values["gas_costs_usd"],
                values["transfer_costs_usd"],
                values["slippage_costs_usd"],
                transfer_chain,
                transfer_contract,
                transfer_started_at,
                transfer_credited_at,
                int(research_costs_complete),
                int(research_cost_consent),
                int(research_transfer_consent),
                research_matched_notional_usd,
                research_consent_version,
                research_consented_at,
                values["opened_at"],
                values["notes"],
                status,
                closed_at,
                long_exit_price,
                short_exit_price,
                exit_fees_usd,
                _utc_iso(),
                position_id,
                user_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("position_not_found")
        connection.commit()
    finally:
        connection.close()
    return next(
        item for item in list_positions(user_id, db_path=db_path) if item["id"] == position_id
    )


def delete_position(
    user_id: int,
    position_id: int,
    *,
    confirm_token: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Delete one user-owned journal record and only its local dependants."""

    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        owned = connection.execute(
            "SELECT id, token, status FROM positions WHERE id = ? AND user_id = ?",
            (int(position_id), int(user_id)),
        ).fetchone()
        if owned is None:
            connection.rollback()
            raise ValueError("position_not_found")
        if not hmac.compare_digest(
            str(owned["token"]).strip().upper(), str(confirm_token or "").strip().upper()
        ):
            connection.rollback()
            raise ValueError("position_delete_confirmation_mismatch")
        related = {
            "funding_cashflows": int(
                connection.execute(
                    "SELECT COUNT(*) FROM funding_cashflows WHERE position_id = ? AND user_id = ?",
                    (int(position_id), int(user_id)),
                ).fetchone()[0]
            ),
            "alert_rules": int(
                connection.execute(
                    "SELECT COUNT(*) FROM position_alert_rules WHERE position_id = ? AND user_id = ?",
                    (int(position_id), int(user_id)),
                ).fetchone()[0]
            ),
            "notifications": int(
                connection.execute(
                    "SELECT COUNT(*) FROM in_app_notifications WHERE position_id = ? AND user_id = ?",
                    (int(position_id), int(user_id)),
                ).fetchone()[0]
            ),
        }
        cursor = connection.execute(
            "DELETE FROM positions WHERE id = ? AND user_id = ?",
            (int(position_id), int(user_id)),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise ValueError("position_not_found")
        connection.commit()
        return {
            "id": int(owned["id"]),
            "token": str(owned["token"]),
            "status": str(owned["status"]),
            "deleted_related": related,
            "exchange_actions": 0,
        }
    finally:
        connection.close()


def close_position(
    user_id: int,
    position_id: int,
    payload: dict[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    long_exit = _positive_float(payload.get("long_exit_price"), "long_exit_price")
    short_exit = _positive_float(payload.get("short_exit_price"), "short_exit_price")
    closed_at = _normalize_iso(str(payload.get("closed_at") or _utc_iso()))
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            """
            UPDATE positions SET status = 'closed', closed_at = ?, long_exit_price = ?,
                short_exit_price = ?, exit_fees_usd = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'open'
            """,
            (
                closed_at,
                long_exit,
                short_exit,
                _optional_nonnegative_float(payload.get("exit_fees_usd")) or 0.0,
                _utc_iso(),
                position_id,
                user_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("open_position_not_found")
        connection.commit()
    finally:
        connection.close()
    return next(
        item for item in list_positions(user_id, db_path=db_path) if item["id"] == position_id
    )


def add_funding_cashflow(
    user_id: int,
    position_id: int,
    payload: dict[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    amount = float(payload.get("amount_usd"))
    venue = str(payload.get("venue") or "").strip()
    if not venue:
        raise ValueError("venue_required")
    occurred_at = _normalize_iso(str(payload.get("occurred_at") or _utc_iso()))
    connection = _connect(db_path)
    try:
        owned = connection.execute(
            "SELECT 1 FROM positions WHERE id = ? AND user_id = ?", (position_id, user_id)
        ).fetchone()
        if owned is None:
            raise ValueError("position_not_found")
        cursor = connection.execute(
            """
            INSERT INTO funding_cashflows (user_id, position_id, venue, amount_usd, occurred_at, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                position_id,
                venue,
                amount,
                occurred_at,
                str(payload.get("note") or "")[:500],
                _utc_iso(),
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM funding_cashflows WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        connection.close()


def add_alert_rule(
    user_id: int,
    position_id: int,
    payload: dict[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    metric = str(payload.get("metric") or "exit_spread_pct")
    operator = str(payload.get("operator") or "lte")
    if metric not in {"exit_spread_pct", "open_spread_pct", "pnl_usd", "funding_usd"}:
        raise ValueError("invalid_alert_metric")
    if operator not in {"lte", "gte"}:
        raise ValueError("invalid_alert_operator")
    threshold = float(payload.get("threshold"))
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        owned = connection.execute(
            "SELECT 1 FROM positions WHERE id = ? AND user_id = ?", (position_id, user_id)
        ).fetchone()
        if owned is None:
            raise ValueError("position_not_found")
        cursor = connection.execute(
            """
            INSERT INTO position_alert_rules (
                user_id, position_id, metric, operator, threshold, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (user_id, position_id, metric, operator, threshold, now, now),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM position_alert_rules WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        connection.close()


def list_notifications(
    user_id: int, *, limit: int = 30, db_path: Path | str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM in_app_notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, max(1, min(200, int(limit)))),
            ).fetchall()
        ]
    finally:
        connection.close()


def create_notification(
    user_id: int,
    *,
    title: str,
    body: str,
    position_id: int | None = None,
    alert_rule_id: int | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO in_app_notifications (
                user_id, alert_rule_id, position_id, title, body, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, alert_rule_id, position_id, title[:160], body[:1000], _utc_iso()),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM in_app_notifications WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        connection.close()


def record_alert_trigger(
    user_id: int,
    rule_id: int,
    *,
    title: str,
    body: str,
    cooldown_seconds: int = 3600,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT r.*, p.token FROM position_alert_rules r
            JOIN positions p ON p.id = r.position_id
            WHERE r.id = ? AND r.user_id = ? AND r.enabled = 1
            """,
            (rule_id, user_id),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        last = row["last_triggered_at"]
        if last:
            try:
                previous = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if (datetime.now(tz=timezone.utc) - previous).total_seconds() < cooldown_seconds:
                    connection.rollback()
                    return None
            except ValueError:
                pass
        now = _utc_iso()
        cursor = connection.execute(
            """
            INSERT INTO in_app_notifications (
                user_id, alert_rule_id, position_id, title, body, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, rule_id, row["position_id"], title[:160], body[:1000], now),
        )
        connection.execute(
            "UPDATE position_alert_rules SET last_triggered_at = ?, updated_at = ? WHERE id = ?",
            (now, now, rule_id),
        )
        connection.commit()
        created = connection.execute(
            "SELECT * FROM in_app_notifications WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(created)
    finally:
        connection.close()


def record_alert_evaluation(
    user_id: int,
    rule_id: int,
    *,
    condition_met: bool,
    title: str,
    body: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Persist one notification per false-to-true threshold crossing."""

    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT r.*, p.token FROM position_alert_rules r
               JOIN positions p ON p.id = r.position_id
               WHERE r.id = ? AND r.user_id = ? AND r.enabled = 1 AND p.status = 'open'""",
            (rule_id, user_id),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        previous = bool(row["last_condition_met"])
        now = _utc_iso()
        created = None
        if condition_met and not previous:
            cursor = connection.execute(
                """INSERT INTO in_app_notifications (
                       user_id, alert_rule_id, position_id, title, body, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, rule_id, row["position_id"], title[:160], body[:1000], now),
            )
            created = int(cursor.lastrowid)
        connection.execute(
            """UPDATE position_alert_rules SET last_condition_met = ?,
               last_triggered_at = CASE WHEN ? THEN ? ELSE last_triggered_at END,
               updated_at = ? WHERE id = ?""",
            (int(condition_met), int(bool(created)), now, now, rule_id),
        )
        connection.commit()
        if created is None:
            return None
        result = connection.execute(
            "SELECT * FROM in_app_notifications WHERE id = ?", (created,)
        ).fetchone()
        return dict(result) if result else None
    finally:
        connection.close()


def mark_notifications_read(user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH) -> int:
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            "UPDATE in_app_notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
            (_utc_iso(), user_id),
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def record_subscription_consent(
    user_id: int,
    *,
    terms_version: str,
    immediate_access: bool,
    ip_address: str = "",
    user_agent: str = "",
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    if not terms_version.strip() or not immediate_access:
        raise ValueError("subscription_consent_required")
    connection = _connect(db_path)
    try:
        connection.execute(
            """INSERT INTO subscription_consents (
                   user_id, terms_version, immediate_access, ip_address, user_agent, accepted_at
               ) VALUES (?, ?, 1, ?, ?, ?)""",
            (
                int(user_id),
                terms_version.strip()[:80],
                ip_address[:120],
                user_agent[:500],
                _utc_iso(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def save_exchange_connection(
    user_id: int,
    venue: str,
    credential_encrypted: str,
    *,
    credential_fields: list[str],
    terms_version: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Store an already-encrypted, opt-in credential bundle."""

    if not credential_encrypted.strip() or not terms_version.strip():
        raise ValueError("invalid_exchange_connection")
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO exchange_connections (
                user_id, venue, credential_encrypted, credential_fields_json,
                terms_version, read_only_confirmed_at, enabled, last_status,
                last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 'pending', NULL, ?, ?)
            ON CONFLICT(user_id, venue) DO UPDATE SET
                credential_encrypted = excluded.credential_encrypted,
                credential_fields_json = excluded.credential_fields_json,
                terms_version = excluded.terms_version,
                read_only_confirmed_at = excluded.read_only_confirmed_at,
                enabled = 1,
                last_status = 'pending',
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                venue,
                credential_encrypted,
                json.dumps(sorted(set(credential_fields)), separators=(",", ":")),
                terms_version,
                now,
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return next(
        row
        for row in list_exchange_connections(user_id, db_path=db_path)
        if row["venue"] == venue
    )


def list_exchange_connections(
    user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """SELECT venue, credential_fields_json, terms_version,
                      read_only_confirmed_at, enabled, last_sync_at,
                      last_status, last_error, created_at, updated_at
               FROM exchange_connections WHERE user_id = ? ORDER BY venue""",
            (int(user_id),),
        ).fetchall()
    finally:
        connection.close()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["credential_fields"] = json.loads(item.pop("credential_fields_json"))
        except (TypeError, json.JSONDecodeError):
            item["credential_fields"] = []
        item["enabled"] = bool(item["enabled"])
        result.append(item)
    return result


def encrypted_exchange_connections(
    *, db_path: Path | str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Worker-only rows; never expose this function through an HTTP response."""

    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT c.user_id, c.venue, c.credential_encrypted,
                   c.terms_version, c.read_only_confirmed_at
            FROM exchange_connections c
            JOIN users u ON u.id = c.user_id
            WHERE c.enabled = 1
              AND (u.role = 'admin' OR u.subscription_status IN ('active', 'trialing'))
            ORDER BY c.user_id, c.venue
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def disconnect_exchange_connection(
    user_id: int, venue: str, *, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[str, Any]:
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            "DELETE FROM exchange_connections WHERE user_id = ? AND venue = ?",
            (int(user_id), venue),
        )
        if cursor.rowcount != 1:
            raise ValueError("exchange_connection_not_found")
        connection.commit()
        return {"venue": venue, "deleted": True}
    finally:
        connection.close()


def record_exchange_connection_sync(
    user_id: int,
    venue: str,
    *,
    status: str,
    error: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        connection.execute(
            """UPDATE exchange_connections
               SET last_sync_at = ?, last_status = ?, last_error = ?, updated_at = ?
               WHERE user_id = ? AND venue = ?""",
            (now, status[:80], (error or "")[:500] or None, now, int(user_id), venue),
        )
        connection.commit()
    finally:
        connection.close()


@contextmanager
def request_user(user: User | None) -> Iterator[None]:
    previous = getattr(_REQUEST_STATE, "user", None)
    _REQUEST_STATE.user = user
    try:
        yield
    finally:
        _REQUEST_STATE.user = previous


def current_user() -> User | None:
    return getattr(_REQUEST_STATE, "user", None)


def set_current_user(user: User | None) -> None:
    _REQUEST_STATE.user = user


def _connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    # Setting journal_mode is a database mutation. Repeating it on every
    # authenticated read makes otherwise independent sessions queue behind a
    # schema-level lock; establish WAL once per database/process instead.
    canonical = str(path.resolve())
    if canonical not in _DATABASE_PRAGMA_READY:
        with _DATABASE_PRAGMA_LOCK:
            if canonical not in _DATABASE_PRAGMA_READY:
                connection.execute("PRAGMA journal_mode = WAL")
                _DATABASE_PRAGMA_READY.add(canonical)
    return connection


def _widen_market_alert_metrics(connection: sqlite3.Connection) -> None:
    """Let the metric column hold the per-token rules as well.

    SQLite cannot alter a CHECK constraint, so a table created before token
    price and funding alerts existed would reject them at insert time -- with a
    constraint error, long after the member had filled in the form. Rebuild it
    once, carrying every existing rule across.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='market_alert_rules'"
    ).fetchone()
    existing = str((row[0] if row else "") or "")
    required_metrics = {"token_price", "route_deliverable", "quote_age_seconds"}
    if not existing or all(metric in existing for metric in required_metrics):
        return
    columns = [item[1] for item in connection.execute("PRAGMA table_info(market_alert_rules)")]
    connection.execute("ALTER TABLE market_alert_rules RENAME TO market_alert_rules_old")
    connection.execute(
        """
        CREATE TABLE market_alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            route_key TEXT NOT NULL,
            symbol TEXT NOT NULL,
            metric TEXT NOT NULL CHECK (metric IN (
                'open_spread_pct', 'funding_24h_pct',
                'token_price', 'token_funding_24h_pct',
                'route_deliverable', 'quote_age_seconds'
            )),
            operator TEXT NOT NULL CHECK (operator IN ('lte', 'gte')),
            threshold REAL NOT NULL,
            stability_seconds INTEGER NOT NULL DEFAULT 10,
            enabled INTEGER NOT NULL DEFAULT 1,
            condition_since TEXT,
            last_condition_met INTEGER NOT NULL DEFAULT 0,
            last_triggered_at TEXT,
            last_value REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    shared = ", ".join(columns)
    connection.execute(
        f"INSERT INTO market_alert_rules ({shared}) SELECT {shared} FROM market_alert_rules_old"
    )
    connection.execute("DROP TABLE market_alert_rules_old")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS market_alert_rules_user "
        "ON market_alert_rules(user_id, enabled, updated_at)"
    )


def _ensure_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _user_from_row(row: sqlite3.Row, *, csrf_token: str | None = None) -> User:
    keys = set(row.keys())
    return User(
        id=int(row["id"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        role=str(row["role"]),
        subscription_status=str(row["subscription_status"]),
        subscription_expires_at=row["subscription_expires_at"],
        monthly_capital_usd=float(row["monthly_capital_usd"])
        if row["monthly_capital_usd"] is not None
        else None,
        subscription_tier=(
            str(row["subscription_tier"]) if "subscription_tier" in keys else "research_pro"
        ),
        billing_customer_id=row["billing_customer_id"] if "billing_customer_id" in keys else None,
        billing_subscription_id=row["billing_subscription_id"]
        if "billing_subscription_id" in keys
        else None,
        subscription_cancel_at_period_end=bool(row["subscription_cancel_at_period_end"])
        if "subscription_cancel_at_period_end" in keys
        else False,
        csrf_token=csrf_token,
    )


def _stripe_id(value: Any, prefix: str) -> str | None:
    candidate = str(value or "")
    return candidate[:255] if candidate.startswith(prefix) else None


def _billing_tier(obj: dict[str, Any]) -> str | None:
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    value = str(metadata.get("spreadboard_tier") or "").strip()
    return value if value in {"scanner", "research_pro"} else None


def _billing_user_id(
    connection: sqlite3.Connection, obj: dict[str, Any], customer_id: str | None
) -> int | None:
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    raw = metadata.get("spreadboard_user_id") or obj.get("client_reference_id")
    if raw not in (None, ""):
        try:
            user_id = int(raw)
        except (TypeError, ValueError):
            raise ValueError("invalid_billing_user") from None
        if connection.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise ValueError("billing_user_not_found")
        return user_id
    if customer_id:
        row = connection.execute(
            "SELECT id FROM users WHERE billing_customer_id = ?", (customer_id,)
        ).fetchone()
        return int(row["id"]) if row else None
    return None


def _assert_customer_owner(
    connection: sqlite3.Connection, user_id: int, customer_id: str | None
) -> None:
    if not customer_id:
        return
    row = connection.execute(
        "SELECT id FROM users WHERE billing_customer_id = ? AND id != ?", (customer_id, user_id)
    ).fetchone()
    if row is not None:
        raise ValueError("billing_customer_conflict")


def _subscription_status(stripe_status: str, event_type: str) -> str:
    if event_type == "customer.subscription.deleted" or stripe_status == "canceled":
        return "cancelled"
    if stripe_status in {"active", "trialing"}:
        return stripe_status
    if stripe_status in {"past_due", "unpaid"}:
        return "past_due"
    return "inactive"


def _stripe_period_end(obj: dict[str, Any]) -> str | None:
    raw = obj.get("current_period_end")
    if not raw:
        items = (
            ((obj.get("items") or {}).get("data") or [])
            if isinstance(obj.get("items"), dict)
            else []
        )
        raw = items[0].get("current_period_end") if items and isinstance(items[0], dict) else None
    try:
        return _utc_iso(datetime.fromtimestamp(int(raw), tz=timezone.utc)) if raw else None
    except (TypeError, ValueError, OSError):
        return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _privacy_hash(value: str) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def _utc_iso(value: datetime | None = None) -> str:
    return (
        (value or datetime.now(tz=timezone.utc))
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalize_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _utc_iso(parsed)


def _optional_normalized_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return _normalize_iso(str(value))


def _payload_bool(payload: dict[str, Any], key: str, *, default: bool = False) -> bool:
    if key not in payload:
        return bool(default)
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _positive_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}_must_be_positive") from exc
    if number <= 0:
        raise ValueError(f"{name}_must_be_positive")
    return number


def _optional_nonnegative_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if number < 0:
        raise ValueError("value_must_be_nonnegative")
    return number


def add_saved_chart(
    user_id: int,
    payload: dict[str, Any],
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Pin a route to a member's own chart list.

    Any route may be saved, including one whose spread is negative: a pair that
    never converges can still be worth watching, and the operator holds exactly
    such a position.
    """
    route_key = str(payload.get("route_key") or "").strip()[:400]
    if not route_key:
        raise ValueError("saved_chart_requires_route")
    label = str(payload.get("label") or "").strip()[:120]
    try:
        ratio = float(payload.get("ratio") or 1.0)
    except (TypeError, ValueError):
        ratio = 1.0
    if ratio <= 0:
        ratio = 1.0
    connection = _connect(db_path)
    try:
        connection.execute(
            """INSERT INTO saved_charts (user_id, route_key, label, ratio, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, route_key) DO UPDATE SET
                   label = excluded.label, ratio = excluded.ratio""",
            (int(user_id), route_key, label, ratio, _utc_iso()),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM saved_charts WHERE user_id = ? AND route_key = ?",
            (int(user_id), route_key),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        connection.close()


def list_saved_charts(
    user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM saved_charts WHERE user_id = ? ORDER BY created_at DESC",
                (int(user_id),),
            )
        ]
    finally:
        connection.close()


def delete_saved_chart(
    user_id: int, route_key: str, *, db_path: Path | str = DEFAULT_DB_PATH
) -> bool:
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            "DELETE FROM saved_charts WHERE user_id = ? AND route_key = ?",
            (int(user_id), str(route_key)),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


FILTER_PRESET_FIELDS = {
    "q",
    "exchange",
    "kind",
    "min_spread_pct",
    "min_abs_funding_24h_pct",
    "sort",
    "direction",
    "funding_only",
    "quote",
    "min_volume_24h_usd",
    "min_market_cap_usd",
    "max_market_cap_usd",
    "min_fdv_usd",
    "max_fdv_usd",
    "max_listing_age_days",
    "asset_class",
    "persistence",
    "view",
    "notional_usd",
}


def _preset_query(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("invalid_filter_query")
    output: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if key not in FILTER_PRESET_FIELDS:
            raise ValueError(f"invalid_filter_field:{key[:40]}")
        text = str(raw_value or "").strip()[:120]
        if text:
            output[key] = text
    return output


def save_filter_preset(
    user_id: int, payload: dict[str, Any], *, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[str, Any]:
    name = " ".join(str(payload.get("name") or "").split())[:60]
    if not name:
        raise ValueError("filter_preset_requires_name")
    query = _preset_query(payload.get("query"))
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM filter_presets WHERE user_id = ?", (int(user_id),)
        ).fetchone()[0]
        existing = connection.execute(
            "SELECT id FROM filter_presets WHERE user_id = ? AND name = ?", (int(user_id), name)
        ).fetchone()
        if count >= 20 and existing is None:
            raise ValueError("filter_preset_limit_reached")
        connection.execute(
            """INSERT INTO filter_presets (user_id, name, query_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, name) DO UPDATE SET
                   query_json = excluded.query_json, updated_at = excluded.updated_at""",
            (int(user_id), name, json.dumps(query, sort_keys=True), now, now),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM filter_presets WHERE user_id = ? AND name = ?", (int(user_id), name)
        ).fetchone()
        result = dict(row) if row else {}
        result["query"] = json.loads(result.pop("query_json", "{}"))
        return result
    finally:
        connection.close()


def list_filter_presets(
    user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT * FROM filter_presets WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
            (int(user_id),),
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["query"] = json.loads(item.pop("query_json", "{}"))
            output.append(item)
        return output
    finally:
        connection.close()


def delete_filter_preset(
    user_id: int, preset_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> bool:
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            "DELETE FROM filter_presets WHERE id = ? AND user_id = ?",
            (int(preset_id), int(user_id)),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def _watch_symbol(value: Any) -> str:
    symbol = "".join(char for char in str(value or "").upper() if char.isalnum() or char in "_-")[
        :24
    ]
    return symbol


def replace_watchlist(
    user_id: int, symbols: Any, *, db_path: Path | str = DEFAULT_DB_PATH
) -> list[str]:
    if not isinstance(symbols, list):
        raise ValueError("watchlist_requires_tokens")
    clean = list(dict.fromkeys(filter(None, (_watch_symbol(item) for item in symbols))))[:10]
    connection = _connect(db_path)
    try:
        connection.execute("DELETE FROM watchlist_tokens WHERE user_id = ?", (int(user_id),))
        now = _utc_iso()
        connection.executemany(
            "INSERT INTO watchlist_tokens (user_id, symbol, position, created_at) VALUES (?, ?, ?, ?)",
            [(int(user_id), symbol, position, now) for position, symbol in enumerate(clean)],
        )
        connection.commit()
        return clean
    finally:
        connection.close()


def list_watchlist(user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH) -> list[str]:
    connection = _connect(db_path)
    try:
        return [
            str(row["symbol"])
            for row in connection.execute(
                "SELECT symbol FROM watchlist_tokens WHERE user_id = ? ORDER BY position, created_at",
                (int(user_id),),
            )
        ]
    finally:
        connection.close()


def all_watchlist_symbols(*, db_path: Path | str = DEFAULT_DB_PATH) -> list[str]:
    """Distinct member-tracked symbols for public-history prioritisation."""
    connection = _connect(db_path)
    try:
        return [
            str(row["symbol"])
            for row in connection.execute(
                "SELECT DISTINCT symbol FROM watchlist_tokens ORDER BY symbol"
            ).fetchall()
        ]
    finally:
        connection.close()


def all_open_position_symbols(*, db_path: Path | str = DEFAULT_DB_PATH) -> list[str]:
    """Distinct public token symbols in open journals, without user or PII data."""

    connection = _connect(db_path)
    try:
        return [
            str(row["token"])
            for row in connection.execute(
                """SELECT DISTINCT token FROM positions
                   WHERE status = 'open' AND token != ''
                   ORDER BY token"""
            ).fetchall()
        ]
    finally:
        connection.close()


def all_open_position_futures_legs(
    *, db_path: Path | str = DEFAULT_DB_PATH
) -> list[tuple[str, str]]:
    """Exact public futures markets in open journals, without user or PII data."""
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """SELECT long_venue, long_market_type, long_symbol,
                      short_venue, short_market_type, short_symbol
               FROM positions WHERE status = 'open'"""
        ).fetchall()
    finally:
        connection.close()
    legs: list[tuple[str, str]] = []
    for row in rows:
        for side in ("long", "short"):
            if str(row[f"{side}_market_type"] or "").casefold() != "futures":
                continue
            venue = str(row[f"{side}_venue"] or "").strip()
            symbol = str(row[f"{side}_symbol"] or "").strip()
            if venue and symbol:
                legs.append((venue, symbol))
    return list(dict.fromkeys(legs))


def all_open_position_market_legs(
    *, db_path: Path | str = DEFAULT_DB_PATH
) -> list[tuple[str, str, str]]:
    """Distinct exact CEX legs in open journals, newest positions first.

    This intentionally returns no user/account fields.  The WebSocket worker
    only needs public market coordinates to keep saved positions marked after
    they leave the ranked opportunity board.
    """

    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """SELECT long_venue, long_market_type, long_symbol,
                      short_venue, short_market_type, short_symbol
               FROM positions
               WHERE status = 'open'
               ORDER BY opened_at DESC, id DESC"""
        ).fetchall()
    finally:
        connection.close()
    legs: list[tuple[str, str, str]] = []
    for row in rows:
        for side in ("long", "short"):
            venue = str(row[f"{side}_venue"] or "").strip()
            market_type = str(row[f"{side}_market_type"] or "").strip()
            symbol = str(row[f"{side}_symbol"] or "").strip()
            if (
                venue
                and symbol
                and market_type in {"Spot", "Futures"}
                and "dex" not in venue.casefold()
            ):
                legs.append((venue, market_type, symbol))
    return list(dict.fromkeys(legs))


def save_web_push_subscription(
    user_id: int,
    payload: dict[str, Any],
    *,
    user_agent: str = "",
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    endpoint = str(payload.get("endpoint") or "").strip()
    keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    _validate_web_push_endpoint(endpoint)
    if not (40 <= len(p256dh) <= 256 and 8 <= len(auth) <= 128):
        raise ValueError("invalid_web_push_keys")
    if not all(char.isalnum() or char in "-_=" for char in p256dh + auth):
        raise ValueError("invalid_web_push_keys")
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        existing = connection.execute(
            "SELECT user_id FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,)
        ).fetchone()
        if existing is not None and int(existing["user_id"]) != int(user_id):
            raise ValueError("web_push_subscription_owned_by_another_account")
        connection.execute(
            """INSERT INTO web_push_subscriptions (
                   user_id, endpoint, p256dh, auth, user_agent, active,
                   failure_count, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                   user_id = excluded.user_id, p256dh = excluded.p256dh,
                   auth = excluded.auth, user_agent = excluded.user_agent,
                   active = 1, failure_count = 0, updated_at = excluded.updated_at""",
            (int(user_id), endpoint, p256dh, auth, str(user_agent or "")[:300], now, now),
        )
        connection.commit()
        row = connection.execute(
            "SELECT id, user_id, active, created_at, updated_at FROM web_push_subscriptions WHERE endpoint = ? AND user_id = ?",
            (endpoint, int(user_id)),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        connection.close()


def remove_web_push_subscription(
    user_id: int,
    endpoint: str,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> bool:
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            "DELETE FROM web_push_subscriptions WHERE user_id = ? AND endpoint = ?",
            (int(user_id), str(endpoint or "").strip()),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def web_push_subscription_count(user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH) -> int:
    connection = _connect(db_path)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM web_push_subscriptions WHERE user_id = ? AND active = 1",
                (int(user_id),),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def pending_web_push_deliveries(
    *, db_path: Path | str = DEFAULT_DB_PATH, limit: int = 100
) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            """SELECT n.id AS notification_id, n.user_id, n.title, n.body,
                      s.id AS subscription_id, s.endpoint, s.p256dh, s.auth,
                      COALESCE(d.attempts, 0) AS attempts
               FROM in_app_notifications n
               JOIN web_push_subscriptions s
                 ON s.user_id = n.user_id AND s.active = 1
                    AND n.created_at >= s.created_at
               LEFT JOIN web_push_deliveries d
                 ON d.notification_id = n.id AND d.subscription_id = s.id
               WHERE (d.status IS NULL OR d.status = 'pending')
                 AND COALESCE(d.attempts, 0) < 3
               ORDER BY n.id, s.id LIMIT ?""",
            (max(1, min(500, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def record_web_push_delivery(
    notification_id: int,
    subscription_id: int,
    *,
    status: str,
    error: str = "",
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    if status not in {"pending", "success", "permanent_failure"}:
        raise ValueError("invalid_web_push_delivery_status")
    now = _utc_iso()
    connection = _connect(db_path)
    try:
        connection.execute(
            """INSERT INTO web_push_deliveries (
                   notification_id, subscription_id, status, attempts, last_error, updated_at
               ) VALUES (?, ?, ?, 1, ?, ?)
               ON CONFLICT(notification_id, subscription_id) DO UPDATE SET
                   status = excluded.status, attempts = web_push_deliveries.attempts + 1,
                   last_error = excluded.last_error, updated_at = excluded.updated_at""",
            (int(notification_id), int(subscription_id), status, str(error or "")[:300], now),
        )
        if status == "success":
            connection.execute(
                "UPDATE web_push_subscriptions SET failure_count = 0, last_success_at = ?, updated_at = ? WHERE id = ?",
                (now, now, int(subscription_id)),
            )
        else:
            connection.execute(
                """UPDATE web_push_subscriptions SET
                       failure_count = failure_count + 1,
                       active = CASE WHEN ? = 'permanent_failure' THEN 0 ELSE active END,
                       updated_at = ? WHERE id = ?""",
                (status, now, int(subscription_id)),
            )
        connection.commit()
    finally:
        connection.close()


def _validate_web_push_endpoint(endpoint: str) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = str(parsed.hostname or "").casefold()
    allowed = {
        "fcm.googleapis.com",
        "web.push.apple.com",
        "push.services.mozilla.com",
        "updates.push.services.mozilla.com",
    }
    allowed.update(
        item.strip().casefold()
        for item in os.environ.get("SPREADBOARD_WEB_PUSH_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    )
    host_allowed = host in allowed or host.endswith(".notify.windows.com")
    if parsed.scheme != "https" or not host_allowed or not parsed.path or len(endpoint) > 2048:
        raise ValueError("invalid_web_push_endpoint")
