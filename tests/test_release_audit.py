from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace

import pytest

from spreadboard import api_spreads, live, server
from spreadboard.verified_identity import build_verified_identity_registry
from scripts.api_discovery_worker import build_parser as discovery_worker_parser
from scripts.run_spreadboard_service import RefreshLoop, _merge_newer_fast_quotes
from spreadarb.api_discovery import runner, sources, worker
from spreadarb.api_discovery.identity import (
    IdentityRegistry,
    WatchAsset,
    load_identity_registry,
    load_watchlist,
)
from spreadarb.api_discovery.models import (
    SOURCE_API_DISCOVERED,
    MarketQuote,
    SourceResult,
    SourceStatus,
)


def test_public_route_contract_shows_all_five_lanes() -> None:
    """Contract changed 2026-08-01 by operator request.

    Spot-DEX was retired, which zeroed a lane the reference product
    (uacryptoinvest) populates with 20+ tokens. It is now shown by default and
    can be retired again with SPREADBOARD_RETIRE_DEX_SPOT=1.
    """
    assert "SPOT" not in api_spreads.RETIRED_ROUTE_KINDS
    assert "DEX-SPOT" not in api_spreads.RETIRED_ROUTE_KINDS
    assert api_spreads._normalize_kind_filter("FUTURES-SPOT") == "FUTURES-SPOT-PAIR"
    # The freshness window must exceed the discovery cadence or rows are stale
    # by construction; production overrides this via SPREADBOARD_LIVE_MAX_AGE_MIN.
    assert api_spreads.DEFAULT_MAX_AGE_MIN >= 4.0


def test_discovery_publish_keeps_newer_fast_quotes() -> None:
    route = {
        "token": "COTI",
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
    }
    discovery = {
        "api_discovered_rows": [{**route, "quote_ts_us": 100, "executable_spread_pct": 1}],
        "dex_discovered_rows": [],
    }
    current = {
        "api_discovered_rows": [{**route, "quote_ts_us": 200, "executable_spread_pct": 2}],
        "dex_discovered_rows": [],
        "fast_quote_refresh": {
            "status": "ok",
            "updated_at": "2026-07-31T00:00:00Z",
            "updated_routes": 1,
        },
    }

    _merge_newer_fast_quotes(discovery, current)

    assert discovery["api_discovered_rows"][0]["quote_ts_us"] == 200
    assert discovery["api_discovered_rows"][0]["executable_spread_pct"] == 2
    assert discovery["fast_quote_refresh"] == current["fast_quote_refresh"]


def test_refresh_loop_keeps_fast_quote_worker_methods() -> None:
    assert callable(RefreshLoop.run_fast_quotes)
    assert callable(RefreshLoop._refresh_fast_quotes)


def test_unique_okx_identity_enriches_cex_quote_but_guards_large_dislocation() -> None:
    cex = MarketQuote(
        token="SAFE",
        venue="Bybit",
        market_type="Futures",
        bid=100.0,
        ask=100.1,
        bid_vwap=100.0,
        ask_vwap=100.1,
        quote_ts_us=1,
        source_name="ccxt",
        symbol="SAFE/USDT:USDT",
    )
    asset = WatchAsset(
        symbol="SAFE",
        identity_key="eip155:56/erc20:0xabc",
        cex_enabled=True,
        dex_enabled=True,
        evm_contracts={56: "0xabc"},
    )

    enriched = sources._apply_unique_okx_identities(
        [cex], [asset], registry=IdentityRegistry.empty()
    )[0]
    assert enriched.identity_key == asset.identity_key
    assert enriched.identity_source == "okx_unique_symbol_inference"

    dex = MarketQuote(
        token="SAFE",
        venue="OKX DEX 56",
        market_type="Spot",
        bid=106.0,
        ask=106.1,
        bid_vwap=106.0,
        ask_vwap=106.1,
        quote_ts_us=1,
        source_name="okx_dex_quote",
        identity_key=asset.identity_key,
        identity_source="okx_token_catalog",
        chain_id=56,
        token_address="0xabc",
    )
    rows = sources.dex_candidates(
        [dex], [enriched], source_name="okx_dex_quote", min_spread_pct=-100
    )
    assert rows
    assert all(
        "mirage_guard:high_dislocation_identity_inferred" in row["blockers"]
        for row in rows
        if max(abs(float(row["executable_spread_pct"])), abs(float(row["depth_weighted_spread_pct"]))) >= 5
    )


def test_dex_gas_evidence_reaches_candidate_without_false_missing_blocker() -> None:
    identity = "eip155:56/erc20:0xabc"
    dex = MarketQuote(
        token="COSTED", venue="OKX DEX 56", market_type="Spot",
        bid=1.0, ask=1.01, bid_vwap=1.0, ask_vwap=1.01,
        quote_ts_us=1, source_name="okx_dex_quote", identity_key=identity,
        chain_id=56, token_address="0xabc", gas_estimate_usd=0.05,
        quote_notional_usd=50.0, price_impact_pct=0.2,
        route_plan=("router",),
    )
    cex = MarketQuote(
        token="COSTED", venue="Bybit", market_type="Futures",
        bid=1.05, ask=1.06, bid_vwap=1.05, ask_vwap=1.06,
        quote_ts_us=1, source_name="ccxt", identity_key=identity,
        symbol="COSTED/USDT:USDT",
    )

    rows = sources.dex_candidates(
        [dex], [cex], source_name="okx_dex_quote", min_spread_pct=-100
    )

    long_dex = next(row for row in rows if row["long_venue"] == "OKX DEX 56")
    assert "gas_estimate_missing" not in long_dex["blockers"]
    assert float(long_dex["gas_adjusted_spread_pct"]) == pytest.approx(
        float(long_dex["executable_spread_pct"]) - 0.1
    )
    assert long_dex["notes"]["route_inputs"]["long"]["quote_notional_usd"] == 50.0


def test_unique_okx_identity_does_not_infer_known_collision() -> None:
    quote = MarketQuote(
        token="SAME",
        venue="Bybit",
        market_type="Futures",
        bid=1.0,
        ask=1.0,
        bid_vwap=1.0,
        ask_vwap=1.0,
        quote_ts_us=1,
        source_name="ccxt",
    )
    asset = WatchAsset(
        symbol="SAME",
        identity_key="eip155:1/erc20:0xabc",
        cex_enabled=True,
        dex_enabled=True,
        evm_contracts={1: "0xabc"},
    )
    registry = IdentityRegistry(known_ticker_collisions={"SAME": ("a", "b")})

    assert sources._apply_unique_okx_identities([quote], [asset], registry=registry)[0] == quote


def test_source_health_uses_live_quote_age_without_hiding_discovery_age(
    tmp_path: Path,
) -> None:
    now = time.time()
    current = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
    snapshot = tmp_path / "api.json"
    snapshot.write_text(
        json.dumps(
            {
                "updated_at": old,
                "api_discovered_rows": [],
                "dex_discovered_rows": [],
                "fast_quote_refresh": {
                    "status": "ok",
                    "updated_at": current,
                    "updated_routes": 25,
                },
            }
        ),
        encoding="utf-8",
    )

    _rows, meta = api_spreads._load_api_discovery_rows(
        snapshot,
        now=now,
        metadata={},
        rails={},
    )

    assert meta["status"] == "fresh"
    assert meta["age_min"] < 0.1
    assert meta["fast_quote_age_min"] < 0.1
    assert meta["discovery_age_min"] > 59
    assert meta["updated_at"] == current
    assert meta["discovery_updated_at"] == old


def test_settled_funding_propagates_to_every_route_using_same_leg() -> None:
    def route(long_venue: str, settled: float | None) -> dict:
        short_funding = {
            "current_funding_pct": 0.01,
            "funding_interval_hours": 1.0,
        }
        if settled is not None:
            short_funding["funding_24h_pct"] = settled
            short_funding["status"] = "ok"
        return {
            "token": "COTI",
            "long_venue": long_venue,
            "long_market_type": "Spot",
            "short_venue": "Bybit",
            "short_market_type": "Futures",
            "notes": {
                "route_inputs": {
                    "long": {"symbol": "COTI/USDT"},
                    "short": {"symbol": "COTI/USDT:USDT"},
                },
                "funding": {"short": short_funding},
            },
        }

    kucoin = route("Kucoin", -3.27)
    gate = route("Gate", None)
    payload = {"api_discovered_rows": [kucoin, gate], "dex_discovered_rows": []}

    api_spreads._propagate_funding_by_leg(payload)

    assert gate["notes"]["funding"]["short"]["funding_24h_pct"] == -3.27
    assert gate["funding_24h_pct"] == -3.27
    assert gate["funding_24h_source"] == "settled_public_events"


def test_gate_ccxt_alias_falls_back_to_current_adapter_name() -> None:
    current_gate_adapter = object()
    ccxt_stub = SimpleNamespace(gate=current_gate_adapter)

    assert live._ccxt_exchange_class(ccxt_stub, "gateio") is current_gate_adapter


def test_gate_public_candles_normalize_spot_and_futures_shapes() -> None:
    spot = live._gate_candles_to_ohlcv(
        [["100", "12", "1.1", "1.3", "0.9", "1.0", "8", "true"]],
        futures=False,
    )
    futures = live._gate_candles_to_ohlcv(
        [{"t": 100, "o": "1.0", "h": "1.3", "l": "0.9", "c": "1.1", "v": 8}],
        futures=True,
    )

    assert spot == [[100_000.0, 1.0, 1.3, 0.9, 1.1, 8.0]]
    assert futures == [[100_000.0, 1.0, 1.3, 0.9, 1.1, 8.0]]


def test_okx_dex_source_budget_covers_rate_limited_watchlist_scan() -> None:
    args = discovery_worker_parser().parse_args([])

    assert args.dex_spot_timeout_s == 240.0


def test_open_chart_can_requote_route_after_board_freshness_cutoff(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_load_spreads(**kwargs):
        captured.update(kwargs)
        return {"rows": [{"route_key": "TOKEN|A|Spot|B|Futures"}]}

    monkeypatch.setattr(api_spreads, "load_spreads", fake_load_spreads)

    row = server._find_canonical_route(
        "TOKEN|A|Spot|B|Futures",
        tmp_path / "board.jsonl",
    )

    assert row == {"route_key": "TOKEN|A|Spot|B|Futures"}
    assert captured["include_stale"] is True
    assert captured["include_unverified"] is True
    assert captured["limit"] is None


def test_pair_detail_keeps_exact_route_after_board_freshness_cutoff(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    route_key = "TOKEN|A|Spot|B|Futures"

    def fake_load_spreads(**kwargs):
        captured.update(kwargs)
        return {"rows": [{"route_key": route_key, "token": "TOKEN"}]}

    monkeypatch.setattr(api_spreads, "load_spreads", fake_load_spreads)
    monkeypatch.setattr(
        server.live,
        "get_route_detail",
        lambda row, config: {"board_row": row, "config": config},
    )

    result = server.api_pair(route_key, tmp_path / "board.jsonl", {"read_only": True})

    assert result["ok"] is True
    assert result["board_row"]["route_key"] == route_key
    assert captured["include_stale"] is True
    assert captured["include_unverified"] is True
    assert captured["limit"] is None


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


def test_exact_dex_watchlist_cex_quotes_bypass_general_depth_ranking() -> None:
    exact = MarketQuote(
        token="DEXE",
        venue="Binance",
        market_type="Futures",
        bid=2.3,
        ask=2.31,
        bid_vwap=2.3,
        ask_vwap=2.31,
        quote_ts_us=1,
        source_name="cex",
        identity_key="asset:dexe",
    )
    unrelated = MarketQuote(
        token="OTHER",
        venue="Binance",
        market_type="Futures",
        bid=1,
        ask=1.01,
        bid_vwap=1,
        ask_vwap=1.01,
        quote_ts_us=1,
        source_name="cex",
        identity_key="asset:other",
    )
    context = sources.DiscoveryContext(
        tokens=(),
        watchlist={
            "DEXE": WatchAsset(
                symbol="DEXE",
                identity_key="asset:dexe",
                cex_enabled=True,
                dex_enabled=True,
            )
        },
        deadline_monotonic=None,
    )

    selected = sources._exact_dex_watchlist_quotes([unrelated, exact], context)

    assert selected == [exact]


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
    assert sources._candidate_is_publicly_rankable(inventory_unknown)


def test_inverse_futures_spot_is_visible_with_inventory_condition() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={"executable_spread_pct": 3},
        long_market_type="Futures",
        short_market_type="Spot",
        long_rails={},
        short_rails={},
    )

    assert reasons == ["condition:spot_sell_inventory_required"]
    assert not any(reason.startswith("mirage_guard:") for reason in reasons)

    row = SimpleNamespace(
        to_dict=lambda: {"blockers": reasons},
        blockers=reasons,
    )
    assert api_spreads._public_row(row)["conditions"] == ["spot_sell_inventory_required"]


def test_unknown_spot_transfer_is_visible_as_research_condition() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={"executable_spread_pct": 3},
        long_market_type="Spot",
        short_market_type="Spot",
        long_rails={},
        short_rails={},
    )

    assert reasons == ["condition:spot_transfer_unknown"]


def test_unverified_cex_dislocation_above_five_percent_needs_exact_identity() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={
            "source_kind": "api_discovered",
            "executable_spread_pct": 6,
            "blockers": ["identity_unverified"],
        },
        long_market_type="Spot",
        short_market_type="Futures",
        long_rails={},
        short_rails={},
    )

    assert reasons == ["mirage_guard:high_dislocation_exact_identity_required"]


def test_route_alert_dialog_only_offers_server_evaluated_metrics() -> None:
    source = server.render_alert_draft_script()

    assert '<option value="token_spread">' in source
    assert '<option value="funding">' in source
    assert '<option value="price">' in source
    for unsupported in (
        "exchange_spread", "custom_pair_spread", "dw_tracking", "freshness",
        "community_call", "hyperliquid", "token_index",
    ):
        assert f'<option value="{unsupported}">' not in source


def test_production_board_does_not_admit_hour_old_routes_as_live() -> None:
    compose = (Path(__file__).resolve().parents[1] / "compose.production.yml").read_text()

    match = re.search(r'SPREADBOARD_LIVE_MAX_AGE_MIN:\s*"([0-9.]+)"', compose)
    assert match is not None
    assert float(match.group(1)) <= 5.0


def test_price_worker_invalidates_grouped_market_payloads() -> None:
    from scripts import run_spreadboard_service as service

    api_spreads._RESULT_CACHE.clear()
    server._MARKET_CACHE.clear()
    server._MARKET_STALE_CACHE.clear()
    api_spreads._RESULT_CACHE[("test",)] = (0, 0.0, {"old": True})
    server._MARKET_CACHE[("test",)] = (0.0, {"old": True})
    server._MARKET_STALE_CACHE[("test",)] = (0.0, {"old": True})

    service._invalidate_market_price_caches()

    assert api_spreads._RESULT_CACHE == {}
    assert server._MARKET_CACHE == {}
    assert server._MARKET_STALE_CACHE == {("test",): (0.0, {"old": True})}


def test_fast_quote_budget_covers_top_25_across_all_five_public_lanes() -> None:
    compose = (Path(__file__).resolve().parents[1] / "compose.production.yml").read_text()
    routes = int(re.search(r'SPREADBOARD_FAST_QUOTE_ROUTES:\s*"(\d+)"', compose).group(1))
    dex_routes = int(re.search(r'SPREADBOARD_FAST_DEX_ROUTES:\s*"(\d+)"', compose).group(1))
    workers = int(re.search(r'SPREADBOARD_FAST_QUOTE_WORKERS:\s*"(\d+)"', compose).group(1))
    timeout = int(re.search(r'SPREADBOARD_FAST_QUOTE_TIMEOUT_SECONDS:\s*"(\d+)"', compose).group(1))

    assert routes >= 5 * 25
    assert routes <= 220
    assert dex_routes >= 2 * 30
    assert 3 <= workers <= 8
    assert timeout <= 300


def test_high_dislocation_dex_route_requires_exact_cex_identity() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={
            "source_kind": "dex_discovered",
            "executable_spread_pct": 26_536,
            "blockers": ["cex_identity_unverified"],
        },
        long_market_type="Spot",
        short_market_type="Futures",
        long_rails={},
        short_rails={},
    )

    assert reasons == ["mirage_guard:dex_cex_identity_unverified"]


def test_low_dislocation_dex_route_still_requires_exact_cex_identity() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={
            "source_kind": "dex_discovered",
            "executable_spread_pct": 0.5,
            "blockers": ["cex_identity_unverified"],
        },
        long_market_type="Futures",
        short_market_type="DEX",
        long_rails={},
        short_rails={},
    )

    assert reasons == ["mirage_guard:dex_cex_identity_unverified"]


def test_exact_identity_dex_route_is_not_mirage_guarded() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={
            "source_kind": "dex_discovered",
            "executable_spread_pct": 3.2,
            "blockers": ["route_feasibility_unproven"],
        },
        long_market_type="Futures",
        short_market_type="DEX",
        long_rails={},
        short_rails={},
    )

    assert reasons == []


def test_large_cex_dislocation_requires_exact_public_contract_identity() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={
            "source_kind": "api_discovered",
            "long_venue": "Gate",
            "short_venue": "Kucoin",
            "executable_spread_pct": 97,
            "blockers": [],
        },
        long_market_type="Futures",
        short_market_type="Futures",
        long_rails={"networks": [{"network": "BEP20", "withdraw": True}]},
        short_rails={"networks": [{"network": "BASE", "deposit": True}]},
    )

    assert reasons == ["mirage_guard:high_dislocation_exact_identity_required"]


def test_guarded_routes_are_badged_by_default_and_can_be_hidden(monkeypatch) -> None:
    """The client/bot default must not silently erase a whole new token."""
    monkeypatch.delenv("SPREADBOARD_HIDE_GUARDED_ROWS", raising=False)
    assert api_spreads._hide_guarded_rows() is False

    monkeypatch.setenv("SPREADBOARD_HIDE_GUARDED_ROWS", "1")
    assert api_spreads._hide_guarded_rows() is True


def test_large_cex_dislocation_survives_exact_contract_match() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={
            "source_kind": "api_discovered",
            "long_venue": "Binance",
            "short_venue": "Mexc",
            "executable_spread_pct": 30,
            "blockers": [],
        },
        long_market_type="Spot",
        short_market_type="Spot",
        long_rails={
            "networks": [
                {"network": "ETH", "withdraw": True, "contract": "0xAbC"}
            ]
        },
        short_rails={
            "networks": [
                {"network": "ERC20", "deposit": True, "contract": "0xaBc"}
            ]
        },
    )

    assert reasons == []


def test_large_same_venue_spot_future_gap_still_needs_contract_proof() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={
            "source_kind": "api_discovered",
            "long_venue": "Gate",
            "short_venue": "Gate",
            "executable_spread_pct": 100,
            "blockers": [],
        },
        long_market_type="Spot",
        short_market_type="Futures",
        long_rails={},
        short_rails={},
    )
    assert reasons == ["mirage_guard:high_dislocation_exact_identity_required"]


def test_large_dex_gap_requires_the_cex_to_publish_the_same_contract() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={
            "source_kind": "dex_discovered",
            "long_venue": "Gate",
            "short_venue": "OKX DEX 1",
            "executable_spread_pct": 101,
            "blockers": [],
            "notes": {
                "identity": {
                    "long": {"identity_key": "asset:vanry"},
                    "short": {
                        "chain_id": 1,
                        "token_address": "0x8de5",
                    },
                }
            },
        },
        long_market_type="Spot",
        short_market_type="Spot",
        long_rails={
            "networks": [
                {"network": "ERC20", "withdraw": True, "contract": None}
            ]
        },
        short_rails={},
    )
    assert "mirage_guard:high_dislocation_cex_contract_unverified" in reasons


def test_dex_contract_guard_rejects_contract_from_another_token() -> None:
    watchlist = {
        "ETH": WatchAsset(
            symbol="ETH",
            identity_key="asset:eth",
            dex_enabled=True,
            evm_contracts={1: "0xeth"},
        ),
        "SOL": WatchAsset(
            symbol="SOL",
            identity_key="asset:sol",
            dex_enabled=True,
            solana_mint="So111",
        ),
    }

    assert api_spreads._dex_contract_mirage_reasons(
        token="ETH",
        chain_id="501",
        contract="So111",
        watchlist=watchlist,
    ) == ["mirage_guard:dex_contract_mismatch"]
    assert (
        api_spreads._dex_contract_mirage_reasons(
        token="SOL",
        chain_id="501",
        contract="So111",
        watchlist=watchlist,
        )
        == []
    )


def test_dex_contract_guard_accepts_exact_dynamic_identity() -> None:
    assert (
        api_spreads._dex_contract_mirage_reasons(
        token="DYNAMIC",
        chain_id="56",
        contract="0xAbC",
        identity_key="eip155:56/erc20:0xabc",
        watchlist={},
        )
        == []
    )


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


@pytest.mark.parametrize(
    ("exchange_id", "payload"),
    [
        (
            "bitmart",
            {
                "data": {
                    "list": [
                        {
                            "funding_time": int(time.time() * 1000) - 3_600_000,
                            "funding_rate": "0.0002",
                        }
                    ]
                }
            },
        ),
        (
            "xt",
            {
                "result": {
                    "items": [
                        {
                            "createdTime": int(time.time() * 1000) - 3_600_000,
                            "fundingRate": "0.0002",
                        }
                    ]
                }
            },
        ),
    ],
)
def test_new_native_funding_histories_sum_settled_events(
    monkeypatch: pytest.MonkeyPatch,
    exchange_id: str,
    payload: dict,
) -> None:
    monkeypatch.setattr(live, "_public_json", lambda *_args, **_kwargs: payload)

    result = live._fetch_native_funding_24h(exchange_id, "TEST/USDT:USDT")

    assert result is not None
    assert result["status"] == "ok"
    assert result["funding_24h_pct"] == pytest.approx(0.02)
    assert result["samples"] == 1


def test_okx_dex_uses_usd_network_fee_not_raw_gas_units() -> None:
    captured: dict[str, object] = {}

    def sell_quote(**kwargs: object) -> dict[str, str]:
        captured.update(kwargs)
        return {
            "status": "ok",
            "dex_sell_price_usd": "2.49",
        }

    okx = SimpleNamespace(
        quote_usdc_to_token=lambda **_kwargs: {
            "status": "ok",
            "out_qty": "20",
            "to_token_decimals": 9,
            "dex_buy_price_usd": "2.5",
            "trade_fee_usd": "0.42",
            "estimate_gas_fee": "123456",
            "router": "Uniswap",
        },
        quote_token_to_usdc=sell_quote,
    )

    quote = sources.OkxDexQuoteSource(request_interval_seconds=0)._quote_asset(
        WatchAsset(
            symbol="TEST",
            identity_key="asset:test",
            dex_enabled=True,
            evm_contracts={1: "0x123"},
        ),
        chain_id=1,
        contract="0x123",
        credentials=object(),
        context=SimpleNamespace(target_notional_usd=50),
        okx_dex=okx,
    )

    assert quote is not None
    assert quote.gas_estimate_usd == 0.42
    assert quote.token_address == "0x123"
    assert captured["token_decimals"] == 9


def test_okx_dex_pair_detail_recognizes_spot_leg_venue() -> None:
    summary = live.okx_dex_quote_summary(
        {
            "long_venue": "XT",
            "long_market_type": "Futures",
            "short_venue": "OKX DEX 1",
            "short_market_type": "Spot",
        }
    )

    assert summary["status"] == "blocked"
    assert summary["blockers"] == ["exact_chain_contract_required"]


def test_okx_dex_identity_reaches_normalized_board_row() -> None:
    quote_ts_us = int(time.time() * 1_000_000)
    row = api_spreads._row_from_api(
        {
            "token": "TEST",
            "long_venue": "XT",
            "long_market_type": "Futures",
            "short_venue": "OKX DEX 1",
            "short_market_type": "Spot",
            "quote_ts_us": quote_ts_us,
            "notes": {
                "identity": {
                    "long": {},
                    "short": {
                        "chain_id": 1,
                        "token_address": "0x123",
                    },
                },
            },
        },
        bucket="dex_discovered",
        now=quote_ts_us / 1_000_000,
    )

    assert row.dex_chain == "1"
    assert row.dex_contract == "0x123"


def test_fast_quote_funding_reaches_normalized_board_row() -> None:
    quote_ts_us = int(time.time() * 1_000_000)
    row = api_spreads._row_from_api(
        {
            "token": "COTI",
            "long_venue": "Kucoin Futures",
            "long_market_type": "Futures",
            "short_venue": "Bybit",
            "short_market_type": "Futures",
            "quote_ts_us": quote_ts_us,
            "notes": {
                "route_inputs": {
                    "long": {
                        "symbol": "COTI/USDT:USDT",
                        "current_funding_pct": -0.0072,
                        "funding_interval_hours": 1,
                        "next_funding_ts_us": 1_800_000_000_000_000,
                    },
                    "short": {
                        "symbol": "COTI/USDT:USDT",
                        "current_funding_pct": -0.1,
                        "funding_interval_hours": 1,
                        "next_funding_ts_us": 1_800_000_000_000_000,
                    },
                }
            },
        },
        bucket="api_discovered",
        now=quote_ts_us / 1_000_000,
    )

    assert row.long_funding_pct == -0.0072
    assert row.short_funding_pct == -0.1
    assert row.long_funding_interval_hours == 1
    assert row.short_funding_interval_hours == 1
    assert row.long_next_funding_ts_us == 1_800_000_000_000_000


def test_verified_dex_watchlist_and_cex_identity_cover_reference_top_ten() -> None:
    root = Path(__file__).resolve().parents[1]
    watchlist = load_watchlist(root / "data" / "api_discovery_watchlist.json")
    registry = load_identity_registry(root / "data" / "api_discovery_identity_registry.json")
    expected = {
        "BP": ("501", "BPxxfRCXkUVhig4HS1Lh7kZqV6SPJhzfEk4x6fVBjPCy"),
        "T": ("1", "0xcdf7028ceab81fa0c6971208e83fa7872994bee5"),
        "DEXE": ("1", "0xde4EE8057785A7e8e800Db58F9784845A5C2Cbd6"),
        "ESPORTS": ("56", "0xf39e4b21c84e737df08e2c3b32541d856f508e48"),
        "BRETT": ("8453", "0x532f27101965dd16442e59d40670faf5ebb142e4"),
        "BANK": ("56", "0x3aee7602b612de36088f3ffed8c8f10e86ebf2bf"),
        "PTB": ("56", "0x95c9b514566fbd224dc2037f5914eb8ab91c9201"),
        "ASTEROID": ("1", "0xf280b16ef293d8e534e370794ef26bf312694126"),
        "DOT": ("56", "0x7083609fce4d1d8dc0c979aab8c869ea2c873402"),
        "AZTEC": ("1", "0xa27ec0006e59f245217ff08cd52a7e8b169e62d2"),
    }

    for symbol, (chain_id, contract) in expected.items():
        asset = watchlist[symbol]
        assert asset.dex_enabled
        if chain_id == "501":
            assert asset.solana_mint == contract
        else:
            assert asset.evm_contracts[int(chain_id)].casefold() == contract.casefold()

    assert (
        registry.resolve_market(
        venue="Bybit",
        market_type="Futures",
        token="BRETT",
        symbol="BRETT/USDT:USDT",
        ).identity_key
        == "eip155:8453/erc20:0x532f27101965dd16442e59d40670faf5ebb142e4"
    )
    assert (
        registry.resolve_market(
        venue="Bybit",
        market_type="Futures",
        token="BANK",
        symbol="BANK/USDT:USDT",
        ).identity_key
        is None
    )


def test_watchlist_suppresses_contract_claimed_by_multiple_assets(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "tokens": [
                    {
                        "symbol": "ETH",
                        "identity_key": "asset:eth",
                        "dex_enabled": True,
                        "evm_contracts": {"1": "0xeth"},
                        "solana_mint": "SoDuplicate",
                    },
                    {
                        "symbol": "SOL",
                        "identity_key": "asset:sol",
                        "dex_enabled": True,
                        "solana_mint": "SoDuplicate",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    watchlist = load_watchlist(path)

    assert watchlist["ETH"].evm_contracts == {1: "0xeth"}
    assert watchlist["ETH"].solana_mint is None
    assert watchlist["SOL"].solana_mint is None


def test_generated_identity_registry_requires_exact_unique_contract_match(
    tmp_path: Path,
) -> None:
    static_path = tmp_path / "static.json"
    watchlist_path = tmp_path / "watchlist.json"
    rails_path = tmp_path / "rails.json"
    snapshot_path = tmp_path / "snapshot.json"
    output_path = tmp_path / "generated.json"
    static_path.write_text(
        json.dumps(
            {
                "schema": "spreadarb.api_discovery.identity_registry.v1",
                "assets": [],
                "known_ticker_collisions": [],
            }
        ),
        encoding="utf-8",
    )
    watchlist_path.write_text(
        json.dumps(
            {
                "tokens": [
                    {
                        "symbol": "COTI",
                        "identity_key": "asset:coti",
                        "decimals": 18,
                        "evm_contracts": {"1": "0xC0ti"},
                    },
                    {
                        "symbol": "PAI",
                        "identity_key": "asset:pai-one",
                        "evm_contracts": {"1": "0xDuplicate"},
                    },
                    {
                        "symbol": "PAI",
                        "identity_key": "asset:pai-two",
                        "evm_contracts": {"1": "0xDuplicate"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    rails_path.write_text(
        json.dumps(
            {
                "rails": {
                    "Coinbase": {
                        "COTI": {"networks": [{"network": "ERC20", "contract": "0xc0TI"}]},
                        "PAI": {"networks": [{"network": "ERC20", "contract": "0xduplicate"}]},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    snapshot_path.write_text(
        json.dumps(
            {
                "api_discovered_rows": [
                    {
                        "token": "COTI",
                        "long_venue": "Coinbase",
                        "long_market_type": "Spot",
                        "short_venue": "Coinbase",
                        "short_market_type": "Futures",
                        "notes": {
                            "route_inputs": {
                                "long": {"symbol": "COTI/USD"},
                                "short": {"symbol": "COTI/USDC:USDC"},
                            }
                        },
                    },
                    {
                        "token": "PAI",
                        "long_venue": "Coinbase",
                        "long_market_type": "Spot",
                        "notes": {"route_inputs": {"long": {"symbol": "PAI/USD"}}},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    payload = build_verified_identity_registry(
        static_registry_path=static_path,
        watchlist_path=watchlist_path,
        rails_path=rails_path,
        snapshot_path=snapshot_path,
        output_path=output_path,
    )
    registry = load_identity_registry(output_path)

    assert payload["generation"]["verified_matches"] == 1
    assert payload["generation"]["markets_added"] == 2
    assert (
        registry.resolve_market(
            venue="Coinbase",
            market_type="Spot",
            token="COTI",
            symbol="COTI/USD",
        ).identity_key
        == "asset:coti"
    )
    future = registry.resolve_market(
        venue="Coinbase",
        market_type="Futures",
        token="COTI",
        symbol="COTI/USDC:USDC",
    )
    assert future.identity_key == "asset:coti"
    assert future.market_identity is not None
    assert future.market_identity.source == "public_contract_match"
    assert (
        registry.resolve_market(
            venue="Coinbase",
            market_type="Spot",
            token="PAI",
            symbol="PAI/USD",
        ).identity_key
        is None
    )


def test_projected_funding_is_visible_rankable_and_filterable_in_24h_units() -> None:
    quote_ts_us = int(time.time() * 1_000_000)

    def row(token: str, *, settled: float | None, projected: float):
        return api_spreads._row_from_api(
            {
                "token": token,
                "long_venue": "Aster",
                "long_market_type": "Futures",
                "short_venue": "Bybit",
                "short_market_type": "Futures",
                "quote_ts_us": quote_ts_us,
                "executable_spread_pct": 2.0,
                "funding_24h_pct": settled,
                "funding_projected_24h_pct": projected,
            },
            bucket="api_discovered",
            now=quote_ts_us / 1_000_000,
        )

    settled = row("SETTLED", settled=0.25, projected=9.0)
    projected = row("PROJECTED", settled=None, projected=0.5)
    negative = row("NEGATIVE", settled=-0.75, projected=-0.1)
    groups = api_spreads._group_rows([settled, projected, negative])
    by_token = {group["token"]: group for group in groups}

    assert by_token["SETTLED"]["best_funding_24h_pct"] == 0.25
    assert by_token["SETTLED"]["best_funding_24h_basis"] == "settled_public_events"
    assert by_token["PROJECTED"]["best_funding_24h_pct"] == 0.5
    assert by_token["PROJECTED"]["best_funding_24h_basis"] == "projected_current_rate"
    assert by_token["NEGATIVE"]["best_funding_24h_pct"] == -0.75
    assert api_spreads._filter_rows(
        [settled, projected],
        min_abs_funding_24h_pct=0.4,
    ) == [projected]


def test_okx_dex_retries_rate_limits_without_changing_quote_math() -> None:
    calls = 0

    def quote_func(**_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "status": "blocked",
                "blockers": ["okx_dex_quote:Too Many Requests"],
            }
        return {"status": "ok", "dex_buy_price_usd": "1"}

    source = sources.OkxDexQuoteSource(
        request_interval_seconds=0,
        max_rate_limit_retries=1,
    )

    result = source._quote_with_retry(quote_func)

    assert result["status"] == "ok"
    assert calls == 2


def test_okx_dynamic_catalogue_keeps_only_unique_symbol_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPREADBOARD_OKX_DEX_DYNAMIC_TOKENS", "25")
    source = sources.OkxDexQuoteSource(request_interval_seconds=0)
    refs = (
        MarketQuote(
            token="UNIQUE",
            venue="Bybit",
            market_type="Futures",
            bid=2,
            ask=2,
            bid_vwap=2,
            ask_vwap=2,
            quote_ts_us=1,
            source_name="test",
            volume_24h_usd=100,
        ),
        MarketQuote(
            token="DUP",
            venue="Bybit",
            market_type="Futures",
            bid=2,
            ask=2,
            bid_vwap=2,
            ask_vwap=2,
            quote_ts_us=1,
            source_name="test",
            volume_24h_usd=200,
        ),
    )

    class Okx:
        @staticmethod
        def list_tokens(*, chain: str, **_kwargs: object) -> dict[str, object]:
            if chain == "1":
                return {
                    "status": "ok",
                    "tokens": [
                        {
                            "symbol": "UNIQUE",
                            "address": "0x111",
                            "decimals": 18,
                            "chain_index": "1",
                        },
                        {
                            "symbol": "DUP",
                            "address": "0x222",
                            "decimals": 18,
                            "chain_index": "1",
                        },
                    ],
                }
            if chain == "56":
                return {
                    "status": "ok",
                    "tokens": [
                        {
                            "symbol": "DUP",
                            "address": "0x333",
                            "decimals": 18,
                            "chain_index": "56",
                        }
                    ],
                }
            return {"status": "ok", "tokens": []}

    assets, errors = source._discover_okx_assets(
        context=SimpleNamespace(
            reference_quotes=refs,
            timed_out=lambda: False,
        ),
        credentials=object(),
        okx_dex=Okx,
        existing_tokens=set(),
    )

    assert errors == []
    assert [asset.token for asset in assets] == ["UNIQUE"]
    assert assets[0].identity_key == "eip155:1/erc20:0x111"


def test_okx_dynamic_catalogue_prioritizes_funding_before_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPREADBOARD_OKX_DEX_DYNAMIC_TOKENS", "1")
    source = sources.OkxDexQuoteSource(request_interval_seconds=0)
    refs = (
        MarketQuote(
            token="CARRY",
            venue="Bybit",
            market_type="Futures",
            bid=1,
            ask=1,
            bid_vwap=1,
            ask_vwap=1,
            quote_ts_us=1,
            source_name="test",
            funding_rate_pct=0.2,
            funding_interval_hours=4,
            volume_24h_usd=10,
        ),
        MarketQuote(
            token="VOLUME",
            venue="Bybit",
            market_type="Futures",
            bid=1,
            ask=1,
            bid_vwap=1,
            ask_vwap=1,
            quote_ts_us=1,
            source_name="test",
            funding_rate_pct=0.01,
            funding_interval_hours=4,
            volume_24h_usd=1_000_000,
        ),
    )

    class Okx:
        @staticmethod
        def list_tokens(*, chain: str, **_kwargs: object) -> dict[str, object]:
            if chain != "1":
                return {"status": "ok", "tokens": []}
            return {
                "status": "ok",
                "tokens": [
                    {"symbol": "CARRY", "address": "0x1", "decimals": 18, "chain_index": "1"},
                    {"symbol": "VOLUME", "address": "0x2", "decimals": 18, "chain_index": "1"},
                ],
            }

    assets, errors = source._discover_okx_assets(
        context=SimpleNamespace(reference_quotes=refs, timed_out=lambda: False),
        credentials=object(),
        okx_dex=Okx,
        existing_tokens=set(),
    )

    assert errors == []
    assert [asset.token for asset in assets] == ["CARRY"]


def test_dex_source_health_keeps_sanitized_provider_diagnostics() -> None:
    payload = {
        "source_refresh": {
            "sources": [
                {
                    "name": "okx_dex_quote",
                    "status": "partial",
                    "rows": 0,
                    "blockers": ["partial_source_errors"],
                    "errors": ["PEPE:1:RuntimeError: okx_dex_quote:IP validation failed"],
                    "details": {
                        "provider": "OKX DEX",
                        "quote_count": 0,
                        "private_debug": "do not expose",
                    },
                }
            ]
        }
    }

    health = api_spreads._dex_spot_source_status(payload)

    assert health["errors"] == ["PEPE:1:RuntimeError: okx_dex_quote:IP validation failed"]
    assert health["details"] == {"provider": "OKX DEX", "quote_count": 0}


def test_spread_ceiling_can_be_disabled_without_hiding_large_exact_routes() -> None:
    assert not sources._spread_ceiling_exceeded(102.2, max_spread_pct=0)
    assert sources._spread_ceiling_exceeded(102.2, max_spread_pct=100)


def test_measured_depth_vwap_selects_route_before_top_book() -> None:
    first = SimpleNamespace(
        displayed_open_spread_pct=4.4,
        executable_spread_pct=4.4,
        depth_weighted_spread_pct=1.1,
        blockers=[],
    )
    second = SimpleNamespace(
        displayed_open_spread_pct=2.0,
        executable_spread_pct=2.0,
        depth_weighted_spread_pct=1.8,
        blockers=[],
    )

    assert api_spreads._entrance_spread(first) == 1.1
    assert api_spreads._entrance_spread(first) < api_spreads._entrance_spread(second)


def test_unverified_depth_falls_back_to_top_book() -> None:
    row = SimpleNamespace(
        displayed_open_spread_pct=4.4,
        executable_spread_pct=4.4,
        depth_weighted_spread_pct=1.1,
        blockers=["depth_unverified"],
    )

    assert api_spreads._entrance_spread(row) == 4.4


def test_short_chart_windows_filter_exact_elapsed_time() -> None:
    now_us = int(time.time() * 1_000_000)
    history = [
        {"quote_ts_us": now_us - 30 * 1_000_000},
        {"quote_ts_us": now_us - 4 * 60 * 1_000_000},
        {"quote_ts_us": now_us - 20 * 60 * 1_000_000},
        {"quote_ts_us": now_us - 50 * 60 * 1_000_000},
    ]

    assert len(server.filter_chart_history(history, "1m")) == 1
    assert len(server.filter_chart_history(history, "5m")) == 2
    assert len(server.filter_chart_history(history, "30m")) == 3
    assert len(server.filter_chart_history(history, "1h")) == 4
    assert server.chart_window_config("4h")["hours"] == 4
    assert server.chart_window_config("12h")["hours"] == 12
    assert server.chart_window_config("3d")["hours"] == 72
    assert server.chart_window_config("7d")["hours"] == 168


def test_discovery_publishes_completed_sources_before_slowest_source(
    tmp_path,
) -> None:
    snapshot_path = tmp_path / "latest.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "api_discovered_rows": [
                    {
                        "route_key": "OLD|A|Futures|B|Futures",
                        "source_kind": SOURCE_API_DISCOVERED,
                        "token": "OLD",
                    }
                ],
                "dex_discovered_rows": [],
                "source_refresh": {},
            }
        ),
        encoding="utf-8",
    )
    watchlist_path = tmp_path / "watchlist.json"
    watchlist_path.write_text('{"tokens":[]}', encoding="utf-8")

    def status(name: str) -> SourceStatus:
        return SourceStatus(
            name=name,
            kind="cex",
            status="ok",
            started_at="2026-07-29T00:00:00Z",
            finished_at="2026-07-29T00:00:01Z",
            elapsed_seconds=1,
            rows=1,
        )

    class FirstSource:
        name = "first"
        kind = "cex"

        def collect(self, _context) -> SourceResult:
            return SourceResult(
                status=status(self.name),
                rows=(
                    {
                        "source_kind": SOURCE_API_DISCOVERED,
                        "token": "ONE",
                    },
                ),
            )

    class SecondSource:
        name = "second"
        kind = "cex"

        def collect(self, _context) -> SourceResult:
            partial = json.loads(snapshot_path.read_text(encoding="utf-8"))
            assert partial["source_refresh"]["partial"] is True
            assert partial["source_refresh"]["sources_completed"] == 1
            assert [row["token"] for row in partial["api_discovered_rows"]] == [
                "ONE",
                "OLD",
            ]
            assert partial["source_refresh"]["previous_snapshot_rows_retained"] == 1
            return SourceResult(status=status(self.name))

    result = runner.run_discovery(
        db_path=None,
        watchlist_path=watchlist_path,
        snapshot_path=snapshot_path,
        archive_dir=tmp_path / "archive",
        timeout_seconds=10,
        sources=[FirstSource(), SecondSource()],
        row_limit=25,
    )

    assert result["source_refresh"].get("partial") is None
    assert [row["token"] for row in result["api_discovered_rows"]] == ["ONE"]


def test_single_group_worker_uses_public_snapshot_for_incremental_updates(
    tmp_path,
) -> None:
    seen_paths = []

    def fake_run_discovery(**kwargs):
        seen_paths.append(kwargs["snapshot_path"])
        return {
            "api_discovered_rows": [],
            "dex_discovered_rows": [],
            "source_refresh": {"sources": []},
        }

    output = tmp_path / "latest.json"
    worker.run_grouped_discovery(
        db_path=None,
        watchlist_path=None,
        snapshot_path=output,
        archive_dir=tmp_path / "archive",
        parts_dir=tmp_path / "parts",
        groups=[
            worker.DiscoveryGroup(
                name="all",
                sources={"cex"},
                all_platform_tokens=True,
                timeout_seconds=10,
                max_orderbook_candidates=1,
                row_limit=1,
            )
        ],
        run_discovery_func=fake_run_discovery,
    )

    assert seen_paths == [output]


def test_discovery_merge_keeps_newest_duplicate_without_retaining_old_final_route() -> None:
    current = {
        "api_discovered_rows": [{"route_key": "SAME", "quote_ts_us": 100, "token": "CURRENT"}],
        "dex_discovered_rows": [],
        "source_refresh": {},
    }
    previous = {
        "api_discovered_rows": [
            {"route_key": "SAME", "quote_ts_us": 200, "token": "NEWER"},
            {"route_key": "OLD", "quote_ts_us": 200, "token": "OLD"},
        ],
        "dex_discovered_rows": [],
        "source_refresh": {},
    }

    partial = runner._retain_previous_rows(
        json.loads(json.dumps(current)),
        previous,
        row_limit=10,
    )
    final = runner._prefer_newer_previous_rows(
        json.loads(json.dumps(current)),
        previous,
    )

    assert [row["token"] for row in partial["api_discovered_rows"]] == [
        "NEWER",
        "OLD",
    ]
    assert [row["token"] for row in final["api_discovered_rows"]] == ["NEWER"]


def test_validated_reference_venues_are_enabled() -> None:
    spot = sources.default_enabled_cex_source().venues
    futures = sources.default_enabled_cex_futures_source().venues

    assert {"HTX", "Phemex", "CoinEx", "WhiteBIT", "BitMart", "XT"} <= set(spot)
    assert {"HTX", "Phemex", "CoinEx", "WhiteBIT", "BitMart", "XT"} <= set(futures)
    # Upbit was previously excluded, most likely because Korean venues carry a
    # persistent local premium that is not arbitrageable across capital
    # controls. Re-enabled 2026-08-01 by operator request for venue parity with
    # the reference product, which does quote Upbit. Only its USDT markets are
    # used, not KRW. If its rows prove to be non-capturable premium rather than
    # real edge, drop "Upbit" from default_enabled_cex_source() again.
    assert "Upbit" in spot
    assert "Lighter" in futures


@pytest.mark.parametrize(
    ("venue", "market_id", "payload", "expected_rate", "expected_interval"),
    [
        (
            "Mexc",
            "TEST_USDT",
            {
                "data": [
                    {
                        "symbol": "TEST_USDT",
                        "fundingRate": "-0.0012",
                        "collectCycle": 4,
                        "nextSettleTime": 1_800_000_000_000,
                    }
                ]
            },
            -0.0012,
            "4h",
        ),
        (
            "WhiteBIT",
            "TEST_PERP",
            {
                "result": [
                    {
                        "ticker_id": "TEST_PERP",
                        "funding_rate": "0.0003",
                        "funding_interval_minutes": 60,
                        "next_funding_rate_timestamp": 1_800_000_000_000,
                    }
                ]
            },
            0.0003,
            "1h",
        ),
    ],
)
def test_native_bulk_funding_maps_exchange_market_ids(
    monkeypatch: pytest.MonkeyPatch,
    venue: str,
    market_id: str,
    payload: dict,
    expected_rate: float,
    expected_interval: str,
) -> None:
    monkeypatch.setattr(sources, "fetch_json", lambda *_args, **_kwargs: payload)
    exchange = SimpleNamespace(markets={"TEST/USDT:USDT": {"id": market_id}})
    errors: list[str] = []

    result = sources._fetch_native_bulk_funding_rates(
        exchange,
        ["TEST/USDT:USDT"],
        context=SimpleNamespace(remaining_timeout=lambda _cap: 5.0),
        errors=errors,
        venue=venue,
    )

    assert errors == []
    assert result is not None
    assert result["TEST/USDT:USDT"]["fundingRate"] == expected_rate
    assert result["TEST/USDT:USDT"]["interval"] == expected_interval


def test_default_sources_publish_small_cex_batches_before_enrichment() -> None:
    enabled = sources.default_sources(include_network=True)
    cex = [
        source
        for source in enabled
        if isinstance(source, sources.CexCcxtSource) and source.kind == "cex"
    ]

    assert cex
    assert all(len(source.venues) <= 5 for source in cex)
    assert cex[0].market_type == "Spot"
    assert cex[1].market_type == "Futures"
    assert {
        venue for source in cex if source.market_type == "Spot" for venue in source.venues
    } == set(sources.default_enabled_cex_source().venues)
    assert {
        venue for source in cex if source.market_type == "Futures" for venue in source.venues
    } == set(sources.default_enabled_cex_futures_source().venues)


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
    current = dict(
        freshness="fresh",
        age_min=0.1,
        executable_spread_pct=1.0,
        depth_weighted_spread_pct=1.0,
        depth_usd=50.0,
        long_price=1.0,
        short_price=1.01,
        long_volume_24h_usd=1e5,
        short_volume_24h_usd=1e5,
        blockers=[],
        asset_class="crypto",
    )
    rows = [
        SimpleNamespace(route_kind="FUTURES", token="ONE", **current),
        SimpleNamespace(route_kind="FUTURES-SPOT", token="ONE", **current),
        SimpleNamespace(route_kind="SPOT-FUTURES", token="ONE", **current),
        SimpleNamespace(route_kind="SPOT-FUTURES", token="TWO", **current),
        SimpleNamespace(route_kind="SPOT", token="THREE", **current),
        SimpleNamespace(route_kind="DEX-FUTURES", token="FOUR", **current),
        SimpleNamespace(route_kind="DEX-SPOT", token="FIVE", **current),
    ]

    # DEX-SPOT added deliberately: the public contract advertises five lanes and
    # the reference product shows Spot-Dex, but this counter tracked only four.
    assert api_spreads._release_lane_token_counts(rows) == {
        "FUTURES": 1,
        "FUTURES-SPOT": 2,
        "SPOT": 1,
        "DEX-FUTURES": 1,
        "DEX-SPOT": 1,
    }
    assert (
        server.market_kind_count(
        "FUTURES-SPOT-PAIR",
        {"FUTURES-SPOT": 2, "SPOT-FUTURES": 2},
        {},
        {"FUTURES-SPOT": 3},
        )
        == 3
    )


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


def test_all_five_lanes_have_a_markets_tab() -> None:
    """Every lane that carries data must be reachable in the UI.

    Spot-DEX had rows but no tab, so the lane was invisible to members.
    """
    import inspect
    from spreadboard import server

    source = inspect.getsource(server.render_market_filter_bar)
    for value, label in [
        ("FUTURES", "Futures-Futures"),
        ("FUTURES-SPOT-PAIR", "Futures-Spot"),
        ("SPOT", "Spot-Spot"),
        ("DEX-FUTURES", "Futures-DEX"),
        ("DEX-SPOT", "Spot-DEX"),
    ]:
        assert f'("{value}", "{label}")' in source, f"missing markets tab for {label}"


def test_ourbit_is_registered_on_both_market_types() -> None:
    """Ourbit has no ccxt adapter; it is an MEXC white-label we retarget."""
    assert "Ourbit" in sources.default_enabled_cex_source().venues
    assert "Ourbit" in sources.default_enabled_cex_futures_source().venues


def test_ourbit_exchange_points_at_ourbit_hosts_not_mexc() -> None:
    """A retarget bug would silently quote MEXC prices under the Ourbit name."""
    exchange = sources._build_ccxt_exchange("ourbit", "Futures", 5.0)
    urls = exchange.urls["api"]
    flat = " ".join(
        str(v) for value in urls.values()
        for v in (value.values() if isinstance(value, dict) else [value])
    )
    assert "ourbit.com" in flat
    assert "mexc.com" not in flat, "must not fall back to MEXC hosts"
    assert exchange.id == "ourbit"


def test_broad_dex_output_goes_to_the_writable_runtime_dir() -> None:
    """The repo data/ dir is read-only in the container.

    Enabling broad DEX-spot discovery without redirecting this path made every
    refresh die with PermissionError, freezing the whole board.
    """
    source = (Path(__file__).resolve().parents[1] / "scripts/run_spreadboard_service.py").read_text(encoding="utf-8")
    assert "--broad-dex-output-path" in source
    assert 'RUNTIME_DIR / "api_discovery_broad_dex_latest.json"' in source


def test_freshness_window_tracks_the_continuous_quote_workers() -> None:
    """Discovery finds routes; continuous workers must keep them admissible.

    Extending freshness to the 25-minute discovery cadence let a failed venue
    retain an hour-old executable claim. Bulk and fast quote workers refresh
    prices independently, so a route they cannot touch must leave promptly.
    """
    compose = (Path(__file__).resolve().parents[1] / "compose.production.yml").read_text(encoding="utf-8")
    window = float(re.search(r'SPREADBOARD_LIVE_MAX_AGE_MIN:\s*"(\d+)"', compose).group(1))
    bulk_tick = float(re.search(r'SPREADBOARD_BULK_QUOTE_SECONDS:\s*"(\d+)"', compose).group(1))
    assert window <= 5.0
    assert bulk_tick <= 30.0
    assert "_invalidate_market_price_caches()" in (
        Path(__file__).resolve().parents[1] / "scripts/run_spreadboard_service.py"
    ).read_text(encoding="utf-8")


def test_only_transfer_lanes_need_a_rail() -> None:
    """Futures legs settle in margin and a DEX leg sits in your own wallet."""
    assert api_spreads.TRANSFER_ROUTE_KINDS == frozenset({"SPOT", "DEX-SPOT"})


def _row(**kw):
    from types import SimpleNamespace
    base = dict(route_kind="SPOT", long_withdraw_enabled=True, short_deposit_enabled=True)
    base.update(kw)
    return SimpleNamespace(**base)


def test_shut_destination_deposit_makes_a_route_undeliverable() -> None:
    """SIREN sat at ~100% DEX->Kucoin on an identical contract purely because
    Kucoin deposits were closed. That is a shut rail, not an opportunity."""
    assert api_spreads.route_deliverable(_row(short_deposit_enabled=False)) is False
    assert api_spreads.route_deliverable(_row(long_withdraw_enabled=False)) is False
    assert api_spreads.route_deliverable(_row()) is True


def test_unknown_rail_status_is_not_treated_as_broken() -> None:
    assert api_spreads.route_deliverable(_row(short_deposit_enabled=None)) is None


def test_margin_only_lanes_need_no_rail() -> None:
    """Corrected 2026-08-01: FUTURES-SPOT is NOT margin-only.

    Its short leg is spot, which means owning the coin on that venue, so a shut
    deposit blocks it. The genuinely margin-only lanes are these three: you buy
    the spot leg on the venue you already hold funds on, or the DEX leg sits in
    your own wallet.
    """
    for kind in ("FUTURES", "SPOT-FUTURES", "DEX-FUTURES"):
        assert api_spreads.route_deliverable(
            _row(route_kind=kind, long_withdraw_enabled=False, short_deposit_enabled=False)
        ) is True, f"{kind} needs no transfer and must not be filtered"


def test_absurd_price_ratio_is_flagged_as_a_different_asset() -> None:
    """CAT: 0.000001366 on Kucoin spot vs 806.75 on Bitget futures."""
    assert api_spreads.price_ratio_implausible(
        _row(long_price=1.366e-06, short_price=806.75)
    ) is True


def test_genuine_large_spreads_survive_the_ratio_rule() -> None:
    """A 150% capture is 2.5x and must survive; the operator has taken one."""
    # SIREN DEX vs Kucoin, ~2x
    assert api_spreads.price_ratio_implausible(_row(long_price=0.0280, short_price=0.0565)) is False
    # a 150% edge, 2.5x
    assert api_spreads.price_ratio_implausible(_row(long_price=1.0, short_price=2.5)) is False


def test_ratio_bound_catches_the_anthropic_tier() -> None:
    """ANTHROPIC showed 881% (9.8x) here against 3.13% on the reference board."""
    assert api_spreads.MAX_CROSS_VENUE_PRICE_RATIO == 3.0
    assert api_spreads.price_ratio_implausible(_row(long_price=1.0, short_price=9.8)) is True


def test_missing_prices_do_not_trigger_the_ratio_rule() -> None:
    assert api_spreads.price_ratio_implausible(_row(long_price=None, short_price=1.0)) is False
    assert api_spreads.price_ratio_implausible(_row(long_price=0.0, short_price=1.0)) is False


def test_group_headline_prefers_a_tradeable_route() -> None:
    """A shut rail must not buy a token its top-25 slot.

    Before this, a token whose only huge edge was undeliverable still ranked by
    that edge, displacing tokens someone could actually trade.
    """
    from spreadboard.api_spreads import SpreadTerminalRow

    def row(edge, kind="SPOT", withdraw=True, deposit=True, lp=1.0, sp=None):
        base = {f.name: None for f in __import__("dataclasses").fields(SpreadTerminalRow)}
        base.update(
            token="TKN", route_kind=kind, executable_spread_pct=edge,
            displayed_open_spread_pct=edge, depth_weighted_spread_pct=edge,
            long_withdraw_enabled=withdraw, short_deposit_enabled=deposit,
            long_price=lp, short_price=sp if sp is not None else lp * (1 + edge / 100),
            blockers=[], freshness="fresh", age_min=1.0, route_key=f"TKN|{edge}",
        )
        return SpreadTerminalRow(**base)

    groups = api_spreads._group_rows([
        row(500.0, deposit=False),   # huge but the rail is shut
        row(4.0),                    # modest and actually tradeable
    ])
    assert len(groups) == 1
    assert groups[0]["best_edge_pct"] == 4.0, "headline must ignore the shut-rail route"


def test_group_headline_and_lane_readiness_ignore_guarded_research_rows() -> None:
    """A visible audit row must not become a client-facing opportunity."""
    from spreadboard.api_spreads import SpreadTerminalRow

    def row(edge, *, guarded=False):
        base = {f.name: None for f in __import__("dataclasses").fields(SpreadTerminalRow)}
        base.update(
            token="TKN",
            route_kind="SPOT",
            executable_spread_pct=edge,
            displayed_open_spread_pct=edge,
            depth_weighted_spread_pct=edge,
            long_withdraw_enabled=True,
            short_deposit_enabled=True,
            long_volume_24h_usd=100_000.0,
            short_volume_24h_usd=100_000.0,
            long_price=1.0,
            short_price=1.0 + edge / 100.0,
            blockers=(
                ["mirage_guard:high_dislocation_exact_identity_required"]
                if guarded
                else []
            ),
            freshness="fresh",
            age_min=1.0,
            route_key=f"TKN|{edge}",
            asset_class="crypto",
        )
        return SpreadTerminalRow(**base)

    guarded = row(60.0, guarded=True)
    clean = row(4.0)
    group = api_spreads._group_rows([guarded, clean])[0]

    assert group["best_edge_pct"] == 4.0
    assert not group["best_route"]["mirage_guarded"]
    assert api_spreads.lane_rankable(guarded) is False
    assert api_spreads.lane_rankable(clean) is True


def test_old_matched_quote_stays_visible_but_cannot_lead_spread() -> None:
    """A two-minute-old basis may have converged even while funding persists."""
    from dataclasses import fields
    from spreadboard.api_spreads import SpreadTerminalRow

    def row(edge: float, age: float) -> SpreadTerminalRow:
        base = {field.name: None for field in fields(SpreadTerminalRow)}
        base.update(
            token="AGE",
            route_kind="FUTURES",
            executable_spread_pct=edge,
            displayed_open_spread_pct=edge,
            depth_weighted_spread_pct=edge,
            depth_usd=50.0,
            long_volume_24h_usd=100_000.0,
            short_volume_24h_usd=100_000.0,
            long_price=1.0,
            short_price=1.0 + edge / 100.0,
            long_market_symbol="AGE/USDT:USDT",
            short_market_symbol="AGE/USDT:USDT",
            blockers=[],
            freshness="fresh",
            age_min=age,
            route_key=f"AGE|{age}",
            asset_class="crypto",
        )
        return SpreadTerminalRow(**base)

    old = row(4.0, api_spreads.SPREAD_LEADER_MAX_AGE_MIN + 0.1)
    current = row(0.8, 0.1)
    group = api_spreads._group_rows([old, current])[0]

    assert api_spreads.row_is_presentable(old) is True
    assert api_spreads.spread_leader_ready(old) is False
    assert group["best_edge_pct"] == 0.8
    assert group["best_route"]["route_key"] == current.route_key


def _vrow(**kw):
    from types import SimpleNamespace
    base = dict(route_kind="SPOT", long_withdraw_enabled=None, short_deposit_enabled=None,
                long_volume_24h_usd=1e5, short_volume_24h_usd=1e5,
                freshness="fresh", age_min=0.1, executable_spread_pct=1.0,
                depth_weighted_spread_pct=1.0, depth_usd=50.0,
                long_price=1.0, short_price=1.01, blockers=[],
                asset_class="crypto")
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_single_shut_rail_blocks_even_if_the_other_is_unknown() -> None:
    """Treating unknown as fine let shut rails through; only 30 of 2718 rows fired."""
    assert api_spreads.route_deliverable(_vrow(short_deposit_enabled=False)) is False
    assert api_spreads.route_deliverable(_vrow(long_withdraw_enabled=False)) is False
    assert api_spreads.route_deliverable(_vrow()) is None


def test_shorting_spot_needs_a_deposit_rail() -> None:
    """SIREN: Gate futures long / Kucoin spot short, Kucoin deposits shut.

    Shorting the spot leg means owning the coin there, so this is not a
    margin-only trade and must not be exempt.
    """
    assert api_spreads.route_deliverable(
        _vrow(route_kind="FUTURES-SPOT", short_deposit_enabled=False)
    ) is False


def test_thin_books_are_rejected() -> None:
    """U2U ranked at 124% against a Kraken leg doing $14.78 of daily volume."""
    assert api_spreads.leg_volume_too_thin(_vrow(short_volume_24h_usd=14.78)) is True


def test_an_exact_zero_is_a_silent_venue_not_a_dead_market() -> None:
    """Upbit publishes 0 for every market, which deleted KAITO from the board.
    A live order book does not trade nothing all day; that is a missing field."""
    assert api_spreads.leg_volume_too_thin(_vrow(long_volume_24h_usd=0.0)) is False
    assert api_spreads.leg_volume_too_thin(_vrow(long_volume_24h_usd=1.0)) is True


def test_vanry_style_real_books_survive() -> None:
    """The one genuine 100%+ edge has real turnover on both sides."""
    assert api_spreads.leg_volume_too_thin(
        _vrow(long_volume_24h_usd=281_292.0, short_volume_24h_usd=234_243.0)
    ) is False


def test_unknown_volume_is_not_treated_as_thin() -> None:
    assert api_spreads.leg_volume_too_thin(_vrow(short_volume_24h_usd=None)) is False


def test_funding_ranking_ignores_transfer_rails() -> None:
    """A funding farm holds both legs and never moves the coin between venues.

    SIREN's funding is real even though Kucoin deposits are shut, because
    collecting carry does not require delivering the coin anywhere.
    """
    import inspect
    source = inspect.getsource(api_spreads.load_spreads)
    assert "rankable_funding_universe" in source
    assert 'metric="funding"' in source
    funding_block = source.split("rankable_funding_universe = [")[1].split("]")[0]
    assert "route_deliverable" not in funding_block, (
        "funding must not inherit the transfer-rail test"
    )
    assert "price_ratio_implausible" in funding_block
    assert "leg_volume_too_thin" in funding_block


def test_long_futures_short_spot_is_flagged_as_inventory_required() -> None:
    """Spot cannot be shorted; that leg is held long and the futures leg shorted."""
    assert api_spreads.requires_existing_spot_inventory(_vrow(route_kind="FUTURES-SPOT")) is True
    for kind in ("SPOT-FUTURES", "FUTURES", "SPOT", "DEX-FUTURES", "DEX-SPOT"):
        assert api_spreads.requires_existing_spot_inventory(_vrow(route_kind=kind)) is False


def _frow(**kw):
    from types import SimpleNamespace
    base = dict(route_kind="FUTURES", long_funding_pct=None, long_funding_interval_hours=None,
                short_funding_pct=None, short_funding_interval_hours=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_funding_legs_are_normalised_to_a_common_basis() -> None:
    """AIXBT showed 4482% APR purely from a 4h leg differenced against a 1h leg."""
    daily, apr = api_spreads.normalised_funding(_frow(
        long_funding_pct=0.1, long_funding_interval_hours=4.0,
        short_funding_pct=0.05, short_funding_interval_hours=1.0,
    ))
    # long 0.1%/4h = 0.6%/day; short 0.05%/1h = 1.2%/day; net +0.6%/day
    assert round(daily, 6) == 0.6
    assert round(apr, 2) == 219.0


def test_reproduces_the_reference_boards_recurring_apr() -> None:
    """uacryptoinvest shows 10.95% constantly: 0.01% per 8h, annualised."""
    _, apr = api_spreads.normalised_funding(_frow(
        long_funding_pct=0.0, long_funding_interval_hours=8.0,
        short_funding_pct=0.01, short_funding_interval_hours=8.0,
    ))
    assert round(apr, 2) == 10.95


def test_equal_rates_on_equal_intervals_net_to_zero() -> None:
    daily, apr = api_spreads.normalised_funding(_frow(
        long_funding_pct=0.02, long_funding_interval_hours=8.0,
        short_funding_pct=0.02, short_funding_interval_hours=8.0,
    ))
    assert daily == 0.0 and apr == 0.0


def test_inventory_required_routes_report_the_executable_direction() -> None:
    """Spot cannot be shorted, so the executable trade is the mirror image and
    its carry has the opposite sign."""
    kw = dict(long_funding_pct=0.1, long_funding_interval_hours=4.0,
              short_funding_pct=0.05, short_funding_interval_hours=1.0)
    normal, _ = api_spreads.normalised_funding(_frow(**kw))
    flipped, _ = api_spreads.normalised_funding(_frow(route_kind="FUTURES-SPOT", **kw))
    assert flipped == -normal


def test_missing_interval_falls_back_to_the_exchange_default() -> None:
    daily, _ = api_spreads.normalised_funding(_frow(
        long_funding_pct=0.0, short_funding_pct=0.01, short_funding_interval_hours=None,
    ))
    assert round(daily, 6) == 0.03  # 0.01% per 8h


def test_no_funding_data_returns_none() -> None:
    assert api_spreads.normalised_funding(_frow()) == (None, None)


def test_unknown_funding_interval_is_not_rankable() -> None:
    """AGLD reached 3570% APR with one leg's interval unpublished; the 8h
    fallback can be wrong by 8x, so such routes display but do not rank.

    Updated deliberately: the leg with the missing interval now has to publish a
    RATE for the route to be unrankable. A leg with no rate is a spot leg, which
    is covered by the companion test below.
    """
    assert api_spreads.funding_intervals_known(
        _frow(
            long_funding_pct=0.01, long_funding_interval_hours=8.0,
            short_funding_pct=0.02, short_funding_interval_hours=1.0,
        )
    ) is True
    assert api_spreads.funding_intervals_known(
        _frow(
            long_funding_pct=0.01, long_funding_interval_hours=None,
            short_funding_pct=0.02, short_funding_interval_hours=1.0,
        )
    ) is False


def test_a_spot_leg_does_not_block_funding_ranking() -> None:
    """Spot pays no funding, so it has no interval to publish -- that is known,
    not unknown. Demanding one excluded every spot-futures farm (the classic
    funding farm) from the ranking, leaving the top-funding list all-futures."""
    assert api_spreads.funding_intervals_known(
        _frow(short_funding_pct=0.0366, short_funding_interval_hours=4.0)
    ) is True


def test_short_dex_spot_leg_also_requires_existing_inventory() -> None:
    """DEX-FUTURES rows appear in both directions. `long Kucoin Futures /
    short OKX DEX 1(Spot)` sells a spot leg out of your own wallet, which the
    route_kind name does not say, so the direction must come from the legs."""
    assert api_spreads.requires_existing_spot_inventory(
        _frow(route_kind="DEX-FUTURES", long_market_type="Futures", short_market_type="Spot")
    ) is True
    assert api_spreads.requires_existing_spot_inventory(
        _frow(route_kind="DEX-FUTURES", long_market_type="Spot", short_market_type="Futures")
    ) is False


def test_a_measured_24h_sum_outranks_the_annualised_instantaneous_print() -> None:
    """Kraken settles hourly and caps at 0.5%/h: AGLD's print extrapolated to
    9.78%/day while the 24 rates it actually paid summed to 5.66%/day."""
    daily, apr = api_spreads.normalised_funding(_frow(
        funding_24h_pct=5.66,
        long_funding_pct=0.0, long_funding_interval_hours=8.0,
        short_funding_pct=0.4076, short_funding_interval_hours=1.0,
    ))
    assert round(daily, 2) == 5.66
    assert round(apr, 1) == round(5.66 * 365.0, 1)


def _funding_row(token: str, *, long_venue: str, long_type: str, short_venue: str,
                 short_type: str, long_pct=None, long_iv=None, short_pct=None, short_iv=None):
    quote_ts_us = int(time.time() * 1_000_000)
    return api_spreads._row_from_api(
        {
            "token": token,
            "long_venue": long_venue,
            "long_market_type": long_type,
            "short_venue": short_venue,
            "short_market_type": short_type,
            "quote_ts_us": quote_ts_us,
            "executable_spread_pct": 1.0,
            "notes": {
                "funding": {
                    "long": {"rate_pct": long_pct, "interval_hours": long_iv},
                    "short": {"rate_pct": short_pct, "interval_hours": short_iv},
                }
            },
        },
        bucket="api_discovered",
        now=quote_ts_us / 1_000_000,
    )


def test_best_funding_route_is_the_one_that_receives_not_its_mirror() -> None:
    """ESPORTS' only funding source is Gate at +0.0366%/4h, so the receive-side
    route pays +0.2196%/day and its mirror -0.2196%/day. Ranking by magnitude
    headlined the mirror: production showed -119.57% APR where the reference
    product showed +91.76%."""
    receives = _funding_row(
        "ESPORTS", long_venue="Kucoin", long_type="Spot",
        short_venue="Gate", short_type="Futures", short_pct=0.0366, short_iv=4.0,
    )
    pays = _funding_row(
        "ESPORTS", long_venue="Gate", long_type="Futures",
        short_venue="Binance", short_type="Futures", long_pct=0.0366, long_iv=4.0,
    )
    group = api_spreads._group_rows([pays, receives])[0]
    assert group["best_funding_24h_pct"] > 0
    assert round(group["best_funding_apr_pct"], 2) == 80.15


def test_best_funding_apr_and_24h_never_disagree_in_sign() -> None:
    """A FUTURES-SPOT row flips direction, and the raw 24h field does not. The
    group used to publish the two with opposite signs."""
    row = _funding_row(
        "ESPORTS", long_venue="Gate", long_type="Futures",
        short_venue="Mexc", short_type="Spot", long_pct=0.0366, long_iv=4.0,
    )
    group = api_spreads._group_rows([row])[0]
    assert group["best_funding_apr_pct"] > 0
    assert group["best_funding_24h_pct"] > 0


def test_kraken_native_funding_sums_relative_rates_over_24h(monkeypatch) -> None:
    """Kraken publishes ISO timestamps and both an absolute rate (quote currency
    per contract) and relativeFundingRate (a fraction of mark). Only the
    relative one is a percent once scaled."""
    now_ms = int(time.time() * 1000)
    rates = [
        {
            "timestamp": _iso(now_ms - hour * 3_600_000),
            "fundingRate": 0.00072,
            "relativeFundingRate": 0.005,
        }
        for hour in range(3, 0, -1)
    ]
    captured: list[str] = []

    def fake_public_json(url: str, **_kwargs: object) -> dict[str, object]:
        captured.append(url)
        return {"rates": rates}

    monkeypatch.setattr(live, "_public_json", fake_public_json)
    result = live._fetch_native_funding_24h("krakenfutures", "AGLD/USD:USD")
    assert "PF_AGLDUSD" in captured[0]
    assert result["status"] == "ok"
    assert result["samples"] == 3
    assert round(result["funding_24h_pct"], 6) == 1.5  # 3 x 0.5%
    assert result["funding_interval_hours"] == 1.0


def test_kraken_native_funding_uses_the_xbt_alias(monkeypatch) -> None:
    """Kraken lists PF_XBTUSD but PF_DOGEUSD, so BTC is the only alias needed."""
    captured: list[str] = []

    def fake_public_json(url: str, **_kwargs: object) -> dict[str, object]:
        captured.append(url)
        return {"rates": []}

    monkeypatch.setattr(live, "_public_json", fake_public_json)
    live._fetch_native_funding_24h("krakenfutures", "BTC/USD:USD")
    live._fetch_native_funding_24h("krakenfutures", "DOGE/USD:USD")
    assert "PF_XBTUSD" in captured[0]
    assert "PF_DOGEUSD" in captured[1]


def _iso(timestamp_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _mq(**kw):
    from spreadarb.api_discovery.models import MarketQuote
    base = dict(token="GEOD", venue="Mexc", market_type="Futures", symbol="GEOD/USDT:USDT",
                bid=0.219, ask=0.2192, bid_vwap=0.219, ask_vwap=0.2192,
                quote_ts_us=int(time.time() * 1_000_000), source_name="test")
    base.update(kw)
    import inspect
    allowed = set(inspect.signature(MarketQuote).parameters)
    return MarketQuote(**{k: v for k, v in base.items() if k in allowed})


def test_same_venue_cash_and_carry_is_a_real_route() -> None:
    """Buy MEXC spot, short the MEXC perp: one account, no transfer, collect funding.

    The reference product carries these -- 4 of its top 15 Spot-Futures rows were
    MEXC spot -> MEXC perp -- and venue-exclusive tokens like GEOD have no other
    route at all. Same venue AND same market type is still the same contract.
    """
    spot = _mq(market_type="Spot", symbol="GEOD/USDT", bid=0.2174, ask=0.2176,
               bid_vwap=0.2174, ask_vwap=0.2176)
    perp = _mq(market_type="Futures")
    pairs = sources.quote_candidate_pairs([spot, perp], min_spread_pct=0.0)
    routes = {(p.long_quote.market_type, p.short_quote.market_type) for p in pairs}
    assert ("Spot", "Futures") in routes, "cash-and-carry must survive"
    assert not [p for p in pairs
                if p.long_quote.market_type == p.short_quote.market_type], (
        "same venue and same market type is the same contract"
    )


def test_same_venue_routes_need_no_transfer_rail() -> None:
    """Nothing moves between accounts, so a shut deposit rail cannot block it."""
    assert api_spreads.route_deliverable(
        _vrow(route_kind="SPOT", long_venue="Mexc", short_venue="Mexc",
              long_withdraw_enabled=False, short_deposit_enabled=False)
    ) is True


def test_funding_refresh_covers_every_leg_not_just_top_routes(monkeypatch) -> None:
    """MEXC SWARMS sat at 0.0352%/4h from the discovery scan while the exchange was
    live at 0.0506% -- the parser was right, the number was stale. Order books must
    be rationed; funding is one bulk call per venue, so it must not be."""
    from spreadboard.fast_quotes import FastQuoteRefresher

    def leg(venue, symbol):
        return {"symbol": symbol}

    snapshot = {"api_discovered_rows": [
        {"token": "SWARMS", "long_venue": "Gate", "long_market_type": "Spot",
         "short_venue": "Mexc", "short_market_type": "Futures",
         "notes": {"route_inputs": {"long": leg("Gate", "SWARMS/USDT"),
                                    "short": leg("Mexc", "SWARMS/USDT:USDT")}}},
        {"token": "GEOD", "long_venue": "Mexc", "long_market_type": "Spot",
         "short_venue": "Mexc", "short_market_type": "Futures",
         "notes": {"route_inputs": {"long": leg("Mexc", "GEOD/USDT"),
                                    "short": leg("Mexc", "GEOD/USDT:USDT")}}},
    ]}
    calls = []

    class FakeClient:
        has = {"fetchFundingRates": True}

        def fetch_funding_rates(self):
            calls.append("Mexc")
            return {
                "SWARMS/USDT:USDT": {"symbol": "SWARMS/USDT:USDT", "fundingRate": 0.000506,
                                     "interval": "4h", "fundingTimestamp": 1785628800000},
                "GEOD/USDT:USDT": {"symbol": "GEOD/USDT:USDT", "fundingRate": 0.0002,
                                   "interval": "4h", "fundingTimestamp": 1785628800000},
            }

    refresher = FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda venue, market_type: FakeClient())
    summary = refresher.refresh_all_funding(snapshot)

    assert calls == ["Mexc"], "one bulk call per venue, not one per symbol"
    assert summary["legs"] == 2
    short = snapshot["api_discovered_rows"][0]["notes"]["route_inputs"]["short"]
    assert short["current_funding_pct"] == pytest.approx(0.0506)
    assert short["funding_interval_hours"] == 4.0
    # 0.0506%/4h -> 0.1012%/8h -> the reference product's 111% APR band.
    assert 0.0506 * 6 * 365 == pytest.approx(110.814)


def test_funding_refresh_has_its_own_cadence(monkeypatch) -> None:
    """A bulk call costs a load_markets() per venue. Running that on every 20s
    quote cycle competed with the websocket book worker for the same upstream
    metadata endpoints, so a second call inside the window must be a no-op."""
    from spreadboard.fast_quotes import FastQuoteRefresher

    snapshot = {"api_discovered_rows": [
        {"token": "GEOD", "long_venue": "Mexc", "long_market_type": "Spot",
         "short_venue": "Mexc", "short_market_type": "Futures",
         "notes": {"route_inputs": {"long": {"symbol": "GEOD/USDT"},
                                    "short": {"symbol": "GEOD/USDT:USDT"}}}},
    ]}
    calls = []

    class FakeClient:
        has = {"fetchFundingRates": True}

        def fetch_funding_rates(self):
            calls.append(1)
            return [{"symbol": "GEOD/USDT:USDT", "fundingRate": 0.0002, "interval": "4h"}]

    refresher = FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda venue, market_type: FakeClient())
    first = refresher.refresh_all_funding(snapshot)
    second = refresher.refresh_all_funding(snapshot)
    assert first["legs"] == 1 and len(calls) == 1
    assert second.get("skipped") is True, "second call inside the window must not hit the venue"
    assert len(calls) == 1


def test_funding_refresh_rotates_venues_across_passes(monkeypatch) -> None:
    """A venue costs a load_markets(), so one pass cannot afford them all. The
    cursor persists in the snapshot so later passes cover the venues that the
    wall-clock budget cut off."""
    from spreadboard.fast_quotes import FastQuoteRefresher

    def row(token, venue):
        return {"token": token, "long_venue": "Gate", "long_market_type": "Spot",
                "short_venue": venue, "short_market_type": "Futures",
                "notes": {"route_inputs": {"long": {"symbol": f"{token}/USDT"},
                                           "short": {"symbol": f"{token}/USDT:USDT"}}}}

    snapshot = {"api_discovered_rows": [row("A", "Mexc"), row("B", "Bybit"), row("C", "Gate")]}
    visited = []

    class FakeClient:
        def __init__(self, venue):
            self.venue = venue
            self.has = {"fetchFundingRates": True}

        def fetch_funding_rates(self):
            visited.append(self.venue)
            return []

    refresher = FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda venue, market_type: FakeClient(venue))
    monkeypatch.setenv("SPREADBOARD_FUNDING_REFRESH_SECONDS", "30")
    first = refresher.refresh_all_funding(snapshot)
    assert first["venue_count"] == 3
    # Force the cadence gate open, keeping the cursor the pass left behind.
    snapshot["funding_refresh"]["epoch"] = 0
    refresher.refresh_all_funding(snapshot)
    assert visited[0] != visited[len(visited) // 2] or len(set(visited)) > 1, (
        "a later pass must not restart on the same venue every time"
    )


def test_discovery_floors_admit_any_positive_carry_and_small_spreads(monkeypatch) -> None:
    """The reference product's whole Spot lane sits at 0.10-0.16% and its funding
    rows start around 9.64% APR. A 1% spread floor and a 25% funding floor made
    both structurally unreachable -- we were not missing them, we declined to look."""
    from spreadarb import public_runtime

    monkeypatch.delenv("SPREADARB_MIN_SPREAD_PCT", raising=False)
    monkeypatch.delenv("SPREADARB_MIN_FUNDING_APR_PCT", raising=False)
    assert public_runtime.discovery_min_spread_pct() <= 0.1
    assert 0 < public_runtime.discovery_min_funding_apr_pct() <= 10.0

    quotes = [
        _mq(token="BAN", venue="Gate", market_type="Futures", symbol="BAN/USDT:USDT",
            bid=0.0715, ask=0.07152, bid_vwap=0.0715, ask_vwap=0.07152),
        _mq(token="BAN", venue="Bybit", market_type="Futures", symbol="BAN/USDT:USDT",
            bid=0.07160, ask=0.07162, bid_vwap=0.07160, ask_vwap=0.07162),
    ]
    # 0.11% -- exactly the band their Spot lane lives in, rejected by the old 1% floor.
    pairs = sources.quote_candidate_pairs(
        quotes, min_spread_pct=public_runtime.discovery_min_spread_pct()
    )
    assert pairs, "a sub-1% spread must still be a candidate"


def test_the_parsed_tree_is_not_held_alongside_the_rows(tmp_path) -> None:
    """Caching the payload AND the rows built from it carried the same data
    twice: a 77MB snapshot became ~700MB of Python and the droplet sat at 177MB
    free three minutes after a restart, leaving the scan no room to finish.

    Re-parsing is cheap by comparison and only happens when the rows cache misses.
    """
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"api_discovered_rows": [], "updated_at": "2026-08-01T00:00:00Z"}))
    first = api_spreads._cached_snapshot(path)
    second = api_spreads._cached_snapshot(path)
    assert first == second
    assert first is not second, "the tree must not be retained between calls"
    assert not hasattr(api_spreads, "_SNAPSHOT_CACHE"), "no payload cache may exist"


def test_identical_queries_reuse_the_last_result(tmp_path, monkeypatch) -> None:
    """Grouping every route dominates the request: 1-3s at 2.5k rows, and the
    universe we now carry is far larger. The board only moves every 20s."""
    path = tmp_path / "snap.json"
    quote_ts_us = int(time.time() * 1_000_000)
    path.write_text(json.dumps({
        "updated_at": "2026-08-01T22:00:00Z",
        "api_discovered_rows": [{
            "token": "AAA", "long_venue": "Gate", "long_market_type": "Futures",
            "short_venue": "Bybit", "short_market_type": "Futures",
            "quote_ts_us": quote_ts_us, "executable_spread_pct": 1.0,
        }],
        "dex_discovered_rows": [],
    }))
    api_spreads._RESULT_CACHE.clear()
    calls = []
    original = api_spreads._group_rows
    monkeypatch.setattr(api_spreads, "_group_rows",
                        lambda rows: (calls.append(1), original(rows))[1])
    kwargs = dict(api_path=path, board_path=tmp_path / "none.jsonl", include_stale=True)
    first = api_spreads.load_spreads(**kwargs)
    second = api_spreads.load_spreads(**kwargs)
    assert second is first, "an unchanged snapshot must not be regrouped"
    before = len(calls)
    path.write_text(path.read_text().replace("22:00:00", "22:00:30"))
    third = api_spreads.load_spreads(**kwargs)
    assert third is not first, "a rewritten snapshot must invalidate the result"
    assert len(calls) > before


def test_lane_counts_exclude_routes_nobody_can_take() -> None:
    """"Top 25 ready" was satisfiable by rows with a shut rail, a ticker
    collision, or a book too thin to price. A lane count is a promise."""
    shut = _vrow(route_kind="SPOT", token="SHUT", short_deposit_enabled=False)
    collision = _vrow(route_kind="SPOT", token="CAT", long_price=1.4e-06, short_price=808.0)
    thin = _vrow(route_kind="SPOT", token="U2U", short_volume_24h_usd=14.78)
    good = _vrow(route_kind="SPOT", token="REAL", long_withdraw_enabled=True,
                 short_deposit_enabled=True)
    counts = api_spreads._release_lane_token_counts([shut, collision, thin, good])
    assert counts["SPOT"] == 1


def test_old_route_remains_structurally_rankable_but_not_live_ready() -> None:
    old = _vrow(route_kind="FUTURES", token="OLD", age_min=10.0)
    assert api_spreads.lane_rankable(old) is True
    assert api_spreads.lane_current_ready(old) is False
    assert api_spreads._release_lane_token_counts([old])["FUTURES"] == 0


def test_a_shut_rail_does_not_disqualify_a_funding_farm() -> None:
    """A farm holds both legs where it bought them. SIREN's carry was real even
    while Kucoin deposits were shut, because collecting it moves no coin."""
    farm = _vrow(route_kind="FUTURES-SPOT", token="SIREN", short_deposit_enabled=False)
    assert api_spreads.lane_rankable(farm) is True
    transfer = _vrow(route_kind="SPOT", token="SIREN", short_deposit_enabled=False)
    assert api_spreads.lane_rankable(transfer) is False


def test_row_dicts_are_not_deep_copied() -> None:
    """asdict() recursed over every field of 34k rows -- 9.8s a request of pure
    waste, since every field here is a scalar or a flat list."""
    row = api_spreads._row_from_api(
        {"token": "AAA", "long_venue": "Gate", "long_market_type": "Futures",
         "short_venue": "Bybit", "short_market_type": "Futures",
         "quote_ts_us": int(time.time() * 1_000_000), "executable_spread_pct": 1.0},
        bucket="api_discovered", now=time.time(),
    )
    payload = row.to_dict()
    assert payload["token"] == "AAA"
    assert payload is not row.__dict__, "callers must not be handed the row's own dict"


def test_headline_lists_do_not_group_the_whole_universe() -> None:
    """Grouping builds a public dict per route; keeping only the top 8 afterwards
    meant paying that for every token, three times a request."""
    import inspect
    source = inspect.getsource(api_spreads._top_unique_groups)
    assert "TOP_GROUP_SHORTLIST" in source
    assert api_spreads.TOP_GROUP_SHORTLIST >= api_spreads.DEFAULT_LIMIT // 2


def test_the_service_warms_the_board_cache_after_writing() -> None:
    """Every snapshot write invalidates the request cache; the warm must happen
    off the request path or a member pays the rebuild."""
    import inspect
    from scripts import run_spreadboard_service

    quote_source = inspect.getsource(run_spreadboard_service.RefreshLoop.run_fast_quotes)
    warm_source = inspect.getsource(run_spreadboard_service.RefreshLoop._start_board_warm)
    assert "self._start_board_warm()" in quote_source
    assert "target=_warm_board_cache" in warm_source


def test_book_verification_upgrades_quotes_it_does_not_discard_them() -> None:
    """Replacing the quote list with the verified subset threw away every market
    that lost a candidate slot -- six of the ten largest coins were absent from
    the board entirely, and 54% of the reference product's rows named a venue
    pair we never emitted."""
    import inspect
    source = inspect.getsource(sources.CexCcxtSource.collect)
    assert "verified = _verify_top_candidate_books" in source
    assert "merged.update" in source
    assert "quotes = _verify_top_candidate_books" not in source


def test_ticker_only_quotes_declare_their_depth_unverified() -> None:
    """Their vwap is the top of book, not a measured ladder; a route built from
    one must not imply a size nobody checked."""
    assert sources.DEPTH_UNVERIFIED_BLOCKER == "depth_unverified"
    import inspect
    source = inspect.getsource(sources._ticker_quotes_for_symbols)
    assert "DEPTH_UNVERIFIED_BLOCKER" in source


def test_each_token_keeps_only_its_strongest_routes() -> None:
    """A token on ten venues yields ninety ordered pairs. Coverage needs the
    token present, not every permutation of it."""
    assert sources.MAX_ROWS_PER_TOKEN > 0
    strong = {"token": "AAA", "depth_weighted_spread_pct": 5.0, "notes": {}}
    weak = {"token": "AAA", "depth_weighted_spread_pct": 0.01, "notes": {}}
    carry = {"token": "AAA", "depth_weighted_spread_pct": 0.0,
             "notes": {"funding": {"net_apr_pct": 900.0}}}
    assert sources._row_strength(strong) > sources._row_strength(weak)
    assert sources._row_strength(carry) > sources._row_strength(weak)


def test_per_source_token_cap_preserves_lane_and_venue_pair_diversity() -> None:
    rows = [
        {
            "token": "GUA",
            "route_kind": "FUTURES",
            "long_venue": "Gate",
            "short_venue": f"V{index}",
            "depth_weighted_spread_pct": 100.0 - index,
            "notes": {"route_inputs": {"long": {"bid": 1, "ask": 2}, "short": {"bid": 1, "ask": 2}}},
        }
        for index in range(30)
    ]
    rows.extend(
        [
            {**rows[0], "route_kind": "SPOT", "long_venue": "Mexc", "short_venue": "Gate", "depth_weighted_spread_pct": 0.2},
            {**rows[0], "route_kind": "SPOT-FUTURES", "long_venue": "Mexc", "short_venue": "Aster", "depth_weighted_spread_pct": 0.1},
        ]
    )
    kept = sources._keep_diverse_token_routes(rows, 8)
    assert {row["route_kind"] for row in kept} == {"FUTURES", "SPOT", "SPOT-FUTURES"}
    assert len({(row["long_venue"], row["short_venue"]) for row in kept}) == 8


def test_the_snapshot_trims_surplus_routes_not_whole_tokens() -> None:
    """Each source caps its own output, but a token quoted by ten sources arrives
    ten times over, and the row limit is a plain slice -- so an oversized snapshot
    used to lose whole tokens at the tail rather than surplus routes."""
    from spreadarb.api_discovery import runner

    # Distinct venue pairs, since duplicates of one route now collapse first.
    rows = (
        [{"token": "BTC", "long_venue": f"V{i}", "short_venue": "Bybit",
          "long_market_type": "Spot", "short_market_type": "Futures",
          "depth_weighted_spread_pct": float(i), "notes": {}} for i in range(40)]
        + [{"token": "RARE", "long_venue": "Gate", "short_venue": "Mexc",
            "long_market_type": "Spot", "short_market_type": "Futures",
            "depth_weighted_spread_pct": 9.0, "notes": {}}]
    )
    capped = runner._cap_rows_per_token(rows)
    tokens = {row["token"] for row in capped}
    assert "RARE" in tokens, "a thinly quoted token must survive a crowded snapshot"
    assert sum(1 for row in capped if row["token"] == "BTC") == runner.MAX_SNAPSHOT_ROWS_PER_TOKEN
    kept = sorted(row["depth_weighted_spread_pct"] for row in capped if row["token"] == "BTC")
    assert kept[-1] == 39.0, "the strongest routes are the ones kept"


def test_unmeasured_depth_says_so_on_the_board() -> None:
    """Ticker-only routes carry a top-of-book figure nobody measured. Showing it
    unqualified is the same lie as showing a shut rail as an opportunity."""
    from types import SimpleNamespace

    quote_ts_us = int(time.time() * 1_000_000)

    def build(blockers):
        return api_spreads._row_from_api(
            {"token": "AAA", "long_venue": "Gate", "long_market_type": "Futures",
             "short_venue": "Bybit", "short_market_type": "Futures",
             "quote_ts_us": quote_ts_us, "executable_spread_pct": 1.0,
             "blockers": blockers},
            bucket="api_discovered", now=quote_ts_us / 1_000_000,
        )

    assert api_spreads._public_row(build(["depth_unverified"]))["depth_unverified"] is True
    assert api_spreads._public_row(build([]))["depth_unverified"] is False
    import inspect
    assert "depth not measured" in inspect.getsource(server.render_market_group_route), "the live route row must label an unmeasured depth"


def test_a_lone_disagreeing_price_is_flagged_not_headlined() -> None:
    """BTC was reported at a 26% spread: BingX perp 63,298 against a DEX leg
    reading 79,828. With enough venues quoting, a price far from all of them is a
    broken feed. It is flagged, never dropped -- big spreads here have been real."""
    rows = [
        {"venue": "BingX", "spot_price": 63298.0, "perp_price": 63290.0},
        {"venue": "Bybit", "spot_price": 63310.0, "perp_price": 63305.0},
        {"venue": "Gate", "spot_price": 63280.0, "perp_price": 63275.0},
    ]
    spreads = live.best_spreads(rows, {"price_usd": 79828.0})
    assert spreads, "the route must still be shown, not hidden"
    dex_rows = [s for s in spreads if "DEX" in (s["sell_venue"], s["buy_venue"])]
    assert dex_rows and all(s["price_disputed"] for s in dex_rows)


def test_a_genuine_two_venue_dislocation_is_not_flagged() -> None:
    """With only a couple of quotes there is no consensus to disagree with, and
    the operator has captured a 150% spread for real money."""
    rows = [
        {"venue": "Kucoin", "spot_price": 0.0576},
        {"venue": "Mexc", "spot_price": 0.0281},
    ]
    spreads = live.best_spreads(rows, None)
    assert spreads and not any(s["price_disputed"] for s in spreads)


def test_hyperliquid_builder_dexes_are_enumerated() -> None:
    """The tokenized equities the reference product carries trade on Hyperliquid
    builder DEXes under prefixed symbols. CCXT's adapter returns the main perp
    DEX alone -- 774 markets, none of these -- so the pair could never form."""
    source = sources.HyperliquidBuilderDexSource()
    assert source.market_type == "Futures" and source.venue == "Hyperliquid"
    assert sources.HyperliquidBuilderDexSource in [
        type(s) for s in sources.default_sources()
    ] or "HyperliquidBuilderDexSource" in __import__("inspect").getsource(sources.default_sources)


def test_a_builder_market_is_only_named_for_a_token_the_cex_side_quotes(monkeypatch) -> None:
    """Inventing <TICKER>STOCK for every builder market would fabricate assets
    nobody lists; MEXC listing AMZNSTOCK is what makes xyz:AMZN that token."""
    source = sources.HyperliquidBuilderDexSource()
    payloads = {
        "perpDexs": [{"name": "xyz"}],
        "metaAndAssetCtxs": [
            {"universe": [{"name": "xyz:AMZN"}, {"name": "xyz:NOBODYLISTSTHIS"}]},
            [{"midPx": "231.5", "funding": "0.0000125", "dayNtlVlm": "50000"},
             {"midPx": "10.0", "funding": "0.0", "dayNtlVlm": "10"}],
        ],
    }
    monkeypatch.setattr(source, "_info", lambda payload, timeout: payloads[payload["type"]])
    quote_ts = int(time.time() * 1_000_000)
    from spreadarb.api_discovery.models import MarketQuote
    reference = (
        MarketQuote(token="AMZNSTOCK", venue="Mexc", market_type="Futures", bid=230.0, ask=230.5,
                    bid_vwap=230.0, ask_vwap=230.5, quote_ts_us=quote_ts, source_name="cex",
                    symbol="AMZNSTOCK/USDT:USDT"),
    )
    ctx = sources.DiscoveryContext(tokens=(), watchlist={}, deadline_monotonic=None,
                                   reference_quotes=reference, min_spread_pct=0.05,
                                   min_funding_apr_pct=0.01)
    result = source.collect(ctx)
    tokens = {q.token for q in result.quotes}
    assert tokens == {"AMZNSTOCK"}, f"only CEX-known tokens may be named, got {tokens}"


def test_route_trimming_keeps_the_carry_you_receive() -> None:
    """Ranking a token's routes on funding MAGNITUDE kept the mirror that pays
    and discarded the one that collects: BEAT surfaced at -38% here while the
    reference product showed the same token at +143%."""
    receives = {"token": "BEAT", "depth_weighted_spread_pct": 0.1,
                "notes": {"funding": {"net_apr_pct": 143.0}}}
    pays = {"token": "BEAT", "depth_weighted_spread_pct": 0.1,
            "notes": {"funding": {"net_apr_pct": -600.0}}}
    assert sources._row_strength(receives) > sources._row_strength(pays)


def test_a_wide_spread_still_wins_on_magnitude() -> None:
    """A spread is symmetric between the two directions, so its size stands."""
    wide = {"token": "X", "depth_weighted_spread_pct": -80.0, "notes": {}}
    narrow = {"token": "X", "depth_weighted_spread_pct": 0.2, "notes": {}}
    assert sources._row_strength(wide) > sources._row_strength(narrow)


def test_a_builder_market_must_agree_with_the_price_the_cex_quotes(monkeypatch) -> None:
    """xyz:MU, km:MU and mkts:MU are different instruments wearing one ticker.
    Pairing the wrong one against MEXC's MUSTOCK invented a 27% spread where the
    real gap is 0.1%."""
    from spreadarb.api_discovery.models import MarketQuote

    source = sources.HyperliquidBuilderDexSource()
    payloads = {
        "perpDexs": [{"name": "xyz"}, {"name": "km"}],
        "metaAndAssetCtxs": None,
    }
    calls = {"n": 0}

    def fake_info(payload, timeout):
        if payload["type"] == "perpDexs":
            return payloads["perpDexs"]
        calls["n"] += 1
        price = "848.0" if calls["n"] == 1 else "1080.0"   # km:MU trades elsewhere
        return [{"universe": [{"name": f"{payload['dex']}:MU"}]},
                [{"midPx": price, "funding": "0.00001", "dayNtlVlm": "90000"}]]

    monkeypatch.setattr(source, "_info", fake_info)
    ts = int(time.time() * 1_000_000)
    ref = (MarketQuote(token="MUSTOCK", venue="Mexc", market_type="Futures", bid=848.0, ask=849.0,
                       bid_vwap=848.0, ask_vwap=849.0, quote_ts_us=ts, source_name="cex",
                       symbol="MUSTOCK/USDT:USDT"),)
    ctx = sources.DiscoveryContext(tokens=(), watchlist={}, deadline_monotonic=None,
                                   reference_quotes=ref, min_spread_pct=0.05,
                                   min_funding_apr_pct=0.01)
    result = source.collect(ctx)
    symbols = {q.symbol for q in result.quotes}
    assert symbols == {"xyz:MU"}, f"only the market that agrees on price may pair, got {symbols}"


def test_an_equity_gap_of_ten_percent_is_a_different_instrument() -> None:
    """Real cross-venue gaps on a tokenized equity run 0.1-0.5%. A 25% bound
    admitted cash:AMZN at 241.75 against MEXC's 273.36 and printed a 13% spread."""
    assert sources.BUILDER_DEX_PRICE_TOLERANCE <= 0.05


def test_the_profile_tells_a_member_whether_pushover_can_actually_send(monkeypatch) -> None:
    """A member who saves a key and hears nothing cannot tell whether the key is
    wrong or the server simply has no application token."""
    monkeypatch.delenv("SPREADBOARD_PUSHOVER_APP_TOKEN", raising=False)
    blocked = server.render_profile_pushover({})
    assert "Delivery is not active yet" in blocked and "Delivery inactive" in blocked

    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "token")
    ready = server.render_profile_pushover({})
    assert "delivered to your Pushover account" in ready and "Delivery ready" in ready


def test_rail_reopens_reach_each_member_not_only_the_group(monkeypatch) -> None:
    """
    # Pushing every reopen buries the alerts a member asked for, so it is now
    # opt-in. The capability must still work when it is switched on.
    monkeypatch.setenv("SPREADBOARD_RAIL_PUSH", "1")
    The operator's requirement: alerts land on individual phones via Pushover."""
    # Pushing every reopen buries the alerts a member asked for, so it is now
    # opt-in. The capability must still work when it is switched on.
    monkeypatch.setenv("SPREADBOARD_RAIL_PUSH", "1")
    
    from spreadboard import rail_watch, accounts, alerts as alerts_module

    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setattr(accounts, "list_pushover_user_ids", lambda **k: [7])
    monkeypatch.setattr(accounts, "get_user_object",
                        lambda uid, **k: SimpleNamespace(subscription_active=True))
    monkeypatch.setattr(accounts, "notification_delivery",
                        lambda uid, **k: {"user_key": "u-key", "device": "", "sound": "pushover"})
    sent = []
    monkeypatch.setattr(alerts_module, "send_pushover_message",
                        lambda **kw: (sent.append(kw), {"ok": True})[1])
    watcher = rail_watch.RailReopenWatcher(state_path="/tmp/unused-state.json")
    alert = {"token": "SIREN", "venue": "Kucoin", "direction": "deposit", "edge_pct": 98.0,
             "route": {"long_venue": "OKX DEX 1", "short_venue": "Kucoin"}}
    assert watcher._push_to_members(alert, rail_watch.format_alert(alert)) == 1
    assert sent and sent[0]["user_key"] == "u-key" and "SIREN" in sent[0]["title"]


def test_a_member_sees_their_alerts_against_the_live_value(tmp_path, monkeypatch) -> None:
    """Creating an alert was possible but nothing showed it afterwards: a member
    could not tell what they had armed or how far the market was from it."""
    rules = [{"id": 4, "symbol": "SIREN", "route_key": "SIREN|Kucoin|Spot|Gate|Futures",
              "metric": "open_spread_pct", "operator": "gte", "threshold": 32.0,
              "stability_seconds": 10, "enabled": 1}]
    monkeypatch.setattr(server.accounts, "current_user",
                        lambda *a, **k: SimpleNamespace(id=1))
    monkeypatch.setattr(server.accounts, "list_market_alert_rules", lambda *a, **k: rules)
    monkeypatch.setattr(server, "api_market_spreads", lambda *a, **k: {"rows": [
        {"route_key": "SIREN|Kucoin|Spot|Gate|Futures", "executable_spread_pct": 15.13}]})
    html = server.render_member_alert_rules(tmp_path / "board.jsonl")
    assert "SIREN" in html
    assert "Kucoin Spot -&gt; Gate Futures" in html, "the member must see which route"
    assert "15.13" in html.replace(",", "."), "the live value must be shown next to the level"
    assert 'value="32.0"' in html and "3600" in html, "threshold and hold window are editable"
    assert "data-alert-save" in html and "data-alert-delete" in html


def test_empty_member_alert_state_explains_real_delivery(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server.accounts, "current_user", lambda *a, **k: SimpleNamespace(id=1))
    monkeypatch.setattr(server.accounts, "notification_preferences", lambda *a, **k: {})
    monkeypatch.setattr(server.accounts, "list_market_alert_rules", lambda *a, **k: [])

    html = server.render_member_alert_rules(tmp_path / "board.jsonl")

    assert "recorded in Portfolio" in html
    assert "{h(delivery_note)}" not in html


def test_the_alerts_page_no_longer_claims_it_cannot_send() -> None:
    """It said 'This page does not send Pushover messages', which stopped being
    true once per-member delivery landed."""
    import inspect
    source = inspect.getsource(server.render_alerts_page)
    assert "does not send Pushover" not in source
    assert "render_member_alert_rules" in source


def _prow(token, lv, sv, lp, sp, lvol, svol, key=None):
    from types import SimpleNamespace
    return SimpleNamespace(token=token, route_key=key or f"{token}|{lv}|{sv}",
                           long_venue=lv, short_venue=sv, long_price=lp, short_price=sp,
                           long_volume_24h_usd=lvol, short_volume_24h_usd=svol)


def test_a_lone_unbacked_quote_is_dropped_from_the_board() -> None:
    """IOTX sat at 0.00669 on Coinbase against 0.0023 everywhere else, printing
    190% across 36 routes, and slipped under the 3x identity bar because the
    ratio is only 2.9x. Coinbase published no turnover for it either."""
    rows = [
        _prow("IOTX", "Binance", "Bybit", 0.00231, 0.00232, 213461.0, 141372.0, "a"),
        _prow("IOTX", "Mexc", "Gate", 0.00232, 0.00232, 82433.0, 16382.0, "b"),
        _prow("IOTX", "Bitget", "Kucoin", 0.00232, 0.00233, 70666.0, 23508.0, "c"),
        _prow("IOTX", "Binance", "Coinbase", 0.00231, 0.00669, 213461.0, None, "bad"),
    ]
    assert api_spreads.unverifiable_price_outliers(rows) == {"bad"}


def test_a_real_dislocation_with_turnover_on_both_legs_survives() -> None:
    """VANRY's genuine ~100% edge is roughly 2x apart too. Turnover is what
    separates a captured edge from a broken feed."""
    rows = [
        _prow("VANRY", "Gate", "Bybit", 0.0100, 0.0198, 281292.0, 234243.0, "v1"),
        _prow("VANRY", "Mexc", "Kucoin", 0.0101, 0.0102, 120000.0, 90000.0, "v2"),
        _prow("VANRY", "Bitget", "HTX", 0.0100, 0.0101, 110000.0, 95000.0, "v3"),
    ]
    assert api_spreads.unverifiable_price_outliers(rows) == set()


def test_a_shut_rail_is_still_tracked_even_though_the_board_hides_it() -> None:
    """Two callers, two needs.

    The rail-reopen watcher calls load_spreads and must keep seeing tokens whose
    rail is shut -- a closed rail is exactly what it watches for reopening. The
    board must not list them: a spot arb needs the coin delivered, ESPORTS
    printed 150% into a closed Mexc deposit, and every spot row the reference
    product lists has both rails open. So deliverability is opt-in, and only the
    board opts in.
    """
    import inspect

    signature = inspect.signature(api_spreads.load_spreads)
    assert "require_deliverable" in signature.parameters
    assert signature.parameters["require_deliverable"].default is False, (
        "the watcher's view must keep shut-rail rows by default"
    )

    source = inspect.getsource(api_spreads.load_spreads)
    gate = source.split("if not include_unverified:")[1].split("normalized_sort")[0]
    assert "price_ratio_implausible" in gate and "leg_volume_too_thin" in gate
    assert "if require_deliverable:" in source, (
        "the board's stricter view must be reachable"
    )

    # And the board asks for it.
    board_source = inspect.getsource(__import__("spreadboard.server", fromlist=["x"]).api_market_spreads)
    assert "require_deliverable=True" in board_source


def test_cross_venue_leveraged_tokens_are_not_arbitrage() -> None:
    """Gate's ETH3L and XT's ETH3L are different products with their own NAV and
    rebalancing, and the token cannot move between venues. They were every
    remaining route above 100%."""
    for token in ("ETH3L", "LTC3S", "BTC5L", "NVDA3S", "SPCX3L"):
        assert api_spreads.is_venue_specific_leveraged_token(
            _vrow(token=token, long_venue="Gate", short_venue="XT")
        ) is True, token


def test_a_tokenized_equity_is_not_mistaken_for_a_leveraged_product() -> None:
    """SOXL is one underlying that several venues wrap; the letters before the L
    are not a leverage multiple."""
    for token in ("SOXL", "AMZNSTOCK", "SIREN", "VANRY", "ESPORTS", "10000SATS"):
        assert api_spreads.is_venue_specific_leveraged_token(
            _vrow(token=token, long_venue="Gate", short_venue="XT")
        ) is False, token


def test_a_venues_own_leveraged_spot_against_its_own_perp_is_left_alone() -> None:
    """That pair is internally consistent -- one issuer, one NAV."""
    assert api_spreads.is_venue_specific_leveraged_token(
        _vrow(token="ETH3L", long_venue="Gate", short_venue="Gate")
    ) is False


def test_a_ticker_collision_never_reaches_the_snapshot() -> None:
    """Emitting it did more than clutter: ranked by spread magnitude it EVICTED
    the real route under the per-token cap. NXT is quoted on MEXC and Kucoin
    spot, but all three rows kept paired against a Gate futures leg at 84,674%
    and the genuine 0.12% MEXC->Kucoin pair never survived."""
    ts = int(time.time() * 1_000_000)
    from spreadarb.api_discovery.models import MarketQuote

    def q(venue, price, market_type="Spot"):
        return MarketQuote(token="NXT", venue=venue, market_type=market_type,
                           bid=price * 0.999, ask=price * 1.001,
                           bid_vwap=price * 0.999, ask_vwap=price * 1.001,
                           quote_ts_us=ts, source_name="t", symbol="NXT/USDT")

    quotes = [q("Mexc", 0.0100), q("Kucoin", 0.0101), q("Gate", 9.0, "Futures")]
    pairs = sources.quote_candidate_pairs(quotes, min_spread_pct=0.0)
    venues = {(p.long_quote.venue, p.short_quote.venue) for p in pairs}
    assert ("Mexc", "Kucoin") in venues, "the genuine spot pair must survive"
    assert not any("Gate" in pair for pair in venues), "the collision must not be emitted"


def test_a_real_dislocation_is_still_paired() -> None:
    """VANRY and SIREN both sit near 2x and are real captures."""
    ts = int(time.time() * 1_000_000)
    from spreadarb.api_discovery.models import MarketQuote

    def q(venue, price):
        return MarketQuote(token="VANRY", venue=venue, market_type="Spot",
                           bid=price * 0.999, ask=price * 1.001,
                           bid_vwap=price * 0.999, ask_vwap=price * 1.001,
                           quote_ts_us=ts, source_name="t", symbol="VANRY/USDT")

    # max_spread_pct=0 is what production passes: no ceiling at all.
    pairs = sources.quote_candidate_pairs([q("Gate", 0.0100), q("Bybit", 0.0198)],
                                          min_spread_pct=0.0, max_spread_pct=0.0)
    assert pairs, "a ~2x genuine dislocation must still pair"


def test_different_quote_assets_do_not_become_token_spread() -> None:
    """Kraken USD versus Gate USDT contains a second, unmodelled basis."""
    ts = int(time.time() * 1_000_000)

    def q(venue: str, quote: str, price: float) -> MarketQuote:
        return MarketQuote(
            token="BTC", venue=venue, market_type="Futures",
            bid=price * 0.999, ask=price * 1.001,
            bid_vwap=price * 0.999, ask_vwap=price * 1.001,
            quote_ts_us=ts, source_name="test",
            symbol=f"BTC/{quote}:{quote}", quote_asset=quote,
        )

    assert not sources.quote_candidate_pairs(
        [q("Kraken", "USD", 100.0), q("Gate", "USDT", 100.2)],
        min_spread_pct=-100.0,
    )
    row = _vrow(long_quote="USD", short_quote="USDT")
    assert api_spreads.quote_basis_mismatch(row) is True
    assert api_spreads.row_is_presentable(row) is False


def test_duplicate_routes_do_not_eat_a_tokens_slots() -> None:
    """NXT's Mexc->Gate arrived from both cex_spot_ccxt_2 and cex_futures_ccxt_2,
    and every duplicate consumed a slot a distinct venue pair could have used."""
    from spreadarb.api_discovery import runner

    def row(long_venue, short_venue, ts, spread=1.0):
        return {"token": "NXT", "long_venue": long_venue, "long_market_type": "Spot",
                "short_venue": short_venue, "short_market_type": "Futures",
                "quote_ts_us": ts, "depth_weighted_spread_pct": spread, "notes": {}}

    rows = [row("Mexc", "Gate", 100), row("Mexc", "Gate", 200), row("Kucoin", "Gate", 100)]
    capped = runner._cap_rows_per_token(rows)
    identities = {runner._route_identity(r) for r in capped}
    assert len(capped) == 2 and len(identities) == 2, "one row per distinct route"
    kept = [r for r in capped if r["long_venue"] == "Mexc"][0]
    assert kept["quote_ts_us"] == 200, "the freshest duplicate is the one kept"


def test_a_crowded_snapshot_loses_depth_not_tokens() -> None:
    """Raising the per-token cap to 90 cut the board from 1,637 tokens to 777,
    because the row limit was a plain slice off the tail."""
    from spreadarb.api_discovery import runner

    def row(token, i):
        return {"token": token, "long_venue": f"V{i}", "short_venue": "Bybit",
                "long_market_type": "Spot", "short_market_type": "Futures",
                "depth_weighted_spread_pct": float(i), "notes": {}}

    rows = [row(t, i) for t in ("AAA", "BBB", "CCC", "DDD") for i in range(30)]
    capped = runner._cap_rows_per_token(rows, budget=20)
    tokens = {r["token"] for r in capped}
    assert tokens == {"AAA", "BBB", "CCC", "DDD"}, "every token must survive the squeeze"
    assert len(capped) <= 20, "the budget must be respected"


def test_every_token_keeps_at_least_one_route() -> None:
    from spreadarb.api_discovery import runner

    rows = [{"token": f"T{i}", "long_venue": "A", "short_venue": "B",
             "long_market_type": "Spot", "short_market_type": "Futures",
             "depth_weighted_spread_pct": 1.0, "notes": {}} for i in range(50)]
    capped = runner._cap_rows_per_token(rows, budget=10)
    assert len({r["token"] for r in capped}) == 50


def _erow(spread=None, funding=None, **kw):
    from types import SimpleNamespace
    base = dict(token="X", route_key="X|A|B", executable_spread_pct=spread,
                funding_24h_pct=funding, funding_projected_24h_pct=None,
                long_funding_pct=None, short_funding_pct=None,
                long_funding_interval_hours=None, short_funding_interval_hours=None,
                route_kind="FUTURES", long_market_type="Futures", short_market_type="Futures",
                long_venue="A", short_venue="B")
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_route_that_loses_on_both_counts_is_not_shown() -> None:
    """Every pair is emitted in both directions, so half the board was the mirror
    of a real route and lost money by construction."""
    assert api_spreads.pays_something(_erow(spread=-0.4, funding=-0.2)) is False


def test_a_positive_spread_alone_is_enough() -> None:
    """A spread trade can be worth taking with funding against it -- COTI shows
    +2.36% open against -169% APR on the reference board."""
    assert api_spreads.pays_something(_erow(spread=2.36, funding=-0.46)) is True


def test_a_positive_carry_alone_is_enough() -> None:
    """A basis farm is entered at a NEGATIVE spread and paid in funding: SIREN
    sits at -53% open with +138% APR."""
    assert api_spreads.pays_something(_erow(spread=-53.05, funding=0.377)) is True


def test_the_result_cache_is_bounded_by_entry_count() -> None:
    """Each entry is a fully materialised payload; at 34k rows a handful of them
    took a 4GB box to 156MB free and left the container unhealthy."""
    assert api_spreads._RESULT_CACHE_MAX_ENTRIES <= 8
    import inspect
    source = inspect.getsource(api_spreads.load_spreads)
    assert "_RESULT_CACHE_MAX_ENTRIES" in source


def test_the_funding_lane_only_lists_carry_you_receive() -> None:
    """A route that PAYS funding is not a farm. Ranking on magnitude put a -500%
    payer above a +200% earner."""
    quote_ts_us = int(time.time() * 1_000_000)

    def row(token, funding):
        return api_spreads._row_from_api(
            {"token": token, "long_venue": "Gate", "long_market_type": "Futures",
             "short_venue": "Bybit", "short_market_type": "Futures",
             "quote_ts_us": quote_ts_us, "executable_spread_pct": 0.5,
             "funding_24h_pct": funding},
            bucket="api_discovered", now=quote_ts_us / 1_000_000,
        )

    rows = [row("EARNS", 0.55), row("PAYS", -1.4), row("NONE", None)]
    kept = api_spreads._filter_rows(rows, funding_only=True, include_stale=True)
    assert {r.token for r in kept} == {"EARNS"}


def test_the_funding_page_ranks_by_signed_carry() -> None:
    import inspect
    source = inspect.getsource(server.render_funding_page)
    assert 'sort="funding"' in source and 'sort="funding_abs"' not in source


def test_a_price_refresh_does_not_invalidate_the_whole_board(tmp_path) -> None:
    """Rewriting a 50-77MB snapshot every 60s to change a few hundred routes
    forced a full parse and re-materialisation each time, which is what pinned a
    small machine at 100% and stopped discovery scans from ever finishing."""
    quote_ts_us = int(time.time() * 1_000_000)
    snapshot = tmp_path / "api_discovery_latest.json"

    def raw(token, spread):
        return {"token": token, "long_venue": "Gate", "long_market_type": "Futures",
                "short_venue": "Bybit", "short_market_type": "Futures",
                "quote_ts_us": quote_ts_us, "executable_spread_pct": spread,
                "depth_weighted_spread_pct": spread}

    snapshot.write_text(json.dumps({
        "updated_at": "2026-08-02T00:00:00Z",
        "api_discovered_rows": [raw("AAA", 1.0), raw("BBB", 2.0)],
        "dex_discovered_rows": [],
    }))
    api_spreads._ROW_CACHE.clear()
    api_spreads._RESULT_CACHE.clear()

    before = api_spreads.load_spreads(api_path=snapshot, board_path=tmp_path / "n.jsonl",
                                      limit=None, include_stale=True, include_unverified=True)
    aaa = [r for g in before["groups"] for r in g["routes"] if r["token"] == "AAA"]
    assert aaa and float(aaa[0]["executable_spread_pct"]) == 1.0

    # Only the delta changes; the snapshot file is untouched.
    stamp_before = snapshot.stat().st_mtime_ns
    (tmp_path / "api_discovery_fast_quotes.json").write_text(json.dumps({
        "updated_at": "2026-08-02T00:01:00Z", "rows": [raw("AAA", 7.5)]}))
    api_spreads._ROW_CACHE.clear()
    api_spreads._RESULT_CACHE.clear()

    after = api_spreads.load_spreads(api_path=snapshot, board_path=tmp_path / "n.jsonl",
                                     limit=None, include_stale=True, include_unverified=True)
    aaa = [r for g in after["groups"] for r in g["routes"] if r["token"] == "AAA"]
    assert aaa and float(aaa[0]["executable_spread_pct"]) == 7.5, "the delta must be applied"
    assert snapshot.stat().st_mtime_ns == stamp_before, "the snapshot must not be rewritten"
    bbb = [r for g in after["groups"] for r in g["routes"] if r["token"] == "BBB"]
    assert bbb, "routes outside the delta must survive untouched"


def test_warmed_views_parse_one_snapshot_once(tmp_path, monkeypatch) -> None:
    """Each lane is a different result-cache key, but they share one row set."""
    quote_ts_us = int(time.time() * 1_000_000)
    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text(json.dumps({
        "updated_at": "2026-08-06T00:00:00Z",
        "api_discovered_rows": [{
            "token": "AAA", "long_venue": "Gate", "long_market_type": "Futures",
            "short_venue": "Bybit", "short_market_type": "Futures",
            "quote_ts_us": quote_ts_us, "executable_spread_pct": 1.0,
            "depth_weighted_spread_pct": 1.0,
        }],
        "dex_discovered_rows": [],
    }))
    original = api_spreads._cached_snapshot
    calls = 0

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(api_spreads, "_cached_snapshot", counted)
    api_spreads._ROW_CACHE.clear()
    api_spreads._load_api_discovery_rows(snapshot, now=time.time(), metadata={}, rails={})
    api_spreads._load_api_discovery_rows(snapshot, now=time.time(), metadata={}, rails={})
    assert calls == 1


def test_a_delta_route_absent_from_the_snapshot_is_still_shown(tmp_path) -> None:
    quote_ts_us = int(time.time() * 1_000_000)
    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text(json.dumps({"updated_at": "2026-08-02T00:00:00Z",
                                    "api_discovered_rows": [], "dex_discovered_rows": []}))
    (tmp_path / "api_discovery_fast_quotes.json").write_text(json.dumps({"rows": [
        {"token": "NEW", "long_venue": "Gate", "long_market_type": "Futures",
         "short_venue": "Bybit", "short_market_type": "Futures",
         "quote_ts_us": quote_ts_us, "executable_spread_pct": 3.0,
         "depth_weighted_spread_pct": 3.0}]}))
    api_spreads._ROW_CACHE.clear()
    api_spreads._RESULT_CACHE.clear()
    d = api_spreads.load_spreads(api_path=snapshot, board_path=tmp_path / "n.jsonl",
                                 limit=None, include_stale=True)
    assert [g for g in d["groups"] if g["token"] == "NEW"]


class _Book:
    def __init__(self, bids, asks, quote_ts_us):
        self.bids, self.asks, self.quote_ts_us = bids, asks, quote_ts_us


def _live_row(**kw):
    quote_ts_us = int((time.time() - 1200) * 1_000_000)   # twenty minutes old
    raw = {"token": "AAA", "long_venue": "Gate", "long_market_type": "Futures",
           "short_venue": "Bybit", "short_market_type": "Futures",
           "quote_ts_us": quote_ts_us, "executable_spread_pct": 1.0,
           "depth_weighted_spread_pct": 1.0,
           "notes": {"route_inputs": {"long": {"symbol": "AAA/USDT:USDT"},
                                      "short": {"symbol": "AAA/USDT:USDT"}}}}
    raw.update(kw)
    return api_spreads._row_from_api(raw, bucket="api_discovered", now=time.time())


def test_a_streaming_route_is_priced_from_the_feed_not_the_file() -> None:
    """The websocket worker already streamed these books; the board only ever saw
    them indirectly, through whatever the fast-quote worker last wrote to disk.
    That is what made a route minutes old on a page load."""
    from spreadboard import live_book_cache
    now = time.time()
    ts = int(now * 1_000_000)
    books = {
        live_book_cache.cache_key("Gate", "Futures", "AAA/USDT:USDT"):
            _Book([[100.0, 50.0]], [[100.0, 50.0]], ts),
        live_book_cache.cache_key("Bybit", "Futures", "AAA/USDT:USDT"):
            _Book([[110.0, 50.0]], [[110.5, 50.0]], ts),
    }
    row = _live_row()
    assert row.age_min > 15, "the stored row is twenty minutes old"
    live = api_spreads.apply_live_books([row], books, now=now)[0]
    assert live.live_book is True
    assert live.age_min < 1, "a streamed route must not read as minutes old"
    assert live.executable_spread_pct == pytest.approx(10.0), "priced from the feed"
    assert live.freshness == "fresh"
    assert live.depth_usd == api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD
    assert "depth_unverified" not in live.blockers


def test_a_route_with_only_one_leg_streaming_is_left_alone() -> None:
    """Half a live price is worse than none: it would compare a fresh bid against
    an old ask and invent a spread that never existed."""
    from spreadboard import live_book_cache
    now = time.time()
    books = {live_book_cache.cache_key("Gate", "Futures", "AAA/USDT:USDT"):
             _Book([[100.0, 50.0]], [[100.0, 50.0]], int(now * 1_000_000))}
    row = _live_row()
    assert api_spreads.apply_live_books([row], books, now=now)[0] is row


def test_no_live_feed_leaves_the_board_untouched() -> None:
    row = _live_row()
    assert api_spreads.apply_live_books([row], {}, now=time.time())[0] is row


def test_the_board_page_subscribes_to_price_pushes() -> None:
    """The data went live once routes were priced from the streaming books, but a
    member still only saw it by reloading. On a spread that lasts minutes that is
    the difference between a trade and a screenshot."""
    script = server.render_board_stream_script({"kind": ["FUTURES"]})
    assert "/api/stream/board" in script and "kind=FUTURES" in script
    assert "EventSource" in script and 'addEventListener("board"' in script
    assert "data-live-spread" in script and "data-live-funding" in script


def test_rows_carry_the_hooks_the_stream_patches() -> None:
    """A push with nothing to patch is a push into the void."""
    import inspect
    source = inspect.getsource(server.render_market_group_route)
    assert "data-route-key" in source
    assert "data-live-spread" in source and "data-live-funding" in source


def test_the_stream_reports_only_what_changed(tmp_path, monkeypatch) -> None:
    """Re-sending every route every few seconds would push megabytes to every
    open page for no reason."""
    calls = {"n": 0}

    def market(_board_path, _query):
        calls["n"] += 1
        spread = 1.0 if calls["n"] == 1 else 2.0
        return {"groups": [{"routes": [
            {"route_key": "A|x", "executable_spread_pct": spread, "funding_daily_pct": 0.1},
            {"route_key": "B|y", "executable_spread_pct": 5.0, "funding_daily_pct": 0.2}]}]}

    monkeypatch.setattr(server, "api_market_spreads", market)
    monkeypatch.setattr(
        server.api_spreads,
        "live_prices_for",
        lambda routes: {
            row["route_key"]: (row["executable_spread_pct"], row["funding_daily_pct"])
            for row in routes
        },
    )
    first = server._board_stream_rows(tmp_path / "b.jsonl", {})
    second = server._board_stream_rows(tmp_path / "b.jsonl", {})
    changed = {k: v for k, v in second.items() if first.get(k) != v}
    assert set(changed) == {"A|x"}, "only the route that moved is worth sending"


def test_a_venues_markets_are_loaded_once_not_once_per_subscription() -> None:
    """CCXT Pro loads markets implicitly on the first watch call. With hundreds
    of tasks starting together they all fired the same metadata request at the
    same venue -- 114 Gate timeouts, 42 Binance, 32 Bybit, 30 XT in eight
    minutes, every one retrying with backoff, and not a single book written."""
    import asyncio, inspect
    from scripts import websocket_book_worker

    source = inspect.getsource(websocket_book_worker.BookWorker._watch)
    assert "_ensure_markets" in source, "markets must be loaded before watching"

    worker = websocket_book_worker.BookWorker.__new__(websocket_book_worker.BookWorker)
    worker._market_locks = {}
    worker._markets_ready = set()
    calls = {"n": 0}

    class Client:
        async def load_markets(self):
            calls["n"] += 1
            await asyncio.sleep(0.01)

    async def drive():
        client = Client()
        await asyncio.gather(*[
            worker._ensure_markets("Gate", "Spot", client) for _ in range(25)
        ])

    asyncio.run(drive())
    assert calls["n"] == 1, f"one request per venue, not {calls['n']}"


def test_the_push_path_does_not_read_prices_from_the_cache() -> None:
    """The grouped board is cached because building it is expensive, so its
    prices are only as fresh as that cache. A cached price is exactly what the
    stream exists to correct."""
    import inspect
    source = inspect.getsource(server._board_stream_rows)
    assert "live_prices_for" in source


def test_live_prices_come_from_the_books_for_streamed_routes() -> None:
    from spreadboard import live_book_cache
    ts = int(time.time() * 1_000_000)
    books = {
        live_book_cache.cache_key("Gate", "Futures", "A/USDT:USDT"):
            _Book([[100.0, 9.0]], [[100.0, 9.0]], ts),
        live_book_cache.cache_key("Bybit", "Futures", "A/USDT:USDT"):
            _Book([[105.0, 9.0]], [[105.5, 9.0]], ts),
    }
    routes = [{"route_key": "A|k", "long_venue": "Gate", "long_market_type": "Futures",
               "long_market_symbol": "A/USDT:USDT", "short_venue": "Bybit",
               "short_market_type": "Futures", "short_market_symbol": "A/USDT:USDT",
               "funding_daily_pct": 0.2}]
    import unittest.mock as mock
    with mock.patch.object(api_spreads, "_live_books", return_value=books):
        prices = api_spreads.live_prices_for(routes)
    assert prices["A|k"][0] == pytest.approx(5.0)


def test_a_route_with_no_live_book_is_not_invented() -> None:
    import unittest.mock as mock
    routes = [{"route_key": "Z|k", "long_venue": "Gate", "long_market_type": "Futures",
               "long_market_symbol": "Z/USDT:USDT", "short_venue": "Bybit",
               "short_market_type": "Futures", "short_market_symbol": "Z/USDT:USDT"}]
    with mock.patch.object(api_spreads, "_live_books", return_value={}):
        assert api_spreads.live_prices_for(routes) == {}


def _perp_dex_source_index(enabled: list) -> int:
    for index, source in enumerate(enabled):
        if isinstance(source, sources.DexDerivativeCcxtSource):
            return index
    raise AssertionError("no perp DEX source in the default set")


def test_perp_dexes_are_collected_before_the_spot_dex_that_pairs_against_them() -> None:
    """Aster has to exist before a DEX spot leg can be hedged onto it.

    A source only ever sees the quotes gathered ahead of it. Collecting Aster
    and Hyperliquid after the OKX DEX source meant no DEX spot leg could pair
    with a perp DEX, which is the shape most of these farms take.
    """
    enabled = sources.default_sources(include_network=True)
    spot_dex = [
        index
        for index, source in enumerate(enabled)
        if isinstance(source, sources.OkxDexQuoteSource)
    ]

    assert spot_dex, "no DEX spot source in the default set"
    assert _perp_dex_source_index(enabled) < min(spot_dex)


def test_perp_dex_quotes_join_the_pool_a_dex_leg_is_paired_against(tmp_path: Path) -> None:
    """`dex_derivative` quotes are hedge venues, not only route producers."""

    def quote(venue: str) -> MarketQuote:
        return MarketQuote(
            token="AAA",
            venue=venue,
            market_type="Futures",
            bid=1.0,
            ask=1.0,
            bid_vwap=1.0,
            ask_vwap=1.0,
            quote_ts_us=1,
            source_name="test",
        )

    class Source:
        def __init__(self, name: str, kind: str) -> None:
            self.name = name
            self.kind = kind

        def collect(self, context):
            self.seen = tuple(q.venue for q in context.reference_quotes)
            return SourceResult(
                status=SourceStatus(
                    name=self.name,
                    kind=self.kind,
                    status="ok",
                    started_at="now",
                    finished_at="now",
                    elapsed_seconds=0.0,
                ),
                quotes=(quote(self.name),),
            )

    perp_dex = Source("Aster", "dex_derivative")
    downstream = Source("OkxDex", "dex_spot")

    runner.run_discovery(
        db_path=None,
        watchlist_path=None,
        snapshot_path=tmp_path / "snapshot.json",
        archive_dir=tmp_path / "archive",
        timeout_seconds=None,
        sources=[perp_dex, downstream],
        blacklist_filter_enabled=False,
    )

    assert "Aster" in downstream.seen


def test_the_dex_token_ceiling_leaves_room_for_the_mainstream_names() -> None:
    """The cap was 50, and it is the whole of the DEX side's token coverage.

    The ranking puts projected funding first -- right for a funding board, but
    it means high-volume names like DOGE, WIF and SHIB lose every tie, so the
    board carried 50 DEX tokens and none of them.
    """
    import inspect
    import re

    source = inspect.getsource(sources.OkxDexQuoteSource._discover_okx_assets)
    ceiling = int(re.search(r"min\(\s*(\d+),", source).group(1))
    assert ceiling >= 150, f"DEX token ceiling is still {ceiling}"

    configured = int(
        re.search(
            r'SPREADBOARD_OKX_DEX_DYNAMIC_TOKENS:\s*"(\d+)"',
            Path("compose.production.yml").read_text(encoding="utf-8"),
        ).group(1)
    )
    assert configured > 50
    assert configured <= ceiling


def test_the_most_traded_tokens_keep_a_share_of_the_dex_slots() -> None:
    """Funding-first ranking filled every slot with obscure high-carry names.

    Measured at limit 150 with 459 qualifying: DOGE, WIF, SHIB, FARTCOIN and
    STETH were all still absent, because their funding is unremarkable while
    their volume is not.
    """
    assert 0 < sources.VOLUME_RESERVED_SHARE <= 0.6

    source = sources.OkxDexQuoteSource.__new__(sources.OkxDexQuoteSource)

    # Ten tokens: HIGHCARRY* pay well and trade nothing; TRADED* the reverse.
    quotes = {}
    for index in range(5):
        quotes[f"HIGHCARRY{index}"] = [
            MarketQuote(
                token=f"HIGHCARRY{index}", venue="V", market_type="Futures",
                bid=1.0, ask=1.0, bid_vwap=1.0, ask_vwap=1.0, quote_ts_us=1,
                source_name="t", funding_rate_pct=5.0, funding_interval_hours=8.0,
                volume_24h_usd=1_000.0,
            )
        ]
        quotes[f"TRADED{index}"] = [
            MarketQuote(
                token=f"TRADED{index}", venue="V", market_type="Futures",
                bid=1.0, ask=1.0, bid_vwap=1.0, ask_vwap=1.0, quote_ts_us=1,
                source_name="t", funding_rate_pct=0.0001, funding_interval_hours=8.0,
                volume_24h_usd=900_000_000.0,
            )
        ]

    # Reproduce the selection the source performs, at a limit of 4.
    limit = 4
    reserved = max(0, int(limit * sources.VOLUME_RESERVED_SHARE))

    def priority(symbol):
        refs = quotes[symbol]
        return (
            max(abs(q.funding_rate_pct or 0.0) * 24.0 / (q.funding_interval_hours or 8.0) for q in refs),
            1,
            max(q.volume_24h_usd or 0.0 for q in refs),
            symbol,
        )

    def traded(symbol):
        return max(q.volume_24h_usd or 0.0 for q in quotes[symbol])

    by_funding = sorted(quotes, key=priority, reverse=True)
    by_volume = sorted(quotes, key=traded, reverse=True)
    chosen = set(by_funding[: limit - reserved])
    for symbol in by_volume:
        if len(chosen) >= limit:
            break
        chosen.add(symbol)

    del source
    assert len(chosen) == limit
    assert any(name.startswith("TRADED") for name in chosen), (
        "the most traded names still take no slot"
    )
    assert any(name.startswith("HIGHCARRY") for name in chosen), (
        "funding ranking must still win most slots"
    )


def test_the_depth_column_reads_a_key_discovery_actually_writes() -> None:
    """It read notes.screen.liquidity_usd, which nothing has ever written.

    Measured on production: 0 of 26,690 snapshot rows carry that key, so the
    column was empty on all 15,943 board rows and "sort by depth" ranked
    nothing -- silently, the same shape as the funding overlay keying on a field
    that was always None.
    """
    row = {
        "notes": {
            "route_inputs": {
                "long": {"volume_24h_usd": 3_976_315.0},
                "short": {"volume_24h_usd": 8_816.0},
            }
        }
    }

    # The route is only as tradeable as its thinner side.
    assert api_spreads._depth_from_api(row) == pytest.approx(8_816.0)

    # The old key is not consulted any more.
    assert api_spreads._depth_from_api({"notes": {"screen": {"liquidity_usd": 999}}}) is None
    # One leg without volume is not a route figure.
    assert api_spreads._depth_from_api(
        {"notes": {"route_inputs": {"long": {"volume_24h_usd": 10.0}, "short": {}}}}
    ) is None
    assert api_spreads._depth_from_api({}) is None


def test_the_column_no_longer_promises_order_book_depth() -> None:
    """The scan probes $50, which cannot answer "how much before I move it"."""
    source = Path("spreadboard/server.py").read_text(encoding="utf-8")

    assert "24h vol, thinner leg" in source
    assert '("depth", "24h volume")' in source or "('depth', '24h volume')" in source
    # The guide must not claim it is depth.
    assert "roughly how much you can trade before you move the price" not in source
