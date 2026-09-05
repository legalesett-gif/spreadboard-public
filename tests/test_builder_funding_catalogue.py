"""Native builder names must reach every existing CEX funding alternative."""

import time

import pytest

from spreadboard import catalog_pairs, fast_quotes, funding_catalog, live, live_book_cache


@pytest.fixture
def markets(monkeypatch, tmp_path):
    definitions = [
        {"token": token, "venue": venue, "market_type": "Futures", "quote": quote,
         "symbol": f"{token}/{quote}:{quote}", "contract_size": 1}
        for token, venue, quote in [("ANSEM", "Mexc", "USDT"), ("ANSEM", "Gate", "USDT"),
                                    ("PARA-ANSEM", "Hyperliquid", "USDC"),
                                    ("WRONG-ANSEM", "Hyperliquid", "USDC"),
                                    ("QUIET-ANSEM", "Hyperliquid", "USDC")]
    ]
    prices = {"ANSEM": 100, "PARA-ANSEM": 101, "WRONG-ANSEM": 40}
    books = {
        live_book_cache.cache_key(m["venue"], m["market_type"], m["symbol"]):
        live_book_cache.CachedBook(bids=[[prices[m["token"]] * .999, 1000]],
                                  asks=[[prices[m["token"]] * 1.001, 1000]],
                                  quote_ts_us=int(time.time() * 1_000_000))
        for m in definitions if m["token"] in prices
    }
    class Store:
        def load_all(self, **kw):
            return books

        def close(self):
            pass

    path = tmp_path / "books.sqlite3"
    path.touch()
    monkeypatch.setattr(live_book_cache, "DEFAULT_PATH", path)
    monkeypatch.setattr(live_book_cache, "LiveBookStore", Store)
    monkeypatch.setattr(live_book_cache, "load_live_book", lambda v, t, s, **kw: books.get(live_book_cache.cache_key(v, t, s)))
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", lambda: {"markets": definitions})
    monkeypatch.setattr(catalog_pairs.public_rails, "load_public_rails", dict)
    rates = {"Mexc|ANSEM/USDT:USDT": {"rate_pct": .02, "interval_hours": 4, "age_seconds": 1},
             "Gate|ANSEM/USDT:USDT": {"rate_pct": .01, "interval_hours": 4, "age_seconds": 1},
             "Hyperliquid|PARA-ANSEM/USDC:USDC": {"rate_pct": .05, "interval_hours": 1, "age_seconds": 1}}
    monkeypatch.setattr(catalog_pairs.bulk_quotes, "load_funding", lambda: rates)
    monkeypatch.setattr(funding_catalog.venue_funding_history, "load", dict)
    monkeypatch.setattr(funding_catalog.funding_radar, "routes_for", lambda *a, **kw: [])
    return definitions


@pytest.mark.parametrize("surface", ["single", "bulk", "funding", "navigation", "summary"])
def test_builder_contract_joins_canonical_token_on_all_surfaces(monkeypatch, markets, surface):
    # A subset request must find the native alias too, without expanding a
    # second PARA-ANSEM group or depending on the bounded provider route list.
    if surface == "single":
        rows = catalog_pairs.for_token("ANSEM", use_cache=False)["routes"]
    elif surface == "summary":
        summary = catalog_pairs.all_token_summaries()["ANSEM"]
        assert summary["quoteable_pair_count"] == 6
        assert summary["best_funding_route"]["short_market_symbol"] == "PARA-ANSEM/USDC:USDC"
        return
    else:
        payloads = catalog_pairs.for_tokens(["ANSEM"])
        assert set(payloads) == {"ANSEM"}
        if surface == "bulk":
            rows = payloads["ANSEM"]["routes"]
        else:
            monkeypatch.setattr(funding_catalog, "_complete_payloads", lambda: payloads)
            monkeypatch.setattr(funding_catalog, "_resident_live_overlay", lambda rows: rows)
            monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
            if surface == "navigation":
                pages = funding_catalog.build_navigation_pages(limit=25)
                assert pages[("FUTURES", "now")]["groups"][0]["routes"][0]["short_market_symbol"] == "PARA-ANSEM/USDC:USDC"
                return
            rows = funding_catalog.page(window="now", route_kind="FUTURES", symbol="ANSEM")["rows"]
    assert len(rows) == 6
    route = next(r for r in rows if r["long_venue"] == "Mexc" and r["short_venue"] == "Hyperliquid")
    assert route["short_market_symbol"] == "PARA-ANSEM/USDC:USDC"
    assert route["funding_daily_pct"] == pytest.approx(1.08)
    assert not route["mirage_guarded"]
    assert all("WRONG-" not in str(r) and "QUIET-" not in str(r) for r in rows)


@pytest.mark.parametrize("symbol,coin,dex", [("PARA-ANSEM/USDC:USDC", "para:ANSEM", "para"),
                                           ("XYZ-AMZN/USDC:USDC", "xyz:AMZN", "xyz"),
                                           ("BTC/USDC:USDC", "BTC", None)])
def test_current_funding_requests_exact_builder_namespace(monkeypatch, symbol, coin, dex):
    def public(url, payload):
        expected = {"type": "metaAndAssetCtxs"}
        if dex:
            expected["dex"] = dex
        assert payload == expected
        return [{"universe": [{"name": coin}]}, [{"funding": "0", "oraclePx": "100"}]]
    monkeypatch.setattr(fast_quotes, "_json_post", public)
    fields = fast_quotes._native_current_funding("Hyperliquid", symbol)
    assert fields["current_funding_pct"] == 0
    assert fields["funding_interval_hours"] == 1
    assert fields["funding_interval_assumed"] is False
    assert fields["index_price"] == 100


def test_delisted_core_funding_is_not_current(monkeypatch):
    monkeypatch.setattr(fast_quotes, "_json_post", lambda *a: [
        {"universe": [{"name": "OLD", "isDelisted": True}]}, [{"funding": "0.01"}]])
    assert fast_quotes._native_current_funding("Hyperliquid", "OLD/USDC:USDC") == {}


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "-Infinity"])
def test_nonfinite_hyperliquid_funding_is_unavailable(monkeypatch, value):
    monkeypatch.setattr(fast_quotes, "_json_post", lambda *a: [
        {"universe": [{"name": "BTC"}]}, [{"funding": value}]])
    assert fast_quotes._native_current_funding("Hyperliquid", "BTC/USDC:USDC") == {}


@pytest.mark.parametrize("symbol,coin", [("PARA-ANSEM/USDC:USDC", "para:ANSEM"),
                                       ("para:ANSEM", "para:ANSEM"),
                                       ("BTC/USDC:USDC", "BTC")])
def test_native_history_keeps_the_exact_builder_identity(monkeypatch, symbol, coin):
    calls = []
    def public(url, *, payload):
        calls.append(payload)
        return [{"time": int(time.time() * 1000)-3_600_000, "fundingRate": "0.0001"}]
    monkeypatch.setattr(live, "_public_json", public)
    result = live._fetch_native_funding_24h("hyperliquid", symbol)
    assert calls[0]["coin"] == coin
    assert calls[0]["type"] == "fundingHistory"
    assert result["history"][0]["rate_pct"] == pytest.approx(.01)


def test_native_book_keeps_lowercase_namespace(monkeypatch):
    calls = []
    def public(url, payload):
        calls.append(payload)
        return {"levels": [[{"px": "100", "sz": "10"}], [{"px": "101", "sz": "10"}]]}
    monkeypatch.setattr(fast_quotes, "_json_post", public)
    assert fast_quotes._native_order_book("Hyperliquid", "Futures", "para:ANSEM") is not None
    assert calls == [{"type": "l2Book", "coin": "para:ANSEM"}]


@pytest.mark.parametrize("native,expected_count", [("para:ANSEM", 1), ("other:ANSEM", 2)])
def test_native_and_catalogue_route_merge_preserves_exact_contracts(markets, native, expected_count):
    rows = catalog_pairs.for_tokens(["ANSEM"])["ANSEM"]["routes"]
    route = next(r for r in rows if r["long_venue"] == "Mexc" and r["short_venue"] == "Hyperliquid")
    provider = {**route, "short_market_symbol": native, "route_key": "provider"}
    # A third route defeats the old structural two-row fallback and proves
    # canonical/native equality is understood as an economic identity.
    unrelated = {**route, "short_market_symbol": "third:ANSEM", "route_key": "unrelated"}
    result = catalog_pairs.with_routes({"routes": [route]}, [provider, unrelated])
    assert len(result["routes"]) == expected_count + 1
    # Two distinct complete identities must not collapse through that fallback.
    pair = catalog_pairs.with_routes({"routes": [route]}, [provider])
    assert len(pair["routes"]) == expected_count
