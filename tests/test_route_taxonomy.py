from __future__ import annotations

from spreadboard import api_spreads, fast_quotes, route_taxonomy


def test_non_okx_perpetual_venues_use_the_normal_futures_lanes() -> None:
    for venue in ("Aster", "Hyperliquid", "Lighter"):
        assert route_taxonomy.route_kind(
            long_venue="Mexc",
            long_market_type="Futures",
            short_venue=venue,
            short_market_type="Futures",
        ) == "FUTURES"
        assert route_taxonomy.route_kind(
            long_venue="Gate",
            long_market_type="Spot",
            short_venue=venue,
            short_market_type="Futures",
        ) == "SPOT-FUTURES"


def test_source_provenance_does_not_override_product_taxonomy() -> None:
    assert route_taxonomy.route_kind(
        long_venue="Future Builder Venue",
        long_market_type="Futures",
        short_venue="Bybit",
        short_market_type="Futures",
        source_kind="dex_discovered",
    ) == "FUTURES"


def test_centralized_routes_keep_their_existing_lanes() -> None:
    assert route_taxonomy.route_kind(
        long_venue="Binance",
        long_market_type="Futures",
        short_venue="Mexc",
        short_market_type="Futures",
    ) == "FUTURES"
    assert route_taxonomy.route_kind(
        long_venue="Gate",
        long_market_type="Spot",
        short_venue="Mexc",
        short_market_type="Futures",
    ) == "SPOT-FUTURES"


def test_velora_is_onchain_but_uses_the_normal_spot_lane() -> None:
    assert route_taxonomy.venue_is_onchain_spot("Velora DEX 56") is True
    assert route_taxonomy.leg_is_onchain_spot(
        venue="Velora DEX 56", market_type="Spot"
    ) is True
    assert route_taxonomy.route_kind(
        long_venue="Velora DEX 56",
        long_market_type="Spot",
        short_venue="Gate",
        short_market_type="Futures",
    ) == "SPOT-FUTURES"


def test_only_okx_dex_uses_product_dex_lanes() -> None:
    assert route_taxonomy.route_kind(
        long_venue="OKX DEX 56",
        long_market_type="Spot",
        short_venue="Gate",
        short_market_type="Futures",
    ) == "DEX-FUTURES"
    assert route_taxonomy.route_kind(
        long_venue="OKX DEX 1",
        long_market_type="DEX",
        short_venue="OKX DEX 56",
        short_market_type="Spot",
    ) == "DEX-SPOT"
    for venue in ("Jupiter Solana", "Velora DEX 56", "0x Ethereum"):
        assert route_taxonomy.leg_is_dex(venue=venue, market_type="DEX") is False
        assert route_taxonomy.route_kind(
            long_venue=venue,
            long_market_type="DEX",
            short_venue="Gate",
            short_market_type="Futures",
        ) == "SPOT-FUTURES"


def test_public_parser_and_fast_worker_share_the_same_futures_lane() -> None:
    row = {
        "source_kind": "dex_discovered",
        "long_venue": "Bitget",
        "long_market_type": "Futures",
        "short_venue": "Aster",
        "short_market_type": "Futures",
    }
    assert api_spreads._route_kind(**row) == "FUTURES"
    assert fast_quotes._fast_quote_lane(row) == "FUTURES"
    assert fast_quotes._is_dex_route(row) is False
