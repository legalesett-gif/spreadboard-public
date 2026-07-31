from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from spreadboard import chart_catalog, historical_spreads, market_history, server


def test_custom_chart_route_round_trip() -> None:
    long_leg = {"venue": "Binance", "market_type": "Spot", "symbol": "COTI/USDT"}
    short_leg = {"venue": "Aster", "market_type": "Futures", "symbol": "COTI/USDT:USDT"}
    key = chart_catalog.custom_route_key("coti", long_leg, short_leg)
    row = chart_catalog.route_from_key(key)
    assert row is not None
    assert row["token"] == "COTI"
    assert row["route_kind"] == "FUTURES-SPOT"
    assert row["long_market_symbol"] == "COTI/USDT"
    assert row["short_market_symbol"] == "COTI/USDT:USDT"
    assert row["blockers"] == ["custom_chart_research_only"]


def test_custom_chart_route_rejects_unknown_venue() -> None:
    key = chart_catalog.custom_route_key(
        "TEST",
        {"venue": "Unknown", "market_type": "Spot", "symbol": "TEST/USDT"},
        {"venue": "Binance", "market_type": "Futures", "symbol": "TEST/USDT:USDT"},
    )
    assert chart_catalog.route_from_key(key) is None


def test_historical_spread_aligns_matching_candles_only() -> None:
    long_rows = [[0, 0, 0, 0, 100], [60_000, 0, 0, 0, 105], [120_000, 0, 0, 0, 110]]
    short_rows = [[0, 0, 0, 0, 110], [120_000, 0, 0, 0, 121]]
    rows = historical_spreads._align(long_rows, short_rows, "1m")
    assert len(rows) == 2
    assert rows[0]["executable_spread_pct"] == pytest.approx(10)
    assert rows[1]["executable_spread_pct"] == pytest.approx(10)
    assert rows[0]["sample_source"] == "historical_ohlcv_close_proxy"
    assert rows[0]["depth_weighted_spread_pct"] is None


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


def test_one_day_chart_budget_can_hold_every_minute() -> None:
    config = server.chart_window_config("1d")
    assert config["hours"] == 24
    assert config["max_points"] >= 24 * 60
