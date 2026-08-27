"""Fast route-index publication is atomic, current, and independent of page builds."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from scripts import live_route_index_worker
from spreadboard import intel, materialized_views, server


def test_worker_publishes_complete_index_without_rendering_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_path = tmp_path / "board.jsonl"
    board_path.write_text("", encoding="utf-8")
    rows = {"GUA-route": {"route_key": "GUA-route", "token": "GUA"}}
    monkeypatch.setattr(
        live_route_index_worker.api_spreads,
        "load_public_route_index",
        lambda: (rows, {"updated_at": "2026-08-24T20:00:00Z"}),
    )

    summary = live_route_index_worker.build(board_path, tmp_path / "materialized")
    store = materialized_views.Store(tmp_path / "materialized")

    assert summary["routes"] == 1
    assert store.live_route_index(board_path=board_path) == rows
    assert store.live_route_index_status()["ready"] is True


def test_source_change_during_fast_build_keeps_previous_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_path = tmp_path / "board.jsonl"
    board_path.write_text("", encoding="utf-8")
    signatures = iter(
        [
            {"board_path": str(board_path.resolve()), "discovery": [1, 10]},
            {"board_path": str(board_path.resolve()), "discovery": [2, 20]},
        ]
    )
    monkeypatch.setattr(
        live_route_index_worker, "source_signature", lambda _path: next(signatures)
    )
    monkeypatch.setattr(
        live_route_index_worker.api_spreads,
        "load_public_route_index",
        lambda: ({"new": {"route_key": "new"}}, {}),
    )

    with pytest.raises(RuntimeError, match="source_generation_changed"):
        live_route_index_worker.build(board_path, tmp_path / "materialized")

    assert not (tmp_path / "materialized" / "live-route-index-current.json").exists()


def test_same_structural_generation_cannot_replace_complete_index_with_thin_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_path = tmp_path / "board.jsonl"
    board_path.write_text("", encoding="utf-8")
    output_root = tmp_path / "materialized"
    signature = {
        "board_path": str(board_path.resolve()),
        "discovery": [1, 10],
        "chart_catalog": [2, 20],
    }
    previous = {
        "old": {"route_key": "old", "token": "OLD", "value": 1},
        "updated": {"route_key": "updated", "token": "GUA", "value": 1},
    }
    materialized_views.Store(output_root).write_live_route_index(
        previous,
        source_signature=signature,
    )
    monkeypatch.setattr(
        live_route_index_worker, "source_signature", lambda _path: signature
    )
    monkeypatch.setattr(
        live_route_index_worker.api_spreads,
        "load_public_route_index",
        lambda: (
            {"updated": {"route_key": "updated", "token": "GUA", "value": 2}},
            {"updated_at": "2026-08-27T21:00:00Z"},
        ),
    )

    summary = live_route_index_worker.build(board_path, output_root)
    stored = materialized_views.Store(output_root).live_route_index(
        board_path=board_path
    )

    assert summary["routes"] == 2
    assert summary["current_routes"] == 1
    assert summary["retained_routes"] == 1
    assert stored == {
        "old": previous["old"],
        "updated": {"route_key": "updated", "token": "GUA", "value": 2},
    }


def test_current_dex_contract_replaces_retained_route_key_twin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_path = tmp_path / "board.jsonl"
    board_path.write_text("", encoding="utf-8")
    output_root = tmp_path / "materialized"
    signature = {
        "board_path": str(board_path.resolve()),
        "discovery": [1, 10],
        "chart_catalog": [2, 20],
    }
    common = {
        "token": "TRX",
        "route_kind": "DEX-FUTURES",
        "long_venue": "OKX DEX 56",
        "long_market_type": "Spot",
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "short_market_symbol": "TRX/USDT:USDT",
        "dex_chain": "56",
        "dex_contract": "0xce7de646e7208a4ef112cb6ed5038fa6cc6b12e3",
    }
    stale = {
        **common,
        "route_key": "TRX|OKX DEX 56|Spot|Aster|Futures",
        "long_market_symbol": "TRX",
        "quote_ts_us": 100,
    }
    fresh = {
        **common,
        "route_key": "CUSTOM:fresh-expanded-route",
        "long_market_symbol": common["dex_contract"],
        "quote_ts_us": 200,
    }
    materialized_views.Store(output_root).write_live_route_index(
        {stale["route_key"]: stale},
        source_signature=signature,
    )
    monkeypatch.setattr(
        live_route_index_worker, "source_signature", lambda _path: signature
    )
    monkeypatch.setattr(
        live_route_index_worker.api_spreads,
        "load_public_route_index",
        lambda: ({fresh["route_key"]: fresh}, {}),
    )

    summary = live_route_index_worker.build(board_path, output_root)
    stored = materialized_views.Store(output_root).live_route_index(
        board_path=board_path
    )

    assert summary["routes"] == 1
    assert summary["retained_routes"] == 0
    assert stored == {fresh["route_key"]: fresh}


def test_new_structural_generation_retains_only_still_listed_cex_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_path = tmp_path / "board.jsonl"
    board_path.write_text("", encoding="utf-8")
    output_root = tmp_path / "materialized"
    old_signature = {"board_path": str(board_path.resolve()), "discovery": [1, 10]}
    new_signature = {"board_path": str(board_path.resolve()), "discovery": [2, 20]}
    listed = {
        "route_key": "listed",
        "route_kind": "FUTURES",
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "long_market_symbol": "GUA/USDT:USDT",
        "short_venue": "Mexc",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
    }
    materialized_views.Store(output_root).write_live_route_index(
        {
            "listed": listed,
            "retired": {**listed, "route_key": "retired", "short_market_symbol": "OLD/USDT:USDT"},
            "dex": {**listed, "route_key": "dex", "route_kind": "DEX-FUTURES"},
        },
        source_signature=old_signature,
    )
    monkeypatch.setattr(
        live_route_index_worker, "source_signature", lambda _path: new_signature
    )
    monkeypatch.setattr(
        live_route_index_worker.chart_catalog,
        "load",
        lambda: {
            "markets": [
                {"venue": "Gate", "market_type": "Futures", "symbol": "GUA/USDT:USDT"},
                {"venue": "Mexc", "market_type": "Futures", "symbol": "GUA/USDT:USDT"},
            ]
        },
    )
    monkeypatch.setattr(
        live_route_index_worker.api_spreads,
        "load_public_route_index",
        lambda: ({"new": {**listed, "route_key": "new"}}, {}),
    )

    live_route_index_worker.build(board_path, output_root)
    stored = materialized_views.Store(output_root).live_route_index(
        board_path=board_path
    )

    # The still-listed route is structurally retained, but the current build's
    # serialization of the same exact legs replaces it rather than duplicating
    # the economic route under two keys.
    assert set(stored or {}) == {"new"}


def test_server_prefers_newer_fast_index_over_older_full_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_path = tmp_path / "board.jsonl"
    board_path.write_text("", encoding="utf-8")
    store = materialized_views.Store(tmp_path / "materialized")
    query = {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]}
    writer = materialized_views.GenerationWriter(
        store, required_queries=(query,), source_signature={}
    )
    writer.write_view(
        query,
        {
            "ok": True,
            "filters": {"limit": 500},
            "summary": {},
            "pagination": {},
            "groups": [],
            "rows": [],
            "top_edges": [],
            "top_funding": [],
        },
    )
    writer.write_route_index({"old": {"route_key": "old", "token": "OLD"}})
    writer.publish()
    time.sleep(0.002)
    store.write_live_route_index(
        {"new": {"route_key": "new", "token": "NEW"}},
        source_signature={"board_path": str(board_path.resolve())},
    )
    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", store)
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = None
        server._ROUTE_INDEX["rows"] = {}

    restored = server.restore_materialized_route_index(board_path)

    assert restored == 1
    assert server._find_canonical_route("new", board_path)["token"] == "NEW"


def test_intel_filter_uses_warm_public_rows_without_parsing_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events.jsonl"
    discovery = tmp_path / "discovery.json"
    discovery.write_text("{}", encoding="utf-8")
    now = time.time()
    assert intel.record_token_attention(
        "GUA", "spread", source="private_bot", events_path=events, now=now
    )
    monkeypatch.setattr(
        intel.board,
        "load_board",
        lambda *_args, **_kwargs: pytest.fail("warm Intel must not parse discovery"),
    )
    route = {
        "token": "GUA",
        "route_key": "GUA-route",
        "route_kind": "FUTURES",
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "short_venue": "Mexc",
        "short_market_type": "Futures",
        "displayed_open_spread_pct": 1.2,
        "executable_spread_pct": 1.2,
        "funding_apr_pct": 100.0,
        "freshness": "fresh",
    }

    payload = intel.build_intel(
        board_path=discovery,
        board_rows=[route],
        events_path=events,
        symbol="GUA",
        now=now + 1,
    )

    assert payload["hot_symbols"][0]["symbol"] == "GUA"
    assert payload["hot_symbols"][0]["best_board"]["route_key"] == "GUA-route"
