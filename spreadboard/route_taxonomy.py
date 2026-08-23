"""One canonical product-lane taxonomy for every SpreadBoard surface.

``DEX`` is a SpreadBoard product name reserved for the OKX DEX aggregator.
Other venues retain their actual instrument type in the ordinary Futures and
Spot lanes, even when they settle on-chain. Operational on-chain detection is
kept separate because Jupiter, Velora and similar swap venues still require
chain/contract identity and provider quotes.
"""

from __future__ import annotations

from typing import Any

DEX_SOURCE_KINDS = {
    "dex_discovered",
    "dex_discovered_rows",
    "dex_derivative",
    "dex_spot",
}

# Product DEX lanes are intentionally OKX-only. Prefix matching covers
# chain-qualified labels such as ``OKX DEX 56`` without accepting a venue just
# because its name contains the letters "DEX".
DEX_VENUE_FAMILIES = ("okx dex",)

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
    del market_type
    return venue_is_dex(venue)


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
    # ``source_kind`` is provenance, not a product-lane instruction. Several
    # normal Futures/Spot venues arrive through legacy dex_* source buckets.
    del source_kind
    return leg_is_dex(
        venue=long_venue, market_type=long_market_type
    ) or leg_is_dex(venue=short_venue, market_type=short_market_type)


def instrument_type(*, venue: Any = None, market_type: Any = None) -> str:
    """Return the actual instrument used by product-lane classification.

    Older provider rows sometimes stored ``DEX`` as a market type. That means
    an on-chain spot swap, not a third instrument class.
    """

    del venue
    normalized = str(market_type or "").strip().casefold()
    return "spot" if normalized == "dex" else normalized


def route_kind(
    *,
    long_venue: Any = None,
    long_market_type: Any = None,
    short_venue: Any = None,
    short_market_type: Any = None,
    source_kind: Any = None,
) -> str:
    long_type = instrument_type(venue=long_venue, market_type=long_market_type)
    short_type = instrument_type(venue=short_venue, market_type=short_market_type)
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
