"""Watching three chains without crediting anyone the wrong amount.

The failure that matters is silent: a BSC transfer read with Arbitrum's six
decimals credits a payment a trillion times over, and a chain with no watcher
takes money and grants nothing.
"""

from __future__ import annotations

import pytest

from spreadboard import accounts, crypto_billing, crypto_watcher

ARB = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
TRON = "TBQuKW6Jj1LhmTQV8ziNqGDNLNVW3hXaPz"
BSC_USDT = "0x55d398326f99059ff775485246999027b3197955"
TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", ARB)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    monkeypatch.setenv("SPREADBOARD_CRYPTO_BSC_RECEIVING_ADDRESS", ARB)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_TRON_RECEIVING_ADDRESS", TRON)
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def _member(db, cents: int):
    user = accounts.create_user(
        email="chainbuyer@example.test",
        display_name="Chain Buyer",
        password="a-secure-chain-password",
        subscription_status="inactive",
        db_path=db,
    )
    accounts.update_subscription(user["id"], status="inactive", expires_at=None, db_path=db)
    return crypto_billing.create_invoice(
        user["id"], 30, tier="research_pro", list_amount_cents=cents, db_path=db
    )


# --------------------------------------------------------------------------
# Which chains are watchable at all
# --------------------------------------------------------------------------


def test_public_endpoints_make_a_configured_chain_watchable(db) -> None:
    assert {c.key for c in crypto_watcher.watchable_chains()} == {"arbitrum", "bsc", "tron"}


def test_a_chain_with_no_endpoint_is_not_watchable(db, monkeypatch) -> None:
    monkeypatch.setitem(crypto_watcher.DEFAULT_RPC_URLS, "tron", "")
    monkeypatch.setenv("SPREADBOARD_CRYPTO_TRON_API_URL", "")

    assert "tron" not in {c.key for c in crypto_watcher.watchable_chains()}


def test_a_chain_with_no_address_is_not_watchable(db, monkeypatch) -> None:
    monkeypatch.delenv("SPREADBOARD_CRYPTO_BSC_RECEIVING_ADDRESS", raising=False)

    assert "bsc" not in {c.key for c in crypto_watcher.watchable_chains()}


# --------------------------------------------------------------------------
# BSC: the eighteen-decimal trap
# --------------------------------------------------------------------------


def test_a_bsc_transfer_credits_the_right_dollars(db) -> None:
    invoice = _member(db, 500)
    raw = 5 * 10**18  # $5.00 at eighteen decimals

    def rpc(_url, method, _params):
        if method == "eth_blockNumber":
            return hex(1_000)
        return [{
            "address": BSC_USDT,
            "data": hex(raw),
            "transactionHash": "0xbsc1",
            "logIndex": "0x0",
            "blockNumber": "0x1",
            "topics": ["t", "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + ARB[2:]],
        }]

    result = crypto_watcher.scan_evm("bsc", db_path=db, rpc_call=rpc)

    assert result["ok"]
    assert result["results"][0]["resolution"] == "settled"
    assert result["results"][0]["amount_cents"] == 500
    assert crypto_billing.get_invoice(invoice["id"], db_path=db)["status"] == "paid"


def test_the_bsc_scan_uses_its_own_cursor(db) -> None:
    calls = []

    def rpc(_url, method, params):
        calls.append(method)
        return hex(50_000) if method == "eth_blockNumber" else []

    crypto_watcher.scan_evm("bsc", db_path=db, rpc_call=rpc)

    assert accounts.chain_cursor("bsc", db_path=db) > 0
    assert accounts.chain_cursor("arbitrum", db_path=db) == 0, "chains must not share a cursor"


def test_the_bsc_scan_only_reads(db) -> None:
    seen = []

    def rpc(_url, method, _params):
        seen.append(method)
        return hex(1_000) if method == "eth_blockNumber" else []

    crypto_watcher.scan_evm("bsc", db_path=db, rpc_call=rpc)

    assert all(m in {"eth_blockNumber", "eth_getLogs"} for m in seen)


def test_a_bsc_scan_watches_only_bsc_contracts(db) -> None:
    captured = {}

    def rpc(_url, method, params):
        if method == "eth_blockNumber":
            return hex(1_000)
        captured["addresses"] = params[0]["address"]
        return []

    crypto_watcher.scan_evm("bsc", db_path=db, rpc_call=rpc)

    assert set(captured["addresses"]) == set(crypto_billing.CHAINS["bsc"].tokens)


# --------------------------------------------------------------------------
# Tron
# --------------------------------------------------------------------------


def test_a_tron_transfer_settles_its_invoice(db) -> None:
    invoice = _member(db, 500)
    # Anchor the cursor: a cold one starts at "now minus six hours", so a
    # fixture timestamp from the past would legitimately be behind it.
    accounts.set_chain_cursor("tron", 1_600_000_000_000, db_path=db)

    def http_get(_url):
        return {"data": [{
            "transaction_id": "abc123",
            "block_timestamp": 1_700_000_000_000,
            "from": "TXsender0000000000000000000000000",
            "to": TRON,
            "value": "5000000",
            "token_info": {"address": TRON_USDT, "decimals": 6, "symbol": "USDT"},
        }]}

    result = crypto_watcher.scan_tron(db_path=db, http_get=http_get)

    assert result["results"][0]["resolution"] == "settled"
    assert crypto_billing.get_invoice(invoice["id"], db_path=db)["status"] == "paid"
    assert accounts.chain_cursor("tron", db_path=db) == 1_700_000_000_000


def test_tron_only_asks_for_confirmed_transfers(db) -> None:
    seen = {}

    def http_get(url):
        seen["url"] = url
        return {"data": []}

    crypto_watcher.scan_tron(db_path=db, http_get=http_get)

    assert "only_confirmed=true" in seen["url"]
    assert TRON in seen["url"]


def test_tron_ignores_a_transfer_addressed_elsewhere(db) -> None:
    _member(db, 500)

    def http_get(_url):
        return {"data": [{
            "transaction_id": "abc123",
            "block_timestamp": 1_700_000_000_000,
            "from": "TXsender0000000000000000000000000",
            "to": "TSomeoneElse00000000000000000000",
            "value": "5000000",
            "token_info": {"address": TRON_USDT, "decimals": 6, "symbol": "USDT"},
        }]}

    result = crypto_watcher.scan_tron(db_path=db, http_get=http_get)

    assert result["results"] == []


def test_a_tron_impostor_token_is_ignored(db) -> None:
    _member(db, 500)

    def http_get(_url):
        return {"data": [{
            "transaction_id": "abc123",
            "block_timestamp": 1_700_000_000_000,
            "from": "TXsender0000000000000000000000000",
            "to": TRON,
            "value": "5000000",
            "token_info": {"address": "TFakeUSDT000000000000000000000000", "decimals": 6},
        }]}

    result = crypto_watcher.scan_tron(db_path=db, http_get=http_get)

    assert result["results"][0]["resolution"] == "ignored"


def test_one_bad_tron_entry_does_not_stall_the_cursor(db) -> None:
    accounts.set_chain_cursor("tron", 1_600_000_000_000, db_path=db)

    def http_get(_url):
        return {"data": [
            {"block_timestamp": 1_700_000_000_000},  # malformed
            {
                "transaction_id": "ok1",
                "block_timestamp": 1_700_000_001_000,
                "from": "TXsender0000000000000000000000000",
                "to": TRON,
                "value": "100",
                "token_info": {"address": TRON_USDT, "decimals": 6},
            },
        ]}

    crypto_watcher.scan_tron(db_path=db, http_get=http_get)

    assert accounts.chain_cursor("tron", db_path=db) == 1_700_000_001_000


def test_a_cold_tron_cursor_does_not_replay_all_history(db) -> None:
    seen = {}

    def http_get(url):
        seen["url"] = url
        return {"data": []}

    crypto_watcher.scan_tron(db_path=db, http_get=http_get)

    stamp = int(seen["url"].split("min_timestamp=")[1])
    assert stamp > 0, "a cold cursor must not start from the genesis block"


# --------------------------------------------------------------------------
# All chains together
# --------------------------------------------------------------------------


def test_one_dead_chain_does_not_stop_the_others(db, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(crypto_watcher, "scan_tron", boom)
    monkeypatch.setattr(crypto_watcher, "scan_evm", lambda *a, **k: {"ok": True, "chain": "bsc"})
    monkeypatch.setattr(crypto_watcher, "scan_once", lambda **k: {"ok": True, "chain": "arbitrum"})

    summaries = crypto_watcher.scan_all(db_path=db)

    assert any(s.get("chain") == "bsc" and s["ok"] for s in summaries)
    assert any(s.get("chain") == "tron" and not s["ok"] for s in summaries)


# --------------------------------------------------------------------------
# Regression: a real BSC payment went uncredited
# --------------------------------------------------------------------------


def test_bsc_reads_the_asset_transfer_index_not_a_log_scan(db, monkeypatch) -> None:
    """Every free BSC endpoint refuses eth_getLogs, at any range.

    A customer paid 5 USDT on BSC and nothing happened: the scan raised
    `-32005 limit exceeded` on every poll, so the cursor was never written and
    the transfer was never seen. eth_blockNumber worked, which is why the chain
    looked reachable. Alchemy is asked for its transfer index first.
    """
    monkeypatch.setenv("SPREADBOARD_CRYPTO_BSC_RPC_URL", "https://bnb-mainnet.g.alchemy.com/v2/k")
    invoice = _member(db, 500)
    used = []

    def rpc(_url, method, params):
        used.append(method)
        if method == "eth_blockNumber":
            return hex(1_000)
        if method == "eth_getLogs":
            raise RuntimeError("rpc_error: {'code': -32005, 'message': 'limit exceeded'}")
        return {"transfers": [{
            "hash": "0xafdfca4e",
            "blockNum": "0x1",
            "from": "0xf91b40449b41c50072d41427ee1add3f7e5dcb5e",
            "rawContract": {"address": BSC_USDT, "value": hex(5 * 10**18)},
        }]}

    result = crypto_watcher.scan_evm("bsc", db_path=db, rpc_call=rpc)

    assert "alchemy_getAssetTransfers" in used
    assert "eth_getLogs" not in used, "the index must be preferred, not a fallback here"
    assert result["results"][0]["resolution"] == "settled"
    assert result["results"][0]["amount_cents"] == 500
    assert crypto_billing.get_invoice(invoice["id"], db_path=db)["status"] == "paid"


def test_a_log_scan_still_works_where_the_node_serves_it(db, monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_CRYPTO_BSC_RPC_URL", "https://bsc-dataseed.binance.org")
    _member(db, 500)

    def rpc(_url, method, _params):
        if method == "eth_blockNumber":
            return hex(1_000)
        assert method == "eth_getLogs"
        return [{
            "address": BSC_USDT,
            "data": hex(5 * 10**18),
            "transactionHash": "0xlogscan",
            "logIndex": "0x0",
            "blockNumber": "0x1",
            "topics": ["t", "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + ARB[2:]],
        }]

    result = crypto_watcher.scan_evm("bsc", db_path=db, rpc_call=rpc)

    assert result["results"][0]["resolution"] == "settled"


def test_a_failing_scan_never_advances_the_cursor_past_unread_blocks(db, monkeypatch) -> None:
    """Skipping ahead after an error would silently lose a payment forever."""
    monkeypatch.setenv("SPREADBOARD_CRYPTO_BSC_RPC_URL", "https://bsc-dataseed.binance.org")

    def rpc(_url, method, _params):
        if method == "eth_blockNumber":
            return hex(50_000)
        raise RuntimeError("rpc_error: {'code': -32005, 'message': 'limit exceeded'}")

    with pytest.raises(RuntimeError):
        crypto_watcher.scan_evm("bsc", db_path=db, rpc_call=rpc)

    assert accounts.chain_cursor("bsc", db_path=db) == 0
