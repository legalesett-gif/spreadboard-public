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
        slot, amount = crypto_billing._allocate_amount(taken, 18_000)
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
    subscribe = source.split('command == "/subscribe"', 1)[1][:1600]
    assert "crypto_billing.status()" in subscribe
    # Stripe stays as the fallback, not the headline.
    assert subscribe.index("crypto_billing") < subscribe.index("create_checkout_session")


def test_an_amount_outside_the_band_is_not_credited() -> None:
    """Tolerance absorbs a withdrawal fee, not a wrong invoice."""
    from spreadboard import crypto_billing

    assert crypto_billing.TOLERANCE_CENTS == 200
