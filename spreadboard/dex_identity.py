"""Canonical DEX chain and contract identities used by research evidence.

Human-facing journals accept familiar network names, while live quotes normally
carry numeric chain IDs. Both must resolve to the same key, and Solana mints
must retain their case because base58 identifiers are case-sensitive.
"""

from __future__ import annotations

from typing import Any

from .verified_identity import NETWORK_CHAIN_IDS


def canonical_chain(value: Any) -> str:
    """Return a numeric chain ID when an exact supported alias is known."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(int(text))
    except (TypeError, ValueError):
        pass
    normalized = "".join(character for character in text.upper() if character.isalnum())
    aliases = {
        **NETWORK_CHAIN_IDS,
        "BNB": 56,
        "BNBSMARTCHAIN": 56,
        "BINANCESMARTCHAIN": 56,
        "BEP20BSC": 56,
        "ARBITRUMONE": 42161,
        "AVALANCHE": 43114,
        "AVAX": 43114,
    }
    chain_id = aliases.get(normalized)
    return str(chain_id) if chain_id is not None else normalized.casefold()


def canonical_contract(chain: Any, value: Any) -> str:
    """Normalize EVM addresses without corrupting case-sensitive Solana mints."""

    text = str(value or "").strip()
    return text if canonical_chain(chain) == "501" else text.casefold()


def identity_key(chain: Any, contract: Any) -> tuple[str, str]:
    canonical = canonical_chain(chain)
    return canonical, canonical_contract(canonical, contract)
