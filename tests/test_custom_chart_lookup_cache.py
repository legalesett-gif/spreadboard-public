"""Repeated custom charts reuse only an exact, bounded generation match."""
from pathlib import Path

import pytest

from spreadboard import server, warm_query_projection


@pytest.fixture
def custom_index(monkeypatch):
    custom = {
        "route_key": "CUSTOM:test", "token": "ESPORTS",
        "long_venue": "OKX DEX 56", "long_market_type": "Spot",
        "long_market_symbol": "ESPORTS", "short_venue": "Mexc",
        "short_market_type": "Futures", "short_market_symbol": "ESPORTS/USDT:USDT",
        "dex_chain": "56", "dex_contract": "0xabc",
    }
    canonical = {**custom, "route_key": "canonical"}
    monkeypatch.setattr(server, "_ROUTE_INDEX", {"rows": {"canonical": canonical}, "signature": None})
    monkeypatch.setattr(server, "_ROUTE_COMPAT_ROWS", {})
    monkeypatch.setattr(server, "_ROUTE_COMPAT_PATHS", {})
    monkeypatch.setattr(server, "_ROUTE_MISS_CACHE", set())
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda key: {**custom, "route_key": key})
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", warm_query_projection.LiveRouteUniverse())
    return custom, canonical


@pytest.mark.parametrize("has_match", [False, True])
def test_repeated_custom_chart_does_not_rescan_canonical_universe(custom_index, has_match):
    custom, canonical = custom_index

    class ScanOnce(dict):
        calls = 0

        def values(self):
            self.calls += 1
            assert self.calls == 1, "repeated custom chart scanned every route again"
            return super().values()

    rows = ScanOnce({"canonical": canonical} if has_match else {})
    server._ROUTE_INDEX["rows"] = rows
    path = Path("board.json")
    for _ in range(3):
        selected = server._find_canonical_route(custom["route_key"], path)
        assert selected == (canonical if has_match else custom)
    assert rows.calls == 1


def test_custom_chart_cache_is_cleared_by_actual_index_restore(monkeypatch, custom_index, tmp_path):
    custom, canonical = custom_index
    path = tmp_path / "board.json"
    assert server._find_canonical_route(custom["route_key"], path) is canonical
    replacement = {**canonical, "route_key": "new-canonical"}

    class Store:
        def status(self):
            return {}

        def live_route_index_status(self):
            return {"ready": True, "built_at_unix": 1}

        def live_route_index(self, **kwargs):
            return {replacement["route_key"]: replacement}

        def payload_for(self, *args, **kwargs):
            return None

    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", Store())
    assert server.restore_materialized_route_index(path) == 1
    assert server._find_canonical_route(custom["route_key"], path) is replacement


@pytest.mark.parametrize("change", ["board_path", "contract"])
def test_custom_cache_cannot_bypass_path_or_exact_contract(custom_index, tmp_path, change):
    custom, canonical = custom_index
    path = tmp_path / "first.json"
    assert server._find_canonical_route(custom["route_key"], path) is canonical
    if change == "board_path":
        path = tmp_path / "second.json"
        server._ROUTE_INDEX["rows"] = {}
    else:
        custom["dex_contract"] = "0xdef"
    selected = server._find_canonical_route(custom["route_key"], path)
    assert selected == custom
    assert selected is not canonical


def test_custom_cache_stays_bounded_and_evicts_matching_path(monkeypatch, custom_index):
    monkeypatch.setattr(server, "_ROUTE_COMPAT_ROW_LIMIT", 2)
    for key in ("CUSTOM:one", "CUSTOM:two", "CUSTOM:three"):
        assert server._find_canonical_route(key, Path("board.json")) is custom_index[1]
    assert set(server._ROUTE_COMPAT_ROWS) == {"CUSTOM:two", "CUSTOM:three"}
    assert set(server._ROUTE_COMPAT_PATHS) == set(server._ROUTE_COMPAT_ROWS)


def test_install_race_cannot_cache_an_old_custom_match(custom_index):
    custom, canonical = custom_index
    replacement = {**canonical, "route_key": "new-canonical"}

    class ReplacedDuringScan(dict):
        def values(self):
            server._ROUTE_INDEX["rows"] = {replacement["route_key"]: replacement}
            return super().values()

    server._ROUTE_INDEX["rows"] = ReplacedDuringScan({"canonical": canonical})
    path = Path("board.json")
    assert server._find_canonical_route(custom["route_key"], path) is canonical
    assert custom["route_key"] not in server._ROUTE_COMPAT_ROWS
    assert server._find_canonical_route(custom["route_key"], path) is replacement
