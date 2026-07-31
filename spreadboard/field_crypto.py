"""Small envelope for encrypting user notification credentials at rest."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class FieldCryptoError(RuntimeError):
    """Raised without exposing a secret or ciphertext."""


def configured() -> bool:
    return bool(os.environ.get("SPREADBOARD_FIELD_ENCRYPTION_KEY", "").strip())


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError) as exc:
        raise FieldCryptoError("encrypted_field_unavailable") from exc


def _fernet() -> Fernet:
    key = os.environ.get("SPREADBOARD_FIELD_ENCRYPTION_KEY", "").strip()
    if not key:
        raise FieldCryptoError("field_encryption_not_configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise FieldCryptoError("invalid_field_encryption_key") from exc
