"""A chain nobody can watch must stop taking money.

The BSC outage was not really a bug in one RPC call. It was that a scan could
fail on every poll for hours while the checkout carried on offering that
network, and nothing anywhere said so. Fixing the call fixes today; this fixes
the shape of the failure.

Two rules: a chain is only offered while its watcher is demonstrably working,
and a chain that stops working says so out loud.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spreadboard import accounts, crypto_watcher

ARB = "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937"
TRON = "TBQuKW6Jj1LhmTQV8ziNqGDNLNVW3hXaPz"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RECEIVING_ADDRESS", ARB)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    monkeypatch.setenv("SPREADBOARD_CRYPTO_BSC_RECEIVING_ADDRESS", ARB)
    monkeypatch.setenv("SPREADBOARD_CRYPTO_TRON_RECEIVING_ADDRESS", TRON)
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def test_a_chain_starts_unproven_rather_than_assumed_working(db) -> None:
    """Never scanned is not the same as scanning fine."""
    assert accounts.chain_health("bsc", db_path=db)["last_ok_at"] is None
    assert not crypto_watcher.chain_is_healthy("bsc", db_path=db)


def test_a_successful_scan_marks_the_chain_healthy(db) -> None:
    accounts.record_chain_scan("bsc", ok=True, db_path=db)

    health = accounts.chain_health("bsc", db_path=db)
    assert health["last_ok_at"] is not None
    assert health["consecutive_failures"] == 0
    assert crypto_watcher.chain_is_healthy("bsc", db_path=db)


def test_failures_accumulate_and_keep_the_error(db) -> None:
    accounts.record_chain_scan("bsc", ok=False, error="rpc_error: limit exceeded", db_path=db)
    accounts.record_chain_scan("bsc", ok=False, error="rpc_error: limit exceeded", db_path=db)

    health = accounts.chain_health("bsc", db_path=db)
    assert health["consecutive_failures"] == 2
    assert "limit exceeded" in health["last_error"]


def test_one_success_clears_the_failure_streak(db) -> None:
    accounts.record_chain_scan("bsc", ok=False, error="boom", db_path=db)
    accounts.record_chain_scan("bsc", ok=True, db_path=db)

    assert accounts.chain_health("bsc", db_path=db)["consecutive_failures"] == 0


def test_a_chain_that_stopped_scanning_goes_stale(db) -> None:
    """Exactly the BSC case: it worked once, then failed for hours."""
    stale = datetime.now(tz=UTC) - timedelta(hours=2)
    accounts.record_chain_scan("bsc", ok=True, db_path=db, now=stale)

    assert not crypto_watcher.chain_is_healthy("bsc", db_path=db)


def test_a_recent_success_is_healthy_even_after_a_blip(db) -> None:
    accounts.record_chain_scan("bsc", ok=False, error="transient", db_path=db)
    accounts.record_chain_scan("bsc", ok=True, db_path=db)

    assert crypto_watcher.chain_is_healthy("bsc", db_path=db)


# --------------------------------------------------------------------------
# The rule that protects the customer
# --------------------------------------------------------------------------


def test_an_unhealthy_chain_is_not_offered_for_payment(db) -> None:
    """A customer must never be shown a network whose payments we cannot see."""
    accounts.record_chain_scan("arbitrum", ok=True, db_path=db)
    accounts.record_chain_scan("tron", ok=True, db_path=db)
    accounts.record_chain_scan("bsc", ok=False, error="limit exceeded", db_path=db)

    offered = {c.key for c in crypto_watcher.payable_chains(db_path=db)}

    assert "bsc" not in offered
    assert offered == {"arbitrum", "tron"}


def test_a_chain_proven_working_is_offered(db) -> None:
    for key in ("arbitrum", "bsc", "tron"):
        accounts.record_chain_scan(key, ok=True, db_path=db)

    assert {c.key for c in crypto_watcher.payable_chains(db_path=db)} == {
        "arbitrum", "bsc", "tron"
    }


def test_nothing_is_offered_before_any_chain_has_proven_itself(db) -> None:
    """On a cold start we have proven nothing, so we promise nothing."""
    assert crypto_watcher.payable_chains(db_path=db) == []


def test_scan_all_records_health_for_every_chain(db, monkeypatch) -> None:
    monkeypatch.setattr(crypto_watcher, "scan_once", lambda **k: {"ok": True})
    monkeypatch.setattr(crypto_watcher, "scan_evm", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        crypto_watcher, "scan_tron",
        lambda **k: (_ for _ in ()).throw(RuntimeError("429 Too Many Requests")),
    )

    crypto_watcher.scan_all(db_path=db)

    assert accounts.chain_health("arbitrum", db_path=db)["last_ok_at"] is not None
    assert accounts.chain_health("bsc", db_path=db)["last_ok_at"] is not None
    tron = accounts.chain_health("tron", db_path=db)
    assert tron["last_ok_at"] is None
    assert "429" in tron["last_error"]


def test_the_operator_is_told_when_a_chain_breaks(db, monkeypatch) -> None:
    """The BSC failure logged every ten seconds and nobody knew."""
    alerts = []
    monkeypatch.setattr(
        crypto_watcher, "_alert_operator", lambda message, **k: alerts.append(message)
    )
    monkeypatch.setattr(crypto_watcher, "scan_once", lambda **k: {"ok": True})
    monkeypatch.setattr(crypto_watcher, "scan_evm", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        crypto_watcher, "scan_tron",
        lambda **k: (_ for _ in ()).throw(RuntimeError("429 Too Many Requests")),
    )

    for _ in range(crypto_watcher.ALERT_AFTER_FAILURES):
        crypto_watcher.scan_all(db_path=db)

    assert alerts, "a chain that cannot be watched must raise its hand"
    assert "Tron" in alerts[0] or "tron" in alerts[0]


def test_the_operator_is_not_told_twice_for_the_same_outage(db, monkeypatch) -> None:
    alerts = []
    monkeypatch.setattr(
        crypto_watcher, "_alert_operator", lambda message, **k: alerts.append(message)
    )
    monkeypatch.setattr(crypto_watcher, "scan_once", lambda **k: {"ok": True})
    monkeypatch.setattr(crypto_watcher, "scan_evm", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        crypto_watcher, "scan_tron",
        lambda **k: (_ for _ in ()).throw(RuntimeError("still down")),
    )

    for _ in range(crypto_watcher.ALERT_AFTER_FAILURES * 4):
        crypto_watcher.scan_all(db_path=db)

    assert len(alerts) == 1, "an outage is one alert, not one per poll"
