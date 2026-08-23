"""One canonical market-lane taxonomy for every SpreadBoard surface.

Market type describes the instrument (spot or perpetual futures); venue type
describes where it trades. Aster, Hyperliquid and Lighter therefore keep a
``Futures`` instrument type while belonging to the DEX lane. Keeping those
concepts separate prevents perpetual DEXes from leaking into the ordinary CEX
Futures and Futures-Spot views.
"""

from __future__ import annotations

from typing import Any

DEX_SOURCE_KINDS = {
    "dex_discovered",
    "dex_discovered_rows",
    "dex_derivative",
    "dex_spot",
}

# Match normalized venue families, not arbitrary substring checks. Prefixes
# cover chain-qualified venues such as ``OKX DEX 56`` and ``0x Ethereum``.
DEX_VENUE_FAMILIES = (
    "okx dex",
    "jupiter",
    "0x",
    "zerox",
    "velora",
    "paraswap",
    "aster",
    "hyperliquid",
    "lighter",
    "dydx",
    "apex",
    "paradex",
)

ONCHAIN_SPOT_VENUE_FAMILIES = (
    "okx dex",
    "jupiter",
    "0x",
    "zerox",
    "velora",
    "paraswap",
)


def source_is_dex(source_kind: Any) -> bool:
    return str(source_kind or "").strip().casefold() in DEX_SOURCE_KINDS


def venue_is_dex(venue: Any) -> bool:
    normalized = " ".join(str(venue or "").strip().casefold().split())
    return any(
        normalized == family or normalized.startswith(f"{family} ")
        for family in DEX_VENUE_FAMILIES
    )


def leg_is_dex(*, venue: Any = None, market_type: Any = None) -> bool:
    return str(market_type or "").strip().casefold() == "dex" or venue_is_dex(venue)


def leg_is_onchain_spot(*, venue: Any = None, market_type: Any = None) -> bool:
    return str(market_type or "").strip().casefold() in {"spot", "dex"} and venue_is_onchain_spot(
        venue
    )


def venue_is_onchain_spot(venue: Any) -> bool:
    normalized = " ".join(str(venue or "").strip().casefold().split())
    return any(
        normalized == family or normalized.startswith(f"{family} ")
        for family in ONCHAIN_SPOT_VENUE_FAMILIES
    )


def route_has_dex(
    *,
    long_venue: Any = None,
    long_market_type: Any = None,
    short_venue: Any = None,
    short_market_type: Any = None,
    source_kind: Any = None,
) -> bool:
    return source_is_dex(source_kind) or leg_is_dex(
        venue=long_venue, market_type=long_market_type
    ) or leg_is_dex(venue=short_venue, market_type=short_market_type)


def route_kind(
    *,
    long_venue: Any = None,
    long_market_type: Any = None,
    short_venue: Any = None,
    short_market_type: Any = None,
    source_kind: Any = None,
) -> str:
    long_type = str(long_market_type or "").strip().casefold()
    short_type = str(short_market_type or "").strip().casefold()
    market_types = {long_type, short_type}
    if route_has_dex(
        long_venue=long_venue,
        long_market_type=long_market_type,
        short_venue=short_venue,
        short_market_type=short_market_type,
        source_kind=source_kind,
    ):
        return "DEX-FUTURES" if "futures" in market_types else "DEX-SPOT"
    if long_type == short_type == "futures":
        return "FUTURES"
    if long_type == "spot" and short_type == "futures":
        return "SPOT-FUTURES"
    if long_type == "futures" and short_type == "spot":
        return "FUTURES-SPOT"
    if long_type == short_type == "spot":
        return "SPOT"
    return "UNKNOWN"
