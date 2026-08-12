from __future__ import annotations

import json

import pytest

from spreadboard import accounts, credential_crypto, exchange_credentials


def user(db_path, email: str) -> dict:
    return accounts.create_user(
        email=email,
        display_name=email.split("@")[0],
        password="correct-horse-battery-staple",
        subscription_status="active",
        db_path=db_path,
    )


def position_payload(token: str = "GUA") -> dict:
    return {
        "token": token,
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_symbol": f"{token}/USDT",
        "long_quantity": 10,
        "long_entry_price": 1,
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_symbol": f"{token}/USDT:USDT",
        "short_quantity": 10,
        "short_entry_price": 1,
    }


def test_position_delete_is_owner_scoped_confirmed_and_cascades(tmp_path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    owner = user(db_path, "owner@example.test")
    attacker = user(db_path, "attacker@example.test")
    created = accounts.create_position(owner["id"], position_payload(), db_path=db_path)
    accounts.add_funding_cashflow(
        owner["id"],
        created["id"],
        {"venue": "Gate", "amount_usd": 2, "occurred_at": "2026-08-12T00:00:00Z"},
        db_path=db_path,
    )
    accounts.add_alert_rule(
        owner["id"],
        created["id"],
        {"metric": "pnl_usd", "operator": "gte", "threshold": 10},
        db_path=db_path,
    )

    with pytest.raises(ValueError, match="position_not_found"):
        accounts.delete_position(
            attacker["id"], created["id"], confirm_token="GUA", db_path=db_path
        )
    with pytest.raises(ValueError, match="position_delete_confirmation_mismatch"):
        accounts.delete_position(
            owner["id"], created["id"], confirm_token="WRONG", db_path=db_path
        )

    deleted = accounts.delete_position(
        owner["id"], created["id"], confirm_token="gua", db_path=db_path
    )
    assert deleted["exchange_actions"] == 0
    assert deleted["deleted_related"]["funding_cashflows"] == 1
    assert deleted["deleted_related"]["alert_rules"] == 1
    assert accounts.list_positions(owner["id"], db_path=db_path) == []


def test_exchange_credentials_are_enveloped_bound_and_never_listed(tmp_path) -> None:
    private_pem, public_pem = credential_crypto.generate_key_pair()
    payload = {"api_key": "not-a-real-key", "secret": "not-a-real-secret"}
    envelope = credential_crypto.encrypt(
        payload,
        context=credential_crypto.context(7, "gate"),
        public_pem=public_pem,
    )
    assert "not-a-real" not in envelope
    assert credential_crypto.decrypt(
        envelope,
        context=credential_crypto.context(7, "gate"),
        private_pem=private_pem,
    ) == payload
    with pytest.raises(Exception):
        credential_crypto.decrypt(
            envelope,
            context=credential_crypto.context(8, "gate"),
            private_pem=private_pem,
        )

    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    member = user(db_path, "member@example.test")
    saved = accounts.save_exchange_connection(
        member["id"],
        "gate",
        envelope,
        credential_fields=["api_key", "secret"],
        terms_version=exchange_credentials.TERMS_VERSION,
        db_path=db_path,
    )
    assert "credential_encrypted" not in saved
    assert "not-a-real" not in json.dumps(saved)
    worker_row = accounts.encrypted_exchange_connections(db_path=db_path)[0]
    assert worker_row["credential_encrypted"] == envelope
    accounts.disconnect_exchange_connection(member["id"], "gate", db_path=db_path)
    assert accounts.list_exchange_connections(member["id"], db_path=db_path) == []


def test_connection_validation_requires_read_only_and_explicit_aster_consent() -> None:
    with pytest.raises(ValueError, match="read_only"):
        exchange_credentials.clean_payload(
            "gate", {"api_key": "key", "secret": "secret"}
        )
    with pytest.raises(ValueError, match="aster_private"):
        exchange_credentials.clean_payload(
            "aster",
            {"api_key": "wallet", "secret": "signer", "read_only_confirmed": True},
        )
    assert exchange_credentials.clean_payload(
        "aster",
        {
            "api_key": "wallet",
            "secret": "signer",
            "read_only_confirmed": True,
            "sensitive_signer_confirmed": True,
        },
    ) == {"api_key": "wallet", "secret": "signer"}
    assert exchange_credentials.validate_consent(
        "gate",
        {
            "credential_fields": ["secret", "api_key"],
            "read_only_confirmed": True,
        },
    ) == ["api_key", "secret"]


def test_server_can_validate_but_not_decrypt_client_envelope(tmp_path, monkeypatch) -> None:
    private_pem, public_pem = credential_crypto.generate_key_pair()
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(public_pem)
    monkeypatch.setenv("SPREADBOARD_ACCOUNTING_PUBLIC_KEY_FILE", str(public_path))
    envelope = credential_crypto.encrypt(
        {"api_key": "key", "secret": "secret"},
        context=credential_crypto.context(1, "gate"),
        public_pem=public_pem,
    )
    credential_crypto.validate_envelope(envelope)
    assert credential_crypto.encryption_available()
    assert not credential_crypto.decryption_available()
    with pytest.raises(RuntimeError, match="accounting_private_key_missing"):
        credential_crypto.decrypt(envelope, context=credential_crypto.context(1, "gate"))
