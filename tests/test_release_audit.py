from __future__ import annotations

import json
from pathlib import Path
import time
from types import SimpleNamespace

from spreadboard import api_spreads, live, server
from scripts.api_discovery_worker import build_parser as discovery_worker_parser
from spreadarb.api_discovery import runner, sources, worker
from spreadarb.api_discovery.identity import (
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


def test_public_route_contract_keeps_spot_spot_and_hides_spot_dex() -> None:
    assert "SPOT" not in api_spreads.RETIRED_ROUTE_KINDS
    assert "DEX-SPOT" in api_spreads.RETIRED_ROUTE_KINDS
    assert api_spreads._normalize_kind_filter("FUTURES-SPOT") == "FUTURES-SPOT-PAIR"


def test_okx_dex_source_budget_covers_rate_limited_watchlist_scan() -> None:
    args = discovery_worker_parser().parse_args([])

    assert args.dex_spot_timeout_s == 240.0


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
    assert api_spreads._public_row(row)["conditions"] == [
        "spot_sell_inventory_required"
    ]


def test_unknown_spot_transfer_is_visible_as_research_condition() -> None:
    reasons = api_spreads._route_mirage_reasons(
        raw={"executable_spread_pct": 3},
        long_market_type="Spot",
        short_market_type="Spot",
        long_rails={},
        short_rails={},
    )

    assert reasons == ["condition:spot_transfer_unknown"]


def test_unverified_cex_dislocation_uses_wider_research_ceiling() -> None:
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

    assert reasons == []


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


def test_verified_dex_watchlist_and_cex_identity_cover_reference_top_ten() -> None:
    root = Path(__file__).resolve().parents[1]
    watchlist = load_watchlist(root / "data" / "api_discovery_watchlist.json")
    registry = load_identity_registry(
        root / "data" / "api_discovery_identity_registry.json"
    )
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

    assert registry.resolve_market(
        venue="Bybit",
        market_type="Futures",
        token="BRETT",
        symbol="BRETT/USDT:USDT",
    ).identity_key == "eip155:8453/erc20:0x532f27101965dd16442e59d40670faf5ebb142e4"
    assert registry.resolve_market(
        venue="Bybit",
        market_type="Futures",
        token="BANK",
        symbol="BANK/USDT:USDT",
    ).identity_key is None


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

    assert health["errors"] == [
        "PEPE:1:RuntimeError: okx_dex_quote:IP validation failed"
    ]
    assert health["details"] == {"provider": "OKX DEX", "quote_count": 0}


def test_spread_ceiling_can_be_disabled_without_hiding_large_exact_routes() -> None:
    assert not sources._spread_ceiling_exceeded(102.2, max_spread_pct=0)
    assert sources._spread_ceiling_exceeded(102.2, max_spread_pct=100)


def test_open_spread_selects_route_before_depth_vwap() -> None:
    first = SimpleNamespace(
        displayed_open_spread_pct=4.4,
        executable_spread_pct=4.4,
        depth_weighted_spread_pct=1.1,
    )
    second = SimpleNamespace(
        displayed_open_spread_pct=2.0,
        executable_spread_pct=2.0,
        depth_weighted_spread_pct=1.8,
    )

    assert api_spreads._entrance_spread(first) == 4.4
    assert api_spreads._entrance_spread(first) > api_spreads._entrance_spread(second)


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
        "api_discovered_rows": [
            {"route_key": "SAME", "quote_ts_us": 100, "token": "CURRENT"}
        ],
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
    assert "Upbit" not in spot


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
        venue
        for source in cex
        if source.market_type == "Spot"
        for venue in source.venues
    } == set(sources.default_enabled_cex_source().venues)
    assert {
        venue
        for source in cex
        if source.market_type == "Futures"
        for venue in source.venues
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
