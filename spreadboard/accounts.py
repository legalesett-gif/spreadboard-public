"""Authenticated SpreadBoard accounts and user-owned portfolio records."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
DEFAULT_DB_PATH = RUNTIME_DIR / "spreadboard_accounts.sqlite3"
SESSION_COOKIE = "spreadboard_session"
SESSION_DAYS = 30
_REQUEST_STATE = threading.local()


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str
    role: str
    subscription_status: str
    subscription_expires_at: str | None
    monthly_capital_usd: float | None
    billing_customer_id: str | None = None
    billing_subscription_id: str | None = None
    subscription_cancel_at_period_end: bool = False
    csrf_token: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

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

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "is_admin": self.is_admin,
            "subscription_status": self.subscription_status,
            "subscription_expires_at": self.subscription_expires_at,
            "subscription_active": self.subscription_active,
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
                monthly_capital_usd REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
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
            CREATE INDEX IF NOT EXISTS sessions_token_hash ON sessions(token_hash);
            CREATE INDEX IF NOT EXISTS sessions_user_expiry ON sessions(user_id, expires_at);
            CREATE INDEX IF NOT EXISTS positions_user_status ON positions(user_id, status, opened_at DESC);
            CREATE INDEX IF NOT EXISTS funding_position_time ON funding_cashflows(position_id, occurred_at);
            CREATE INDEX IF NOT EXISTS alert_rules_user_position ON position_alert_rules(user_id, position_id, enabled);
            CREATE INDEX IF NOT EXISTS notifications_user_time ON in_app_notifications(user_id, created_at DESC);
            """
        )
        _ensure_columns(connection, "users", {
            "billing_provider": "TEXT",
            "billing_customer_id": "TEXT",
            "billing_subscription_id": "TEXT",
            "subscription_cancel_at_period_end": "INTEGER NOT NULL DEFAULT 0",
            "billing_updated_at": "TEXT",
        })
        _ensure_columns(connection, "position_alert_rules", {
            "last_condition_met": "INTEGER NOT NULL DEFAULT 0",
        })
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
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
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
            SELECT u.*, s.csrf_token, s.id AS session_id
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (_token_hash(token), _utc_iso()),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
            (_utc_iso(), row["session_id"]),
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
    status = subscription_status if subscription_status in {"inactive", "trialing", "active"} else "trialing"
    now = datetime.now(tz=timezone.utc)
    expires = now + timedelta(days=max(1, min(3660, int(subscription_days))))
    connection = _connect(db_path)
    try:
        try:
            cursor = connection.execute(
                """
                INSERT INTO users (
                    email, display_name, password_hash, role, subscription_status,
                    subscription_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'member', ?, ?, ?, ?)
                """,
                (normalized_email, clean_name, hash_password(password), status,
                 _utc_iso(expires), _utc_iso(now), _utc_iso(now)),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("email_already_registered") from exc
        connection.commit()
        return get_user(int(cursor.lastrowid), db_path=db_path) or {}
    finally:
        connection.close()


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


def list_alert_user_ids(*, db_path: Path | str = DEFAULT_DB_PATH) -> list[int]:
    connection = _connect(db_path)
    try:
        return [int(row["user_id"]) for row in connection.execute(
            """SELECT DISTINCT p.user_id FROM positions p
               JOIN position_alert_rules r ON r.position_id = p.id AND r.user_id = p.user_id
               WHERE p.status = 'open' AND r.enabled = 1 ORDER BY p.user_id"""
        ).fetchall()]
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
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    if status not in {"inactive", "trialing", "active", "past_due", "cancelled"}:
        raise ValueError("invalid_subscription_status")
    normalized_expiry = _normalize_iso(expires_at) if expires_at else None
    connection = _connect(db_path)
    try:
        connection.execute(
            "UPDATE users SET subscription_status = ?, subscription_expires_at = ?, updated_at = ? WHERE id = ?",
            (status, normalized_expiry, _utc_iso(), user_id),
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
    obj = ((event.get("data") or {}).get("object") or {})
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
                if isinstance(obj.get("parent"), dict) else None
            )
        subscription_id = _stripe_id(raw_subscription, "sub_")
        user_id = _billing_user_id(connection, obj, customer_id)
        result = "ignored_no_user"
        if user_id is not None:
            _assert_customer_owner(connection, user_id, customer_id)
            now = _utc_iso()
            explicit_user = bool(
                (isinstance(obj.get("metadata"), dict) and obj["metadata"].get("spreadboard_user_id"))
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
                       billing_subscription_id = COALESCE(?, billing_subscription_id), billing_updated_at = ?, updated_at = ? WHERE id = ?""",
                    (customer_id, subscription_id, now, now, user_id),
                )
                result = "customer_linked"
            elif event_type.startswith("customer.subscription.") and mismatched_subscription and not explicit_user:
                result = "ignored_subscription_mismatch"
            elif event_type.startswith("customer.subscription."):
                status = _subscription_status(str(obj.get("status") or ""), event_type)
                expiry = _stripe_period_end(obj)
                connection.execute(
                    """UPDATE users SET billing_provider = 'stripe', billing_customer_id = COALESCE(?, billing_customer_id),
                       billing_subscription_id = COALESCE(?, billing_subscription_id), subscription_status = ?,
                       subscription_expires_at = ?, subscription_cancel_at_period_end = ?, billing_updated_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (customer_id, subscription_id, status, expiry, int(bool(obj.get("cancel_at_period_end"))), now, now, user_id),
                )
                result = f"subscription_{status}"
            elif event_type.startswith("invoice.") and (not subscription_id or mismatched_subscription):
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


def update_account_settings(
    user_id: int,
    *,
    display_name: str | None = None,
    monthly_capital_usd: float | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    connection = _connect(db_path)
    try:
        row = connection.execute("SELECT display_name FROM users WHERE id = ?", (user_id,)).fetchone()
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
        positions = [dict(row) for row in connection.execute(
            "SELECT * FROM positions WHERE user_id = ? ORDER BY status DESC, opened_at DESC",
            (user_id,),
        ).fetchall()]
        for position in positions:
            position["funding_cashflows"] = [dict(row) for row in connection.execute(
                "SELECT * FROM funding_cashflows WHERE user_id = ? AND position_id = ? ORDER BY occurred_at",
                (user_id, position["id"]),
            ).fetchall()]
            position["alert_rules"] = [dict(row) for row in connection.execute(
                "SELECT * FROM position_alert_rules WHERE user_id = ? AND position_id = ? ORDER BY created_at",
                (user_id, position["id"]),
            ).fetchall()]
        return positions
    finally:
        connection.close()


def create_position(user_id: int, payload: dict[str, Any], *, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    required_text = (
        "token", "long_venue", "long_market_type", "short_venue", "short_market_type"
    )
    values = {key: str(payload.get(key) or "").strip() for key in required_text}
    if not all(values.values()):
        raise ValueError("position_route_fields_required")
    numeric = {
        key: _positive_float(payload.get(key), key)
        for key in (
            "long_quantity", "long_entry_price", "short_quantity", "short_entry_price"
        )
    }
    now = _utc_iso()
    opened_at = _normalize_iso(str(payload.get("opened_at") or now))
    long_symbol = str(payload.get("long_symbol") or "").strip() or None
    short_symbol = str(payload.get("short_symbol") or "").strip() or None
    entry_spread = payload.get("entry_spread_pct")
    if entry_spread in (None, ""):
        entry_spread = (numeric["short_entry_price"] / numeric["long_entry_price"] - 1) * 100
    route_key = str(payload.get("route_key") or "").strip() or "|".join(
        (
            values["token"].upper(), values["long_venue"], values["long_market_type"],
            values["short_venue"], values["short_market_type"],
        )
    )
    connection = _connect(db_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO positions (
                user_id, token, route_key, long_venue, long_market_type, long_symbol,
                long_quantity, long_entry_price, short_venue, short_market_type,
                short_symbol, short_quantity, short_entry_price, entry_spread_pct,
                capital_usd, entry_fees_usd, opened_at, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, values["token"].upper(), route_key, values["long_venue"],
                values["long_market_type"], long_symbol, numeric["long_quantity"],
                numeric["long_entry_price"], values["short_venue"],
                values["short_market_type"], short_symbol, numeric["short_quantity"],
                numeric["short_entry_price"], float(entry_spread),
                _optional_nonnegative_float(payload.get("capital_usd")),
                _optional_nonnegative_float(payload.get("entry_fees_usd")) or 0.0,
                opened_at, str(payload.get("notes") or "")[:2000], now, now,
            ),
        )
        connection.commit()
        position_id = int(cursor.lastrowid)
    finally:
        connection.close()
    return next(item for item in list_positions(user_id, db_path=db_path) if item["id"] == position_id)


def close_position(user_id: int, position_id: int, payload: dict[str, Any], *, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
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
                closed_at, long_exit, short_exit,
                _optional_nonnegative_float(payload.get("exit_fees_usd")) or 0.0,
                _utc_iso(), position_id, user_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("open_position_not_found")
        connection.commit()
    finally:
        connection.close()
    return next(item for item in list_positions(user_id, db_path=db_path) if item["id"] == position_id)


def add_funding_cashflow(user_id: int, position_id: int, payload: dict[str, Any], *, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
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
            (user_id, position_id, venue, amount, occurred_at, str(payload.get("note") or "")[:500], _utc_iso()),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM funding_cashflows WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    finally:
        connection.close()


def add_alert_rule(user_id: int, position_id: int, payload: dict[str, Any], *, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
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
        row = connection.execute("SELECT * FROM position_alert_rules WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    finally:
        connection.close()


def list_notifications(user_id: int, *, limit: int = 30, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    connection = _connect(db_path)
    try:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM in_app_notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(200, int(limit)))),
        ).fetchall()]
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
        row = connection.execute("SELECT * FROM in_app_notifications WHERE id = ?", (cursor.lastrowid,)).fetchone()
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


def mark_notifications_read(
    user_id: int, *, db_path: Path | str = DEFAULT_DB_PATH
) -> int:
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
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


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
        monthly_capital_usd=float(row["monthly_capital_usd"]) if row["monthly_capital_usd"] is not None else None,
        billing_customer_id=row["billing_customer_id"] if "billing_customer_id" in keys else None,
        billing_subscription_id=row["billing_subscription_id"] if "billing_subscription_id" in keys else None,
        subscription_cancel_at_period_end=bool(row["subscription_cancel_at_period_end"]) if "subscription_cancel_at_period_end" in keys else False,
        csrf_token=csrf_token,
    )


def _stripe_id(value: Any, prefix: str) -> str | None:
    candidate = str(value or "")
    return candidate[:255] if candidate.startswith(prefix) else None


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
        items = ((obj.get("items") or {}).get("data") or []) if isinstance(obj.get("items"), dict) else []
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
    return (value or datetime.now(tz=timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _utc_iso(parsed)


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
