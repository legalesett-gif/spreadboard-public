"""Last-complete navigation views survive restarts and failed rebuilds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spreadboard import materialized_views, server


def _payload(tokens: list[str]) -> dict[str, object]:
    groups = [
        {
            "token": token,
            "routes": [{"token": token, "route_key": f"{token}|A|Futures|B|Futures"}],
        }
        for token in tokens
    ]
    return {
        "ok": True,
        "filters": {"kind": "FUTURES", "offset": 0, "limit": 500},
        "summary": {
            "matching_tokens": len(groups),
            "returned_tokens": len(groups),
            "matching_rows": len(groups),
            "returned_rows": len(groups),
        },
        "pagination": {
            "offset": 0,
            "limit": 500,
            "returned_rows": len(groups),
            "matching_rows": len(groups),
            "has_previous": False,
            "has_more": False,
        },
        "groups": groups,
        "rows": [route for group in groups for route in group["routes"]],
        "top_edges": [],
        "top_funding": [],
    }


def test_query_identity_is_order_independent_and_excludes_presentation_flags() -> None:
    first = {
        "kind": ["FUTURES"],
        "limit": ["500"],
        "no_cache": ["1"],
        "view": ["table"],
    }
    second = {"limit": ["500"], "kind": ["FUTURES"]}

    assert materialized_views.query_identity(first) == materialized_views.query_identity(second)


def test_incomplete_generation_never_replaces_last_complete(tmp_path: Path) -> None:
    store = materialized_views.Store(tmp_path)
    first = materialized_views.GenerationWriter(
        store,
        required_queries=({"kind": ["FUTURES"], "limit": ["500"]},),
        source_signature={"discovery": [1, 10]},
    )
    first.write_view({"kind": ["FUTURES"], "limit": ["500"]}, _payload(["OLD"]))
    first.write_route_index({"old": {"route_key": "old"}})
    first.publish()

    second = materialized_views.GenerationWriter(
        store,
        required_queries=(
            {"kind": ["FUTURES"], "limit": ["500"]},
            {"kind": ["SPOT"], "limit": ["500"]},
        ),
        source_signature={"discovery": [2, 20]},
    )
    second.write_view({"kind": ["FUTURES"], "limit": ["500"]}, _payload(["NEW"]))
    with pytest.raises(ValueError, match="missing_required_views"):
        second.publish()

    restored = store.payload_for({"kind": ["FUTURES"], "limit": ["500"]})
    assert restored and restored["groups"][0]["token"] == "OLD"


def test_status_reads_navigation_counts_from_rendered_payload_metadata(
    tmp_path: Path,
) -> None:
    store = materialized_views.Store(tmp_path)
    query = {"kind": ["FUTURES"], "limit": ["500"]}
    writer = materialized_views.GenerationWriter(
        store,
        required_queries=(query,),
        source_signature={},
    )
    writer.write_view(query, _payload(["A", "B"]))
    writer.write_route_index({})
    writer.publish()

    status = store.status()

    assert status["navigation_token_count"] == 2
    assert status["navigation_route_count"] == 2
    assert status["empty_view_count"] == 0


def test_corrupt_generation_fails_closed_without_destroying_manifest(tmp_path: Path) -> None:
    store = materialized_views.Store(tmp_path)
    writer = materialized_views.GenerationWriter(
        store,
        required_queries=({"kind": ["FUTURES"], "limit": ["500"]},),
        source_signature={},
    )
    writer.write_view({"kind": ["FUTURES"], "limit": ["500"]}, _payload(["SAFE"]))
    writer.write_route_index({})
    manifest = writer.publish()
    view_file = tmp_path / "generations" / manifest["generation"] / manifest["views"][0]["file"]
    view_file.write_text("{broken", encoding="utf-8")

    assert store.payload_for({"kind": ["FUTURES"], "limit": ["500"]}) is None
    assert json.loads((tmp_path / "current.json").read_text())["generation"] == manifest["generation"]


def test_full_materialized_lane_serves_any_page_without_rebuilding(tmp_path: Path) -> None:
    store = materialized_views.Store(tmp_path)
    query = {"kind": ["FUTURES"], "limit": ["500"]}
    writer = materialized_views.GenerationWriter(
        store,
        required_queries=(query,),
        source_signature={},
    )
    writer.write_view(query, _payload(["A", "B", "C", "D"]))
    writer.write_route_index({})
    writer.publish()

    page = store.payload_for({"kind": ["FUTURES"], "offset": ["1"], "limit": ["2"]})
    assert page is not None
    # Repricing happens against the full lane. A newly promoted token can enter
    # page 1 before pagination is applied.
    page["groups"].reverse()
    page = materialized_views.finalize_projection(page)

    assert [group["token"] for group in page["groups"]] == ["C", "B"]
    assert [row["token"] for row in page["rows"]] == ["B", "C"]
    assert page["pagination"] == {
        "offset": 1,
        "limit": 2,
        "returned_rows": 2,
        "matching_rows": 4,
        "has_previous": True,
        "has_more": True,
    }
    assert page["summary"]["matching_tokens"] == 4
    assert page["summary"]["returned_tokens"] == 2


def test_projected_page_is_sliced_before_live_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public 25-row page must not reprice its persisted 500-row superset."""

    payload = _payload(["A", "B", "C", "D"])
    payload["_materialized_projection"] = {"query": {"limit": ["2"]}}
    overlaid_group_counts: list[int] = []
    monkeypatch.setattr(
        server,
        "_apply_spread_freshness_coalesced",
        lambda value: overlaid_group_counts.append(len(value.get("groups") or [])) or value,
    )

    result = server._sync_telegram_client_universe(payload)

    assert overlaid_group_counts == [2]
    assert [group["token"] for group in result["groups"]] == ["A", "B"]
    assert "_materialized_projection" not in result


def test_server_uses_last_complete_view_before_calling_expensive_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = materialized_views.Store(tmp_path)
    writer = materialized_views.GenerationWriter(
        store,
        required_queries=({},),
        source_signature={},
    )
    payload = _payload(["READY"])
    payload["filters"] = {"offset": 0, "limit": 25}
    writer.write_view({}, payload)
    writer.write_route_index({})
    writer.publish()

    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", store)
    monkeypatch.setattr(server, "_MARKET_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_CACHE_INFLIGHT", {})
    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **_kwargs: pytest.fail("persistent navigation view must win before a cold build"),
    )
    monkeypatch.setattr(server, "_sync_telegram_client_universe", lambda value: value)

    result = server.api_market_spreads(tmp_path / "board.jsonl", {})

    assert result["groups"][0]["token"] == "READY"


def test_route_index_restores_from_generation_without_parsing_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = materialized_views.Store(tmp_path)
    writer = materialized_views.GenerationWriter(
        store,
        required_queries=({},),
        source_signature={},
    )
    writer.write_view({}, _payload(["READY"]))
    writer.write_route_index({"route": {"route_key": "route", "token": "READY"}})
    writer.publish()
    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", store)
    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **_kwargs: pytest.fail("restoring a route index must not parse discovery"),
    )
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = None
        server._ROUTE_INDEX["rows"] = {}
        # A new generation clears the bookmark compatibility cache too;
        # production does this inside restore_materialized_route_index.
        server._ROUTE_COMPAT_PATHS.clear()
        server._ROUTE_COMPAT_ROWS.clear()

    restored = server.restore_materialized_route_index(tmp_path / "board.jsonl")

    assert restored == 1
    assert server._find_canonical_route("route", tmp_path / "board.jsonl")["token"] == "READY"


def test_health_exposes_last_complete_generation_without_building(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = materialized_views.Store(tmp_path)
    writer = materialized_views.GenerationWriter(
        store,
        required_queries=({},),
        source_signature={},
    )
    writer.write_view({}, _payload(["READY"]))
    writer.write_route_index({"route": {"route_key": "route"}})
    writer.publish()
    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", store)
    monkeypatch.setattr(server.api_spreads, "fast_quote_health", dict)

    health = server._health_with_fast_quote_state({"ok": True})

    assert health["materialized_views"]["ready"] is True
    assert health["materialized_views"]["view_count"] == 1
    assert health["materialized_views"]["route_count"] == 1
    assert health["materialized_views"]["serving_contract"] == (
        "last_complete_plus_live_overlay"
    )


def test_cleanup_removes_only_old_builder_staging_directories(tmp_path: Path) -> None:
    store = materialized_views.Store(tmp_path)
    generations = tmp_path / "generations"
    old = generations / ".old.tmp"
    fresh = generations / ".fresh.tmp"
    unrelated = generations / "operator-data"
    for path in (old, fresh, unrelated):
        path.mkdir(parents=True)
        (path / "data.json").write_text("x", encoding="utf-8")
    old.touch()
    old_stamp = 1_000.0
    import os

    os.utime(old, (old_stamp, old_stamp))
    os.utime(fresh, (10_000.0, 10_000.0))

    cleanup = store.cleanup_staging(max_age_seconds=3_600, now=10_000.0)

    assert cleanup == {"removed": 1, "bytes": 1}
    assert not old.exists()
    assert fresh.exists()
    assert unrelated.exists()
