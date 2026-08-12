"""Validation and display rules for opt-in private-ledger connections."""

from __future__ import annotations

from typing import Any


TERMS_VERSION = "2026-08-12"
VENUES: dict[str, dict[str, Any]] = {
    "aster": {
        "label": "Aster",
        "fields": ("api_key", "secret"),
        "field_labels": {"api_key": "Wallet address", "secret": "Private account key"},
        "sensitive_signer": True,
    },
    "binance": {"label": "Binance", "fields": ("api_key", "secret")},
    "bingx": {"label": "BingX", "fields": ("api_key", "secret")},
    "bitget": {"label": "Bitget", "fields": ("api_key", "secret", "passphrase")},
    "bybit": {"label": "Bybit", "fields": ("api_key", "secret")},
    "gate": {"label": "Gate", "fields": ("api_key", "secret")},
    "kucoin": {"label": "KuCoin", "fields": ("api_key", "secret", "passphrase")},
    "mexc": {"label": "MEXC", "fields": ("api_key", "secret")},
    "okx": {"label": "OKX", "fields": ("api_key", "secret", "passphrase")},
}


def normalize_venue(value: str) -> str:
    compact = "".join(character for character in str(value or "").casefold() if character.isalnum())
    aliases = {
        "asterfutures": "aster",
        "bingx": "bingx",
        "bitgetfutures": "bitget",
        "gateio": "gate",
        "gatefutures": "gate",
        "kucoinfutures": "kucoin",
        "mexc": "mexc",
        "mexcfutures": "mexc",
        "okx": "okx",
    }
    slug = aliases.get(compact, compact)
    if slug not in VENUES:
        raise ValueError("unsupported_exchange_connection")
    return slug


def clean_payload(venue: str, payload: dict[str, Any]) -> dict[str, str]:
    slug = normalize_venue(venue)
    spec = VENUES[slug]
    result: dict[str, str] = {}
    for field in spec["fields"]:
        value = str(payload.get(field) or "").strip()
        if not value:
            raise ValueError(f"{field}_required")
        if len(value) > 1024:
            raise ValueError(f"{field}_too_long")
        result[field] = value
    if not payload.get("read_only_confirmed"):
        raise ValueError("read_only_api_permissions_confirmation_required")
    if spec.get("sensitive_signer") and not payload.get("sensitive_signer_confirmed"):
        raise ValueError("aster_private_account_key_confirmation_required")
    return result


def validate_consent(venue: str, payload: dict[str, Any]) -> list[str]:
    slug = normalize_venue(venue)
    spec = VENUES[slug]
    if not payload.get("read_only_confirmed"):
        raise ValueError("read_only_api_permissions_confirmation_required")
    if spec.get("sensitive_signer") and not payload.get("sensitive_signer_confirmed"):
        raise ValueError("aster_private_account_key_confirmation_required")
    fields = payload.get("credential_fields")
    if not isinstance(fields, list) or sorted(set(map(str, fields))) != sorted(spec["fields"]):
        raise ValueError("invalid_credential_fields")
    return list(spec["fields"])


def public_catalog() -> list[dict[str, Any]]:
    return [
        {
            "slug": slug,
            "label": spec["label"],
            "fields": list(spec["fields"]),
            "field_labels": spec.get("field_labels", {}),
            "sensitive_signer": bool(spec.get("sensitive_signer")),
        }
        for slug, spec in VENUES.items()
    ]
