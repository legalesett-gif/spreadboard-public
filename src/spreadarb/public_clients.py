"""Configuration shared by public market-data clients, not execution clients."""

from __future__ import annotations

import sys
from typing import Any


def configure_public_market_client(client: Any, venue: str) -> None:
    """Load every advertised Hyperliquid builder, retaining explicit dex lists.

    CCXT's default HIP-3 limit=10 iterates indices 1..9, dropping the tenth
    builder (io). Its loop stops at the actual returned metadata length, so
    this sentinel removes that arbitrary discovery truncation without issuing
    requests for nonexistent builders. Existing market/price/identity gates
    still decide which discovered contracts are usable.
    """
    if venue != "Hyperliquid":
        return
    options = client.options.setdefault("fetchMarkets", {})
    hip3 = options.setdefault("hip3", {})
    if not hip3.get("dexes"):
        hip3["limit"] = sys.maxsize
