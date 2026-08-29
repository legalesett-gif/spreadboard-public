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


#: Quote assets that are all "a dollar" for the purpose of pairing two legs.
#:
#: A token's USDC perpetual and its USDT perpetual are the same trade. The
#: stablecoin basis between them is a rounding error against the spreads this
#: board ranks, and refusing to pair across it removed whole venues from the
#: product rather than removing a risk -- Hyperliquid quotes its perpetuals in
#: USDC, so every Hyperliquid route was silently unbuildable.
#:
#: This is the single definition. Anything that decides whether two legs may be
#: paired, or whether an external reference row is one we could have built,
#: must ask here rather than comparing quote strings.
USD_PEGGED_QUOTES = frozenset(
    {
        "USD",
        "USDT",
        "USDC",
        "USDE",
        "FDUSD",
        "BUSD",
        "DAI",
        "TUSD",
        "USD1",
        "USDP",
        "PYUSD",
        "USDD",
        "USDS",
        "RLUSD",
    }
)


def quote_is_usd_pegged(quote: Any) -> bool:
    """True when this quote asset is a dollar for pairing purposes."""

    return str(quote or "").strip().upper() in USD_PEGGED_QUOTES


def quotes_are_interchangeable(left: Any, right: Any) -> bool:
    """True when two legs' quote assets describe the same trade.

    Two dollar quotes are interchangeable however they are spelled. A dollar
    quote against BTC, ETH or EUR is not: that pairing is currency risk wearing
    a token spread's clothes, and it stays rejected.

    An unknown quote on either side is treated as compatible, because dropping
    a route for missing metadata loses real coverage to a blank field.
    """

    a = str(left or "").strip().upper()
    b = str(right or "").strip().upper()
    if not a or not b:
        return True
    if a == b:
        return True
    return a in USD_PEGGED_QUOTES and b in USD_PEGGED_QUOTES
