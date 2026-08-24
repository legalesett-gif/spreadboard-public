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
    monkeypatch.setattr(worker.server, "_route_index", lambda _path: {"route": {"route_key": "route"}})
    monkeypatch.setattr(worker.server, "api_market_spreads", lambda _path, query: _payload((query.get("kind") or ["ALL"])[0]))
    monkeypatch.setattr(worker.server, "api_intel", lambda *_args, **_kwargs: {"ok": True, "intel": "ready"})
    monkeypatch.setattr(worker.server, "mark_historical_dex_archive_ready", lambda: None)
    monkeypatch.setattr(worker.funding_catalog, "clear_cache", lambda: None)
    monkeypatch.setattr(worker.funding_catalog, "refresh_cache", dict)
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
    monkeypatch.setattr(worker.server, "_route_index", lambda _path: {})
    monkeypatch.setattr(worker.server, "api_market_spreads", lambda *_args, **_kwargs: _payload("ONE"))
    monkeypatch.setattr(worker.server, "api_intel", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(worker.funding_catalog, "clear_cache", lambda: None)
    monkeypatch.setattr(worker.funding_catalog, "refresh_cache", dict)

    with pytest.raises(RuntimeError, match="source_generation_changed"):
        worker.build(board, tmp_path / "materialized")

    assert not (tmp_path / "materialized" / "current.json").exists()
