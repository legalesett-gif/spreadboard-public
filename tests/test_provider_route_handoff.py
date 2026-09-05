"""The actual finalizer and index pipeline must carry exact builder futures."""

import json
import sys
import time

import pytest

from scripts import live_route_index_worker as worker
from scripts import run_spreadboard_service as service
from scripts import snapshot_finalize_worker as finalizer
from spreadboard import api_spreads, live_book_cache, materialized_views, provider_routes


def raw_row():
    return {
        "token": "OPENAI", "long_venue": "Gate", "short_venue": "Hyperliquid",
        "long_market_type": "Futures", "short_market_type": "Futures",
        "source_name": "hyperliquid_builder_dex", "source_kind": "dex_discovered",
        "quote_ts_us": 1, "executable_spread_pct": 5.0,
        "blockers": ["depth_unverified", "executor_attestation_missing"],
        "notes": {"route_inputs": {
            "long": {"symbol": "OPENAI/USDT:USDT", "ask": 1400, "bid": 1399},
            "short": {"symbol": "io:OAI", "ask": 1471, "bid": 1470},
        }},
    }


def test_finalizer_bootstrap_then_warm_index_uses_only_compact_artifact(tmp_path, monkeypatch):
    discovery = tmp_path / "discovery.json"
    snapshot = {"dex_discovered_rows": [raw_row()], "api_discovered_rows": [
        {"source_name": "cex", "token": "UNRELATED"},
    ]}
    discovery.write_text(json.dumps(snapshot))
    monkeypatch.setattr(sys, "argv", ["finalize", "--stage", "providers",
        "--staging-path", str(tmp_path / "unused"), "--published-path", str(discovery)])
    assert finalizer.main() == 0
    artifact = provider_routes.load(discovery)
    assert artifact["api_discovered_rows"] == []
    assert artifact["dex_discovered_rows"] == [raw_row()]

    monkeypatch.setattr(api_spreads, "DEFAULT_API_DISCOVERY_PATH", discovery)
    monkeypatch.setattr(api_spreads.token_metadata, "load_token_metadata", dict)
    monkeypatch.setattr(api_spreads.public_rails, "load_public_rails", dict)
    monkeypatch.setattr(api_spreads, "_apply_fast_quote_delta", lambda *a, **kw: [])
    monkeypatch.setattr(api_spreads, "_complete_current_catalogue_rows", lambda *a, **kw: ([], {}))
    monkeypatch.setattr(worker.chart_catalog, "load", lambda: {"markets": []})
    monkeypatch.setattr(worker.coverage_reconciliation, "record_book_coverage", lambda _: {})
    monkeypatch.setattr(worker.catalog_pairs, "dex_futures_routes", lambda *a, **kw: [])
    monkeypatch.setattr(api_spreads, "load_public_route_index", lambda: pytest.fail("broad discovery parsed"))
    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    stamp = int(time.time() * 1e6)
    for venue, symbol, price in [("Gate", "OPENAI/USDT:USDT", 1400),
                                 ("Hyperliquid", "IO-OAI/USDC:USDC", 1470)]:
        store.put(venue, "Futures", symbol, bids=[[price, 10]], asks=[[price + 1, 10]], quote_ts_us=stamp)
    books = store.load_all(max_age_seconds=90)
    monkeypatch.setattr(api_spreads, "_live_books", lambda: books)
    board = tmp_path / "board.jsonl"
    board.write_text("")
    output = tmp_path / "materialized"
    storage = materialized_views.Store(output)
    storage.write_live_route_index({"seed": {"route_key": "seed"}}, source_signature=worker.source_signature(board))
    worker.build(board, output)
    rows = list(storage.live_route_index(board_path=board).values())
    assert len(rows) == 1
    row = rows[0]
    assert row["short_venue"] == "Hyperliquid"
    assert row["short_market_symbol"] == "io:OAI"
    assert row["quote_ts_us"] == stamp
    assert row["executable_spread_pct"] == pytest.approx((1470 / 1401 - 1) * 100)
    assert "executor_attestation_missing" in row["blockers"]

    # A cold book cache keeps identity available, without inventing current quotes.
    monkeypatch.setattr(api_spreads, "_live_books", dict)
    worker.build(board, output)
    cold = next(iter(storage.live_route_index(board_path=board).values()))
    assert cold["quote_ts_us"] == 1
    assert not api_spreads.spread_quote_current(cold)


def test_mismatched_or_oversized_provider_artifact_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "discovery.json"
    path.write_text("{}")
    provider_routes.publish({"dex_discovered_rows": [raw_row()]}, path)
    path.write_text('{"new":true}')
    assert provider_routes.load(path) is None
    provider_routes.publish({"dex_discovered_rows": [raw_row()]}, path)
    monkeypatch.setattr(provider_routes, "MAX_BYTES", 10)
    assert provider_routes.load(path) is None


def test_builder_cache_alias_never_collapses_namespaces(tmp_path):
    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    stamp = int(time.time() * 1e6)
    store.put("Hyperliquid", "Futures", "IO-OAI/USDC:USDC",
              bids=[[10, 5]], asks=[[11, 5]], quote_ts_us=stamp)
    assert store.get("Hyperliquid", "Futures", "io:OAI").quote_ts_us == stamp
    assert store.get("Hyperliquid", "Futures", "vntl:OAI") is None
    assert store.get("Hyperliquid", "Futures", "OAI/USDC:USDC") is None


def test_cold_full_index_keeps_source_through_public_serialization(tmp_path, monkeypatch):
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps({"dex_discovered_rows": [raw_row()]}))
    monkeypatch.setattr(api_spreads, "_live_books", dict)
    monkeypatch.setattr(api_spreads, "_expand_current_dex_futures_pairs", lambda rows, **kw: rows)
    monkeypatch.setattr(api_spreads.token_metadata, "load_token_metadata", dict)
    monkeypatch.setattr(api_spreads.public_rails, "load_public_rails", dict)
    monkeypatch.setattr(worker.chart_catalog, "load", lambda: {"markets": []})
    monkeypatch.setattr(worker.catalog_pairs, "for_tokens", lambda *a, **kw: {})
    rows, _ = api_spreads.load_public_route_index(api_path=path)
    assert len(rows) == 1
    assert next(iter(rows.values()))["source_name"] == "hyperliquid_builder_dex"


def test_normal_snapshot_publish_also_publishes_provider_artifact(tmp_path, monkeypatch):
    staging, published = tmp_path / "staging.json", tmp_path / "published.json"
    staging.write_text(json.dumps({"dex_discovered_rows": [raw_row()]}))
    monkeypatch.setattr(sys, "argv", ["finalize", "--stage", "publish",
        "--staging-path", str(staging), "--published-path", str(published)])
    monkeypatch.setattr(finalizer.market_history, "record_snapshot", lambda _: 0)
    def exit_process(code):
        raise SystemExit(code)
    monkeypatch.setattr(finalizer.os, "_exit", exit_process)
    with pytest.raises(SystemExit) as result:
        finalizer.main()
    assert result.value.code == 0
    assert provider_routes.load(published)["dex_discovered_rows"] == [raw_row()]


def test_missing_handoff_retains_the_index_instead_of_silently_erasing_providers(tmp_path, monkeypatch):
    discovery = tmp_path / "discovery.json"
    discovery.write_text("{}")
    monkeypatch.setattr(api_spreads, "DEFAULT_API_DISCOVERY_PATH", discovery)
    monkeypatch.setattr(api_spreads, "_live_books", dict)
    monkeypatch.setattr(api_spreads.public_rails, "load_public_rails", dict)
    with pytest.raises(RuntimeError, match="provider_artifact_unavailable"):
        worker._current_dex_rows({}, metadata={}, now=time.time())


def test_collector_repairs_missing_side_artifact_before_index_build(tmp_path, monkeypatch):
    from types import SimpleNamespace
    discovery = tmp_path / "discovery.json"
    discovery.write_text("{}")
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "collector")
    monkeypatch.setattr(service, "SNAPSHOT_PATH", discovery)
    calls = []

    def finalize(stage):
        calls.append(stage)
        return provider_routes.publish({}, discovery)

    def build(*a, **kw):
        assert provider_routes.load(discovery) is not None
        calls.append("index")
        return SimpleNamespace(returncode=0, timed_out=False, stdout='{"routes":1}', stderr="")

    monkeypatch.setattr(service, "_finalize_snapshot", finalize)
    monkeypatch.setattr(service, "_run_worker", build)
    assert service._refresh_live_route_index(install=False)
    assert calls == ["providers", "index"]


def test_provider_bootstrap_waits_for_the_heavy_slot(monkeypatch):
    import threading
    monkeypatch.setattr(service, "_HEAVY_CHILD_SLOT", threading.Semaphore(0))
    monkeypatch.setattr(service, "HEAVY_CHILD_SLOT_WAIT_SECONDS", 0.001)
    result = service._run_worker([
        sys.executable, "scripts/snapshot_finalize_worker.py", "--stage", "providers",
    ], timeout=1)
    assert result.stderr == "heavy_child_slot_busy"


def test_bootstrap_cannot_label_old_rows_with_a_new_discovery_signature(tmp_path, monkeypatch):
    discovery = tmp_path / "discovery.json"
    discovery.write_text(json.dumps({"dex_discovered_rows": [raw_row()]}))
    original = provider_routes.publish

    def race(snapshot, path, **kwargs):
        # A new generation arrives after finalizer's read/check but before
        # its side-artifact write. The old payload must remain detectably old.
        path.write_text('{"dex_discovered_rows":[]}')
        return original(snapshot, path, **kwargs)

    monkeypatch.setattr(provider_routes, "publish", race)
    monkeypatch.setattr(sys, "argv", ["finalize", "--stage", "providers",
        "--staging-path", str(tmp_path / "unused"), "--published-path", str(discovery)])
    assert finalizer.main() == 0
    assert provider_routes.load(discovery) is None
