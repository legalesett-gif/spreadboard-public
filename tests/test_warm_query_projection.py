"""Arbitrary market filters stay on the continuously warm route universe."""

from __future__ import annotations

import threading
import time
from itertools import count
from pathlib import Path

import pytest

from spreadboard import materialized_views, server, warm_query_projection


def _route(
    token: str,
    *,
    route_key: str,
    long_venue: str = "Gate",
    short_venue: str = "Mexc",
    spread: float = 0.5,
    funding: float = 0.1,
) -> dict[str, object]:
    now_us = int(time.time() * 1_000_000)
    return {
        "token": token,
        "token_name": f"{token} Token",
        "route_key": route_key,
        "route_kind": "FUTURES",
        "long_venue": long_venue,
        "long_market_type": "Futures",
        "short_venue": short_venue,
        "short_market_type": "Futures",
        "long_market_symbol": f"{token}/USDT:USDT",
        "short_market_symbol": f"{token}/USDT:USDT",
        "long_quote": "USDT",
        "short_quote": "USDT",
        "long_price": 1.0,
        "short_price": 1.01,
        "long_bid": 0.999,
        "long_ask": 1.0,
        "short_bid": 1.01,
        "short_ask": 1.011,
        "executable_spread_pct": spread,
        "displayed_open_spread_pct": spread,
        "depth_weighted_spread_pct": spread,
        "depth_usd": 500.0,
        "matched_size_notional_usd": 500.0,
        "depth_unverified": False,
        "funding_daily_pct": funding,
        "funding_projected_24h_pct": funding,
        "funding_apr_pct": funding * 365.0,
        "long_funding_pct": 0.0,
        "short_funding_pct": funding / 3.0,
        "long_funding_interval_hours": 8.0,
        "short_funding_interval_hours": 8.0,
        "long_volume_24h_usd": 1_000_000.0,
        "short_volume_24h_usd": 1_000_000.0,
        "market_cap_usd": 10_000_000.0,
        "fdv_usd": 12_000_000.0,
        "listing_age_days": 30.0,
        "asset_class": "crypto",
        "freshness": "fresh",
        "quote_ts_us": now_us,
        "age_min": 0.0,
        "href": f"/pair/{route_key}",
        "deliverable": True,
        "identity_mismatch": False,
        "thin_book": False,
        "mirage_guarded": False,
        "live_book": True,
        "spread_quote_current": True,
        "tokenized_guard": {"rankable": True},
    }


def _template() -> dict[str, object]:
    return {
        "ok": True,
        "source_health": {"canonical_api": {"status": "fresh", "row_count": 2}},
        "exchange_options": ["Gate", "Mexc", "Bybit"],
        "route_kind_counts": {"FUTURES": 2},
        "asset_class_counts": {"crypto": 2},
        "route_kind_token_counts": {"FUTURES": 2},
        "lane_token_counts": {"FUTURES": 2},
        "top_edges": [],
        "top_funding": [],
    }


def _ready_universe(
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[str, dict[str, object]],
    updates: dict[str, tuple[object, ...]] | None = None,
) -> warm_query_projection.LiveRouteUniverse:
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install(rows)  # type: ignore[arg-type]
    monkeypatch.setattr(
        warm_query_projection.api_spreads,
        "live_route_updates_for",
        lambda *_args, **_kwargs: updates
        if updates is not None
        else {
            key: (
                row["depth_weighted_spread_pct"],
                row["funding_daily_pct"],
                row["quote_ts_us"],
                "matched_vwap",
            )
            for key, row in rows.items()
        },
    )
    universe.refresh()
    return universe


def test_arbitrary_filters_project_from_one_live_atomic_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        "GUA-route": _route("GUA", route_key="GUA-route"),
        "BTW-route": _route(
            "BTW", route_key="BTW-route", long_venue="Bybit", spread=0.2
        ),
    }
    universe = _ready_universe(
        monkeypatch,
        rows,
        updates={
            "GUA-route": (2.5, 0.3, int(time.time() * 1_000_000), "matched_vwap"),
            "BTW-route": (0.2, 0.1, int(time.time() * 1_000_000), "matched_vwap"),
        },
    )
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)

    payload = warm_query_projection.project(
        {"q": ["gua"], "exchange": ["gate"], "min_spread_pct": ["2"]},
        template=_template(),
        limit=25,
        offset=0,
    )

    assert payload is not None
    assert payload["mode"] == "materialized_live_query_projection"
    assert [group["token"] for group in payload["groups"]] == ["GUA"]
    assert payload["groups"][0]["best_edge_pct"] == pytest.approx(2.5)
    assert payload["materialized_live_universe"]["updated_route_count"] == 2
    assert payload["materialized_live_universe"]["current_priced_route_count"] == 2
    assert payload["materialized_live_universe"]["current_priced_token_count"] == 2


def test_live_health_does_not_count_funding_only_tuples_as_priced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        "priced": _route("GUA", route_key="priced"),
        "funding-only": _route("BTW", route_key="funding-only"),
        "empty": _route("SPCX", route_key="empty"),
    }
    universe = _ready_universe(
        monkeypatch,
        rows,
        updates={
            "priced": (0.5, 0.2, int(time.time() * 1_000_000), "matched_vwap"),
            "funding-only": (None, 0.3, None, None),
            "empty": (None, None, None, None),
        },
    )

    status = universe.status()

    assert status["updated_route_count"] == 3
    assert status["current_priced_route_count"] == 1
    assert status["current_priced_token_count"] == 1
    assert status["funding_only_route_count"] == 1
    assert status["current_priced_route_kind_counts"] == {"FUTURES": 1}


def test_empty_search_is_a_complete_projection_not_a_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {"GUA-route": _route("GUA", route_key="GUA-route")}
    universe = _ready_universe(monkeypatch, rows)
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)

    payload = warm_query_projection.project(
        {"q": ["does-not-exist"]}, template=_template(), limit=25, offset=0
    )

    assert payload is not None
    assert payload["groups"] == []
    assert server._market_payload_cacheable(payload) is True


def test_server_uses_live_projection_before_discovery_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = {"GUA-route": _route("GUA", route_key="GUA-route")}
    store = materialized_views.Store(tmp_path / "materialized")
    default_query = {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]}
    writer = materialized_views.GenerationWriter(
        store, required_queries=(default_query,), source_signature={}
    )
    template = _template()
    template.update(
        {
            "filters": {"offset": 0, "limit": 500},
            "summary": {"matching_rows": 1, "matching_tokens": 1},
            "pagination": {"offset": 0, "limit": 500},
            "groups": [],
            "rows": [],
        }
    )
    writer.write_view(default_query, template)
    writer.write_route_index(rows)  # type: ignore[arg-type]
    writer.publish()
    universe = _ready_universe(monkeypatch, rows)
    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", store)
    monkeypatch.setattr(server.warm_query_projection, "LIVE_UNIVERSE", universe)
    monkeypatch.setattr(server, "_MARKET_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_CACHE_INFLIGHT", {})
    monkeypatch.setattr(server, "_sync_telegram_client_universe", lambda value: value)
    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **_kwargs: pytest.fail("an arbitrary warm query must not parse discovery"),
    )

    result = server.api_market_spreads(
        tmp_path / "board.jsonl", {"q": ["GUA"], "min_spread_pct": ["0.1"]}
    )

    assert result["mode"] == "materialized_live_query_projection"
    assert result["groups"][0]["token"] == "GUA"


def test_research_projection_ranks_by_current_top_book_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    low = _route("SPCX", route_key="low", spread=0.1)
    low.update({"depth_weighted_spread_pct": None, "depth_unverified": True})
    high = _route("SPCX", route_key="high", spread=7.8)
    high.update(
        {
            "depth_weighted_spread_pct": None,
            "depth_unverified": True,
            "mirage_guarded": True,
        }
    )
    universe = _ready_universe(monkeypatch, {"low": low, "high": high})
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)

    projected = warm_query_projection.project(
        {"q": ["SPCX"], "evidence": ["research"], "include_unverified": ["1"]},
        template=_template(),
        limit=25,
        offset=0,
    )

    assert projected is not None
    assert projected["groups"][0]["best_route"]["route_key"] == "high"
    assert projected["groups"][0]["best_edge_pct"] == 7.8


def test_default_projection_merges_matched_and_indicative_spreads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched = _route("SPCX", route_key="matched", spread=0.4)
    indicative = _route("SPCX", route_key="indicative", spread=1.2)
    indicative.update({"depth_weighted_spread_pct": None, "depth_unverified": True})
    excluded = _route("SPCX", route_key="excluded", spread=9.0)
    excluded.update({"identity_mismatch": True})
    universe = _ready_universe(
        monkeypatch,
        {"matched": matched, "indicative": indicative, "excluded": excluded},
    )
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)

    projected = warm_query_projection.project(
        {"q": ["SPCX"]}, template=_template(), limit=25, offset=0
    )

    assert projected is not None
    assert projected["filters"]["evidence"] == "all"
    assert projected["filters"]["include_unverified"] is True
    assert {row["route_key"] for row in projected["rows"]} == {
        "matched",
        "indicative",
    }
    assert projected["groups"][0]["best_route"]["route_key"] == "indicative"


def test_projection_failure_releases_single_flight_and_serves_durable_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = materialized_views.Store(tmp_path / "materialized")
    query = {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]}
    writer = materialized_views.GenerationWriter(
        store, required_queries=(query,), source_signature={}
    )
    durable = {
        **_template(),
        "mode": "durable-fallback",
        "groups": [{"token": "GUA", "routes": []}],
        "rows": [],
    }
    writer.write_view(query, durable)
    writer.write_route_index({})
    writer.publish()
    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", store)
    monkeypatch.setattr(server, "_MARKET_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_CACHE_INFLIGHT", {})
    monkeypatch.setattr(server, "_sync_telegram_client_universe", lambda value: value)
    monkeypatch.setattr(server.warm_query_projection.LIVE_UNIVERSE, "template", lambda: durable)
    monkeypatch.setattr(
        server.warm_query_projection,
        "project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection")),
    )
    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **_kwargs: pytest.fail("durable fallback must avoid discovery"),
    )

    result = server.api_market_spreads(tmp_path / "board.jsonl", query)

    assert result["mode"] == "durable-fallback"
    assert server._MARKET_CACHE_INFLIGHT == {}


def test_failed_refresh_retains_the_previous_complete_live_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {"GUA-route": _route("GUA", route_key="GUA-route")}
    universe = _ready_universe(monkeypatch, rows)
    before = universe.snapshot()[1]

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("temporary sqlite handoff")

    monkeypatch.setattr(
        warm_query_projection.api_spreads, "live_route_updates_for", fail
    )
    status = universe.refresh()

    assert universe.snapshot()[1] == before
    assert status["ready"] is True
    assert "temporary sqlite handoff" in str(status["last_error"])


def test_partial_refresh_keeps_only_a_still_current_price_and_new_funding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    quote_ts_us = int((now - 20.0) * 1_000_000)
    rows = {"GUA-route": _route("GUA", route_key="GUA-route")}
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install(rows)  # type: ignore[arg-type]
    observations = iter(
        [
            {"GUA-route": (1.25, 0.10, quote_ts_us, "matched_vwap")},
            {"GUA-route": (None, 0.22, None, None)},
        ]
    )
    monkeypatch.setattr(
        warm_query_projection.api_spreads,
        "live_route_updates_for",
        lambda *_args, **_kwargs: next(observations),
    )
    universe.refresh()
    universe.refresh()

    update = universe.snapshot()[1]["GUA-route"]
    assert update == (1.25, 0.22, quote_ts_us, "matched_vwap")


def test_partial_refresh_never_extends_an_expired_price() -> None:
    now = time.time()
    expired_ts_us = int(
        (now - warm_query_projection.api_spreads.LIVE_BOOK_MAX_AGE_SECONDS - 1.0)
        * 1_000_000
    )

    merged = warm_query_projection._merge_live_updates(
        {"GUA-route": (1.25, 0.10, expired_ts_us, "matched_vwap")},
        {"GUA-route": (None, 0.22, None, None)},
        route_keys={"GUA-route"},
        now=now,
    )

    assert merged["GUA-route"] == (None, 0.22, None, None)


def test_targeted_spot_refresh_preserves_unrelated_route_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_us = int(time.time() * 1_000_000)
    futures = _route("GUA", route_key="GUA-futures")
    spot = {
        **_route("GUA", route_key="GUA-spot"),
        "route_kind": "SPOT",
        "long_market_type": "Spot",
        "short_market_type": "Spot",
        "long_market_symbol": "GUA/USDT",
        "short_market_symbol": "GUA/USDT",
    }
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({"GUA-futures": futures, "GUA-spot": spot})
    responses = iter(
        [
            {"GUA-futures": (0.5, 0.1, now_us, "matched_vwap")},
            {"GUA-spot": (0.2, None, now_us, "top_book")},
            {"GUA-spot": (0.35, None, now_us, "top_book")},
        ]
    )
    observed_route_keys: list[set[str]] = []

    def updates(routes: list[dict[str, object]], **_kwargs: object):
        observed_route_keys.append({str(row["route_key"]) for row in routes})
        return next(responses)

    monkeypatch.setattr(
        warm_query_projection.api_spreads, "live_route_updates_for", updates
    )
    universe.refresh()
    universe.refresh_route_kinds({"SPOT"})

    current = universe.snapshot()[1]
    assert observed_route_keys == [
        {"GUA-futures"},
        {"GUA-spot"},
        {"GUA-spot"},
    ]
    assert current["GUA-futures"] == (0.5, 0.1, now_us, "matched_vwap")
    assert current["GUA-spot"] == (0.35, None, now_us, "top_book")


def test_full_refresh_publishes_a_finished_family_before_slower_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow later lane must not leave a fresh earlier lane invisible."""

    now_us = int(time.time() * 1_000_000)
    futures = _route("GUA", route_key="GUA-futures")
    spot = {
        **_route("GUA", route_key="GUA-spot"),
        "route_kind": "SPOT",
        "long_market_type": "Spot",
        "short_market_type": "Spot",
        "long_market_symbol": "GUA/USDT",
        "short_market_symbol": "GUA/USDT",
    }
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({"GUA-futures": futures, "GUA-spot": spot})
    slow_lane_started = threading.Event()
    release_slow_lane = threading.Event()

    def updates(routes: list[dict[str, object]], **_kwargs: object):
        route_key = str(routes[0]["route_key"])
        if route_key == "GUA-spot":
            slow_lane_started.set()
            assert release_slow_lane.wait(timeout=2.0)
            return {route_key: (0.2, None, now_us, "top_book")}
        return {route_key: (0.75, 0.1, now_us, "matched_vwap")}

    monkeypatch.setattr(
        warm_query_projection.api_spreads, "live_route_updates_for", updates
    )
    worker = threading.Thread(target=universe.refresh)
    worker.start()
    assert slow_lane_started.wait(timeout=2.0)

    during_refresh, status = universe.update_snapshot()
    assert during_refresh["GUA-futures"] == (
        0.75,
        0.1,
        now_us,
        "matched_vwap",
    )
    assert status["ready"] is True

    release_slow_lane.set()
    worker.join(timeout=2.0)
    assert worker.is_alive() is False


def test_worker_refreshes_every_priority_lane_between_full_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[object] = []

    class FakeUniverse:
        def refresh(self):
            actions.append("full")

        def refresh_route_kinds(self, route_kinds):
            actions.append(set(route_kinds))

    class BoundedStop:
        waits = 0

        def is_set(self):
            return self.waits >= 5

        def wait(self, _seconds):
            self.waits += 1

    ticks = count(100.0)
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", FakeUniverse())
    monkeypatch.setattr(
        warm_query_projection.time, "monotonic", lambda: next(ticks)
    )
    worker = warm_query_projection.Worker(
        BoundedStop(), interval_seconds=10.0, priority_interval_seconds=2.0
    )

    worker.run()

    assert actions == [
        "full",
        {"FUTURES"},
        {"FUTURES-SPOT", "SPOT-FUTURES"},
        {"DEX-FUTURES", "DEX-SPOT"},
        {"SPOT"},
    ]


def test_update_snapshot_reuses_immutable_maps_without_copying_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {"GUA-route": _route("GUA", route_key="GUA-route")}
    universe = _ready_universe(monkeypatch, rows)

    updates, status = universe.update_snapshot()

    assert updates is universe._updates
    assert updates["GUA-route"][0] == pytest.approx(0.5)
    assert status["ready"] is True
    assert status["updated_route_count"] == 1
