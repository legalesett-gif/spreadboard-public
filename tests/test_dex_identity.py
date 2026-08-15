from spreadboard.dex_identity import canonical_chain, canonical_contract, identity_key


def test_chain_aliases_resolve_to_numeric_identity() -> None:
    assert canonical_chain("Base") == "8453"
    assert canonical_chain("BEP-20 (BSC)") == "56"
    assert canonical_chain("Arbitrum One") == "42161"
    assert canonical_chain("501") == "501"


def test_evm_contracts_fold_case_but_solana_mints_do_not() -> None:
    assert canonical_contract("BSC", "0xAbC") == "0xabc"
    assert canonical_contract("Solana", "AbCd") == "AbCd"
    assert identity_key("SOL", "AbCd") != identity_key("501", "abcd")
