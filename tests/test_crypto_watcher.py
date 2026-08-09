"""Arbitrum log watcher. No network access -- the RPC transport is injected."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spreadboard import accounts, crypto_billing, crypto_watcher  # noqa: E402


RECEIVER = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
OTHER = "0x2222222222222222222222222222222222222222"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", RECEIVER)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    monkeypatch.setenv("SPREADBOARD_CRYPTO_CONFIRMATIONS", "6")
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def make_user(db, email="a@example.com") -> int:
    created = accounts.create_user(
        email=email, display_name="A", password="correct horse battery staple",
        subscription_status="inactive", subscription_days=0, db_path=db,
    )
    user_id = int(created["id"])
    accounts.update_subscription(user_id, status="inactive", expires_at=None, db_path=db)
    return user_id


def transfer_log(dollars: float, *, tx="0xaaa", log_index=0, token=USDC, block=900,
                 sender="0x1111111111111111111111111111111111111111"):
    return {
        "address": token,
        "topics": [
            crypto_billing.TRANSFER_TOPIC,
            "0x" + sender.removeprefix("0x").rjust(64, "0"),
            "0x" + RECEIVER.removeprefix("0x").rjust(64, "0"),
        ],
        "data": hex(int(round(dollars * 1_000_000))),
        "transactionHash": tx,
        "logIndex": hex(log_index),
        "blockNumber": hex(block),
    }


def fake_rpc(head: int, logs: list[dict]):
    seen: dict[str, Any] = {}

    def transport(url, method, params):
        if method == "eth_blockNumber":
            return hex(head)
        if method == "eth_getLogs":
            seen["filter"] = params[0]
            return logs
        raise AssertionError(f"unexpected rpc method {method}")

    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def test_settles_a_matching_transfer(db):
    user_id = make_user(db)
    crypto_billing.create_invoice(user_id, 30, db_path=db)
    result = crypto_watcher.scan_once(db_path=db, rpc_call=fake_rpc(1000, [transfer_log(149.0)]))
    assert result["ok"] is True
    assert [r["resolution"] for r in result["results"]] == ["settled"]
    assert accounts.get_user_object(user_id, db_path=db).subscription_status == "active"


def test_only_scans_confirmed_blocks(db):
    transport = fake_rpc(1000, [])
    crypto_watcher.scan_once(db_path=db, rpc_call=transport)
    to_block = int(transport.seen["filter"]["toBlock"], 16)
    assert to_block <= 1000 - 6, "must stay behind the confirmation depth"


def test_filter_is_scoped_to_allowlisted_tokens_and_receiver(db):
    transport = fake_rpc(1000, [])
    crypto_watcher.scan_once(db_path=db, rpc_call=transport)
    flt = transport.seen["filter"]
    assert set(a.lower() for a in flt["address"]) == set(crypto_billing.TOKENS.keys())
    assert flt["topics"][0] == crypto_billing.TRANSFER_TOPIC
    assert flt["topics"][2].endswith(RECEIVER.removeprefix("0x"))


def test_cursor_advances_and_prevents_reprocessing(db):
    user_id = make_user(db)
    crypto_billing.create_invoice(user_id, 30, db_path=db)
    log = transfer_log(149.0)

    first = crypto_watcher.scan_once(db_path=db, rpc_call=fake_rpc(1000, [log]))
    cursor = crypto_watcher.get_cursor(db_path=db)
    assert cursor == first["to_block"]

    # Same log served again (as after a restart) must not grant a second period.
    expiry = accounts.get_user_object(user_id, db_path=db).subscription_expires_at
    second = crypto_watcher.scan_once(db_path=db, rpc_call=fake_rpc(1200, [log]))
    assert [r["resolution"] for r in second["results"]] == ["duplicate"]
    assert accounts.get_user_object(user_id, db_path=db).subscription_expires_at == expiry


def test_impostor_token_in_logs_is_ignored(db):
    user_id = make_user(db)
    crypto_billing.create_invoice(user_id, 30, db_path=db)
    result = crypto_watcher.scan_once(
        db_path=db, rpc_call=fake_rpc(1000, [transfer_log(149.0, token=OTHER)])
    )
    assert [r["resolution"] for r in result["results"]] == ["ignored"]
    assert accounts.get_user_object(user_id, db_path=db).subscription_status == "inactive"


def test_one_malformed_log_does_not_stall_the_cursor(db):
    user_id = make_user(db)
    crypto_billing.create_invoice(user_id, 30, db_path=db)
    broken = {"address": USDC, "topics": [], "data": "0x", "transactionHash": "0xbad"}
    result = crypto_watcher.scan_once(
        db_path=db, rpc_call=fake_rpc(1000, [broken, transfer_log(149.0, tx="0xgood")])
    )
    assert result["ok"] is True
    assert crypto_watcher.get_cursor(db_path=db) > 0
    assert accounts.get_user_object(user_id, db_path=db).subscription_status == "active"


def test_unconfigured_watcher_is_inert(db, monkeypatch):
    monkeypatch.delenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", raising=False)
    result = crypto_watcher.scan_once(db_path=db, rpc_call=fake_rpc(1000, [transfer_log(149.0)]))
    assert result == {"ok": False, "reason": "not_configured"}


def test_start_background_returns_none_when_unconfigured(db, monkeypatch):
    monkeypatch.delenv("SPREADBOARD_CRYPTO_RPC_URL", raising=False)
    assert crypto_watcher.start_background(db_path=db) is None
