"""Native chart, alert and batch calls must not collect an unrelated web heap."""

from __future__ import annotations

from threading import Lock

import pytest

from spreadboard import alerts, fast_quotes, server


@pytest.fixture
def native_quote(monkeypatch):
    route = {
        "route_key": "GC-TEST|Aster|Futures|Bybit|Futures",
        "token": "GC-TEST",
        "route_kind": "FUTURES",
        "long_venue": "Aster",
        "long_market_type": "Futures",
        "long_market_symbol": "GC-TEST/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "GC-TEST/USDT:USDT",
    }
    state = {"owns_client": False, "quote_ok": True, "closed": [], "gc": [], "samples": []}
    instances = []
    real_init = fast_quotes.FastQuoteRefresher.__init__

    class Client:
        def close(self):
            state["closed"].append(self)

    def init(refresher, *args, **kwargs):
        real_init(refresher, *args, **kwargs)
        instances.append(refresher)
        if state["owns_client"]:
            refresher._clients[("Aster", "Futures")] = Client()
            refresher._client_request_locks[("Aster", "Futures")] = Lock()

    def book(venue, market_type, symbol):
        state["samples"].append((venue, market_type, symbol))
        if not state["quote_ok"]:
            return [], []
        bid, ask = (101.0, 102.0) if venue == "Bybit" else (99.0, 100.0)
        return [(bid, 100.0)], [(ask, 100.0)]

    monkeypatch.setattr(fast_quotes.FastQuoteRefresher, "__init__", init)
    monkeypatch.setattr(fast_quotes.live_book_cache, "load_live_book", lambda *a, **k: None)
    monkeypatch.setattr(fast_quotes, "_native_order_book", book)
    monkeypatch.setattr(fast_quotes, "_native_current_funding", lambda *a, **k: {})
    monkeypatch.setattr(fast_quotes.gc, "collect", lambda: state["gc"].append(True))
    monkeypatch.setattr(server, "_CHART_SAMPLE_CACHE", {})
    monkeypatch.setattr(server, "_CHART_SAMPLE_INFLIGHT", {})
    monkeypatch.setattr(server, "_record_live_chart_route", lambda row: (1, None))
    return route, state, instances


@pytest.mark.parametrize("surface", ["chart", "alert"])
@pytest.mark.parametrize("owns_client", [False, True])
@pytest.mark.parametrize("quote_ok", [False, True])
def test_native_surface_releases_owned_clients_without_unneeded_full_gc(
    monkeypatch, native_quote, surface, owns_client, quote_ok
):
    route, state, instances = native_quote
    state.update(owns_client=owns_client, quote_ok=quote_ok)
    close_calls = []
    real_close = fast_quotes.FastQuoteRefresher.close

    def close(refresher):
        close_calls.append(refresher)
        real_close(refresher)

    monkeypatch.setattr(fast_quotes.FastQuoteRefresher, "close", close)
    if surface == "chart":
        result = server._refresh_chart_route(route)
        assert result["status"] == ("ok" if quote_ok else "unavailable")
    else:
        result = alerts._quote_custom_alert_route(route)
        assert (result is not None) == quote_ok
    assert len(state["samples"]) == 2
    assert close_calls == instances and len(instances) == 1
    assert len(state["closed"]) == int(owns_client)
    assert len(state["gc"]) == int(owns_client)
    assert instances[0]._clients == {}
    assert instances[0]._client_request_locks == {}


@pytest.mark.parametrize("owns_client", [False, True])
def test_native_batch_discards_only_owned_client_before_final_close(native_quote, owns_client):
    route, state, instances = native_quote
    state["owns_client"] = owns_client
    refresher = fast_quotes.FastQuoteRefresher()
    key = ("Aster", "Futures", route["long_market_symbol"])
    result = refresher._quote_venue_jobs(
        key[:2], [(key, route, "long")], target_notional_usd=50.0
    )
    assert result[key]["ask_vwap"] == 100.0
    assert len(state["closed"]) == int(owns_client)
    assert len(state["gc"]) == int(owns_client)
    assert instances[0]._clients == {}
    assert instances[0]._client_request_locks == {}
    refresher.close()
    assert len(state["closed"]) == int(owns_client)
    assert len(state["gc"]) == int(owns_client)
