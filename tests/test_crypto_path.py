"""The crypto payment path, end to end.

The watcher is what makes a crypto payment mean anything: without it a member
sends USDC and the invoice simply expires an hour later having credited nothing.
"""

from __future__ import annotations

import inspect

import pytest


def test_production_starts_the_chain_watcher() -> None:
    """It was only started by server.py's CLI main(), which production does not run.

    Production runs scripts/run_spreadboard_service.py, and the cursor sat at 0
    with no watcher log line -- so no crypto payment could ever be detected.
    """
    from scripts import run_spreadboard_service as service

    source = inspect.getsource(service)
    assert "crypto_watcher.start_background" in source
    main = inspect.getsource(service.main)
    assert "crypto_watcher" in main


def test_the_watcher_never_replays_the_whole_chain() -> None:
    """A cold cursor must not scan Arbitrum from block 0."""
    from spreadboard import crypto_watcher

    source = inspect.getsource(crypto_watcher.scan_once)
    assert "FIRST_RUN_LOOKBACK" in source
    assert crypto_watcher.FIRST_RUN_LOOKBACK > 0


def test_the_watcher_only_reads() -> None:
    """It holds no key and must never be able to move funds."""
    from spreadboard import crypto_watcher

    source = inspect.getsource(crypto_watcher)
    for forbidden in ("eth_sendRawTransaction", "eth_sendTransaction", "signTransaction", "private_key"):
        assert forbidden not in source


def test_each_invoice_gets_an_amount_nobody_else_is_using() -> None:
    """One address serves every member, so the amount is the identifier."""
    from spreadboard import crypto_billing

    taken: list[int] = []
    amounts = []
    for _ in range(5):
        slot, amount = crypto_billing._allocate_amount(taken, 14_900)
        taken.append(amount)
        amounts.append(amount)

    assert len(set(amounts)) == len(amounts)
    # Every pair is further apart than the tolerance band, or two payments
    # would match the same invoice.
    for i, a in enumerate(amounts):
        for b in amounts[i + 1:]:
            assert abs(a - b) > 2 * crypto_billing.TOLERANCE_CENTS


def test_telegram_subscribe_leads_with_crypto() -> None:
    from spreadboard import telegram_bot

    source = inspect.getsource(telegram_bot.handle_update)
    # In-bot checkout now answers /subscribe first, so skip that branch and
    # assert the ordering of what remains: crypto leads, Stripe is the
    # fallback, and Stripe never becomes the headline.
    subscribe = source.rsplit('command == "/subscribe"', 1)[1][:1600]
    assert "crypto_billing.status()" in subscribe
    assert subscribe.index("crypto_billing") < subscribe.index("create_checkout_session")


def test_in_bot_checkout_answers_subscribe_before_the_website_link() -> None:
    """The whole point is that nobody is sent to the website to pay."""
    from spreadboard import telegram_bot

    source = inspect.getsource(telegram_bot.handle_update)
    assert source.index("telegram_checkout.begin") < source.index("crypto_billing.status()")


def test_only_the_exact_invoice_amount_is_credited() -> None:
    """A wrong amount is parked rather than guessed into a paid tier."""
    from spreadboard import crypto_billing

    assert crypto_billing.TOLERANCE_CENTS == 0


def test_alchemy_uses_asset_transfers_not_a_range_scan(monkeypatch) -> None:
    """The free tier caps eth_getLogs at ten blocks and Arbitrum makes ~14,400
    an hour, so every range scan failed with a 400 and no payment could land."""
    from spreadboard import crypto_watcher

    seen: list[str] = []

    def call(method, params):
        seen.append(method)
        if method == "alchemy_getAssetTransfers":
            return {"transfers": [{
                "hash": "0xabc", "blockNum": "0x10", "from": "0x" + "1" * 40,
                "rawContract": {"address": "0xtoken", "value": "0x2540be400"},
            }]}
        return []

    settings = type("S", (), {
        "rpc_url": "https://arb-mainnet.g.alchemy.com/v2/key",
        "receiving_address": "0x" + "e" * 40,
    })()

    logs = crypto_watcher._transfers_to_us(call, settings, 100, 200)

    assert seen == ["alchemy_getAssetTransfers"], "it still range-scanned"
    assert len(logs) == 1
    entry = logs[0]
    # Shaped exactly like an eth_getLogs entry, so the caller is unchanged.
    assert entry["transactionHash"] == "0xabc"
    assert int(entry["data"], 16) == 10_000_000_000
    assert len(entry["topics"]) == 3


def test_a_non_alchemy_rpc_still_range_scans(monkeypatch) -> None:
    from spreadboard import crypto_watcher

    seen: list[str] = []

    def call(method, params):
        seen.append(method)
        return []

    settings = type("S", (), {
        "rpc_url": "https://arb1.arbitrum.io/rpc",
        "receiving_address": "0x" + "e" * 40,
    })()

    crypto_watcher._transfers_to_us(call, settings, 100, 200)

    assert seen == ["eth_getLogs"]


def test_asset_transfers_failing_falls_back_rather_than_stalling(monkeypatch) -> None:
    from spreadboard import crypto_watcher

    seen: list[str] = []

    def call(method, params):
        seen.append(method)
        if method == "alchemy_getAssetTransfers":
            raise RuntimeError("temporarily unavailable")
        return []

    settings = type("S", (), {
        "rpc_url": "https://arb-mainnet.g.alchemy.com/v2/key",
        "receiving_address": "0x" + "e" * 40,
    })()

    crypto_watcher._transfers_to_us(call, settings, 100, 200)

    assert seen == ["alchemy_getAssetTransfers", "eth_getLogs"]
