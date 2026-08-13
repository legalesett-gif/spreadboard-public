from __future__ import annotations

from pathlib import Path
import time

from spreadboard import api_spreads, catalog_pairs, live_book_cache


def _book(
    bid: float,
    ask: float,
    *,
    stamp: int | None = None,
    amount: float = 10_000,
) -> live_book_cache.CachedBook:
    return live_book_cache.CachedBook(
        bids=[[bid, amount]],
        asks=[[ask, amount]],
        quote_ts_us=stamp if stamp is not None else int(time.time() * 1_000_000),
    )


def _catalog() -> dict:
    return {
        "ok": True,
        "generated_at": "2026-08-13T00:00:00Z",
        "markets": [
            {"token": "GUA", "venue": "Mexc", "market_type": "Spot", "symbol": "GUA/USDT", "quote": "USDT", "contract_size": 1},
            {"token": "GUA", "venue": "Mexc", "market_type": "Futures", "symbol": "GUA/USDT:USDT", "quote": "USDT", "contract_size": 1},
            {"token": "GUA", "venue": "Gate", "market_type": "Futures", "symbol": "GUA/USDT:USDT", "quote": "USDT", "contract_size": 1},
        ],
    }


def test_complete_pairs_use_warm_exact_symbols_and_keep_spread_and_funding_separate(monkeypatch) -> None:
    books = {
        ("Mexc", "Spot", "GUA/USDT"): _book(0.0499, 0.0500),
        ("Mexc", "Futures", "GUA/USDT:USDT"): _book(0.0504, 0.0505),
        ("Gate", "Futures", "GUA/USDT:USDT"): _book(0.0510, 0.0511),
    }
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", _catalog)
    monkeypatch.setattr(
        catalog_pairs.live_book_cache,
        "load_live_book",
        lambda venue, market_type, symbol, **_kwargs: books.get((venue, market_type, symbol)),
    )
    monkeypatch.setattr(
        catalog_pairs.bulk_quotes,
        "load_funding",
        lambda: {
            "Mexc|GUA/USDT:USDT": {"rate_pct": 0.01, "interval_hours": 8},
            "Gate|GUA/USDT:USDT": {"rate_pct": 0.02, "interval_hours": 4},
        },
    )
    monkeypatch.setattr(catalog_pairs.public_rails, "load_public_rails", lambda: {})
    monkeypatch.setattr(
        catalog_pairs.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": None, "7d": None, "30d": None},
    )
    monkeypatch.setattr(catalog_pairs.time, "time", lambda: 1_700_000_010.0)

    payload = catalog_pairs.for_token("gua", use_cache=False)

    assert payload["catalog_market_count"] == 3
    assert payload["fresh_market_count"] == 3
    # two directed futures/futures routes plus two executable spot/futures farms
    assert payload["route_count"] == 4
    farm = next(
        row
        for row in payload["routes"]
        if row["long_market_type"] == "Spot" and row["short_venue"] == "Gate"
    )
    assert farm["long_market_symbol"] == "GUA/USDT"
    assert farm["short_market_symbol"] == "GUA/USDT:USDT"
    assert round(farm["executable_spread_pct"], 2) == 2.0
    assert round(farm["funding_projected_24h_pct"], 2) == 0.12
    assert farm["depth_weighted_spread_pct"] is not None
    assert farm["depth_unverified"] is False
    assert farm["route_key"].startswith("CUSTOM:")


def test_incomplete_depth_is_top_book_not_fabricated_vwap(monkeypatch) -> None:
    catalog = _catalog()
    catalog["markets"] = catalog["markets"][:2]
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", lambda: catalog)
    books = {
        ("Mexc", "Spot", "GUA/USDT"): _book(0.049, 0.050, amount=0),
        ("Mexc", "Futures", "GUA/USDT:USDT"): _book(0.051, 0.052, amount=0),
    }
    monkeypatch.setattr(
        catalog_pairs.live_book_cache,
        "load_live_book",
        lambda venue, market_type, symbol, **_kwargs: books.get((venue, market_type, symbol)),
    )
    monkeypatch.setattr(catalog_pairs.bulk_quotes, "load_funding", lambda: {})
    monkeypatch.setattr(catalog_pairs.public_rails, "load_public_rails", lambda: {})
    monkeypatch.setattr(
        catalog_pairs.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": None, "7d": None, "30d": None},
    )

    route = catalog_pairs.for_token("GUA", use_cache=False)["routes"][0]
    assert round(route["executable_spread_pct"], 8) == 2.0
    assert route["depth_weighted_spread_pct"] is None
    assert route["depth_unverified"] is True


def test_identity_ratio_and_closed_spot_rail_reject_mirages(monkeypatch) -> None:
    catalog = {
        "generated_at": "now",
        "markets": [
            {"token": "CAT", "venue": "Gate", "market_type": "Spot", "symbol": "CAT/USDT", "quote": "USDT"},
            {"token": "CAT", "venue": "XT", "market_type": "Spot", "symbol": "CAT/USDT", "quote": "USDT"},
            {"token": "CAT", "venue": "Mexc", "market_type": "Futures", "symbol": "CAT/USDT:USDT", "quote": "USDT"},
        ],
    }
    books = {
        ("Gate", "Spot", "CAT/USDT"): _book(1.0, 1.01),
        ("XT", "Spot", "CAT/USDT"): _book(1.2, 1.21),
        # More than 3x from either spot leg: a reused ticker, never a route.
        ("Mexc", "Futures", "CAT/USDT:USDT"): _book(4.0, 4.1),
    }
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", lambda: catalog)
    monkeypatch.setattr(
        catalog_pairs.live_book_cache,
        "load_live_book",
        lambda venue, market_type, symbol, **_kwargs: books.get((venue, market_type, symbol)),
    )
    monkeypatch.setattr(catalog_pairs.bulk_quotes, "load_funding", lambda: {})
    monkeypatch.setattr(
        catalog_pairs.public_rails,
        "load_public_rails",
        lambda: {"Gate": {"CAT": {"withdraw": False}}, "XT": {"CAT": {"deposit": True}}},
    )
    monkeypatch.setattr(
        catalog_pairs.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": None, "7d": None, "30d": None},
    )

    payload = catalog_pairs.for_token("CAT", use_cache=False)
    assert payload["routes"] == []
    assert payload["rejected"]["price_ratio"] == 2
    assert payload["rejected"]["closed_rail"] == 1


def test_telegram_falls_back_to_complete_catalog_when_scanner_omits_token(monkeypatch, tmp_path: Path) -> None:
    from spreadboard import telegram_queries

    monkeypatch.setattr(telegram_queries, "_warm_payload", lambda _path: {"groups": []})
    monkeypatch.setattr(
        telegram_queries.catalog_pairs,
        "for_token",
        lambda symbol, limit=100: {"routes": [{"token": symbol, "route_key": "complete"}]},
    )
    assert telegram_queries._rows_for("GUA", tmp_path) == [
        {"token": "GUA", "route_key": "complete"}
    ]


def test_damaged_book_cache_returns_an_empty_pair_payload(monkeypatch) -> None:
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", _catalog)
    monkeypatch.setattr(
        catalog_pairs.live_book_cache,
        "load_live_book",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad sqlite")),
    )
    monkeypatch.setattr(catalog_pairs.bulk_quotes, "load_funding", lambda: {})
    monkeypatch.setattr(catalog_pairs.public_rails, "load_public_rails", lambda: {})
    payload = catalog_pairs.for_token("GUA", use_cache=False)
    assert payload["ok"] is False
    assert payload["routes"] == []
    assert payload["missing_book_count"] == 3


def test_identity_warned_catalogue_pair_remains_visible_but_cannot_lead(monkeypatch, tmp_path) -> None:
    catalog = {
        "markets": [
            {"token": "T", "venue": "Gate", "market_type": "Futures", "symbol": "T/USDT:USDT", "quote": "USDT"},
            {"token": "T", "venue": "Mexc", "market_type": "Futures", "symbol": "T/USDT:USDT", "quote": "USDT"},
            {"token": "T", "venue": "Bybit", "market_type": "Futures", "symbol": "T/USDT:USDT", "quote": "USDT"},
        ]
    }
    books = {
        live_book_cache.cache_key("Gate", "Futures", "T/USDT:USDT"): _book(1.0, 1.0),
        # 60% away: still within the broad 3x collision boundary, but identity warned.
        live_book_cache.cache_key("Mexc", "Futures", "T/USDT:USDT"): _book(1.6, 1.61),
        live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT"): _book(1.01, 1.02),
    }
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", lambda: catalog)
    cache_path = tmp_path / "books.sqlite3"
    cache_path.touch()
    monkeypatch.setattr(catalog_pairs.live_book_cache, "DEFAULT_PATH", cache_path)

    class Store:
        def load_all(self, **_kwargs):
            return books

        def close(self):
            return None

    monkeypatch.setattr(catalog_pairs.live_book_cache, "LiveBookStore", Store)
    monkeypatch.setattr(catalog_pairs.bulk_quotes, "load_funding", lambda: {})
    monkeypatch.setattr(catalog_pairs.public_rails, "load_public_rails", lambda: {})

    summary = catalog_pairs.all_token_summaries()["T"]

    assert summary["quoteable_pair_count"] == 6
    assert summary["best_spread_route"]["short_venue"] == "Bybit"
    assert summary["best_spread_route"]["mirage_guarded"] is False


def test_missing_catalogue_quote_age_is_not_current() -> None:
    assert api_spreads.spread_quote_current({"age_min": None}) is False
    assert api_spreads.spread_quote_current({}) is False
    assert api_spreads.spread_quote_current({"age_min": 0.1}) is True


def test_current_dex_rows_merge_with_complete_cex_pairs() -> None:
    cex = {
        "ok": True,
        "token": "GUA",
        "route_count": 1,
        "routes": [{"route_key": "cex", "route_kind": "FUTURES", "executable_spread_pct": 1.0}],
    }
    dex = {
        "route_key": "dex",
        "route_kind": "DEX-FUTURES",
        "long_venue": "OKX DEX 56",
        "short_venue": "Gate",
        "executable_spread_pct": 2.0,
    }
    merged = catalog_pairs.with_routes(cex, [dex], limit=10)
    assert merged["route_count"] == 2
    assert merged["displayed_route_count"] == 2
    assert merged["dex_route_count"] == 1
    assert [row["route_key"] for row in merged["routes"]] == ["dex", "cex"]
