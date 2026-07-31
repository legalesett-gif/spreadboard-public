from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from spreadboard import market_history, server
from spreadboard.fast_quotes import (
    FastQuoteRefresher,
    _expanded_token_rows,
    _fast_quote_lane,
    _has_permanent_mirage_guard,
    _native_spot_order_book,
    _native_current_funding,
    _okx_dex_leg_quote,
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
    assert not _has_permanent_mirage_guard(
        {"blockers": ["mirage_guard:fast_requote_pending"]}
    )
    assert not _has_permanent_mirage_guard(
        {"blockers": ["mirage_guard:fast_requote_unavailable"]}
    )
    assert not _has_permanent_mirage_guard(
        {"blockers": ["mirage_guard:spot_sell_inventory_required"]}
    )


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

    assert _fast_quote_lane(futures) == "FUTURES"
    assert _fast_quote_lane(futures_spot) == "FUTURES-SPOT"
    assert _fast_quote_lane(spot) == "SPOT"
    assert _fast_quote_lane(dex) == "DEX-FUTURES"
    assert (
        _fast_quote_lane({**dex, "blockers": ["cex_identity_unverified"]})
        is None
    )
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
    assert set(calls) == {("long", True), ("short", True)}


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

    assert calls == 2


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
    assert saved[0]["depth_weighted_spread_pct"] == pytest.approx(
        (108 / 101 - 1) * 100
    )
    assert saved[0]["exit_spread_pct"] == pytest.approx((99 / 112 - 1) * 100)
    assert saved[0]["long_ask_vwap_price"] == pytest.approx(101.0)
    assert saved[0]["short_bid_vwap_price"] == pytest.approx(108.0)
    assert saved[0]["sample_source"] == "live_chart_exact_route"
    assert saved[0]["target_notional_usd"] == 50.0
    assert saved[0]["long_current_funding_pct"] == pytest.approx(0.01)
    assert saved[0]["short_current_funding_pct"] == pytest.approx(-0.08)
    assert saved[0]["short_funding_interval_hours"] == pytest.approx(4.0)
    assert _formula_errors(saved[0]) == []


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
    assert saved["fast_quote_refresh"]["updated_at"] == result["updated_at"]


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

    assert market_history.load_history(
        route_key=row["route_key"],
        db_path=db_path,
    ) == []


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
                    "route_key": (
                        f"{token}|Kucoin Futures|Futures|{short_venue}|Futures"
                    ),
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
                    "route_key": (
                        f"{token}|WhiteBIT|Spot|{short_venue}|Futures"
                    ),
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
    for index in range(30):
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
                "depth_weighted_spread_pct": 30 - index,
                "executable_spread_pct": 30 - index,
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
    result = refresher.refresh(snapshot_path, route_limit=120)
    saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
    updated = [
        row
        for row in saved["api_discovered_rows"]
        if row.get("fast_quote_verified_at")
    ]

    assert result["selected_routes"] == 97
    assert result["updated_routes"] == 97
    assert sum(row["route_kind"] == "FUTURES" for row in updated) == 30
    assert sum(row["route_kind"] == "FUTURES-SPOT" for row in updated) == 30
    assert sum(row["route_kind"] == "SPOT" for row in updated) == 25
    assert sum(row["route_kind"] == "DEX-FUTURES" for row in updated) == 12
    assert {row["token"] for row in updated if row["route_kind"] == "FUTURES"} == {
        f"FUT{index:02d}" for index in range(25)
    }
    assert {
        row["token"] for row in updated if row["route_kind"] == "FUTURES-SPOT"
    } == {f"SPOT{index:02d}" for index in range(25)}
    assert {row["token"] for row in updated if row["route_kind"] == "SPOT"} == {
        f"CASH{index:02d}" for index in range(25)
    }
    assert {
        row["token"] for row in updated if row["route_kind"] == "DEX-FUTURES"
    } == {f"DEX{index:02d}" for index in range(12)}
    assert sum(
        row["token"] == "FUT00" and row["route_kind"] == "FUTURES"
        for row in updated
    ) == 2
    assert sum(
        row["token"] == "SPOT00" and row["route_kind"] == "FUTURES-SPOT"
        for row in updated
    ) == 2


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
    assert not server._native_chart_route(kucoin_futures)


def test_live_chart_surface_explains_series_and_streams_exact_route() -> None:
    html = server.render_live_spread_chart(_route()["route_key"], [], "1h")

    assert "$50 VWAP" in html
    assert "Open ask → bid" in html
    assert "Out top book" in html
    assert "?live=1&amp;" not in html
    assert "?live=1&hours=" in html
    assert "gap_threshold_seconds" in html
    assert "new EventSource" in html
    assert "/api/stream/" in html
    assert "setInterval(refresh, 5000)" in html
    assert "/assets/lightweight-charts.js" in html
    assert "max_points=25000" in html
    assert "subscribeCrosshairMove" in html
    assert "moveToPane(1)" in html
    assert "setHeight(" in html
    assert "setStretchFactor" not in html
