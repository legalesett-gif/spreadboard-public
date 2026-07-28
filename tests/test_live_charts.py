from __future__ import annotations

from pathlib import Path
import time

import pytest

from spreadboard import market_history, server
from spreadboard.fast_quotes import FastQuoteRefresher


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
    monkeypatch.setattr(
        refresher,
        "_leg_quote",
        lambda _row, side, **_kwargs: quotes[side],
    )

    result = refresher.quote_route(_route(), target_notional_usd=50.0)

    assert result["status"] == "ok"
    assert result["row"]["executable_spread_pct"] == pytest.approx(10.0)
    assert result["row"]["depth_weighted_spread_pct"] == pytest.approx((108 / 101 - 1) * 100)
    assert result["row"]["quote_ts_us"] == 2_000_000


def test_history_persists_entry_matched_exit_and_sample_provenance(tmp_path: Path) -> None:
    row = _route()
    quote_ts_us = int(time.time() * 1_000_000)
    row.update(
        {
            "quote_ts_us": quote_ts_us,
            "executable_spread_pct": 10.0,
            "depth_weighted_spread_pct": 6.93,
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
    assert saved[0]["depth_weighted_spread_pct"] == pytest.approx(6.93)
    assert saved[0]["exit_spread_pct"] == pytest.approx((99 / 112 - 1) * 100)
    assert saved[0]["sample_source"] == "live_chart_exact_route"
    assert saved[0]["target_notional_usd"] == 50.0
    assert saved[0]["long_current_funding_pct"] == pytest.approx(0.01)
    assert saved[0]["short_current_funding_pct"] == pytest.approx(-0.08)
    assert saved[0]["short_funding_interval_hours"] == pytest.approx(4.0)


def test_aster_and_hyperliquid_futures_are_not_mislabeled_as_dex() -> None:
    row = _route()
    assert market_history.route_kind_for(row) == "FUTURES"
    row["long_venue"] = "Hyperliquid"
    assert market_history.route_kind_for(row) == "FUTURES"


def test_live_chart_surface_explains_series_and_polls_exact_route() -> None:
    html = server.render_live_spread_chart(_route()["route_key"], [], "1h")

    assert "In $50 VWAP" in html
    assert "In top book" in html
    assert "Out top book" in html
    assert "?live=1&amp;" not in html
    assert "?live=1&hours=" in html
    assert "gap_threshold_seconds" in html
    assert "setInterval(refresh, 15000)" in html
