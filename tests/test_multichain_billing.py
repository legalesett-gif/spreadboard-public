"""Three chains, three decimal conventions, one amount in cents.

BSC's USDT and USDC carry eighteen decimals where Arbitrum's and Tron's carry
six. Reading a BSC transfer with a six-decimal assumption credits a payment a
trillion times over; writing a BSC wallet URI with six turns $5 into a dust
transfer. Every conversion below is anchored to the chain, never to a default.
"""

from __future__ import annotations

import pytest

from spreadboard import crypto_billing


def test_every_chain_is_registered_with_its_own_tokens() -> None:
    assert set(crypto_billing.CHAINS) == {"arbitrum", "bsc", "tron"}
    for chain in crypto_billing.CHAINS.values():
        assert chain.tokens, f"{chain.key} has no tokens"
        assert {t["symbol"] for t in chain.tokens.values()} == {"USDC", "USDT"}


def test_bsc_stablecoins_are_eighteen_decimals() -> None:
    """The single most dangerous number in this feature."""
    for token in crypto_billing.CHAINS["bsc"].tokens.values():
        assert int(token["decimals"]) == 18


def test_arbitrum_and_tron_stablecoins_are_six_decimals() -> None:
    for key in ("arbitrum", "tron"):
        for token in crypto_billing.CHAINS[key].tokens.values():
            assert int(token["decimals"]) == 6


def test_five_dollars_converts_correctly_on_every_chain() -> None:
    expected = {"arbitrum": 5_000_000, "bsc": 5 * 10**18, "tron": 5_000_000}
    for key, chain in crypto_billing.CHAINS.items():
        for contract, token in chain.tokens.items():
            raw = crypto_billing.cents_to_units(500, int(token["decimals"]))
            assert raw == expected[key], f"{key} {contract}"
            assert crypto_billing.units_to_cents(raw, int(token["decimals"])) == 500


def test_a_bsc_transfer_is_not_read_with_arbitrum_decimals() -> None:
    """18-decimal raw units read as 6 decimals would credit a trillion dollars."""
    raw = 5 * 10**18
    assert crypto_billing.units_to_cents(raw, 18) == 500
    assert crypto_billing.units_to_cents(raw, 6) != 500


def test_token_contracts_are_distinct_across_chains() -> None:
    seen: dict[str, str] = {}
    for key, chain in crypto_billing.CHAINS.items():
        for contract in chain.tokens:
            assert contract not in seen, f"{contract} claimed by {seen.get(contract)} and {key}"
            seen[contract] = key


def test_a_contract_is_only_valid_on_its_own_chain() -> None:
    """Matching a token by symbol, or across chains, is how fakes get accepted."""
    bsc_usdt = next(
        c for c, t in crypto_billing.CHAINS["bsc"].tokens.items() if t["symbol"] == "USDT"
    )
    assert crypto_billing.token_for(bsc_usdt, chain="bsc") is not None
    assert crypto_billing.token_for(bsc_usdt, chain="arbitrum") is None
    assert crypto_billing.token_for(bsc_usdt, chain="tron") is None


def test_tron_addresses_are_base58_and_evm_addresses_are_hex() -> None:
    assert crypto_billing.CHAINS["tron"].kind == "tron"
    for contract in crypto_billing.CHAINS["tron"].tokens:
        assert contract.startswith("T") and len(contract) == 34
    for key in ("arbitrum", "bsc"):
        assert crypto_billing.CHAINS[key].kind == "evm"
        for contract in crypto_billing.CHAINS[key].tokens:
            assert contract.startswith("0x") and len(contract) == 42
            assert contract == contract.lower(), "EVM contracts are compared lowercased"


def test_a_chain_without_a_receiving_address_is_not_offered(monkeypatch) -> None:
    for name in (
        "SPREADBOARD_CRYPTO_RECEIVING_ADDRESS",
        "SPREADBOARD_CRYPTO_BSC_RECEIVING_ADDRESS",
        "SPREADBOARD_CRYPTO_TRON_RECEIVING_ADDRESS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert crypto_billing.enabled_chains() == []


def test_only_configured_chains_are_offered(monkeypatch) -> None:
    monkeypatch.setenv(
        "SPREADBOARD_CRYPTO_RECEIVING_ADDRESS",
        "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937",
    )
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    monkeypatch.delenv("SPREADBOARD_CRYPTO_BSC_RECEIVING_ADDRESS", raising=False)
    monkeypatch.delenv("SPREADBOARD_CRYPTO_TRON_RECEIVING_ADDRESS", raising=False)

    assert [c.key for c in crypto_billing.enabled_chains()] == ["arbitrum"]


def test_a_tron_address_is_rejected_on_an_evm_chain(monkeypatch) -> None:
    """A wrong-format address is a lost payment, so it must never be offered."""
    monkeypatch.setenv(
        "SPREADBOARD_CRYPTO_BSC_RECEIVING_ADDRESS", "TBQuKW6Jj1LhmTQV8ziNqGDNLNVW3hXaPz"
    )
    monkeypatch.setenv("SPREADBOARD_CRYPTO_BSC_RPC_URL", "https://example.invalid/rpc")

    assert "bsc" not in [c.key for c in crypto_billing.enabled_chains()]


def test_an_evm_address_is_rejected_on_tron(monkeypatch) -> None:
    monkeypatch.setenv(
        "SPREADBOARD_CRYPTO_TRON_RECEIVING_ADDRESS",
        "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937",
    )

    assert "tron" not in [c.key for c in crypto_billing.enabled_chains()]


def test_a_mistyped_tron_checksum_is_rejected(monkeypatch) -> None:
    """One wrong character, and the funds are gone. Verify, do not trust."""
    monkeypatch.setenv(
        "SPREADBOARD_CRYPTO_TRON_RECEIVING_ADDRESS", "TBQuKW6Jj1LhmTQV8ziNqGDNLNVW3hXaPa"
    )

    assert "tron" not in [c.key for c in crypto_billing.enabled_chains()]


def test_the_real_tron_address_passes(monkeypatch) -> None:
    monkeypatch.setenv(
        "SPREADBOARD_CRYPTO_TRON_RECEIVING_ADDRESS", "TBQuKW6Jj1LhmTQV8ziNqGDNLNVW3hXaPz"
    )

    assert "tron" in [c.key for c in crypto_billing.enabled_chains()]


def test_wallet_uris_carry_the_chain_and_raw_amount(monkeypatch) -> None:
    monkeypatch.setenv(
        "SPREADBOARD_CRYPTO_BSC_RECEIVING_ADDRESS",
        "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937",
    )
    options = crypto_billing.payment_options(
        500, "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937", chain="bsc"
    )

    assert options
    for option in options:
        assert "@56/" in option["wallet_uri"]
        assert option["amount_raw"] == str(5 * 10**18)


def test_tron_has_no_ethereum_wallet_uri(monkeypatch) -> None:
    options = crypto_billing.payment_options(
        500, "TBQuKW6Jj1LhmTQV8ziNqGDNLNVW3hXaPz", chain="tron"
    )

    assert options
    for option in options:
        assert not option["wallet_uri"].startswith("ethereum:")
        assert option["amount_raw"] == "5000000"


@pytest.mark.parametrize("chain", ["arbitrum", "bsc", "tron"])
def test_every_chain_states_how_many_confirmations(chain) -> None:
    assert crypto_billing.CHAINS[chain].confirmations >= 1
