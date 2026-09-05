"""Pair construction must agree with the quote policy used by both pages."""

import time

import pytest

from spreadboard import api_spreads, catalog_pairs, funding_catalog, live_book_cache


@pytest.mark.parametrize("surface", ["spreads", "funding"])
def test_dollar_quote_pairs_reach_spreads_and_funding_but_eur_does_not(
    monkeypatch, tmp_path, surface,
) -> None:
    markets = [
        {"token": "KAITO", "venue": venue, "market_type": "Futures",
         "symbol": f"KAITO/{quote}:{quote}", "quote": quote}
        for venue, quote in (("WhiteBIT", "USDT"), ("Hyperliquid", "USDC"), ("Gate", "EUR"))
    ]
    books = {
        live_book_cache.cache_key(m["venue"], "Futures", m["symbol"]):
        live_book_cache.CachedBook(
            bids=[[100.4 if m["venue"] == "Hyperliquid" else 99.9, 1000]],
            asks=[[100.5 if m["venue"] == "Hyperliquid" else 100.0, 1000]],
            quote_ts_us=int(time.time() * 1_000_000),
        ) for m in markets
    }

    class Store:
        def load_all(self, **_kwargs):
            return books

        def close(self):
            pass

    path = tmp_path / "books.sqlite3"
    path.touch()
    monkeypatch.setattr(live_book_cache, "DEFAULT_PATH", path)
    monkeypatch.setattr(live_book_cache, "LiveBookStore", Store)
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", lambda: {"markets": markets})
    rates = {
        "WhiteBIT|KAITO/USDT:USDT": {"rate_pct": 0.01, "interval_hours": 8},
        "Hyperliquid|KAITO/USDC:USDC": {"rate_pct": 0.01, "interval_hours": 1},
    }
    monkeypatch.setattr(catalog_pairs.bulk_quotes, "load_funding", lambda: rates)
    monkeypatch.setattr(catalog_pairs.public_rails, "load_public_rails", dict)
    reductions = []

    def reduce_routes(routes):
        reductions.append(len(routes))
        return [r for r in routes if r["short_venue"] == "Hyperliquid"]

    # The ordinary builder retains both directions. Its bounded caller can
    # reduce a completed token without changing the exact-token API.
    full = catalog_pairs.for_tokens(["KAITO"])
    assert len(full["KAITO"]["routes"]) == 2
    payloads = catalog_pairs.for_tokens(["KAITO"], route_reducer=reduce_routes)
    routes = payloads["KAITO"]["routes"]
    assert reductions == [2]
    assert len(routes) == payloads["KAITO"]["route_count"] == 1
    assert payloads["KAITO"]["displayed_route_count"] == 1
    assert all(r["long_quote"] != "EUR" and r["short_quote"] != "EUR" for r in routes)
    route = next(r for r in routes if r["short_venue"] == "Hyperliquid")
    if surface == "spreads":
        assert api_spreads.spread_evidence_state(route) == "verified"
        return

    monkeypatch.setattr(funding_catalog, "_complete_payloads", lambda: payloads)
    monkeypatch.setattr(funding_catalog, "_resident_live_overlay", lambda rows: rows)
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(funding_catalog.venue_funding_history, "load", dict)
    page = funding_catalog.page(window="now", route_kind="FUTURES")
    assert page["matching_token_count"] == 1
    assert len(page["rows"]) == 1
    assert page["rows"][0]["route_key"] == route["route_key"]
    assert round(page["rows"][0]["funding_daily_pct"], 6) == 0.21
