"""The background builder publishes one coherent generation or nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import materialized_view_worker as worker
from spreadboard import materialized_views


def _payload(token: str) -> dict[str, object]:
    route = {"route_key": f"{token}|A|Futures|B|Futures", "token": token}
    return {
        "ok": True,
        "filters": {},
        "groups": [{"token": token, "routes": [route]}],
        "rows": [route],
        "source_health": {"canonical_api": {"row_count": 1}},
    }


def test_worker_writes_views_route_index_and_intel_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.jsonl"
    board.write_text("", encoding="utf-8")
    queries = ({}, {"kind": ["FUTURES"], "limit": ["500"]})
    monkeypatch.setattr(worker.service, "_materialized_view_queries", lambda: queries)
    monkeypatch.setattr(worker, "source_signature", lambda _path: {"stable": True})
    monkeypatch.setattr(worker, "_release_memory", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker.api_spreads,
        "load_public_route_index",
        lambda: ({"route": {"route_key": "route"}}, {}),
    )
    monkeypatch.setattr(worker.server, "api_market_spreads", lambda _path, query: _payload((query.get("kind") or ["ALL"])[0]))
    monkeypatch.setattr(worker.server, "api_intel", lambda *_args, **_kwargs: {"ok": True, "intel": "ready"})
    monkeypatch.setattr(worker.server, "mark_historical_dex_archive_ready", lambda: None)
    monkeypatch.setattr(worker.funding_catalog, "clear_cache", lambda: None)
    monkeypatch.setattr(worker.funding_catalog, "restore_persisted_cache", lambda: {"ready": True})
    monkeypatch.setattr(worker.telegram_queries, "replace_payload", lambda _payload: None)
    monkeypatch.setattr(worker.telegram_queries, "replace_funding_payloads", lambda _payloads: None)

    summary = worker.build(board, tmp_path / "materialized")
    store = materialized_views.Store(tmp_path / "materialized")

    assert summary["status"] == "ok"
    assert summary["views"] == 2
    assert store.payload_for({})["groups"][0]["token"] == "ALL"
    assert store.route_index() == {"route": {"route_key": "route"}}
    assert store.extra("intel-default") == {"ok": True, "intel": "ready"}


def test_worker_does_not_publish_a_generation_mixed_across_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.jsonl"
    board.write_text("", encoding="utf-8")
    signatures = iter(({"generation": 1}, {"generation": 2}))
    monkeypatch.setattr(worker.service, "_materialized_view_queries", lambda: ({},))
    monkeypatch.setattr(worker, "source_signature", lambda _path: next(signatures))
    monkeypatch.setattr(worker, "_release_memory", lambda **_kwargs: None)
    monkeypatch.setattr(worker.api_spreads, "load_public_route_index", lambda: ({}, {}))
    monkeypatch.setattr(worker.server, "api_market_spreads", lambda *_args, **_kwargs: _payload("ONE"))
    monkeypatch.setattr(worker.server, "api_intel", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(worker.funding_catalog, "clear_cache", lambda: None)
    monkeypatch.setattr(worker.funding_catalog, "restore_persisted_cache", lambda: {"ready": True})

    with pytest.raises(RuntimeError, match="source_generation_changed"):
        worker.build(board, tmp_path / "materialized")

    assert not (tmp_path / "materialized" / "current.json").exists()


def test_funding_warm_view_prioritises_every_visible_exact_futures_leg() -> None:
    payload = {
        "filters": {"funding_only": True},
        "groups": [
            {
                "best_funding_route": {
                    "long_venue": "Gate",
                    "long_market_type": "Futures",
                    "long_market_symbol": "ONE/USDT:USDT",
                    "short_venue": "Mexc",
                    "short_market_type": "Futures",
                    "short_market_symbol": "ONE/USDT:USDT",
                },
                "routes": [
                    {
                        "long_venue": "Aster",
                        "long_market_type": "Futures",
                        "long_market_symbol": "ONE/USDT:USDT",
                        "short_venue": "Kraken",
                        "short_market_type": "Spot",
                        "short_market_symbol": "ONE/USD",
                    }
                ],
            }
        ],
    }

    assert worker._funding_priority_legs(payload) == [
        ("Gate", "ONE/USDT:USDT"),
        ("Mexc", "ONE/USDT:USDT"),
        ("Aster", "ONE/USDT:USDT"),
    ]


def test_collector_restart_seeds_history_demand_from_last_complete_view(
    monkeypatch, tmp_path: Path
) -> None:
    from scripts import run_spreadboard_service as service

    payload = {
        "filters": {"funding_only": True},
        "groups": [
            {
                "routes": [
                    {
                        "long_venue": "Gate",
                        "long_market_type": "Futures",
                        "long_market_symbol": "ONE/USDT:USDT",
                        "short_market_type": "Spot",
                    }
                ]
            }
        ],
    }

    class Store:
        def payload_for(self, _query, **_kwargs):
            return payload

    monkeypatch.setattr(service.materialized_views, "default_store", Store)
    monkeypatch.setattr(
        service, "_materialized_view_queries", lambda: ({"funding_only": ["1"]},)
    )
    monkeypatch.setattr(
        service.funding_history_demand, "DEFAULT_PATH", tmp_path / "demand.json"
    )

    assert service._seed_funding_history_demand() == 1
    assert service.funding_history_demand.legs() == [("Gate", "ONE/USDT:USDT")]


def test_worker_archives_every_dex_route_before_compacting_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.jsonl"
    board.write_text("", encoding="utf-8")
    query = {
        "funding_only": ["1"],
        "kind": ["DEX-FUTURES"],
        "sort": ["funding"],
        "direction": ["desc"],
        "limit": ["500"],
        "offset": ["0"],
    }
    routes = [
        {
            "route_key": f"DEX-{index}",
            "token": "GUA",
            "route_kind": "DEX-FUTURES",
        }
        for index in range(5)
    ]
    payload = {
        "ok": True,
        "filters": {"funding_only": True},
        "groups": [{"token": "GUA", "routes": routes, "route_count": 5}],
        "rows": routes,
        "source_health": {"canonical_api": {"row_count": 5}},
    }
    archived: list[dict] = []
    monkeypatch.setattr(worker.service, "_materialized_view_queries", lambda: (query,))
    monkeypatch.setattr(worker.service, "FUNDING_ARCHIVE_QUERIES", (query,))
    monkeypatch.setattr(worker, "source_signature", lambda _path: {"stable": True})
    monkeypatch.setattr(worker, "_release_memory", lambda **_kwargs: None)
    monkeypatch.setattr(worker.api_spreads, "load_public_route_index", lambda: ({}, {}))
    monkeypatch.setattr(worker.server, "api_market_spreads", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(worker.server, "api_intel", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(worker.funding_catalog, "clear_cache", lambda: None)
    monkeypatch.setattr(worker.funding_catalog, "restore_persisted_cache", lambda: {"ready": True})
    monkeypatch.setattr(worker.funding_radar, "refresh", lambda value: archived.extend(value) or len(archived))
    monkeypatch.setattr(worker.telegram_queries, "replace_payload", lambda _payload: None)
    monkeypatch.setattr(worker.telegram_queries, "replace_funding_payloads", lambda _payloads: None)

    worker.build(board, tmp_path / "materialized")
    stored = materialized_views.Store(tmp_path / "materialized").payload_for(query)

    assert [route["route_key"] for route in archived] == [f"DEX-{index}" for index in range(5)]
    assert len(stored["groups"][0]["routes"]) == 3
    assert stored["groups"][0]["route_count"] == 5
