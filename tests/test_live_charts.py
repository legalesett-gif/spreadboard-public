from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
import sqlite3
import time

import pytest

from spreadboard import fast_quotes, live_book_cache, market_history, server
from spreadboard.fast_quotes import (
    FastQuoteRefresher,
    _expanded_token_rows,
    _expand_selected_dex_tokens,
    _dex_rotating_rows,
    _fast_quote_lane,
    _fresh_fast_quote_row,
    _has_permanent_mirage_guard,
    _cannot_lead_public_lane,
    _native_order_book,
    _native_spot_order_book,
    _native_current_funding,
    _okx_dex_leg_quote,
    _retire_failed_fast_quote,
    _select_fast_quote_rows,
    _snapshot_row_key,
    _kraken_asset_code,
    _native_linear_symbol,
)
from scripts.audit_live_charts import _formula_errors


def _route() -> dict:
    return {
        "route_key": "TEST|Aster|Futures|Bybit|Futures",
        "token": "TEST",
        "route_kind": "FUTURES",
        "long_venue": "Aster",
        "long_market_type": "Futures",
        "long_market_symbol": "TEST/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "TEST/USDT:USDT",
    }


def test_fast_quote_selection_keeps_top_tokens_and_their_other_routes() -> None:
    rows = [
        {"token": "ONE", "depth_weighted_spread_pct": 9.0},
        {"token": "ONE", "depth_weighted_spread_pct": 8.0},
        {"token": "TWO", "depth_weighted_spread_pct": 7.0},
        {"token": "TWO", "depth_weighted_spread_pct": 6.0},
        {"token": "THREE", "depth_weighted_spread_pct": 5.0},
    ]
    selected = _expanded_token_rows(rows, token_limit=2, route_limit=4)

    assert [row["token"] for row in selected[:2]] == ["ONE", "TWO"]
    assert [(row["token"], row["depth_weighted_spread_pct"]) for row in selected] == [
        ("ONE", 9.0),
        ("TWO", 7.0),
        ("ONE", 8.0),
        ("TWO", 6.0),
    ]


def test_fast_quote_cycle_retries_temporary_guards_only() -> None:
    assert not _has_permanent_mirage_guard({"blockers": ["mirage_guard:fast_requote_pending"]})
    assert not _has_permanent_mirage_guard({"blockers": ["mirage_guard:fast_requote_unavailable"]})
    assert not _has_permanent_mirage_guard(
        {"blockers": ["mirage_guard:spot_sell_inventory_required"]}
    )


def test_dex_rotation_keeps_negative_basis_high_funding_token_warm() -> None:
    rows = [
        {
            "token": "GUA",
            "depth_weighted_spread_pct": -0.4,
            "funding_projected_24h_pct": 1.1,
            "quote_ts_us": 1,
        },
        {
            "token": "HEADLINE",
            "depth_weighted_spread_pct": 8.0,
            "funding_projected_24h_pct": 0.0,
            "quote_ts_us": 9,
        },
        {
            "token": "OTHER",
            "depth_weighted_spread_pct": 2.0,
            "funding_projected_24h_pct": 0.0,
            "quote_ts_us": 2,
        },
    ]

    selected = _dex_rotating_rows(rows, priority_tokens={"GUA"}, route_limit=2)

    assert [row["token"] for row in selected] == ["GUA", "HEADLINE"]


def test_combined_dex_rotation_keeps_every_priority_token_before_leaders() -> None:
    rows = [
        {
            "token": token,
            "depth_weighted_spread_pct": spread,
            "quote_ts_us": timestamp,
        }
        for token, spread, timestamp in (
            ("GUA", -0.4, 10),
            ("ESPORTS", -0.2, 20),
            ("HEADLINE", 20.0, 30),
        )
    ]
    selected = _select_fast_quote_rows(
        {
            "FUTURES": [], "FUTURES-SPOT": [], "SPOT": [],
            "DEX-FUTURES": rows, "DEX-SPOT": [dict(rows[0])],
        },
        route_limit=3,
        priority_tokens={"GUA", "ESPORTS"},
    )

    assert [row["token"] for row in selected] == ["GUA", "ESPORTS", "HEADLINE"]


def test_disjoint_dex_lanes_share_the_finite_provider_contract_budget() -> None:
    lanes = {
        "FUTURES": [], "FUTURES-SPOT": [], "SPOT": [],
        "DEX-FUTURES": [
            {
                "route_key": f"F{index}|OKX DEX 1|Spot|Gate|Futures",
                "token": f"F{index}",
                "depth_weighted_spread_pct": 100 - index,
            }
            for index in range(30)
        ],
        "DEX-SPOT": [
            {
                "route_key": f"S{index}|OKX DEX 1|Spot|Mexc|Spot",
                "token": f"S{index}",
                "depth_weighted_spread_pct": 100 - index,
            }
            for index in range(30)
        ],
    }

    selected = _select_fast_quote_rows(
        lanes, route_limit=60, priority_tokens=set()
    )

    assert sum(str(row["token"]).startswith("F") for row in selected) >= 14
    assert sum(str(row["token"]).startswith("S") for row in selected) >= 14
    assert len({fast_quotes._dex_contract_identity(row) for row in selected}) <= 28


def test_shared_dex_contracts_keep_25_tokens_current_in_both_public_lanes() -> None:
    lanes = {"FUTURES": [], "FUTURES-SPOT": [], "SPOT": []}
    for lane, short_type in (("DEX-FUTURES", "Futures"), ("DEX-SPOT", "Spot")):
        lanes[lane] = [
            {
                "route_key": f"{lane}-{index}",
                "token": f"T{index}",
                "long_venue": "OKX DEX 1",
                "long_market_type": "Spot",
                "short_venue": "Gate",
                "short_market_type": short_type,
                "depth_weighted_spread_pct": 100 - index,
                "notes": {"identity": {"long": {
                    "chain_id": "1", "token_address": f"0x{index:040x}",
                }}},
            }
            for index in range(30)
        ]

    selected = _select_fast_quote_rows(lanes, route_limit=70, priority_tokens=set())
    counts = {
        lane: len({row["token"] for row in selected if _fast_quote_lane(row) == lane})
        for lane in ("DEX-FUTURES", "DEX-SPOT")
    }

    assert counts["DEX-FUTURES"] >= 25
    assert counts["DEX-SPOT"] >= 25
    assert len({fast_quotes._dex_contract_identity(row) for row in selected}) <= 28


def test_shared_dex_contracts_rotate_oldest_half_of_top_leader_pool() -> None:
    lanes = {"FUTURES": [], "FUTURES-SPOT": [], "SPOT": []}
    for lane, short_type in (("DEX-FUTURES", "Futures"), ("DEX-SPOT", "Spot")):
        lanes[lane] = [
            {
                "route_key": f"{lane}-{index}",
                "token": f"T{index}",
                "long_venue": "OKX DEX 1",
                "long_market_type": "Spot",
                "short_venue": "Gate",
                "short_market_type": short_type,
                "depth_weighted_spread_pct": 100 - index,
                "quote_ts_us": (index + 1) * 1_000_000,
                "notes": {"identity": {"long": {
                    "chain_id": "1", "token_address": f"0x{index:040x}",
                }}},
            }
            for index in range(30)
        ]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SPREADBOARD_FAST_DEX_ROUTES", "40")
        monkeypatch.setenv("SPREADBOARD_FAST_DEX_CONTRACTS", "14")
        first = _select_fast_quote_rows(lanes, route_limit=43, priority_tokens=set())
        first_tokens = {
            row["token"] for row in first if (_fast_quote_lane(row) or "").startswith("DEX-")
        }
        assert first_tokens == {f"T{index}" for index in range(14)}

        for lane in ("DEX-FUTURES", "DEX-SPOT"):
            for row in lanes[lane]:
                if row["token"] in first_tokens:
                    row["quote_ts_us"] = 100_000_000
        second = _select_fast_quote_rows(lanes, route_limit=43, priority_tokens=set())
        second_tokens = {
            row["token"] for row in second if (_fast_quote_lane(row) or "").startswith("DEX-")
        }
        assert second_tokens == {f"T{index}" for index in range(14, 28)}


def test_production_shared_dex_contracts_publish_one_complete_top_25() -> None:
    lanes = {"FUTURES": [], "FUTURES-SPOT": [], "SPOT": []}
    for lane, short_type in (("DEX-FUTURES", "Futures"), ("DEX-SPOT", "Spot")):
        lanes[lane] = [
            {
                "route_key": f"{lane}-{index}",
                "token": f"T{index}",
                "long_venue": "OKX DEX 1",
                "long_market_type": "Spot",
                "short_venue": "Gate",
                "short_market_type": short_type,
                "depth_weighted_spread_pct": 100 - index,
                # Make the lower-ranked rows much older. Production selection
                # must still publish the current top 25 rather than an old tail.
                "quote_ts_us": (index + 1) * 1_000_000,
                "notes": {"identity": {"long": {
                    "chain_id": "1", "token_address": f"0x{index:040x}",
                }}},
            }
            for index in range(30)
        ]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SPREADBOARD_FAST_DEX_ROUTES", "50")
        monkeypatch.setenv("SPREADBOARD_FAST_DEX_CONTRACTS", "25")
        selected = _select_fast_quote_rows(lanes, route_limit=53, priority_tokens=set())

    dex_tokens = {
        row["token"] for row in selected
        if (_fast_quote_lane(row) or "").startswith("DEX-")
    }
    counts = {
        lane: len({row["token"] for row in selected if _fast_quote_lane(row) == lane})
        for lane in ("DEX-FUTURES", "DEX-SPOT")
    }
    assert dex_tokens == {f"T{index}" for index in range(25)}
    assert counts == {"DEX-FUTURES": 25, "DEX-SPOT": 25}


def test_production_fast_budget_reserves_dex_truth_and_cex_canaries() -> None:
    lanes: dict[str, list[dict]] = {}
    for lane in ("FUTURES", "FUTURES-SPOT", "SPOT"):
        lanes[lane] = [
            {
                **_route(),
                "route_key": f"{lane}-{index}",
                "token": f"{lane}{index}",
                "depth_weighted_spread_pct": 100 - index,
                "long_market_type": "Spot" if lane != "FUTURES" else "Futures",
                "short_market_type": "Spot" if lane == "SPOT" else "Futures",
            }
            for index in range(10)
        ]
    for lane, short_type in (("DEX-FUTURES", "Futures"), ("DEX-SPOT", "Spot")):
        lanes[lane] = [
            {
                "route_key": f"{lane}-{index}",
                "token": f"T{index}",
                "long_venue": "OKX DEX 1",
                "long_market_type": "Spot",
                "short_venue": "Gate",
                "short_market_type": short_type,
                "depth_weighted_spread_pct": 100 - index,
                "notes": {"identity": {"long": {
                    "chain_id": "1", "token_address": f"0x{index:040x}",
                }}},
            }
            for index in range(30)
        ]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SPREADBOARD_FAST_DEX_ROUTES", "40")
        monkeypatch.setenv("SPREADBOARD_FAST_DEX_CONTRACTS", "14")
        selected = _select_fast_quote_rows(lanes, route_limit=43, priority_tokens=set())
    counts = {
        lane: len({row["token"] for row in selected if _fast_quote_lane(row) == lane})
        for lane in lanes
    }

    # Fixtures expose exactly one shared route per lane/contract, so 14 shared
    # contracts expand to 28 DEX rows plus the three CEX canaries. Production
    # can use the remaining budget for additional pairings on those contracts.
    assert len(selected) == 31
    assert counts["FUTURES"] == counts["FUTURES-SPOT"] == counts["SPOT"] == 1
    assert counts["DEX-FUTURES"] == 14
    assert counts["DEX-SPOT"] == 14


def test_fast_quote_budget_is_failure_tolerant_across_all_lanes(monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_FAST_DEX_ROUTES", "70")
    lanes = {}
    for lane, prefix in (
        ("FUTURES", "F"), ("FUTURES-SPOT", "P"), ("SPOT", "S"),
        ("DEX-FUTURES", "D"), ("DEX-SPOT", "X"),
    ):
        rows = []
        for index in range(90):
            token = f"{prefix}{index}"
            if lane == "FUTURES":
                long_venue, long_type, short_venue, short_type = "Aster", "Futures", "Bybit", "Futures"
            elif lane == "FUTURES-SPOT":
                long_venue, long_type, short_venue, short_type = "Mexc", "Spot", "Bybit", "Futures"
            elif lane == "SPOT":
                long_venue, long_type, short_venue, short_type = "Mexc", "Spot", "Gate", "Spot"
            elif lane == "DEX-FUTURES":
                long_venue, long_type, short_venue, short_type = "OKX DEX 1", "Spot", "Bybit", "Futures"
            else:
                long_venue, long_type, short_venue, short_type = "OKX DEX 1", "Spot", "Mexc", "Spot"
            rows.append({
                "route_key": f"{token}|{long_venue}|{long_type}|{short_venue}|{short_type}",
                "token": token,
                "long_venue": long_venue,
                "long_market_type": long_type,
                "long_market_symbol": f"{token}/USDT" + (":USDT" if long_type == "Futures" else ""),
                "short_venue": short_venue,
                "short_market_type": short_type,
                "short_market_symbol": f"{token}/USDT" + (":USDT" if short_type == "Futures" else ""),
                "depth_weighted_spread_pct": 90 - index,
                "blockers": [],
                "notes": {"identity": {"long": {
                    "chain_id": "1", "token_address": f"0x{index:040x}",
                }}},
            })
        lanes[lane] = rows

    selected = _select_fast_quote_rows(lanes, route_limit=220, priority_tokens=set())

    counts = {
        lane: len({row["token"] for row in selected if _fast_quote_lane(row) == lane})
        for lane in lanes
    }
    assert counts == {
        "FUTURES": 50, "FUTURES-SPOT": 50, "SPOT": 50,
        "DEX-FUTURES": 14, "DEX-SPOT": 14,
    }


def test_fast_quote_skips_unverified_tokenized_capacity() -> None:
    assert _cannot_lead_public_lane({
        "token": "AAPLSTOCK",
        "long_venue": "A",
        "short_venue": "B",
        "long_market_symbol": "AAPLSTOCK/USDT:USDT",
        "short_market_symbol": "AAPLSTOCK/USDT:USDT",
    })
    assert not _cannot_lead_public_lane({
        "token": "GUA", "long_venue": "A", "short_venue": "B",
    })


def test_fast_quote_skips_known_unrankable_spot_routes_before_using_capacity() -> None:
    row = {
        "token": "VANRY",
        "source_kind": "api_discovered",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "short_venue": "Binance",
        "short_market_type": "Spot",
        "depth_weighted_spread_pct": 60.0,
    }
    rails = {
        "Mexc": {"VANRY": {"withdraw": True, "networks": []}},
        "Binance": {"VANRY": {"deposit": True, "networks": []}},
    }
    assert _cannot_lead_public_lane(row, rails=rails)

    row["token"] = "THIN"
    row["depth_weighted_spread_pct"] = 1.0
    assert _cannot_lead_public_lane(
        row,
        rails={},
        metadata={"THIN": {"total_volume_usd": 999.0}},
    )


def test_selected_dex_token_uses_spare_budget_for_more_current_pairs() -> None:
    seeds = [{"token": "GUA", "long_venue": "OKX DEX 56", "short_venue": "Gate"}]
    rows = [
        *seeds,
        {"token": "GUA", "long_venue": "OKX DEX 56", "short_venue": "Mexc"},
        {"token": "OTHER", "long_venue": "OKX DEX 56", "short_venue": "Bybit"},
    ]

    expanded = _expand_selected_dex_tokens(seeds, rows, 2)

    assert [(row["token"], row["short_venue"]) for row in expanded] == [
        ("GUA", "Gate"),
        ("GUA", "Mexc"),
    ]


def test_fast_delta_retains_only_current_verified_rows() -> None:
    now_us = 10_000_000
    assert _fresh_fast_quote_row(
        {"fast_quote_verified_at": "now", "quote_ts_us": 9_000_000},
        now_us=now_us,
        max_age_seconds=2,
    )
    assert not _fresh_fast_quote_row(
        {"fast_quote_verified_at": "old", "quote_ts_us": 1_000_000},
        now_us=now_us,
        max_age_seconds=2,
    )


def test_failed_fast_quote_retires_the_old_live_claim() -> None:
    row = {
        "quote_ts_us": int(time.time() * 1_000_000),
        "fast_quote_verified_at": "2026-08-13T00:00:00Z",
        "freshness": "fresh",
        "status": "live",
    }

    _retire_failed_fast_quote(row)

    assert row["quote_ts_us"] == 0
    assert row["fast_quote_verified_at"] is None
    assert row["freshness"] == "stale"
    assert row["status"] == "refreshing"


def test_fast_delta_identity_includes_exact_market_symbols() -> None:
    first = {
        "token": "BTC",
        "long_venue": "Kraken",
        "long_market_type": "Spot",
        "long_market_symbol": "BTC/USD",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": "BTC/USDT:USDT",
    }
    second = {**first, "long_market_symbol": "BTC/USDT"}

    assert _snapshot_row_key(first) != _snapshot_row_key(second)


def test_fast_quote_lane_covers_all_public_route_families() -> None:
    futures = _route()
    futures_spot = {
        **_route(),
        "long_venue": "Gate",
        "long_market_type": "Spot",
    }
    spot = {
        **_route(),
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "short_venue": "Mexc",
        "short_market_type": "Spot",
    }
    dex = {
        **_route(),
        "short_venue": "OKX DEX 56",
        "short_market_type": "Spot",
        "notes": {
            "identity": {
                "short": {
                    "chain_id": "56",
                    "token_address": "0x123",
                }
            }
        },
    }
    dex_spot = {
        **dex,
        "long_venue": "Gate",
        "long_market_type": "Spot",
    }

    assert _fast_quote_lane(futures) == "FUTURES"
    assert _fast_quote_lane(futures_spot) == "FUTURES-SPOT"
    assert _fast_quote_lane(spot) == "SPOT"
    assert _fast_quote_lane(dex) == "DEX-FUTURES"
    assert _fast_quote_lane(dex_spot) == "DEX-SPOT"
    assert _fast_quote_lane({**dex, "blockers": ["cex_identity_unverified"]}) is None
    assert _fast_quote_lane({**dex, "notes": {}}) is None


def test_fast_quote_gate_client_uses_available_ccxt_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ccxt

    class GateClient:
        def __init__(self, params: dict) -> None:
            self.params = params

        def load_markets(self) -> dict:
            return {}

    monkeypatch.delattr(ccxt, "gateio", raising=False)
    monkeypatch.setattr(ccxt, "gate", GateClient)

    client = FastQuoteRefresher()._client("Gate", "Spot")

    assert isinstance(client, GateClient)
    assert client.params["options"]["defaultType"] == "spot"


def test_exact_route_quote_uses_four_book_sides_and_matched_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresher = FastQuoteRefresher()
    quotes = {
        "long": {
            "symbol": "TEST/USDT:USDT",
            "bid": 99.0,
            "ask": 100.0,
            "bid_vwap": 98.0,
            "ask_vwap": 101.0,
            "contract_size": 1.0,
            "quote_ts_us": 2_000_000,
            "current_funding_pct": 0.01,
            "funding_interval_hours": 1.0,
        },
        "short": {
            "symbol": "TEST/USDT:USDT",
            "bid": 110.0,
            "ask": 112.0,
            "bid_vwap": 108.0,
            "ask_vwap": 113.0,
            "contract_size": 1.0,
            "quote_ts_us": 2_000_010,
            "current_funding_pct": -0.08,
            "funding_interval_hours": 4.0,
        },
    }
    calls: list[tuple[str, bool | None]] = []

    def quote_leg(_row: dict, side: str, **kwargs: object) -> dict:
        calls.append((side, kwargs.get("include_funding")))
        return quotes[side]

    monkeypatch.setattr(
        refresher,
        "_leg_quote",
        quote_leg,
    )

    result = refresher.quote_route(_route(), target_notional_usd=50.0)

    assert result["status"] == "ok"
    assert result["row"]["executable_spread_pct"] == pytest.approx(10.0)
    assert result["row"]["depth_weighted_spread_pct"] == pytest.approx((108 / 101 - 1) * 100)
    assert result["row"]["quote_ts_us"] == 2_000_000
    assert result["row"]["long_funding_interval_hours"] == 1.0
    assert result["row"]["short_funding_interval_hours"] == 4.0
    assert result["row"]["long_funding_interval_assumed"] is False
    assert result["row"]["funding_projected_24h_pct"] == pytest.approx(-0.72)
    assert set(calls) == {("long", True), ("short", True)}


def test_fresh_external_funding_cache_skips_duplicate_venue_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    funding = tmp_path / "live_funding.json"
    funding.write_text('{"legs":{}}')
    monkeypatch.setenv("SPREADBOARD_DATA_DIR", str(tmp_path))

    assert fast_quotes._external_funding_is_fresh()

    old = time.time() - 601
    os.utime(funding, (old, old))
    assert not fast_quotes._external_funding_is_fresh()


def test_exact_dex_route_rejects_out_of_bounds_spread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresher = FastQuoteRefresher()
    route = {
        **_route(),
        "short_venue": "OKX DEX 56",
        "short_market_type": "Spot",
        "short_market_symbol": "TEST/USDC",
        "dex_chain": "56",
        "dex_contract": "0x123",
    }

    monkeypatch.setattr(
        refresher,
        "_leg_quote",
        lambda _row, side, **_kwargs: {
            "symbol": "TEST/USDT",
            "bid": 1.0 if side == "long" else 3.0,
            "ask": 1.0 if side == "long" else 3.1,
            "bid_vwap": 1.0 if side == "long" else 3.0,
            "ask_vwap": 1.0 if side == "long" else 3.1,
            "quote_ts_us": 2_000_000,
        },
    )

    result = refresher.quote_route(route)

    assert result == {
        "status": "unavailable",
        "error": "exact_route_spread_out_of_bounds",
    }


def test_relative_value_quote_applies_multiplier_without_rewriting_raw_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresher = FastQuoteRefresher()
    route = {
        **_route(),
        "notes": {
            "relative_value": {"long_multiplier": 1, "short_multiplier": 10},
            "route_inputs": {
                "long": {"symbol": "XYZ-SKHX/USDC:USDC"},
                "short": {"symbol": "XYZ-SKHY/USDC:USDC"},
            },
        },
    }
    quotes = {
        "long": {"bid": 99, "ask": 100, "bid_vwap": 98, "ask_vwap": 101, "quote_ts_us": 1},
        "short": {"bid": 13, "ask": 13.1, "bid_vwap": 12.9, "ask_vwap": 13.2, "quote_ts_us": 2},
    }
    monkeypatch.setattr(refresher, "_leg_quote", lambda _row, side, **_kwargs: quotes[side])

    result = refresher.quote_route(route)

    assert result["row"]["executable_spread_pct"] == pytest.approx(30)
    assert result["row"]["depth_weighted_spread_pct"] == pytest.approx((129 / 101 - 1) * 100)
    assert result["row"]["notes"]["route_inputs"]["short"]["bid"] == 13


def test_hyperliquid_xyz_catalog_symbol_maps_to_xyz_coin() -> None:
    assert fast_quotes._hyperliquid_coin("XYZ-SKHX") == "xyz:SKHX"


def test_native_gate_spot_order_book_is_sorted_and_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def fake_json_url(url: str) -> dict:
        requested.append(url)
        return {
            "bids": [["0.102", "2"], ["0.104", "3"], ["0.103", "4"]],
            "asks": [["0.107", "5"], ["0.105", "6"], ["0.106", "7"]],
        }

    monkeypatch.setattr("spreadboard.fast_quotes._json_url", fake_json_url)

    bids, asks = _native_spot_order_book("Gate", "COTI/USDT") or ([], [])

    assert "currency_pair=COTI_USDT" in requested[0]
    assert bids == [[0.104, 3.0], [0.103, 4.0], [0.102, 2.0]]
    assert asks == [[0.105, 6.0], [0.106, 7.0], [0.107, 5.0]]


@pytest.mark.parametrize(
    ("venue", "payload", "url_fragment"),
    [
        (
            "Mexc",
            {"data": {"bids": [[2.0, 3]], "asks": [[2.1, 4]]}},
            "/depth/TEST_USDT",
        ),
        (
            "HTX",
            {"tick": {"bids": [[2.0, 3]], "asks": [[2.1, 4]]}},
            "contract_code=TEST-USDT",
        ),
        (
            "CoinEx",
            {"data": {"depth": {"bids": [[2.0, 3]], "asks": [[2.1, 4]]}}},
            "market=TESTUSDT",
        ),
        (
            "Phemex",
            {"result": {"orderbook_p": {"bids": [[2.0, 3]], "asks": [[2.1, 4]]}}},
            "/md/v2/orderbook",
        ),
        (
            "WhiteBIT",
            {"bids": [[2.0, 3]], "asks": [[2.1, 4]]},
            "TEST_PERP",
        ),
        (
            "BitMart",
            {"data": {"bids": [[2.0, 3]], "asks": [[2.1, 4]]}},
            "symbol=TESTUSDT",
        ),
        (
            "XT",
            {"result": {"b": [[2.0, 3]], "a": [[2.1, 4]]}},
            f"level={fast_quotes.BOOK_DEPTH_LEVELS}",
        ),
        (
            "Coinbase International",
            {
                "best_bid_price": "2.0",
                "best_bid_size": "3",
                "best_ask_price": "2.1",
                "best_ask_size": "4",
            },
            "TEST-PERP/quote",
        ),
    ],
)
def test_secondary_native_futures_books_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
    venue: str,
    payload: dict,
    url_fragment: str,
) -> None:
    requested: list[str] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode()

    def fake_urlopen(request, timeout: float):
        requested.append(request.full_url)
        assert timeout == 8.0
        return Response()

    monkeypatch.setattr("spreadboard.fast_quotes.urlopen", fake_urlopen)

    bids, asks = _native_order_book(venue, "Futures", "TEST/USDT:USDT") or ([], [])

    assert url_fragment in requested[0]
    assert bids == [[2.0, 3.0]]
    assert asks == [[2.1, 4.0]]


def test_native_hyperliquid_book_uses_exact_l2_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "spreadboard.fast_quotes._json_post",
        lambda *_args, **_kwargs: {
            "levels": [[{"px": "2", "sz": "3"}], [{"px": "2.1", "sz": "4"}]]
        },
    )

    bids, asks = _native_order_book("Hyperliquid", "Futures", "TEST/USDC:USDC") or ([], [])

    assert bids == [[2.0, 3.0]]
    assert asks == [[2.1, 4.0]]


def test_live_book_cache_rejects_stale_and_returns_fresh(tmp_path: Path) -> None:
    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    try:
        store.put(
            "Bybit",
            "Futures",
            "TEST/USDT:USDT",
            bids=[[2.0, 3.0]],
            asks=[[2.1, 4.0]],
            quote_ts_us=int(time.time() * 1_000_000),
        )
        fresh = store.get("Bybit", "Futures", "TEST/USDT:USDT", max_age_seconds=5)
        assert fresh is not None
        assert fresh.bids == [[2.0, 3.0]]
        assert fresh.source == "public_websocket"

        store.put(
            "Bybit",
            "Futures",
            "OLD/USDT:USDT",
            bids=[[1.0, 1.0]],
            asks=[[1.1, 1.0]],
            quote_ts_us=int((time.time() - 30) * 1_000_000),
        )
        assert store.get("Bybit", "Futures", "OLD/USDT:USDT", max_age_seconds=5) is None
    finally:
        store.close()


def test_exact_route_prefers_fresh_websocket_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresher = FastQuoteRefresher()
    websocket_book = live_book_cache.CachedBook(
        bids=[[2.0, 30.0]],
        asks=[[2.1, 30.0]],
        quote_ts_us=123_000_000,
    )
    monkeypatch.setattr(
        "spreadboard.fast_quotes.live_book_cache.load_live_book",
        lambda *_args, **_kwargs: websocket_book,
    )
    monkeypatch.setattr(
        "spreadboard.fast_quotes._native_order_book",
        lambda *_args, **_kwargs: pytest.fail("REST should not be called for a fresh WS book"),
    )

    result = refresher._leg_quote(
        _route(),
        "long",
        target_notional_usd=50,
        cache={},
        include_funding=False,
    )

    assert result is not None
    assert result["quote_ts_us"] == 123_000_000
    assert result["quote_source"] == "public_websocket"


def test_native_binance_funding_uses_official_interval_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_json_url(url: str):
        if "fundingInfo" in url:
            return [{"symbol": "ANTHROPICUSDT", "fundingIntervalHours": 8}]
        return {"lastFundingRate": "0.00005", "nextFundingTime": 1_800_000_000_000}

    monkeypatch.setattr("spreadboard.fast_quotes._json_url", fake_json_url)

    result = _native_current_funding("Binance", "ANTHROPIC/USDT:USDT")

    assert result["current_funding_pct"] == pytest.approx(0.005)
    assert result["funding_interval_hours"] == 8


def test_native_kucoin_funding_uses_public_current_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "spreadboard.fast_quotes._json_url",
        lambda _url: {
            "data": {
                "value": "-0.000072",
                "granularity": 3_600_000,
                "fundingTime": 1_800_000_000_000,
            }
        },
    )

    result = _native_current_funding("Kucoin Futures", "COTI/USDT:USDT")

    assert result["current_funding_pct"] == pytest.approx(-0.0072)
    assert result["funding_interval_hours"] == 1
    assert result["next_funding_ts_us"] == 1_800_000_000_000_000


def test_native_mexc_funding_uses_public_contract_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "spreadboard.fast_quotes._json_url",
        lambda _url: {
            "data": {
                "fundingRate": "-0.002911",
                "collectCycle": 4,
                "nextSettleTime": 1_800_000_000_000,
            }
        },
    )

    result = _native_current_funding("Mexc", "VANRY/USDT:USDT")

    assert result["current_funding_pct"] == pytest.approx(-0.2911)
    assert result["funding_interval_hours"] == 4
    assert result["next_funding_ts_us"] == 1_800_000_000_000_000


def test_native_bingx_funding_preserves_reported_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "spreadboard.fast_quotes._json_url",
        lambda _url: {
            "data": {
                "lastFundingRate": "0.001342",
                "fundingIntervalHours": 1,
                "nextFundingTime": 1_800_000_000_000,
            }
        },
    )

    result = _native_current_funding("Bingx", "COTI/USDT:USDT")

    assert result["current_funding_pct"] == pytest.approx(0.1342)
    assert result["funding_interval_hours"] == 1


def test_native_whitebit_funding_uses_ticker_id_and_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "spreadboard.fast_quotes._json_url",
        lambda _url: {
            "result": [
                {
                    "ticker_id": "COTI_PERP",
                    "funding_rate": "0.00005",
                    "funding_interval_minutes": 240,
                    "next_funding_rate_timestamp": 1_800_000_000_000,
                }
            ]
        },
    )

    result = _native_current_funding("WhiteBIT", "COTI/USDT:USDT")

    assert result["current_funding_pct"] == pytest.approx(0.005)
    assert result["funding_interval_hours"] == 4
    assert result["next_funding_ts_us"] == 1_800_000_000_000_000


def test_native_hyperliquid_funding_uses_live_asset_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "spreadboard.fast_quotes._json_post",
        lambda *_args, **_kwargs: [
            {"universe": [{"name": "BTC"}, {"name": "ZK"}]},
            [{"funding": "0.00001"}, {"funding": "-0.0000180442"}],
        ],
    )

    result = _native_current_funding("Hyperliquid", "ZK/USDC:USDC")

    assert result["current_funding_pct"] == pytest.approx(-0.00180442)
    assert result["funding_interval_hours"] == 1
    assert result["next_funding_ts_us"] > int(time.time() * 1_000_000)


def test_native_kraken_spot_parses_pretrade_book(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def fake_json_url(url: str):
        requested.append(url)
        return {
            "error": [],
            "result": {
                "bids": [{"price": "0.98", "qty": "20"}],
                "asks": [{"price": "1.02", "qty": "25"}],
            },
        }

    monkeypatch.setattr("spreadboard.fast_quotes._json_url", fake_json_url)

    bids, asks = _native_spot_order_book("Kraken", "TEST/USD") or ([], [])

    assert requested and "PreTrade" in requested[0]
    assert bids == [[0.98, 20.0]]
    assert asks == [[1.02, 25.0]]


def test_native_kraken_funding_converts_velocity_to_hourly_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "spreadboard.fast_quotes._json_url",
        lambda _url: {
            "tickers": [
                {
                    "symbol": "PF_XBTUSD",
                    "fundingRate": 0.8,
                    "indexPrice": 40_000,
                }
            ]
        },
    )

    result = _native_current_funding("Kraken Futures", "BTC/USD:USD")

    assert _kraken_asset_code("BTC") == "XBT"
    assert result["current_funding_pct"] == pytest.approx(0.002)
    assert result["funding_interval_hours"] == 1
    assert result["next_funding_ts_us"] > int(time.time() * 1_000_000)


def test_native_linear_symbols_preserve_venue_usdc_conventions() -> None:
    assert _native_linear_symbol("Bybit", "BTC", "USDC") == "BTCPERP"
    assert _native_linear_symbol("Bitget", "BTC", "USDC") == "BTCPERP"
    assert _native_linear_symbol("Kucoin Futures", "BTC", "USDT") == "XBTUSDTM"
    assert _native_linear_symbol("Kucoin Futures", "BTC", "USDC") == "XBTUSDCM"
    assert _native_linear_symbol("Binance", "BTC", "USDC") == "BTCUSDC"


def test_exact_okx_dex_leg_requotes_both_sides_at_matched_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spreadarb.dex import okx_quotes

    monkeypatch.setattr(
        okx_quotes,
        "quote_usdc_to_token",
        lambda **_kwargs: {
            "status": "ok",
            "out_qty": "20",
            "to_token_decimals": 18,
            "dex_buy_price_usd": "2.5",
        },
    )
    monkeypatch.setattr(
        okx_quotes,
        "quote_token_to_usdc",
        lambda **_kwargs: {
            "status": "ok",
            "dex_sell_price_usd": "2.45",
        },
    )

    result = _okx_dex_leg_quote(
        {
            "token": "TEST",
            "dex_chain": "1",
            "dex_contract": "0x123",
        },
        "short",
        target_notional_usd=50,
    )

    assert result is not None
    assert result["bid"] == pytest.approx(2.45)
    assert result["ask"] == pytest.approx(2.5)
    assert result["bid_vwap"] == pytest.approx(2.45)
    assert result["ask_vwap"] == pytest.approx(2.5)


def test_exact_okx_dex_leg_reads_raw_discovery_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spreadarb.dex import okx_quotes

    captured: dict[str, str] = {}

    def fake_buy(**kwargs: object) -> dict:
        captured.update(
            chain=str(kwargs["chain"]),
            token_address=str(kwargs["token_address"]),
        )
        return {
            "status": "ok",
            "out_qty": "20",
            "to_token_decimals": 18,
            "dex_buy_price_usd": "2.5",
        }

    monkeypatch.setattr(okx_quotes, "quote_usdc_to_token", fake_buy)
    monkeypatch.setattr(
        okx_quotes,
        "quote_token_to_usdc",
        lambda **_kwargs: {
            "status": "ok",
            "dex_sell_price_usd": "2.45",
        },
    )
    row = {
        "token": "TEST",
        "long_venue": "Bybit",
        "short_venue": "OKX DEX 56",
        "notes": {
            "identity": {
                "short": {
                    "chain_id": "56",
                    "token_address": "0x123",
                }
            }
        },
    }

    result = _okx_dex_leg_quote(row, "short", target_notional_usd=50)

    assert result is not None
    assert captured == {"chain": "56", "token_address": "0x123"}


def test_fast_okx_dex_leg_quotes_only_the_opening_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spreadarb.dex import okx_quotes

    calls: list[str] = []

    def fake_buy(**_kwargs: object) -> dict:
        calls.append("buy")
        return {
            "status": "ok",
            "out_qty": "20",
            "to_token_decimals": 9,
            "dex_buy_price_usd": "2.5",
        }

    def fake_sell(**kwargs: object) -> dict:
        calls.append("sell")
        assert kwargs["token_quantity"] == Decimal("20")
        assert kwargs["token_decimals"] == 9
        return {"status": "ok", "dex_sell_price_usd": "2.45"}

    monkeypatch.setattr(okx_quotes, "quote_usdc_to_token", fake_buy)
    monkeypatch.setattr(okx_quotes, "quote_token_to_usdc", fake_sell)
    common = {
        "token": "TEST",
        "dex_chain": "1",
        "dex_contract": "0x123",
        "notes": {
            "identity": {"short": {"decimals": 9}},
            "route_inputs": {"short": {"bid": 2.5}},
        },
    }

    long_result = _okx_dex_leg_quote(
        common,
        "long",
        target_notional_usd=50,
        quote_both=False,
    )
    assert long_result is not None
    assert long_result["ask"] == pytest.approx(2.5)
    assert "bid" not in long_result
    assert calls == ["buy"]

    calls.clear()
    short_result = _okx_dex_leg_quote(
        common,
        "short",
        target_notional_usd=50,
        quote_both=False,
    )
    assert short_result is not None
    assert short_result["bid"] == pytest.approx(2.45)
    assert "ask" not in short_result
    assert calls == ["sell"]


def test_exact_okx_dex_leg_reuses_cycle_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresher = FastQuoteRefresher()
    calls = 0

    def fake_quote(*_args: object, **_kwargs: object) -> dict:
        nonlocal calls
        calls += 1
        return {"bid": 1.0, "ask": 1.1}

    monkeypatch.setattr("spreadboard.fast_quotes._okx_dex_leg_quote", fake_quote)
    row = {
        "token": "TEST",
        "short_venue": "OKX DEX 56",
        "short_market_type": "Spot",
        "short_market_symbol": "TEST/USDC",
        "dex_chain": "56",
        "dex_contract": "0x123",
    }
    cache: dict = {}

    first = refresher._leg_quote(
        row,
        "short",
        target_notional_usd=50,
        cache=cache,
        include_funding=True,
    )
    second = refresher._leg_quote(
        row,
        "short",
        target_notional_usd=50,
        cache=cache,
        include_funding=True,
    )

    assert first == second
    assert calls == 1

    opposite = {
        **row,
        "long_venue": row["short_venue"],
        "long_market_type": row["short_market_type"],
        "long_market_symbol": row["short_market_symbol"],
    }
    refresher._leg_quote(
        opposite,
        "long",
        target_notional_usd=50,
        cache=cache,
        include_funding=True,
    )
    assert calls == 2

    other = {
        **row,
        "token": "OTHER",
        "dex_contract": "0x456",
    }
    refresher._leg_quote(
        other,
        "short",
        target_notional_usd=50,
        cache=cache,
        include_funding=True,
    )

    assert calls == 3


def test_history_window_does_not_reinsert_an_older_current_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stale_route = {
        **_route(),
        "quote_ts_us": int((time.time() - 600) * 1_000_000),
        "long_bid": 99,
        "long_ask": 100,
        "short_bid": 110,
        "short_ask": 112,
    }
    monkeypatch.setattr(server, "_find_canonical_route", lambda *_args: stale_route)
    monkeypatch.setattr(server, "_refresh_chart_route", lambda *_args: {"status": "idle"})
    monkeypatch.setattr(market_history, "load_history", lambda **_kwargs: [])

    payload = server.api_history(
        stale_route["route_key"],
        tmp_path / "board.json",
        {"hours": [str(1 / 60)]},
    )

    assert payload["ok"]
    assert payload["count"] == 0
    assert payload["rows"] == []


def test_history_persists_entry_matched_exit_and_sample_provenance(tmp_path: Path) -> None:
    row = _route()
    quote_ts_us = int(time.time() * 1_000_000)
    row.update(
        {
            "quote_ts_us": quote_ts_us,
            "executable_spread_pct": 10.0,
            "depth_weighted_spread_pct": (108 / 101 - 1) * 100,
            "target_notional_usd": 50.0,
            "notes": {
                "route_inputs": {
                    "long": {"bid": 99.0, "ask": 100.0, "bid_vwap": 98.0, "ask_vwap": 101.0},
                    "short": {
                        "bid": 110.0,
                        "ask": 112.0,
                        "bid_vwap": 108.0,
                        "ask_vwap": 113.0,
                    },
                }
            },
        }
    )
    row["notes"]["route_inputs"]["long"].update(
        {"current_funding_pct": 0.01, "funding_interval_hours": 1.0}
    )
    row["notes"]["route_inputs"]["short"].update(
        {"current_funding_pct": -0.08, "funding_interval_hours": 4.0}
    )
    db_path = tmp_path / "history.sqlite3"

    assert market_history.record_route(row, db_path=db_path) == 1
    saved = market_history.load_history(
        route_key=row["route_key"],
        since_us=quote_ts_us - 1,
        db_path=db_path,
    )

    assert saved[0]["executable_spread_pct"] == pytest.approx(10.0)
    assert saved[0]["depth_weighted_spread_pct"] == pytest.approx((108 / 101 - 1) * 100)
    assert saved[0]["exit_spread_pct"] == pytest.approx((99 / 112 - 1) * 100)
    assert saved[0]["long_ask_vwap_price"] == pytest.approx(101.0)
    assert saved[0]["short_bid_vwap_price"] == pytest.approx(108.0)
    assert saved[0]["sample_source"] == "live_chart_exact_route"
    assert saved[0]["target_notional_usd"] == 50.0
    assert saved[0]["long_current_funding_pct"] == pytest.approx(0.01)
    assert saved[0]["short_current_funding_pct"] == pytest.approx(-0.08)
    assert saved[0]["short_funding_interval_hours"] == pytest.approx(4.0)
    assert _formula_errors(saved[0]) == []


def test_history_reads_funding_from_the_canonical_notes_shape(tmp_path: Path) -> None:
    row = _route()
    quote_ts_us = int(time.time() * 1_000_000)
    row.update({
        "quote_ts_us": quote_ts_us,
        "executable_spread_pct": 1.0,
        "depth_weighted_spread_pct": 1.0,
        "notes": {
            "route_inputs": {
                "long": {"bid": 99, "ask": 100, "bid_vwap": 99, "ask_vwap": 100},
                "short": {"bid": 101, "ask": 102, "bid_vwap": 101, "ask_vwap": 102},
            },
            "funding": {
                "long": {"current_funding_pct": 0.01, "funding_interval_hours": 1},
                "short": {"current_funding_pct": -0.08, "funding_interval_hours": 4},
            },
        },
    })
    db_path = tmp_path / "history.sqlite3"
    assert market_history.record_route(row, db_path=db_path) == 1
    saved = market_history.load_history(route_key=row["route_key"], db_path=db_path)
    assert saved[0]["long_current_funding_pct"] == pytest.approx(0.01)
    assert saved[0]["short_funding_interval_hours"] == pytest.approx(4.0)


def test_history_reader_does_not_run_schema_writes_against_active_writer(tmp_path: Path) -> None:
    row = _route()
    row.update({
        "quote_ts_us": int(time.time() * 1_000_000),
        "executable_spread_pct": 1.0,
        "depth_weighted_spread_pct": 1.0,
    })
    db_path = tmp_path / "history.sqlite3"
    market_history.record_route(row, db_path=db_path)
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE route_points SET executable_spread_pct = 2")
        assert market_history.load_history(route_key=row["route_key"], db_path=db_path)
    finally:
        writer.rollback()
        writer.close()


def test_live_quote_survives_a_momentary_history_writer_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        market_history,
        "record_route",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )

    inserted, state = server._record_live_chart_route(_route())

    assert inserted == 0
    assert state == "history_write_deferred"


def test_history_bucketing_keeps_latest_sample_per_bucket(tmp_path: Path) -> None:
    route = _route()
    start_us = int(time.time() * 1_000_000) - 60_000_000
    db_path = tmp_path / "history.sqlite3"
    for offset_seconds, spread in ((1, 1.0), (5, 2.0), (12, 3.0), (19, 4.0)):
        row = {
            **route,
            "quote_ts_us": start_us + offset_seconds * 1_000_000,
            "executable_spread_pct": spread,
            "depth_weighted_spread_pct": spread,
            "notes": {
                "route_inputs": {
                "long": {"bid": 99, "ask": 100, "bid_vwap": 100, "ask_vwap": 100},
                "short": {"bid": 101, "ask": 102, "bid_vwap": 101, "ask_vwap": 102},
                }
            },
        }
        assert market_history.record_route(row, db_path=db_path) == 1

    saved = market_history.load_history(
        route_key=route["route_key"],
        since_us=start_us,
        bucket_seconds=10,
        max_points=10,
        db_path=db_path,
    )

    assert len(saved) in {2, 3}
    assert saved[-1]["executable_spread_pct"] == pytest.approx(4.0)
    assert [row["quote_ts_us"] for row in saved] == sorted(row["quote_ts_us"] for row in saved)


def test_fast_quote_refresh_preserves_broad_snapshot_freshness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    route = _route()
    route.update(
        {
            "depth_weighted_spread_pct": 2.0,
            "executable_spread_pct": 2.0,
            "blockers": [],
        }
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "updated_at": "2020-01-01T00:00:00Z",
                "expires_at": "2020-01-01T00:01:00Z",
                "api_discovered_rows": [route],
                "dex_discovered_rows": [],
            }
        ),
        encoding="utf-8",
    )
    refresher = FastQuoteRefresher()

    def quote_leg(_row: dict, side: str, **_kwargs: object) -> dict:
        return {
            "symbol": "TEST/USDT:USDT",
            "bid": 100.0 if side == "long" else 103.0,
            "ask": 101.0 if side == "long" else 104.0,
            "bid_vwap": 100.0 if side == "long" else 103.0,
            "ask_vwap": 101.0 if side == "long" else 104.0,
            "contract_size": 1.0,
            "quote_ts_us": 2_000_000,
        }

    monkeypatch.setattr(refresher, "_leg_quote", quote_leg)
    result = refresher.refresh(snapshot_path, route_limit=2)
    saved = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert result["updated_at"] != "2020-01-01T00:00:00Z"
    assert saved["updated_at"] == "2020-01-01T00:00:00Z"
    assert saved["expires_at"] == "2020-01-01T00:01:00Z"

    # The refreshed routes now land in a delta beside the snapshot instead of
    # rewriting it. Rewriting a 50-77MB file every 60s to change a few hundred
    # rows invalidated every cache and forced a full rebuild each time.
    delta = json.loads(
        (snapshot_path.with_name("api_discovery_fast_quotes.json")).read_text(encoding="utf-8")
    )
    assert delta["fast_quote_refresh"]["updated_at"] == result["updated_at"]
    assert delta["rows"], "the routes that were re-quoted must be in the delta"
    assert saved.get("fast_quote_refresh") is None, "the snapshot must not be rewritten"


def test_history_hides_legacy_dex_identity_outliers(tmp_path: Path) -> None:
    row = {
        **_route(),
        "route_key": "PTB|Bybit|Futures|OKX DEX 56|Spot",
        "token": "PTB",
        "route_kind": "DEX-FUTURES",
        "short_venue": "OKX DEX 56",
        "short_market_type": "Spot",
        "quote_ts_us": 2_000_000,
        "executable_spread_pct": 1.7,
        "depth_weighted_spread_pct": 1.6,
        "notes": {
            "route_inputs": {
                "long": {"bid": 0.000621, "ask": 0.000622},
                "short": {"bid": 0.000633, "ask": 0.000636},
            }
        },
    }
    db_path = tmp_path / "history.sqlite3"
    assert market_history.record_route(row, db_path=db_path) == 1

    connection = market_history._connect(db_path)
    try:
        connection.execute(
            """
            UPDATE route_points
            SET executable_spread_pct = 9821.0,
                depth_weighted_spread_pct = 9818.0
            WHERE route_key = ?
            """,
            (row["route_key"],),
        )
        connection.commit()
    finally:
        connection.close()

    assert (
        market_history.load_history(
        route_key=row["route_key"],
        db_path=db_path,
        )
        == []
    )


def test_fast_quote_refresh_covers_top_25_in_each_primary_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows: list[dict] = []
    for index in range(30):
        token = f"FUT{index:02d}"
        for route_index, short_venue in enumerate(("Mexc", "Bybit")):
            rows.append(
                {
                    **_route(),
                    "route_key": (f"{token}|Kucoin Futures|Futures|{short_venue}|Futures"),
                    "token": token,
                    "long_venue": "Kucoin Futures",
                    "short_venue": short_venue,
                    "long_market_symbol": f"{token}/USDT:USDT",
                    "short_market_symbol": f"{token}/USDT:USDT",
                    "depth_weighted_spread_pct": 30 - index - route_index / 10,
                    "executable_spread_pct": 30 - index - route_index / 10,
                    "blockers": [],
                }
            )
    for index in range(30):
        token = f"SPOT{index:02d}"
        for route_index, short_venue in enumerate(("Kucoin Futures", "Bybit")):
            rows.append(
                {
                    **_route(),
                    "route_key": (f"{token}|WhiteBIT|Spot|{short_venue}|Futures"),
                    "token": token,
                    "route_kind": "FUTURES-SPOT",
                    "long_venue": "WhiteBIT",
                    "long_market_type": "Spot",
                    "short_venue": short_venue,
                    "long_market_symbol": f"{token}/USDT",
                    "short_market_symbol": f"{token}/USDT:USDT",
                    "depth_weighted_spread_pct": 30 - index - route_index / 10,
                    "executable_spread_pct": 30 - index - route_index / 10,
                    "blockers": [],
                }
            )
    for index in range(80):
        token = f"CASH{index:02d}"
        rows.append(
            {
                **_route(),
                "route_key": f"{token}|Gate|Spot|Mexc|Spot",
                "token": token,
                "route_kind": "SPOT",
                "long_venue": "Gate",
                "long_market_type": "Spot",
                "short_venue": "Mexc",
                "short_market_type": "Spot",
                "long_market_symbol": f"{token}/USDT",
                "short_market_symbol": f"{token}/USDT",
                "depth_weighted_spread_pct": 80 - index,
                "executable_spread_pct": 80 - index,
                "blockers": [],
            }
        )
    for index in range(12):
        token = f"DEX{index:02d}"
        rows.append(
            {
                **_route(),
                "route_key": f"{token}|Bybit|Futures|OKX DEX 56|Spot",
                "token": token,
                "route_kind": "DEX-FUTURES",
                "long_venue": "Bybit",
                "short_venue": "OKX DEX 56",
                "short_market_type": "Spot",
                "long_market_symbol": f"{token}/USDT:USDT",
                "short_market_symbol": f"{token}/USDC",
                "notes": {
                    "identity": {
                        "short": {
                            "chain_id": "56",
                            "token_address": f"0x{index:040x}",
                        }
                    }
                },
                "depth_weighted_spread_pct": 30 - index,
                "executable_spread_pct": 30 - index,
                "blockers": [],
            }
        )
    for index in range(12):
        token = f"DEXCASH{index:02d}"
        rows.append(
            {
                **_route(),
                "route_key": f"{token}|Gate|Spot|OKX DEX 56|Spot",
                "token": token,
                "route_kind": "DEX-SPOT",
                "long_venue": "Gate",
                "long_market_type": "Spot",
                "short_venue": "OKX DEX 56",
                "short_market_type": "Spot",
                "long_market_symbol": f"{token}/USDT",
                "short_market_symbol": f"{token}/USDC",
                "notes": {
                    "identity": {
                        "short": {
                            "chain_id": "56",
                            "token_address": f"0x{index + 100:040x}",
                        }
                    }
                },
                "depth_weighted_spread_pct": 30 - index,
                "executable_spread_pct": 30 - index,
                "blockers": [],
            }
        )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "updated_at": "2020-01-01T00:00:00Z",
                "expires_at": "2020-01-01T00:01:00Z",
                "api_discovered_rows": rows,
                "dex_discovered_rows": [],
            }
        ),
        encoding="utf-8",
    )
    refresher = FastQuoteRefresher()

    def quote_leg(row: dict, side: str, **_kwargs: object) -> dict:
        symbol = row[f"{side}_market_symbol"]
        return {
            "symbol": symbol,
            "bid": 100.0 if side == "long" else 103.0,
            "ask": 101.0 if side == "long" else 104.0,
            "bid_vwap": 100.0 if side == "long" else 103.0,
            "ask_vwap": 101.0 if side == "long" else 104.0,
            "contract_size": 1.0,
            "quote_ts_us": 2_000_000,
        }

    monkeypatch.setattr(refresher, "_leg_quote", quote_leg)
    result = refresher.refresh(snapshot_path, route_limit=200)
    # Re-quoted routes are published as a delta rather than by rewriting the
    # snapshot, so lane coverage is asserted against that.
    saved = json.loads(
        (snapshot_path.with_name("api_discovery_fast_quotes.json")).read_text(encoding="utf-8")
    )
    updated = [row for row in saved["rows"] if row.get("fast_quote_verified_at")]

    assert result["selected_routes"] == 154
    assert result["updated_routes"] == 154
    assert sum(row["route_kind"] == "FUTURES" for row in updated) == 44
    assert sum(row["route_kind"] == "FUTURES-SPOT" for row in updated) == 43
    assert sum(row["route_kind"] == "SPOT" for row in updated) == 43
    assert sum(row["route_kind"] == "DEX-FUTURES" for row in updated) == 12
    assert sum(row["route_kind"] == "DEX-SPOT" for row in updated) == 12
    assert {row["token"] for row in updated if row["route_kind"] == "FUTURES"} == {
        f"FUT{index:02d}" for index in range(30)
    }
    assert {row["token"] for row in updated if row["route_kind"] == "FUTURES-SPOT"} == {
        f"SPOT{index:02d}" for index in range(30)
    }
    assert {row["token"] for row in updated if row["route_kind"] == "SPOT"} == {
        f"CASH{index:02d}" for index in range(43)
    }
    assert {row["token"] for row in updated if row["route_kind"] == "DEX-FUTURES"} == {
        f"DEX{index:02d}" for index in range(12)
    }
    assert {row["token"] for row in updated if row["route_kind"] == "DEX-SPOT"} == {
        f"DEXCASH{index:02d}" for index in range(12)
    }
    assert sum(row["token"] == "FUT00" and row["route_kind"] == "FUTURES" for row in updated) == 2
    assert (
        sum(row["token"] == "SPOT00" and row["route_kind"] == "FUTURES-SPOT" for row in updated)
        == 2
    )
    assert all(row["displayed_open_spread_pct"] == row["executable_spread_pct"] for row in updated)
    assert all(row["depth_unverified"] is False for row in updated)
    assert all("depth_unverified" not in row.get("blockers", []) for row in updated)


def test_aster_and_hyperliquid_futures_are_not_mislabeled_as_dex() -> None:
    row = _route()
    assert market_history.route_kind_for(row) == "FUTURES"
    row["long_venue"] = "Hyperliquid"
    assert market_history.route_kind_for(row) == "FUTURES"


def test_native_spot_and_futures_routes_sample_inside_web_process() -> None:
    assert server._native_chart_route(_route())
    gate_spot = {
        **_route(),
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "long_market_symbol": "TEST/USDT",
    }
    assert server._native_chart_route(gate_spot)
    kucoin_futures = {
        **_route(),
        "long_venue": "Kucoin Futures",
    }
    assert server._native_chart_route(kucoin_futures)


def test_live_chart_surface_explains_series_and_streams_exact_route() -> None:
    html = server.render_live_spread_chart(_route()["route_key"], [], "1h")

    assert "$50 VWAP" in html
    assert "Open ask → bid" in html
    assert "Out top book" in html
    assert "?live=1&amp;" not in html
    assert "?live=1&wait=0&hours=" in html
    assert "gap_threshold_seconds" in html
    assert "new EventSource" in html
    assert "/api/stream/" in html
    assert "setInterval(refresh, 5000)" in html
    assert "/assets/lightweight-charts.js" in html
    assert "bucket_seconds=${bucketSeconds}" in html
    assert "max_points=${maxPoints}" in html
    assert "subscribeCrosshairMove" in html
    assert "moveToPane(1)" in html
    assert "setHeight(" in html
    assert "setStretchFactor" not in html


def test_board_reads_live_when_only_the_delta_is_fresh(tmp_path: Path) -> None:
    """A fresh delta beside an old snapshot must read as live, not "reconnecting".

    The fast worker stopped rewriting the snapshot, so the snapshot's embedded
    `fast_quote_refresh` freezes at whenever the last discovery scan wrote it.
    Reading freshness from it told members the feed was reconnecting while the
    quotes on screen were a minute old, and dropped the page into a 5s reload.
    """
    from datetime import datetime, timedelta, timezone

    from spreadboard import api_spreads

    now = time.time()
    stale = (datetime.now(tz=timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    current = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    snapshot_path = tmp_path / "api_discovery_latest.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "updated_at": stale,
                "api_discovered_rows": [_route()],
                "dex_discovered_rows": [],
                "fast_quote_refresh": {
                    "status": "ok",
                    "updated_at": stale,
                    "updated_routes": 12,
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "api_discovery_fast_quotes.json").write_text(
        json.dumps(
            {
                "schema": "spreadboard.fast_quote_delta.v1",
                "updated_at": current,
                "fast_quote_refresh": {
                    "status": "ok",
                    "updated_at": current,
                    "updated_routes": 259,
                },
                "rows": [],
            }
        ),
        encoding="utf-8",
    )

    _rows, meta = api_spreads._load_api_discovery_rows(snapshot_path, now=now)

    assert meta["status"] == "fresh", meta
    assert meta["age_min"] is not None and meta["age_min"] < 5.0
    # The discovery scan really is three hours old; only the quote age is fresh.
    assert meta["discovery_age_min"] > 150.0


def test_fast_quote_refresh_writes_what_it_has_when_the_deadline_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cycle that runs long must still publish its quotes.

    The service runs this as a subprocess with a hard timeout and kills it on
    overrun, which discarded every quote taken that cycle -- the board reported
    `timeout, updated_routes: 0` and the delta went 32 minutes without moving.
    A deadline below the parent's kill time lets it stop and write.
    """
    routes = []
    for index in range(6):
        route = _route()
        route.update(
            {
                "route_key": f"TEST{index}|Aster|Futures|Bybit|Futures",
                "token": f"TEST{index}",
                "depth_weighted_spread_pct": 2.0,
                "executable_spread_pct": 2.0,
                "blockers": [],
            }
        )
        routes.append(route)

    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "updated_at": "2020-01-01T00:00:00Z",
                "api_discovered_rows": routes,
                "dex_discovered_rows": [],
            }
        ),
        encoding="utf-8",
    )

    refresher = FastQuoteRefresher()
    monkeypatch.setattr(refresher, "refresh_all_funding", lambda _payload: {"skipped": True})

    calls = {"n": 0}

    def slow_leg(_row: dict, side: str, **_kwargs: object) -> dict:
        calls["n"] += 1
        time.sleep(0.05)
        return {
            "symbol": "TEST/USDT:USDT",
            "bid": 100.0 if side == "long" else 103.0,
            "ask": 101.0 if side == "long" else 104.0,
            "bid_vwap": 100.0 if side == "long" else 103.0,
            "ask_vwap": 101.0 if side == "long" else 104.0,
            "contract_size": 1.0,
            "quote_ts_us": 2_000_000,
        }

    monkeypatch.setattr(refresher, "_leg_quote", slow_leg)

    # Far too little time to quote every leg.
    result = refresher.refresh(snapshot_path, route_limit=12, deadline_seconds=0.12)

    # It returned rather than running to completion, and it still produced a
    # summary the caller can act on instead of being killed with nothing.
    assert isinstance(result, dict)
    assert calls["n"] < 12, "the deadline did not stop the cycle early"


def test_fast_quote_refresh_publishes_completed_routes_before_slow_batches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A completed quote must not wait behind an unrelated slow venue batch."""

    from threading import Event, Thread

    routes = []
    for token, venue in (("FAST", "Aster"), ("SLOW", "Bybit")):
        route = _route()
        route.update(
            {
                "route_key": f"{token}|{venue}|Futures|{venue}|Futures",
                "token": token,
                "long_venue": venue,
                "short_venue": venue,
                "long_market_symbol": f"{token}/USDT:USDT",
                "short_market_symbol": f"{token}/USDT:USDT",
                "depth_weighted_spread_pct": 2.0,
                "blockers": [],
            }
        )
        routes.append(route)

    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "updated_at": "2020-01-01T00:00:00Z",
                "api_discovered_rows": routes,
                "dex_discovered_rows": [],
            }
        ),
        encoding="utf-8",
    )
    slow_started = Event()
    release_slow = Event()
    refresher = FastQuoteRefresher()
    monkeypatch.setattr(fast_quotes, "_external_funding_is_fresh", lambda: True)
    monkeypatch.setattr(fast_quotes.public_rails, "load_public_rails", lambda: {})
    monkeypatch.setattr(fast_quotes.token_metadata, "load_token_metadata", lambda: {})
    monkeypatch.setattr(
        fast_quotes,
        "_is_dex_route",
        lambda row: row.get("token") == "FAST",
    )

    def quote_batch(venue_key, jobs, **_kwargs):
        if venue_key[0] == "Bybit":
            slow_started.set()
            assert release_slow.wait(3.0)
        stamp = int(time.time() * 1_000_000)
        return {
            key: {
                "symbol": row[f"{side}_market_symbol"],
                "bid": 103.0,
                "ask": 101.0,
                "bid_vwap": 103.0,
                "ask_vwap": 101.0,
                "contract_size": 1.0,
                "quote_ts_us": stamp,
            }
            for key, row, side in jobs
        }

    monkeypatch.setattr(refresher, "_quote_venue_jobs", quote_batch)
    result: dict[str, object] = {}

    def run() -> None:
        # Three CEX lanes share the budget; six leaves two slots for FUTURES.
        result.update(refresher.refresh(snapshot_path, route_limit=6))

    worker = Thread(target=run)
    worker.start()
    assert slow_started.wait(2.0)
    delta_path = snapshot_path.with_name("api_discovery_fast_quotes.json")
    for _ in range(100):
        if delta_path.exists():
            partial = json.loads(delta_path.read_text(encoding="utf-8"))
            if partial.get("fast_quote_refresh", {}).get("updated_routes") == 1:
                break
        time.sleep(0.01)
    else:
        raise AssertionError("completed fast route was not published while slow batch waited")

    assert partial["fast_quote_refresh"]["cycle_complete"] is False
    fast = next(row for row in partial["rows"] if row["token"] == "FAST")
    slow = next(row for row in partial["rows"] if row["token"] == "SLOW")
    assert fast["fast_quote_verified_at"]
    assert not slow.get("fast_quote_verified_at")

    release_slow.set()
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    complete = json.loads(delta_path.read_text(encoding="utf-8"))
    assert complete["fast_quote_refresh"]["cycle_complete"] is True
    assert complete["fast_quote_refresh"]["updated_routes"] == 2
    assert result["updated_routes"] == 2


def test_a_custom_pair_prices_from_the_books_not_the_board(monkeypatch) -> None:
    """A custom chart is not on the board, so it has no board price.

    The ccxt fallback cannot cover it either -- loading a venue's markets cold
    costs ~32s against a 6s enrichment budget -- so the operator's own SKHY/SKHX
    position charted as an empty page.
    """
    from spreadboard import live

    from spreadboard.live_book_cache import CachedBook

    seen = {}

    class Store:
        def get(self, venue, market_type, symbol, *, max_age_seconds=5.0):
            assert (venue, market_type) == ("Hyperliquid", "Futures")
            seen["max_age_seconds"] = max_age_seconds
            return CachedBook(bids=[[1052.6, 4.0]], asks=[[1052.8, 4.0]], quote_ts_us=1)

    from spreadboard import live_book_cache

    monkeypatch.setattr(live_book_cache, "LiveBookStore", Store)

    quote = live.book_quote("Hyperliquid", "Futures", "XYZ-SKHX/USDC:USDC")

    assert quote["bid"] == 1052.6
    assert quote["ask"] == 1052.8
    assert quote["price"] == pytest.approx(1052.7)
    assert quote["source"] == "live_book"
    # Not the store's five-second default, which would call every swept book
    # absent: the sweep refreshes a venue roughly every 230 seconds.
    from spreadboard import api_spreads

    assert seen["max_age_seconds"] == api_spreads.LIVE_BOOK_MAX_AGE_SECONDS
    assert seen["max_age_seconds"] > 5.0


def test_book_quote_is_silent_when_there_is_no_book(monkeypatch) -> None:
    from spreadboard import live

    assert live.book_quote(None, "Futures", "X/Y") == {}
    assert live.book_quote("Hyperliquid", None, "X/Y") == {}
    assert live.book_quote("Hyperliquid", "Futures", None) == {}


def test_a_fixed_ratio_pair_is_compared_in_the_same_unit(monkeypatch) -> None:
    """SKHY and SKHX are the same asset at ten to one.

    Carried as a display label only, the chart read +7572% where the real
    dislocation is about 23%.
    """
    from spreadboard import live
    from spreadboard.live_book_cache import CachedBook

    prices = {
        "XYZ-SKHX/USDC:USDC": 1164.95,
        "XYZ-SKHY/USDC:USDC": 151.83,
    }

    class Store:
        def get(self, venue, market_type, symbol, *, max_age_seconds=5.0):
            price = prices[symbol]
            return CachedBook(bids=[[price, 0.0]], asks=[[price, 0.0]], quote_ts_us=1)

    from spreadboard import live_book_cache

    monkeypatch.setattr(live_book_cache, "LiveBookStore", Store)

    row = {
        "symbol": "SKHY",
        "long_venue": "Hyperliquid",
        "long_market_type": "Futures",
        "long_market_symbol": "XYZ-SKHX/USDC:USDC",
        "short_venue": "Hyperliquid",
        "short_market_type": "Futures",
        "short_market_symbol": "XYZ-SKHY/USDC:USDC",
        # One SKHX is worth ten SKHY, so SKHY carries the x10.
        "notes": {"relative_value": {"long_multiplier": 1.0, "short_multiplier": 10.0}},
    }

    long_leg = live._leg_detail_from_board(row, side="long")
    short_leg = live._leg_detail_from_board(row, side="short")

    assert long_leg["price"] == pytest.approx(1164.95)
    assert short_leg["price"] == pytest.approx(1518.3)

    spread = (long_leg["price"] / short_leg["price"] - 1) * 100
    assert -30 < spread < -15, f"normalized spread should be ~-23%, got {spread:.1f}%"


def test_no_ratio_leaves_the_price_alone(monkeypatch) -> None:
    from spreadboard import live
    from spreadboard.live_book_cache import CachedBook

    class Store:
        def get(self, venue, market_type, symbol, *, max_age_seconds=5.0):
            return CachedBook(bids=[[100.0, 0.0]], asks=[[100.0, 0.0]], quote_ts_us=1)

    from spreadboard import live_book_cache

    monkeypatch.setattr(live_book_cache, "LiveBookStore", Store)

    row = {
        "symbol": "AAA",
        "long_venue": "Hyperliquid",
        "long_market_type": "Futures",
        "long_market_symbol": "A/USDC:USDC",
    }
    assert live._leg_detail_from_board(row, side="long")["price"] == pytest.approx(100.0)
