from __future__ import annotations

from types import SimpleNamespace

from spreadboard import api_spreads, live, server
from spreadarb.api_discovery import sources
from spreadarb.api_discovery.models import MarketQuote


def test_public_route_contract_keeps_spot_spot_and_hides_spot_dex() -> None:
    assert "SPOT" not in api_spreads.RETIRED_ROUTE_KINDS
    assert "DEX-SPOT" in api_spreads.RETIRED_ROUTE_KINDS
    assert api_spreads._normalize_kind_filter("FUTURES-SPOT") == "FUTURES-SPOT-PAIR"


def test_depth_candidate_selection_prefers_unique_tokens() -> None:
    pairs = [
        SimpleNamespace(token="ONE"),
        SimpleNamespace(token="ONE"),
        SimpleNamespace(token="TWO"),
        SimpleNamespace(token="THREE"),
    ]

    selected = sources._unique_token_first(pairs, 3)

    assert [pair.token for pair in selected] == ["ONE", "TWO", "THREE"]


def test_depth_budget_is_balanced_between_route_classes() -> None:
    def quote(token: str, venue: str, market_type: str) -> MarketQuote:
        return MarketQuote(
            token=token,
            venue=venue,
            market_type=market_type,
            bid=2,
            ask=1,
            bid_vwap=2,
            ask_vwap=1,
            quote_ts_us=1,
            source_name="test",
        )

    pairs = [
        sources.QuoteCandidatePair(
            "ONE", quote("ONE", "A", "Futures"), quote("ONE", "B", "Futures"), 1, 1
        ),
        sources.QuoteCandidatePair(
            "TWO", quote("TWO", "C", "Spot"), quote("TWO", "D", "Spot"), 1, 1
        ),
        sources.QuoteCandidatePair(
            "THREE",
            quote("THREE", "E", "Futures"),
            quote("THREE", "F", "Spot"),
            1,
            1,
        ),
    ]

    selected = sources._balanced_route_candidates(pairs, 3)

    assert {pair.token for pair in selected} == {"ONE", "TWO", "THREE"}


def test_public_candidate_priority_rejects_high_unknown_identity() -> None:
    def quote(market_type: str, identity: str | None = None) -> MarketQuote:
        return MarketQuote(
            token="SAME",
            venue=f"venue-{market_type}-{identity}",
            market_type=market_type,
            bid=2,
            ask=1,
            bid_vwap=2,
            ask_vwap=1,
            quote_ts_us=1,
            source_name="test",
            identity_key=identity,
        )

    unknown = sources.QuoteCandidatePair("SAME", quote("Spot"), quote("Futures"), 80, 80)
    identified = sources.QuoteCandidatePair(
        "SAME",
        quote("Spot", "asset:same"),
        quote("Futures", "asset:same"),
        80,
        80,
    )
    inventory_unknown = sources.QuoteCandidatePair(
        "SAME",
        quote("Futures", "asset:same"),
        quote("Spot", "asset:same"),
        2,
        2,
    )

    assert not sources._candidate_is_publicly_rankable(unknown)
    assert sources._candidate_is_publicly_rankable(identified)
    assert not sources._candidate_is_publicly_rankable(inventory_unknown)


def test_inverse_futures_spot_is_conditional_not_hidden() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={"executable_spread_pct": 3},
        long_market_type="Futures",
        short_market_type="Spot",
        long_rails={},
        short_rails={},
    )

    assert reasons == ["spot_sell_inventory_required"]
    assert not any(reason.startswith("mirage_guard:") for reason in reasons)

    row = SimpleNamespace(
        to_dict=lambda: {"blockers": reasons},
        blockers=reasons,
    )
    assert api_spreads._public_row(row)["conditions"] == reasons


def test_native_settled_history_is_not_mislabeled_as_current_funding() -> None:
    result = live._native_funding_result(
        [
            {"timestamp_ms": 1_000, "rate_pct": 0.01},
            {"timestamp_ms": 28_801_000, "rate_pct": 0.02},
        ],
        exchange_id="example",
    )

    assert result["funding_24h_pct"] == 0.03
    assert result["funding_interval_hours"] == 8.0
    assert "current_funding_pct" not in result
    assert "projected_24h_pct" not in result


def test_validated_reference_venues_are_enabled() -> None:
    spot = sources.default_enabled_cex_source().venues
    futures = sources.default_enabled_cex_futures_source().venues

    assert {"HTX", "Phemex", "CoinEx", "WhiteBIT"} <= set(spot)
    assert {"HTX", "Phemex", "CoinEx", "WhiteBIT"} <= set(futures)
    assert "Upbit" not in spot


def test_lane_counts_are_unique_assets_not_route_permutations() -> None:
    rows = [
        SimpleNamespace(route_kind="FUTURES", token="ONE"),
        SimpleNamespace(route_kind="FUTURES", token="ONE"),
        SimpleNamespace(route_kind="FUTURES", token="TWO"),
        SimpleNamespace(route_kind="SPOT", token="ONE"),
    ]

    assert api_spreads._route_kind_token_counts(rows) == {
        "FUTURES": 2,
        "SPOT": 1,
    }


def test_release_lane_counts_merge_spot_futures_directions() -> None:
    rows = [
        SimpleNamespace(route_kind="FUTURES", token="ONE"),
        SimpleNamespace(route_kind="FUTURES-SPOT", token="ONE"),
        SimpleNamespace(route_kind="SPOT-FUTURES", token="ONE"),
        SimpleNamespace(route_kind="SPOT-FUTURES", token="TWO"),
        SimpleNamespace(route_kind="SPOT", token="THREE"),
        SimpleNamespace(route_kind="DEX-FUTURES", token="FOUR"),
    ]

    assert api_spreads._release_lane_token_counts(rows) == {
        "FUTURES": 1,
        "FUTURES-SPOT": 2,
        "SPOT": 1,
        "DEX-FUTURES": 1,
    }
    assert server.market_kind_count(
        "FUTURES-SPOT-PAIR",
        {"FUTURES-SPOT": 2, "SPOT-FUTURES": 2},
        {},
        {"FUTURES-SPOT": 3},
    ) == 3


def test_current_snapshot_can_seed_a_new_route_chart() -> None:
    point = server._current_history_point(
        {
            "route_key": "ONE|A|Futures|B|Futures",
            "quote_ts_us": 123,
            "token": "ONE",
            "route_kind": "FUTURES",
            "long_venue": "A",
            "short_venue": "B",
            "executable_spread_pct": 1.2,
            "depth_weighted_spread_pct": 1.1,
            "long_bid": 10,
            "long_ask": 11,
            "short_bid": 12,
            "short_ask": 13,
        }
    )

    assert point["depth_weighted_spread_pct"] == 1.1
    assert point["long_ask_price"] == 11
    assert point["short_bid_price"] == 12
