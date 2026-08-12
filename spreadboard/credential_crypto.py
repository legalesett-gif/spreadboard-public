"""Envelope encryption for subscriber-owned exchange credentials.

The web application receives only the RSA public key and can therefore seal a
credential bundle but cannot recover it.  The accounting worker is the only
service given the private key.  Each ciphertext is bound to its user and venue
so copying a database row to another account does not make it decryptable.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SCHEMA = "spreadboard.exchange_credentials.v1"


def public_key_path() -> Path | None:
    value = os.environ.get("SPREADBOARD_ACCOUNTING_PUBLIC_KEY_FILE", "").strip()
    return Path(value) if value else None


def private_key_path() -> Path | None:
    value = os.environ.get("SPREADBOARD_ACCOUNTING_PRIVATE_KEY_FILE", "").strip()
    return Path(value) if value else None


def encryption_available() -> bool:
    path = public_key_path()
    return bool(path and path.is_file())


def public_key_pem() -> str:
    return _read_required(public_key_path(), "accounting_public_key_missing").decode("ascii")


def decryption_available() -> bool:
    path = private_key_path()
    return bool(path and path.is_file())


def generate_key_pair() -> tuple[bytes, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def encrypt(payload: dict[str, Any], *, context: str, public_pem: bytes | None = None) -> str:
    if not context:
        raise ValueError("credential_context_required")
    key_bytes = public_pem or _read_required(public_key_path(), "accounting_public_key_missing")
    public_key = serialization.load_pem_public_key(key_bytes)
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise ValueError("invalid_accounting_public_key")
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, context.encode("utf-8"))
    wrapped_key = public_key.encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return json.dumps(
        {
            "schema": SCHEMA,
            "wrapped_key": _encode(wrapped_key),
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def decrypt(
    envelope: str, *, context: str, private_pem: bytes | None = None
) -> dict[str, Any]:
    if not context:
        raise ValueError("credential_context_required")
    private_bytes = private_pem or _read_required(
        private_key_path(), "accounting_private_key_missing"
    )
    private_key = serialization.load_pem_private_key(private_bytes, password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("invalid_accounting_private_key")
    try:
        data = json.loads(envelope)
        if data.get("schema") != SCHEMA:
            raise ValueError("unsupported_credential_envelope")
        data_key = private_key.decrypt(
            _decode(data["wrapped_key"]),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        plaintext = AESGCM(data_key).decrypt(
            _decode(data["nonce"]),
            _decode(data["ciphertext"]),
            context.encode("utf-8"),
        )
        payload = json.loads(plaintext)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_credential_envelope") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid_credential_payload")
    return payload


def validate_envelope(envelope: str) -> None:
    """Reject malformed or oversized client-sealed envelopes without decrypting."""

    if not envelope or len(envelope) > 16_384:
        raise ValueError("invalid_credential_envelope")
    try:
        data = json.loads(envelope)
        if data.get("schema") != SCHEMA:
            raise ValueError("unsupported_credential_envelope")
        wrapped = _decode(data["wrapped_key"])
        nonce = _decode(data["nonce"])
        ciphertext = _decode(data["ciphertext"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_credential_envelope") from exc
    if not 256 <= len(wrapped) <= 1024 or len(nonce) != 12 or not 16 <= len(ciphertext) <= 8192:
        raise ValueError("invalid_credential_envelope")


def context(user_id: int, venue: str) -> str:
    return f"spreadboard:exchange-credential:{int(user_id)}:{venue.strip().casefold()}"


def _read_required(path: Path | None, error: str) -> bytes:
    if path is None or not path.is_file():
        raise RuntimeError(error)
    return path.read_bytes()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    raw = str(value).encode("ascii")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
