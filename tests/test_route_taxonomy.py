from __future__ import annotations

from spreadboard import api_spreads, fast_quotes, route_taxonomy


def test_perpetual_dex_venues_never_enter_cex_lanes() -> None:
    for venue in ("Aster", "Hyperliquid", "Lighter"):
        assert route_taxonomy.route_kind(
            long_venue="Mexc",
            long_market_type="Futures",
            short_venue=venue,
            short_market_type="Futures",
        ) == "DEX-FUTURES"
        assert route_taxonomy.route_kind(
            long_venue="Gate",
            long_market_type="Spot",
            short_venue=venue,
            short_market_type="Futures",
        ) == "DEX-FUTURES"


def test_dex_source_kind_is_a_safe_fallback_for_new_provider_names() -> None:
    assert route_taxonomy.route_kind(
        long_venue="Future Builder Venue",
        long_market_type="Futures",
        short_venue="Bybit",
        short_market_type="Futures",
        source_kind="dex_discovered",
    ) == "DEX-FUTURES"


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


def test_velora_is_an_exact_onchain_spot_leg() -> None:
    assert route_taxonomy.venue_is_onchain_spot("Velora DEX 56") is True
    assert route_taxonomy.leg_is_onchain_spot(
        venue="Velora DEX 56", market_type="Spot"
    ) is True
    assert route_taxonomy.route_kind(
        long_venue="Velora DEX 56",
        long_market_type="Spot",
        short_venue="Gate",
        short_market_type="Futures",
    ) == "DEX-FUTURES"


def test_public_parser_and_fast_worker_share_the_same_dex_lane() -> None:
    row = {
        "source_kind": "dex_discovered",
        "long_venue": "Bitget",
        "long_market_type": "Futures",
        "short_venue": "Aster",
        "short_market_type": "Futures",
    }
    assert api_spreads._route_kind(**row) == "DEX-FUTURES"
    assert fast_quotes._fast_quote_lane(row) == "DEX-FUTURES"
    assert fast_quotes._is_dex_route(row) is True
