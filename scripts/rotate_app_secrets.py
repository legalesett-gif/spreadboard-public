#!/usr/bin/env python3
"""Rotate SpreadBoard-owned secrets without emitting any secret value.

This script is intended to run on the production host during a coordinated
maintenance window. It preserves encrypted notification fields by rewrapping
them under a new Fernet key, invalidates old browser-push subscriptions when
the VAPID identity changes, and removes obsolete bootstrap credentials.
Provider-issued API keys are deliberately out of scope: only the issuing
provider can revoke and replace those credentials.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
import secrets
import sqlite3

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


ENV_PATH = Path("/opt/spreadboard/secrets/app.env")
DB_PATH = Path("/opt/spreadboard/runtime/spreadboard_accounts.sqlite3")


def _parse(path: Path) -> list[tuple[str | None, str]]:
    rows = []
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            rows.append((None, line))
            continue
        name, value = line.split("=", 1)
        rows.append((name, value))
    return rows


def _vapid_pair() -> tuple[str, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_number = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_point = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return _b64url(public_point), _b64url(private_number)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def rotate() -> dict[str, object]:
    rows = _parse(ENV_PATH)
    current = {name: value for name, value in rows if name}
    old_field = current.get("SPREADBOARD_FIELD_ENCRYPTION_KEY", "")
    if not old_field:
        raise RuntimeError("field_encryption_key_missing")
    new_field = Fernet.generate_key().decode("ascii")
    new_vapid_public, new_vapid_private = _vapid_pair()
    replacements = {
        "SPREADBOARD_FIELD_ENCRYPTION_KEY": new_field,
        "SPREADBOARD_TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(48),
        "SPREADBOARD_VAPID_PUBLIC_KEY": new_vapid_public,
        "SPREADBOARD_VAPID_PRIVATE_KEY": new_vapid_private,
    }
    remove = {
        "SPREADBOARD_ADMIN_EMAIL",
        "SPREADBOARD_ADMIN_NAME",
        "SPREADBOARD_ADMIN_PASSWORD",
    }

    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        encrypted = connection.execute(
            "SELECT user_id, pushover_user_key_encrypted FROM notification_preferences "
            "WHERE pushover_user_key_encrypted IS NOT NULL AND pushover_user_key_encrypted != ''"
        ).fetchall()
        old_fernet = Fernet(old_field.encode("ascii"))
        new_fernet = Fernet(new_field.encode("ascii"))
        for user_id, ciphertext in encrypted:
            try:
                plaintext = old_fernet.decrypt(str(ciphertext).encode("ascii"))
            except InvalidToken as exc:
                raise RuntimeError("notification_rewrap_failed") from exc
            connection.execute(
                "UPDATE notification_preferences SET pushover_user_key_encrypted = ? WHERE user_id = ?",
                (new_fernet.encrypt(plaintext).decode("ascii"), int(user_id)),
            )
        push_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM web_push_subscriptions WHERE active = 1"
            ).fetchone()[0]
        )
        connection.execute("UPDATE web_push_subscriptions SET active = 0 WHERE active = 1")

        output = []
        seen = set()
        for name, value in rows:
            if name is None:
                output.append(value)
                continue
            if name in remove:
                continue
            if name in replacements:
                output.append(f"{name}={replacements[name]}")
                seen.add(name)
            else:
                output.append(f"{name}={value}")
        for name, value in replacements.items():
            if name not in seen:
                output.append(f"{name}={value}")
        temporary = ENV_PATH.with_name("app.env.rotating")
        temporary.write_text("\n".join(output).rstrip() + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, ENV_PATH)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "ok": True,
        "rotated": sorted(replacements),
        "removed_bootstrap_credentials": sorted(remove),
        "notification_fields_rewrapped": len(encrypted),
        "browser_push_subscriptions_invalidated": push_count,
        "provider_credentials_rotated": 0,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(rotate(), sort_keys=True))
