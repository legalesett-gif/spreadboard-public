"""Production warm reads must carry the oracle as well as the displayed price."""

import time

import pytest

from scripts import materialized_view_worker
from spreadboard import (
    api_spreads,
    bulk_quotes,
    catalog_pairs,
    live_book_cache,
    materialized_views,
    server,
    warm_query_projection,
)


def route(kind="FUTURES"):
    spot = kind == "SPOT-FUTURES"
    return {
        "route_key": kind, "token": "OPENAI", "route_kind": kind,
        "long_venue": "Gate", "long_market_type": "Spot" if spot else "Futures",
        "long_market_symbol": "OPENAI/USDT" if spot else "OPENAI/USDT:USDT",
        "short_venue": "Hyperliquid", "short_market_type": "Futures",
        "short_market_symbol": "io:OAI", "long_quote": "USDT", "short_quote": "USDC",
        "source_name": "hyperliquid_builder_dex",
        "long_price": 850 if spot else 1400, "short_price": 1470,
        "long_ask": 850 if spot else 1400, "short_bid": 1470,
        "executable_spread_pct": 70 if spot else 5,
        "quote_ts_us": int(time.time() * 1e6),
        "short_index_price": 850,
        "notes": {"route_inputs": {"short": {"index_price": 850}}},
        "blockers": ["depth_unverified"], "depth_unverified": True,
        "long_volume_24h_usd": 1000000, "short_volume_24h_usd": 1000000,
    }


@pytest.fixture
def live(tmp_path, monkeypatch):
    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    stamp = int(time.time() * 1e6)
    for venue, kind, symbol, price in [
        ("Gate", "Spot", "OPENAI/USDT", 850),
        ("Gate", "Futures", "OPENAI/USDT:USDT", 1400),
        ("Hyperliquid", "Futures", "IO-OAI/USDC:USDC", 1470),
    ]:
        store.put(venue, kind, symbol, bids=[[price, 0]], asks=[[price + 1, 0]], quote_ts_us=stamp)
    books = store.load_all(max_age_seconds=90)
    monkeypatch.setattr(live_book_cache, "load_live_books_by_keys", lambda *a, **kw: books)
    monkeypatch.setattr(api_spreads, "_with_retained_books", lambda current, keys: current)
    monkeypatch.setattr(api_spreads, "_fast_quote_updates_for", lambda rows: {})
    rates = {"Hyperliquid|io:OAI": {"index_price": 1466, "observed_at": time.time(), "rate_pct": 0.01, "interval_hours": 1}}
    monkeypatch.setattr(bulk_quotes, "load_funding", lambda: rates)
    return books, rates


@pytest.mark.parametrize("partial", [False, True])
def test_live_api_filters_spot_trap_after_resident_price_refresh(tmp_path, monkeypatch, live, partial):
    rows = [route(), route("SPOT-FUTURES")]
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({r["route_key"]: r for r in rows})
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)
    monkeypatch.setattr(catalog_pairs, "for_token", lambda *a, **kw: {"routes": [rows[0]], "token": "OPENAI"})
    if partial:
        universe.refresh_route_kinds({"FUTURES", "SPOT-FUTURES"})
    else:
        universe.refresh()
    result = server.api_market_spreads(tmp_path / "board.jsonl", {"q": ["OPENAI"]})
    assert [r["route_kind"] for r in result["rows"]] == ["FUTURES"]


def test_retained_price_takes_new_index_and_expires_it_without_refresh(monkeypatch, live):
    books, rates = live
    r = route("SPOT-FUTURES")
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({r["route_key"]: r})
    universe.refresh()
    books.clear()  # Partial read: only its already-current price may be retained.
    rates["Hyperliquid|io:OAI"]["index_price"] = 1450
    universe.refresh()
    rows, _ = universe.target_rows(tokens=["OPENAI"])
    assert api_spreads._leg_index_price(rows[0], "short") == 1450
    assert api_spreads.spot_disagrees_with_perp_index(rows[0])
    observed = rates["Hyperliquid|io:OAI"]["observed_at"]
    monkeypatch.setattr(api_spreads.time, "time", lambda: observed + bulk_quotes.FUNDING_MAX_AGE_SECONDS + 1)
    assert api_spreads._leg_index_price(rows[0], "short") is None


def test_retained_price_clears_expired_index_instead_of_using_structural_value(live):
    books, rates = live
    r = route("SPOT-FUTURES")
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({r["route_key"]: r})
    universe.refresh()
    prior, _ = universe.target_rows(tokens=["OPENAI"])
    prior_quote_ts_us = prior[0]["quote_ts_us"]
    books.clear()
    rates.clear()
    universe.refresh()
    rows, _ = universe.target_rows(tokens=["OPENAI"])
    assert api_spreads._leg_index_price(rows[0], "short") is None
    assert rows[0]["quote_ts_us"] == prior_quote_ts_us


def test_cached_http_payload_refresh_carries_index(live):
    payload = {"rows": [route("SPOT-FUTURES")], "groups": []}
    server._apply_spread_freshness(payload)
    assert api_spreads.spot_disagrees_with_perp_index(payload["rows"][0])


def test_materializer_waits_for_publisher_without_replacing_broad_index(tmp_path, monkeypatch):
    board = tmp_path / "board.jsonl"
    board.write_text("")
    store = materialized_views.Store(tmp_path / "views")
    old = {"kept": {"route_key": "kept", "token": "KEEP"}}
    store.write_live_route_index(old, source_signature={"board_path": str(board.resolve()), "discovery": [1, 10]})
    pointer = store.live_route_pointer_path.read_bytes()
    monkeypatch.setattr(materialized_view_worker, "source_signature", lambda _: {"board_path": str(board.resolve()), "discovery": [2, 10]})
    monkeypatch.setattr(materialized_view_worker.api_spreads, "load_public_route_index", lambda: ({}, {}))
    monkeypatch.setattr(materialized_view_worker.service, "_materialized_view_queries", tuple)
    with pytest.raises(RuntimeError, match="awaiting_current_live_route_index"):
        materialized_view_worker.build(board, store.root)
    assert store.live_route_pointer_path.read_bytes() == pointer
    assert store.live_route_index() == old
