"""Opening a chart by route must not rebuild the whole board.

_find_canonical_route built the entire twelve-thousand-row board and scanned it
for one key, costing 14.6s on every chart opened by route -- most of the thirty
seconds a member waited.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from spreadboard import server


def test_a_legacy_request_builds_one_token_then_reuses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = []

    def build(**_kwargs):
        builds.append(1)
        return {"rows": [{"route_key": "A|X|Spot|Y|Spot"}, {"route_key": "B|X|Spot|Y|Spot"}]}

    monkeypatch.setattr(server.api_spreads, "load_spreads", build)
    monkeypatch.setattr(server, "_file_signature", lambda _p: "sig")
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda _key: None)
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = None
        server._ROUTE_INDEX["rows"] = {}

    board = Path("board.jsonl")
    first = server._find_canonical_route("A|X|Spot|Y|Spot", board)
    second = server._find_canonical_route("B|X|Spot|Y|Spot", board)

    assert first and second
    assert builds == [1], "the second exact route must reuse the token-scoped index"


def test_a_new_snapshot_retains_index_until_background_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = []
    signature = {"value": "one"}

    def build(**_kwargs):
        builds.append(1)
        return {"rows": [{"route_key": "A|X|Spot|Y|Spot"}]}

    monkeypatch.setattr(server.api_spreads, "load_spreads", build)
    monkeypatch.setattr(server, "_file_signature", lambda _p: signature["value"])
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = None
        server._ROUTE_INDEX["rows"] = {}

    board = Path("board.jsonl")
    server._find_canonical_route("A|X|Spot|Y|Spot", board)
    signature["value"] = "two"
    server._route_index(board)

    assert len(builds) == 2, (
        "the request builds one token; the explicit background path then owns "
        "the complete changed-snapshot rebuild"
    )


def test_existing_route_uses_retained_index_during_snapshot_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = (str(Path("new-board.jsonl")), "old")
        server._ROUTE_INDEX["rows"] = {
            "A|X|Spot|Y|Spot": {"route_key": "A|X|Spot|Y|Spot", "token": "A"}
        }

    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **_kwargs: pytest.fail("retained route must render without a synchronous rebuild"),
    )

    row = server._find_canonical_route("A|X|Spot|Y|Spot", Path("new-board.jsonl"))

    assert row and row["token"] == "A"


def test_standard_route_prefers_current_resident_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        "route_key": "A|X|Spot|Y|Spot",
        "token": "A",
        "displayed_open_spread_pct": 3.25,
    }
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(
        server.warm_query_projection.LIVE_UNIVERSE,
        "target_rows",
        lambda **_kwargs: ([current], {"ready": True}),
    )
    monkeypatch.setattr(
        server,
        "_route_index",
        lambda _path: pytest.fail("resident route must not rebuild the board"),
    )

    selected = server._find_canonical_route(current["route_key"], Path("board.jsonl"))

    assert selected is current


def test_a_catalogue_route_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: {"route_key": "custom"})

    def explode(**_kwargs):
        raise AssertionError("must not build the board for a catalogue route")

    monkeypatch.setattr(server.api_spreads, "load_spreads", explode)

    assert server._find_canonical_route("custom", Path("board.jsonl")) == {"route_key": "custom"}


def test_catalogue_route_rejoins_the_exact_warm_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = {
        "route_key": "CUSTOM:ong",
        "token": "ONG",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_market_symbol": "ONG/USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "ONG/USDT:USDT",
    }
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: custom)
    monkeypatch.setattr(
        server.catalog_pairs,
        "for_token",
        lambda *_args, **_kwargs: pytest.fail("chart navigation must not query the catalogue"),
    )
    monkeypatch.setattr(
        server.token_rankings,
        "load",
        dict,
    )
    monkeypatch.setattr(
        server.token_rankings,
        "dex_routes_for",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        server.catalog_pairs,
        "with_routes",
        lambda payload, _extra, **_kwargs: payload,
    )
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["rows"] = {}

    selected = server._find_canonical_route("CUSTOM:ong", Path("board.jsonl"))

    assert selected is custom


def test_catalogue_route_replaces_stale_index_economics_but_keeps_history_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = {
        "route_key": "CUSTOM:ong",
        "token": "ONG",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_market_symbol": "ONG/USDT",
        "short_venue": "Binance",
        "short_market_type": "Spot",
        "short_market_symbol": "ONG/USDT",
    }
    stale_index = {
        **custom,
        "route_key": "ONG|Mexc|Spot|Binance|Spot",
        "quote_ts_us": 1_700_000_000_000_000,
        "executable_spread_pct": 1.0,
        "depth_weighted_spread_pct": 0.9,
        "notes": {"history": "preserve"},
    }
    current_catalogue = {
        **custom,
        "quote_ts_us": int(time.time() * 1_000_000),
        "executable_spread_pct": 2.9,
        "displayed_open_spread_pct": 2.9,
        "depth_weighted_spread_pct": 2.8,
    }
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: custom)
    monkeypatch.setattr(
        server.catalog_pairs,
        "for_token",
        lambda *_args, **_kwargs: {"routes": [current_catalogue]},
    )
    monkeypatch.setattr(server.token_rankings, "load", dict)
    monkeypatch.setattr(server.token_rankings, "dex_routes_for", lambda *_args: [])
    monkeypatch.setattr(
        server.catalog_pairs,
        "with_routes",
        lambda payload, _extra, **_kwargs: payload,
    )
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["rows"] = {stale_index["route_key"]: stale_index}

    selected = server._find_canonical_route("CUSTOM:ong", Path("board.jsonl"))

    assert selected["route_key"] == stale_index["route_key"]
    assert selected["notes"] == {"history": "preserve"}
    assert selected["quote_ts_us"] == stale_index["quote_ts_us"]
    assert selected["depth_weighted_spread_pct"] == pytest.approx(0.9)


def test_historical_route_uses_indexed_radar_without_board_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = {
        "route_key": "GUA|Bingx|Futures|Kucoin Futures|Futures",
        "token": "GUA",
    }
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(
        server.warm_query_projection.LIVE_UNIVERSE,
        "target_rows",
        lambda **_kwargs: ([], {"ready": True}),
    )
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda _key: retained)
    monkeypatch.setattr(
        server,
        "_route_index",
        lambda _path: pytest.fail("radar chart must not rebuild the board"),
    )
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = None
        server._ROUTE_INDEX["rows"] = {}

    assert server._find_canonical_route(retained["route_key"], Path("board")) is retained


def test_pair_row_calls_matched_size_spread_executable() -> None:
    row = {
        "route_key": "CUSTOM:ong",
        "token": "ONG",
        "route_kind": "SPOT-FUTURES",
        "executable_spread_pct": 2.9,
        "displayed_open_spread_pct": 2.9,
        "depth_weighted_spread_pct": 2.8,
        "quote_ts_us": int(time.time() * 1_000_000),
    }

    pair_row = server._canonical_pair_row(row)

    assert pair_row["spread_pct"] == pytest.approx(2.8)
    assert pair_row["displayed_open_spread_pct"] == pytest.approx(2.9)
