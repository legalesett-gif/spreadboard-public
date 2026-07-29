from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from spreadboard import market_history, server
from spreadboard.fast_quotes import (
    FastQuoteRefresher,
    _expanded_token_rows,
    _has_permanent_mirage_guard,
)


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
    assert not _has_permanent_mirage_guard(
        {"blockers": ["mirage_guard:fast_requote_pending"]}
    )
    assert not _has_permanent_mirage_guard(
        {"blockers": ["mirage_guard:fast_requote_unavailable"]}
    )
    assert _has_permanent_mirage_guard(
        {"blockers": ["mirage_guard:spot_sell_inventory_required"]}
    )


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
    assert set(calls) == {("long", False), ("short", False)}


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


def test_fast_quote_refresh_advances_canonical_heartbeat(
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
    assert saved["updated_at"] == result["updated_at"]
    assert saved["updated_at"] != "2020-01-01T00:00:00Z"
    assert saved["expires_at"] > saved["updated_at"]


def test_fast_quote_refresh_covers_top_25_in_each_primary_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows: list[dict] = []
    for index in range(30):
        token = f"FUT{index:02d}"
        rows.append(
            {
                **_route(),
                "route_key": f"{token}|Aster|Futures|Bybit|Futures",
                "token": token,
                "long_market_symbol": f"{token}/USDT:USDT",
                "short_market_symbol": f"{token}/USDT:USDT",
                "depth_weighted_spread_pct": 30 - index,
                "executable_spread_pct": 30 - index,
                "blockers": [],
            }
        )
    for index in range(30):
        token = f"SPOT{index:02d}"
        rows.append(
            {
                **_route(),
                "route_key": f"{token}|Gate|Spot|Bybit|Futures",
                "token": token,
                "route_kind": "FUTURES-SPOT",
                "long_venue": "Gate",
                "long_market_type": "Spot",
                "long_market_symbol": f"{token}/USDT",
                "short_market_symbol": f"{token}/USDT:USDT",
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
    result = refresher.refresh(snapshot_path)
    saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
    updated = [
        row
        for row in saved["api_discovered_rows"]
        if row.get("fast_quote_verified_at")
    ]

    assert result["selected_routes"] == 50
    assert result["updated_routes"] == 50
    assert sum(row["route_kind"] == "FUTURES" for row in updated) == 25
    assert sum(row["route_kind"] == "FUTURES-SPOT" for row in updated) == 25
    assert {row["token"] for row in updated if row["route_kind"] == "FUTURES"} == {
        f"FUT{index:02d}" for index in range(25)
    }
    assert {
        row["token"] for row in updated if row["route_kind"] == "FUTURES-SPOT"
    } == {f"SPOT{index:02d}" for index in range(25)}


def test_aster_and_hyperliquid_futures_are_not_mislabeled_as_dex() -> None:
    row = _route()
    assert market_history.route_kind_for(row) == "FUTURES"
    row["long_venue"] = "Hyperliquid"
    assert market_history.route_kind_for(row) == "FUTURES"


def test_live_chart_surface_explains_series_and_polls_exact_route() -> None:
    html = server.render_live_spread_chart(_route()["route_key"], [], "1h")

    assert "$50 VWAP" in html
    assert "Open ask → bid" in html
    assert "Out top book" in html
    assert "?live=1&amp;" not in html
    assert "?live=1&hours=" in html
    assert "gap_threshold_seconds" in html
    assert "setInterval(refresh, 5000)" in html
    assert "/assets/lightweight-charts.js" in html
    assert "max_points=25000" in html
    assert "subscribeCrosshairMove" in html
    assert "moveToPane(1)" in html
    assert "setHeight(" in html
    assert "setStretchFactor" not in html
