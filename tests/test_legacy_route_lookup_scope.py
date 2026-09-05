"""A saved legacy route must not materialize the full discovery universe."""
import json
import time
from functools import partial

import pytest

from spreadboard import api_spreads, bulk_quotes, server, warm_query_projection


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    now = time.time()

    def raw(token, suffix="main", spread=1.0):
        return {
            "token": token, "route_key": f"{token.strip().upper()}|{suffix}",
            "long_venue": "Gate", "long_market_type": "Futures",
            "short_venue": {"main": "Bybit", "provider": "Mexc", "delta-only": "OKX"}[suffix],
            "short_market_type": "Futures",
            "quote_ts_us": int(now * 1_000_000),
            "executable_spread_pct": spread,
        }

    path = tmp_path / "api_discovery_latest.json"
    path.write_text(json.dumps({
        "api_discovered_rows": [raw("AAA"), raw("AAAB"), raw("BBB")],
        "dex_discovered_rows": [raw(" aaa ", "provider")],
    }))
    (tmp_path / "api_discovery_fast_quotes.json").write_text(json.dumps({
        "rows": [raw("AAA", spread=7.5), raw("AAA", "delta-only"), raw("BBB", "delta-only")],
    }))
    monkeypatch.setattr(api_spreads, "_ROW_CACHE", {})
    monkeypatch.setattr(api_spreads, "_RESULT_CACHE", {})
    monkeypatch.setattr(api_spreads, "_live_books", dict)
    monkeypatch.setattr(api_spreads.token_metadata, "load_token_metadata", dict)
    monkeypatch.setattr(api_spreads.public_rails, "load_public_rails", dict)
    monkeypatch.setattr(bulk_quotes, "load_funding", dict)
    return path, tmp_path / "board.jsonl", now


def test_actual_legacy_resolver_builds_only_its_token_and_keeps_exact_key(snapshot, monkeypatch):
    path, board_path, _ = snapshot
    monkeypatch.setattr(server, "_ROUTE_INDEX", {"rows": {}, "signature": None})
    monkeypatch.setattr(server, "_ROUTE_COMPAT_ROWS", {})
    monkeypatch.setattr(server, "_ROUTE_COMPAT_PATHS", {})
    monkeypatch.setattr(server, "_ROUTE_MISS_CACHE", set())
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda key: None)
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", warm_query_projection.LiveRouteUniverse())
    monkeypatch.setattr(api_spreads, "load_spreads", partial(api_spreads.load_spreads, api_path=path))
    original = api_spreads._row_from_api
    constructed = []

    def scoped(raw, **kwargs):
        token = str(raw.get("token") or "").upper().strip()
        assert token == "AAA", "one saved route constructed an unrelated token"
        constructed.append(raw["route_key"])
        return original(raw, **kwargs)

    monkeypatch.setattr(api_spreads, "_row_from_api", scoped)
    selected = server._find_canonical_route("AAA|Gate|Futures|Bybit|Futures", board_path)
    assert selected["route_key"] == "AAA|Gate|Futures|Bybit|Futures"
    assert selected["executable_spread_pct"] == 7.5
    assert server._find_canonical_route("AAA|Gate|Futures|Mexc|Futures", board_path)["short_venue"] == "Mexc"
    assert server._find_canonical_route("AAA|Gate|Futures|OKX|Futures", board_path)["short_venue"] == "OKX"
    assert server._find_canonical_route("AAA|missing", board_path) is None
    assert set(constructed) == {"AAA|main", "AAA|provider", "AAA|delta-only"}
    assert len(api_spreads._ROW_CACHE) == 1
    assert {row.token for entry in api_spreads._ROW_CACHE.values() for row in entry[1]} == {"AAA"}


@pytest.mark.parametrize("scoped_first", [False, True])
def test_result_and_row_caches_do_not_mix_scope_with_normal_fuzzy_search(snapshot, scoped_first):
    path, board_path, now = snapshot
    kwargs = {"api_path": path, "board_path": board_path, "q": "AAA", "now": now,
              "include_stale": True, "include_unverified": True, "limit": None}
    for exact in ([" aaa ", None] if scoped_first else [None, " aaa "]):
        result = api_spreads.load_spreads(**kwargs, exact_token=exact)
        assert {row["token"] for row in result["rows"]} == ({"AAA"} if exact else {"AAA", "AAAB"})
        cached_rows = next(iter(api_spreads._ROW_CACHE.values()))[1]
        assert {row.token for row in cached_rows} == ({"AAA"} if exact else {"AAA", "AAAB", "BBB"})
    # Scoped requests share the existing single-entry bound; there is no new
    # per-token resident index, and another token cannot reuse the prior rows.
    result = api_spreads.load_spreads(**{**kwargs, "q": "BBB"}, exact_token="BBB")
    assert {row["token"] for row in result["rows"]} == {"BBB"}
    assert len(api_spreads._ROW_CACHE) == 1
