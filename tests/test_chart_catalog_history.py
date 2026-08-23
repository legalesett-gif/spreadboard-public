from __future__ import annotations

from datetime import datetime, timezone
import json
import os

import pytest

from spreadboard import chart_catalog, historical_spreads, market_history, server


def test_catalog_load_is_cached_until_the_atomic_artifact_changes(tmp_path, monkeypatch) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"markets": [{"token": "ONE", "venue": "A", "market_type": "Spot", "symbol": "ONE/USDT"}]}))
    monkeypatch.setattr(chart_catalog, "dex_market_entries", lambda: [])
    chart_catalog._LOAD_CACHE.update({"key": None, "payload": None})

    first = chart_catalog.load(path)
    second = chart_catalog.load(path)
    assert second is first

    path.write_text(json.dumps({"markets": [{"token": "TWO", "venue": "A", "market_type": "Spot", "symbol": "TWO/USDT"}]}))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    refreshed = chart_catalog.load(path)
    assert refreshed is not first
    assert refreshed["markets"][0]["token"] == "TWO"


def test_custom_chart_route_round_trip() -> None:
    long_leg = {"venue": "Binance", "market_type": "Spot", "symbol": "COTI/USDT"}
    short_leg = {"venue": "Aster", "market_type": "Futures", "symbol": "COTI/USDT:USDT"}
    key = chart_catalog.custom_route_key("coti", long_leg, short_leg)
    row = chart_catalog.route_from_key(key)
    assert row is not None
    assert row["token"] == "COTI"
    assert row["route_kind"] == "SPOT-FUTURES"
    assert row["long_market_symbol"] == "COTI/USDT"
    assert row["short_market_symbol"] == "COTI/USDT:USDT"
    assert row["blockers"] == ["custom_chart_research_only"]


def test_skhx_skhynix_route_is_explicitly_normalized_ten_to_one() -> None:
    row = chart_catalog.route_from_key(chart_catalog.skhx_skhynix_route_key())

    assert row is not None
    assert row["token"] == "SKHX / SK HYNIX"
    assert row["long_market_symbol"] == "XYZ-SKHX/USDC:USDC"
    assert row["short_market_symbol"] == "XYZ-SKHY/USDC:USDC"
    assert row["notes"]["relative_value"] == {
        "long_multiplier": 1.0,
        "short_multiplier": 10.0,
    }


def test_custom_chart_route_rejects_unknown_venue() -> None:
    key = chart_catalog.custom_route_key(
        "TEST",
        {"venue": "Unknown", "market_type": "Spot", "symbol": "TEST/USDT"},
        {"venue": "Binance", "market_type": "Futures", "symbol": "TEST/USDT:USDT"},
    )
    assert chart_catalog.route_from_key(key) is None


def test_identity_verified_dex_can_be_the_long_custom_chart_leg() -> None:
    dex = next(
        item for item in chart_catalog.dex_market_entries()
        if item["token"] == "ESPORTS" and item["venue"] == "OKX DEX 56"
    )
    key = chart_catalog.custom_route_key(
        "ESPORTS",
        dex,
        {"venue": "Mexc", "market_type": "Futures", "symbol": "ESPORTS/USDT:USDT"},
    )

    row = chart_catalog.route_from_key(key)

    assert row is not None
    assert row["route_kind"] == "DEX-FUTURES"
    assert row["long_venue"] == "OKX DEX 56"
    assert row["dex_chain"] == "56"
    assert row["dex_contract"] == "0xf39e4b21c84e737df08e2c3b32541d856f508e48"
    assert row["notes"]["identity"]["long"]["token_address"] == row["dex_contract"]


def test_gua_dex_identity_remains_available_when_the_live_rate_cools() -> None:
    dex = next(
        item
        for item in chart_catalog.dex_market_entries()
        if item["token"] == "GUA" and item["venue"] == "OKX DEX 56"
    )

    assert dex["dex_contract"] == "0xa5c8e1513b6a08334b479fe4d71f1253259469be"
    key = chart_catalog.custom_route_key(
        "GUA",
        dex,
        {"venue": "Aster", "market_type": "Futures", "symbol": "GUA/USDT:USDT"},
    )
    row = chart_catalog.route_from_key(key)
    assert row is not None
    assert row["long_venue"] == "OKX DEX 56"
    assert row["route_kind"] == "DEX-FUTURES"


def test_custom_dex_leg_rejects_a_ticker_with_the_wrong_contract() -> None:
    key = chart_catalog.custom_route_key(
        "ESPORTS",
        {
            "token": "ESPORTS",
            "venue": "OKX DEX 56",
            "market_type": "Spot",
            "symbol": "ESPORTS",
            "dex_chain": "56",
            "dex_contract": "0x0000000000000000000000000000000000000000",
        },
        {"venue": "Mexc", "market_type": "Futures", "symbol": "ESPORTS/USDT:USDT"},
    )

    assert chart_catalog.route_from_key(key) is None


def test_chart_builder_keeps_dex_identity_in_the_signed_route_key() -> None:
    dex = next(item for item in chart_catalog.dex_market_entries() if item["token"] == "ESPORTS")
    html = server.render_chart_builder_script([dex], None)

    assert '"venue": "OKX DEX 56"' in html
    assert '"dex_chain": "56"' in html
    assert '"dex_contract": "0xf39e4b21c84e737df08e2c3b32541d856f508e48"' in html
    assert "dex_chain:leg.dex_chain" in html


def test_chart_builder_can_prefill_a_requested_token_without_selecting_a_route() -> None:
    html = server.render_chart_builder(
        [],
        None,
        {"token_count": 1, "count": 2},
        selected_token="ESPORTS",
    )

    assert 'data-chart-token list="chart-token-list" value="ESPORTS"' in html


def test_chart_page_prefills_token_when_persisted_catalog_has_no_tokens_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "api_source_health", lambda *_args: {"ok": True})
    monkeypatch.setattr(
        server.chart_catalog,
        "load",
        lambda: {
            "token_count": 1,
            "count": 2,
            "markets": [
                {"token": "ESPORTS", "venue": "OKX DEX 56", "market_type": "Spot", "symbol": "ESPORTS"},
                {"token": "ESPORTS", "venue": "Mexc", "market_type": "Futures", "symbol": "ESPORTS/USDT:USDT"},
            ],
        },
    )

    html = server.render_charts_page(None, {}, {"token": ["ESPORTS"]})

    assert 'data-chart-token list="chart-token-list" value="ESPORTS"' in html
    assert '"venue": "OKX DEX 56"' in html


def test_funding_history_dialog_overrides_global_table_minimum_width() -> None:
    html = server.shell("Charts", "charts", "")
    assert "/assets/app.css" in html

    assert ".funding-history-dialog table { width: 100%; min-width: 0;" in server.APP_CSS


def test_historical_spread_aligns_matching_candles_only() -> None:
    long_rows = [[0, 0, 0, 0, 100], [60_000, 0, 0, 0, 105], [120_000, 0, 0, 0, 110]]
    short_rows = [[0, 0, 0, 0, 110], [120_000, 0, 0, 0, 121]]
    rows = historical_spreads._align(long_rows, short_rows, "1m")
    assert len(rows) == 2
    assert rows[0]["executable_spread_pct"] == pytest.approx(10)
    assert rows[1]["executable_spread_pct"] == pytest.approx(10)
    assert rows[0]["sample_source"] == "historical_ohlcv_close_proxy"
    assert rows[0]["depth_weighted_spread_pct"] is None


def test_historical_relative_value_spread_applies_leg_multipliers() -> None:
    rows = historical_spreads._align(
        [[0, 0, 0, 0, 100]],
        [[0, 0, 0, 0, 13]],
        "1m",
        short_multiplier=10,
    )

    assert rows[0]["long_price"] == 100
    assert rows[0]["short_price"] == 130
    assert rows[0]["executable_spread_pct"] == pytest.approx(30)


def test_relative_value_history_normalizes_prices_and_exit(tmp_path) -> None:
    row = chart_catalog.route_from_key(chart_catalog.skhx_skhynix_route_key())
    assert row is not None
    row.update(
        {
            "quote_ts_us": int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000),
            "executable_spread_pct": 29.9,
        }
    )
    row["notes"]["route_inputs"]["long"].update({"bid": 99, "ask": 100})
    row["notes"]["route_inputs"]["short"].update({"bid": 13, "ask": 13.1})

    market_history.record_route(row, db_path=tmp_path / "history.sqlite3")
    point = market_history.load_history(
        route_key=row["route_key"], db_path=tmp_path / "history.sqlite3"
    )[0]

    assert point["long_ask_price"] == 100
    assert point["short_bid_price"] == 130
    assert point["exit_spread_pct"] == pytest.approx((99 / 131 - 1) * 100)


def test_history_preserves_custom_route_key(tmp_path) -> None:
    key = chart_catalog.custom_route_key(
        "BTC",
        {"venue": "Aster", "market_type": "Futures", "symbol": "BTC/USDT:USDT"},
        {"venue": "Hyperliquid", "market_type": "Futures", "symbol": "BTC/USDC:USDC"},
    )
    row = chart_catalog.route_from_key(key)
    assert row is not None
    row.update(
        {
            "quote_ts_us": int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000),
            "executable_spread_pct": 1.0,
            "depth_weighted_spread_pct": 0.9,
        }
    )
    assert market_history.record_route(row, db_path=tmp_path / "history.sqlite3") == 1
    history = market_history.load_history(route_key=key, db_path=tmp_path / "history.sqlite3")
    assert len(history) == 1
    assert history[0]["route_key"] == key


def test_catalog_refresh_retains_last_successful_venue(tmp_path, monkeypatch) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-31T10:00:00Z",
                "markets": [
                    {
                        "token": "BTC",
                        "venue": "Gate",
                        "market_type": "Spot",
                        "symbol": "BTC/USDT",
                    }
                ],
            }
        )
    )

    def fake_load(venue: str, market_type: str):
        if venue == "Gate" and market_type == "Spot":
            raise RuntimeError("temporary_timeout")
        return []

    monkeypatch.setattr(chart_catalog, "_load_job_subprocess", fake_load)
    payload = chart_catalog.refresh(path=path, workers=2)
    retained = [item for item in payload["markets"] if item.get("venue") == "Gate"]
    assert retained == [
        {"token": "BTC", "venue": "Gate", "market_type": "Spot", "symbol": "BTC/USDT"}
    ]
    assert payload["health"]["Gate|Spot"]["status"] == "stale_cache"
    assert payload["health"]["Gate|Spot"]["catalogued_at"] == "2026-07-31T10:00:00Z"


def test_catalog_excludes_inverse_perpetuals_from_stablecoin_chart_path() -> None:
    inverse = {"active": True, "swap": True, "quote": "USD", "settle": "BTC"}
    linear = {"active": True, "swap": True, "quote": "USDT", "settle": "USDT"}

    assert not chart_catalog._catalog_market_supported(inverse, "Futures")
    assert chart_catalog._catalog_market_supported(linear, "Futures")


def test_one_day_chart_budget_can_hold_every_minute() -> None:
    config = server.chart_window_config("1d")
    assert config["hours"] == 24
    assert config["max_points"] >= 24 * 60


def test_historical_cache_is_not_poisoned_by_one_point_stream_request(monkeypatch, tmp_path) -> None:
    now = datetime.now(tz=timezone.utc).timestamp()
    rows = [
        {"quote_ts_us": index * 60_000_000, "sample_source": "historical_ohlcv_close_proxy"}
        for index in range(1440)
    ]
    cached = {"status": "ok", "cached_at": now, "rows": rows}
    monkeypatch.setattr(historical_spreads, "_cache_path", lambda *_: tmp_path / "cached.json")
    monkeypatch.setattr(historical_spreads, "_read_cache", lambda *_: cached)
    route = {"route_key": "route", "long_venue": "Aster", "short_venue": "Binance"}

    latest = historical_spreads.load_or_fetch(route, hours=24, max_points=1)
    full = historical_spreads.load_or_fetch(route, hours=24, max_points=1440)

    assert latest["rows"] == [rows[-1]]
    assert full["rows"] == rows


def test_history_merge_preserves_full_window_and_prefers_exact_bucket() -> None:
    minute = 60_000_000
    proxy = [
        {"quote_ts_us": index * minute, "sample_source": "historical_ohlcv_close_proxy"}
        for index in range(1440)
    ]
    exact = [
        {"quote_ts_us": (1439 * minute) + 30_000_000, "sample_source": "exact_public_order_book"}
    ]

    rows = server._merge_history_rows(
        proxy,
        exact,
        since_us=0,
        max_points=1200,
        bucket_seconds=60,
    )

    assert len(rows) == 1200
    assert rows[0]["quote_ts_us"] == 0
    assert rows[-1] == exact[0]
    assert (rows[-1]["quote_ts_us"] - rows[0]["quote_ts_us"]) / 3_600_000_000 > 23.9


def test_position_suggestions_use_canonical_live_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **_: {"rows": [{
            "token": "COTI", "route_key": "route", "route_kind": "FUTURES-SPOT",
            "long_venue": "Gate", "long_market_type": "Spot", "long_market_symbol": "COTI/USDT",
            "long_ask": 0.01, "short_venue": "Bybit", "short_market_type": "Futures",
            "short_market_symbol": "COTI/USDT:USDT", "short_bid": 0.011,
            "depth_weighted_spread_pct": 9.8, "funding_24h_pct": -1.2, "age_min": 0.1,
        }]},
    )
    monkeypatch.setattr(
        server.chart_catalog,
        "load",
        lambda: {"generated_at": "now", "markets": [
            {"token": "COTI", "venue": "Gate", "market_type": "Spot", "symbol": "COTI/USDT"},
            {"token": "COTI", "venue": "Bybit", "market_type": "Futures", "symbol": "COTI/USDT:USDT"},
        ]},
    )
    result = server.api_position_suggestions(tmp_path / "board", {"q": ["COTI"]})
    assert result["tokens"] == ["COTI"]
    assert result["routes"][0]["source"] == "live public books"
    assert result["routes"][0]["long_entry_price"] == 0.01
    assert result["routes"][0]["short_entry_price"] == 0.011
    assert {item["venue"] for item in result["legs"]} == {"Gate", "Bybit"}

    empty = server.api_position_suggestions(tmp_path / "board", {})
    assert empty["routes"] == []
    assert empty["legs"] == []
