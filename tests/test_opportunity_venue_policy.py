"""Venue retirement must precede collection, retention, ranking and pagination."""
import copy
import json
import time
from types import SimpleNamespace

import pytest

from scripts import live_route_index_worker
from spreadarb import venue_policy
from spreadarb.api_discovery import sources
from spreadboard import (
    api_spreads,
    bulk_quotes,
    chart_catalog,
    fast_quotes,
    funding_catalog,
    materialized_views,
    ourbit_quotes,
    server,
    warm_query_projection,
)


def route(token="AAA", long="Gate", short="Bybit", kind="FUTURES", spread=1.0):
    long_type = "Spot" if kind == "SPOT-FUTURES" else "Futures"
    short_type = "Spot" if kind == "FUTURES-SPOT" else "Futures"
    return {
        "token": token, "route_key": f"{token}|{long}|{long_type}|{short}|{short_type}",
        "route_kind": kind, "long_venue": long, "short_venue": short,
        "long_market_type": long_type, "short_market_type": short_type,
        "long_market_symbol": f"{token}/USDT:USDT", "short_market_symbol": f"{token}/USDT:USDT",
        "long_quote": "USDT", "short_quote": "USDT",
        "quote_ts_us": int(time.time() * 1_000_000), "executable_spread_pct": spread,
        "depth_weighted_spread_pct": spread, "funding_daily_pct": 0.02,
        "funding_24h_pct": 0.02, "funding_apr_pct": 7.3,
        "long_ask": 100.0, "short_bid": 100.0 + spread,
        "depth_usd": 500.0, "matched_size_notional_usd": 500.0,
        "depth_unverified": False, "deliverable": True,
    }


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("kind", ["FUTURES", "FUTURES-SPOT", "SPOT-FUTURES", "DEX-FUTURES"])
def test_policy_excludes_only_ourbit_on_either_leg_and_all_product_types(side, kind):
    kept = route(kind=kind)
    excluded = {**kept, f"{side}_venue": " OURBIT "}
    assert venue_policy.opportunity_route_enabled(kept)
    assert not venue_policy.opportunity_route_enabled(excluded)
    assert not venue_policy.opportunity_route_enabled(SimpleNamespace(**excluded))
    assert venue_policy.opportunity_venue_enabled("OurbitDifferentVenue")


def test_discovery_preserves_every_other_existing_venue():
    assert set(sources.default_enabled_cex_source().venues) == {
        "Binance", "Bybit", "Bitget", "OKX", "Gate", "Mexc", "Kucoin", "Bingx",
        "Coinbase", "Kraken", "HTX", "Phemex", "CoinEx", "WhiteBIT", "BitMart", "XT", "Upbit",
    }
    assert set(sources.default_enabled_cex_futures_source().venues) == {
        "Binance", "Bybit", "Bitget", "OKX", "Gate", "Mexc", "Kucoin Futures", "Bingx",
        "Kraken Futures", "Coinbase International", "HTX", "Phemex", "CoinEx", "WhiteBIT",
        "BitMart", "XT", "Lighter",
    }


def test_catalogue_read_removes_old_ourbit_markets_without_mutating_other_markets(tmp_path, monkeypatch):
    kept = [{"token": "AAA", "venue": v, "market_type": k, "symbol": "AAA/USDT", "contract_size": 0.1}
            for v in ("Gate", "Mexc", "Hyperliquid") for k in ("Spot", "Futures")]
    removed = [{**kept[0], "venue": "Ourbit"}, {**kept[0], "venue": "ourbit", "market_type": "Futures"}]
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"markets": kept + removed, "health": {"Ourbit|Spot": {}, "Gate|Spot": {"status": "ok"}}}))
    monkeypatch.setattr(chart_catalog, "dex_market_entries", list)
    monkeypatch.setattr(chart_catalog, "_LOAD_CACHE", {"key": None, "payload": None})
    actual = chart_catalog.load(path)
    assert actual["markets"] == sorted(kept, key=lambda r: (r["token"], r["venue"], r["market_type"], r["symbol"]))
    assert actual["count"] == len(kept)
    assert "Ourbit|Spot" not in actual["health"]
    assert actual["health"]["Gate|Spot"] == {"status": "ok"}
    assert json.loads(path.read_text())["markets"] == kept + removed


def test_snapshot_and_delta_filter_before_actual_row_construction(tmp_path, monkeypatch):
    path = tmp_path / "api_discovery_latest.json"
    kept = [route(), route(short="Mexc")]
    path.write_text(json.dumps({"api_discovered_rows": [kept[0], route(long="Ourbit")]}))
    api_spreads._fast_quote_delta_path(path).write_text(json.dumps({"rows": [kept[1], route(short="Ourbit")]}))
    monkeypatch.setattr(api_spreads, "_ROW_CACHE", {})
    monkeypatch.setattr(bulk_quotes, "load_funding", dict)
    actual = api_spreads._row_from_api
    built = []

    def observe(raw, **kwargs):
        built.append(raw["route_key"])
        return actual(raw, **kwargs)

    monkeypatch.setattr(api_spreads, "_row_from_api", observe)
    rows, _ = api_spreads._load_api_discovery_rows(path, now=time.time(), metadata={}, rails={})
    assert set(built) == {r["route_key"] for r in kept}
    assert {r.route_key for r in rows} == set(built)


@pytest.mark.parametrize("warm", [False, True])
def test_actual_index_build_drops_old_and_new_ourbit_rows(tmp_path, monkeypatch, warm):
    board = tmp_path / "board"
    output = tmp_path / "views"
    signature = {"board_path": str(board), "generation": 1}
    kept, retired = route(), route(long="Ourbit")
    old = {r["route_key"]: r for r in [kept, retired]}
    store = materialized_views.Store(output)
    store.write_live_route_index(old, source_signature=signature if warm else {**signature, "generation": 0})
    monkeypatch.setattr(live_route_index_worker, "source_signature", lambda _: signature)
    monkeypatch.setattr(api_spreads, "load_public_route_index", lambda: (old, {}))
    monkeypatch.setattr(live_route_index_worker, "_current_generation_rows", lambda _: (old, {}))
    monkeypatch.setattr(live_route_index_worker, "_retained_structural_cex_rows", lambda rows: rows)
    monkeypatch.setattr(live_route_index_worker.coverage_reconciliation, "record_book_coverage", lambda _: {})
    result = live_route_index_worker.build(board, output)
    assert result["routes"] == 1
    assert result["current_routes"] == 1
    assert store.live_route_index(board_path=board) == {kept["route_key"]: kept}


def test_actual_web_restore_filters_previous_release_index(tmp_path, monkeypatch):
    board = tmp_path / "board"
    kept, retired = route(), route(short="Ourbit")
    store = materialized_views.Store(tmp_path / "views")
    store.write_live_route_index({r["route_key"]: r for r in [kept, retired]}, source_signature={"board_path": str(board)})
    universe = warm_query_projection.LiveRouteUniverse()
    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", store)
    monkeypatch.setattr(server, "_ROUTE_INDEX", {"signature": None, "rows": {}})
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)
    assert server.restore_materialized_route_index(board) == 1
    assert server._ROUTE_INDEX["rows"] == {kept["route_key"]: kept}
    visible = universe.target_rows(all_rows=True)[0]
    assert len(visible) == 1
    assert {key: visible[0][key] for key in kept} == kept
    assert universe.template() == {"ok": True}
    monkeypatch.setattr(server, "_MARKET_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_CACHE_INFLIGHT", {})
    monkeypatch.setattr(api_spreads, "attach_funding_history", lambda _: None)
    monkeypatch.setattr(api_spreads, "live_route_updates_for", lambda *a, **k: {})

    def no_discovery(**kwargs):
        raise AssertionError("valid restored index fell back to a broad HTTP build")

    monkeypatch.setattr(api_spreads, "load_spreads", no_discovery)
    page = server.api_market_spreads(board, {"kind": ["FUTURES"]})
    assert page["mode"] == "materialized_live_query_projection"
    assert {r["route_key"] for r in page["rows"]} == {kept["route_key"]}


def test_warm_projection_filters_before_ranking_and_pagination(monkeypatch):
    rows = [route("KEPT", spread=1.0), route("HIGH", long="Ourbit", spread=5.0)]
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({r["route_key"]: r for r in rows})
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)
    monkeypatch.setattr(api_spreads, "attach_funding_history", lambda _: None)
    actual = warm_query_projection.project({}, template={"ok": True}, limit=1, offset=0)
    assert [g["token"] for g in actual["groups"]] == ["KEPT"]
    assert [r["route_key"] for r in actual["rows"]] == [rows[0]["route_key"]]


@pytest.mark.parametrize("retained", [False, True])
def test_funding_eligibility_filters_both_current_and_retained_routes(monkeypatch, retained):
    kept, retired = route(), route(long="Ourbit")
    monkeypatch.setattr(funding_catalog.funding_radar, "routes_for", lambda *a, **k: [route(short="Ourbit")])
    rows = funding_catalog._all_routes(route_kind=None, symbol=None, exchange=None, quote=None,
        include_retained=retained, payloads={"AAA": {"routes": [kept, retired]}})
    assert [r["route_key"] for r in rows] == [kept["route_key"]]


def test_funding_collapse_preserves_eligible_alternative_to_disabled_long():
    kept = route(long="Mexc", short="Gate", spread=1.0)
    excluded = route(long="Ourbit", short="Gate", spread=8.0)
    assert funding_catalog.collapse_to_short_legs([excluded, kept]) == [kept]


def test_old_collapsed_funding_cache_requires_rebuild(tmp_path, monkeypatch):
    path = tmp_path / "funding.json"
    monkeypatch.setattr(funding_catalog, "DEFAULT_CACHE_PATH", path)
    envelope = {"schema": funding_catalog.PERSISTED_SCHEMA, "saved_at_unix": 1,
                "payloads": {"AAA": {"routes": [route(long="Ourbit")]}}}
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="venue_policy_changed"):
        funding_catalog._read_persisted_cache()
    envelope["payloads"]["AAA"]["routes"] = [route()]
    path.write_text(json.dumps(envelope))
    assert funding_catalog._read_persisted_cache() == (envelope["payloads"], 1)


@pytest.mark.parametrize("location", ["rows", "routes", "best_route", "best_funding_route", "top_edges", "top_funding"])
def test_persisted_ranked_pages_cannot_reintroduce_ourbit(tmp_path, location):
    kept, retired = route(), route(long="Ourbit")
    payload = {"ok": True, "rows": [kept], "groups": [{"token": "AAA", "routes": [kept]}]}
    if location == "rows":
        payload["rows"].append(retired)
    elif location == "routes":
        payload["groups"][0]["routes"].append(retired)
    elif location in {"top_edges", "top_funding"}:
        payload[location] = [{"token": "AAA", "routes": [retired]}]
    else:
        payload["groups"][0][location] = retired
    store = materialized_views.Store(tmp_path)
    writer = materialized_views.GenerationWriter(store, required_queries=({},), source_signature={})
    writer.write_view({}, payload)
    writer.write_route_index({})
    writer.publish()
    assert store.payload_for({}) is None


def test_normal_quote_sweep_does_not_call_ourbit_or_build_its_board_priority(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(ourbit_quotes, "sweep", lambda **k: asked.append("Ourbit") or 1)
    monkeypatch.setattr(bulk_quotes, "_ourbit_depth_priority", lambda: asked.append("full-board") or [])
    monkeypatch.setattr(bulk_quotes, "VENUE_IDS", {"Aster": "aster", "Gate": "gateio", "Mexc": "mexc"})
    monkeypatch.setattr(bulk_quotes, "sweep_venue", lambda venue, **k: asked.append(venue) or 1)
    monkeypatch.setattr(bulk_quotes, "CURSOR_PATH", tmp_path / "cursor")
    monkeypatch.setattr(bulk_quotes.fair_price, "write", lambda *a, **k: None)
    bulk_quotes.sweep(store=SimpleNamespace(), budget_seconds=30, workers=1)
    assert asked == ["Aster", "Gate", "Mexc", "Aster"]


def test_funding_sweep_skips_ourbit_and_expires_its_preexisting_rates(tmp_path, monkeypatch):
    asked = []
    path = tmp_path / "funding.json"
    stamp = time.time()
    path.write_text(json.dumps({"legs": {"Ourbit|AAA/USDT:USDT": {"rate_pct": 1}},
                                "leg_updated_at": {"Ourbit|AAA/USDT:USDT": stamp}}))

    class Refresher:
        def _bulk_funding_rates(self, venue):
            asked.append(venue)
            return {"AAA/USDT:USDT": {"current_funding_pct": 0.02, "funding_interval_hours": 8}}

        def close(self):
            pass

    monkeypatch.setattr(fast_quotes, "FastQuoteRefresher", Refresher)
    bulk_quotes.sweep_funding(["Ourbit", "Gate", "XT"], cache_path=path, merge_existing=True)
    assert asked == ["Gate", "XT"]
    assert set(json.loads(path.read_text())["legs"]) == {"Gate|AAA/USDT:USDT", "XT|AAA/USDT:USDT"}


def test_fast_quote_selection_and_fallback_funding_obey_policy(monkeypatch):
    kept, excluded = route(), route(long="Ourbit")
    assert fast_quotes._cannot_lead_public_lane(excluded, rails={}, metadata={})
    assert not fast_quotes._cannot_lead_public_lane(kept, rails={}, metadata={})
    refresher = fast_quotes.FastQuoteRefresher()
    asked = []
    monkeypatch.setattr(refresher, "_bulk_funding_rates", lambda venue: asked.append(venue) or {})
    refresher.refresh_all_funding({"api_discovered_rows": [copy.deepcopy(kept), excluded]})
    assert set(asked) == {"Gate", "Bybit"}


def test_actual_fast_refresh_filters_disabled_routes_before_quote_selection(tmp_path, monkeypatch):
    path = tmp_path / "discovery.json"
    kept, excluded = route(), route(long="Ourbit")
    path.write_text(json.dumps({"api_discovered_rows": [kept, excluded]}))
    monkeypatch.setattr(fast_quotes, "_external_funding_is_fresh", lambda: True)
    monkeypatch.setattr(fast_quotes.public_rails, "load_public_rails", dict)
    monkeypatch.setattr(fast_quotes.token_metadata, "load_token_metadata", dict)
    seen = []

    def select(lanes, **kwargs):
        seen.extend(r["route_key"] for rows in lanes.values() for r in rows)
        return []

    monkeypatch.setattr(fast_quotes, "_select_fast_quote_rows", select)
    fast_quotes.FastQuoteRefresher().refresh(path, deadline_seconds=1)
    assert seen == [kept["route_key"]]
