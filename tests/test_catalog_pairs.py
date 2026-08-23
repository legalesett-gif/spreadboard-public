from __future__ import annotations

from pathlib import Path
import time

from spreadboard import api_spreads, catalog_pairs, live_book_cache, server


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


def test_spot_spot_catalogue_does_not_invent_zero_funding_or_a_funding_leader(
    monkeypatch,
) -> None:
    """Production LUNC changed from no futures funding to ``0.000%`` only
    after the 25-token page expanded it from the warm catalogue. Spot legs do
    not settle perpetual funding, so zero is a fabricated measurement here,
    not a real rate and not a candidate for Top Funding Pairs.
    """

    catalog = {
        "generated_at": "now",
        "markets": [
            {
                "token": "LUNC",
                "venue": "HTX",
                "market_type": "Spot",
                "symbol": "LUNC/USDT",
                "quote": "USDT",
            },
            {
                "token": "LUNC",
                "venue": "Bybit",
                "market_type": "Spot",
                "symbol": "LUNC/USDT",
                "quote": "USDT",
            },
        ],
    }
    books = {
        ("HTX", "Spot", "LUNC/USDT"): _book(0.00006, 0.000061),
        ("Bybit", "Spot", "LUNC/USDT"): _book(0.000063, 0.000064),
    }
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", lambda: catalog)
    monkeypatch.setattr(
        catalog_pairs.live_book_cache,
        "load_live_book",
        lambda venue, market_type, symbol, **_kwargs: books.get(
            (venue, market_type, symbol)
        ),
    )
    monkeypatch.setattr(catalog_pairs.bulk_quotes, "load_funding", dict)
    monkeypatch.setattr(catalog_pairs.public_rails, "load_public_rails", dict)

    payload = catalog_pairs.for_token("LUNC", use_cache=False)
    route = payload["routes"][0]
    grouped = catalog_pairs.group(payload)

    assert route["route_kind"] == "SPOT"
    assert route["funding_projected_24h_pct"] is None
    assert route["funding_daily_pct"] is None
    assert grouped["best_funding_route"] is None
    assert grouped["best_funding_24h_pct"] is None
    assert grouped["best_funding_24h_basis"] is None


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


def test_catalogue_merge_deduplicates_the_same_exact_legs_across_key_formats() -> None:
    catalogue = {
        "ok": True,
        "token": "GUA",
        "routes": [
            {
                "route_key": "CUSTOM:catalogue-key",
                "route_kind": "FUTURES",
                "long_venue": "Mexc",
                "long_market_type": "Futures",
                "long_market_symbol": "GUA/USDT:USDT",
                "short_venue": "Gate",
                "short_market_type": "Futures",
                "short_market_symbol": "GUA/USDT:USDT",
                "depth_weighted_spread_pct": 0.8,
                "funding_24h_pct": None,
            }
        ],
    }
    scanner = {
        "route_key": "GUA|Mexc|Futures|Gate|Futures",
        "route_kind": "FUTURES",
        "long_venue": "Mexc",
        "long_market_type": "Futures",
        "long_market_symbol": "GUA/USDT:USDT",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
        "depth_weighted_spread_pct": 0.7,
        "funding_24h_pct": 0.41,
        "settled_funding_windows": {"1d": 0.41, "7d": 1.2, "30d": None},
    }

    merged = catalog_pairs.with_routes(catalogue, [scanner])

    assert merged["route_count"] == 1
    assert len(merged["routes"]) == 1
    assert merged["routes"][0]["route_key"] == "CUSTOM:catalogue-key"
    assert merged["routes"][0]["depth_weighted_spread_pct"] == 0.8
    assert merged["routes"][0]["funding_24h_pct"] == 0.41
    assert merged["routes"][0]["settled_funding_windows"]["7d"] == 1.2


def test_spread_and_funding_catalogue_filters_are_economically_independent(monkeypatch) -> None:
    monkeypatch.setattr(
        catalog_pairs.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": None, "7d": None, "30d": None},
    )
    payload = {
        "token": "GUA",
        "routes": [
            {
                "route_key": "spread_only",
                "route_kind": "FUTURES",
                "depth_weighted_spread_pct": 1.2,
                "funding_daily_pct": -0.4,
            },
            {
                "route_key": "funding_only",
                "route_kind": "FUTURES",
                "depth_weighted_spread_pct": -0.8,
                "funding_daily_pct": 0.6,
            },
        ],
    }

    spread = catalog_pairs.filtered(payload, funding_only=False)
    funding = catalog_pairs.filtered(payload, funding_only=True)

    assert [row["route_key"] for row in spread["routes"]] == ["spread_only"]
    assert [row["route_key"] for row in funding["routes"]] == ["funding_only"]
    assert funding["routes"][0]["funding_24h_pct"] is None


def test_bulk_catalogue_expansion_reads_the_shared_book_store_once(monkeypatch, tmp_path: Path) -> None:
    books = {
        live_book_cache.cache_key("Mexc", "Spot", "GUA/USDT"): _book(0.0499, 0.05),
        live_book_cache.cache_key("Mexc", "Futures", "GUA/USDT:USDT"): _book(0.0504, 0.0505),
        live_book_cache.cache_key("Gate", "Futures", "GUA/USDT:USDT"): _book(0.051, 0.0511),
    }
    cache_path = tmp_path / "books.sqlite3"
    cache_path.touch()
    monkeypatch.setattr(catalog_pairs.live_book_cache, "DEFAULT_PATH", cache_path)
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", _catalog)
    calls = {"load_all": 0}

    class Store:
        def load_all(self, **_kwargs):
            calls["load_all"] += 1
            return books

        def close(self):
            return None

    monkeypatch.setattr(catalog_pairs.live_book_cache, "LiveBookStore", Store)
    monkeypatch.setattr(catalog_pairs.bulk_quotes, "load_funding", lambda: {})
    monkeypatch.setattr(catalog_pairs.public_rails, "load_public_rails", lambda: {})

    payloads = catalog_pairs.for_tokens(["GUA", "MISSING"])

    assert calls["load_all"] == 1
    assert payloads["GUA"]["route_count"] == 4
    assert payloads["MISSING"]["route_count"] == 0


def test_visible_board_group_expands_beyond_scanner_quota(monkeypatch) -> None:
    now_us = int(time.time() * 1_000_000)

    def route(key: str, spread: float, carry: float) -> dict:
        return {
            "token": "GUA",
            "route_key": key,
            "route_kind": "FUTURES",
            "long_venue": "Mexc",
            "long_market_type": "Futures",
            "long_market_symbol": "GUA/USDT:USDT",
            "short_venue": key,
            "short_market_type": "Futures",
            "short_market_symbol": "GUA/USDT:USDT",
            "depth_weighted_spread_pct": spread,
            "executable_spread_pct": spread,
            "funding_daily_pct": carry,
            "funding_projected_24h_pct": carry,
            "quote_ts_us": now_us,
            "age_min": 0.0,
            "mirage_guarded": False,
        }

    scanner_route = route("Gate", 0.5, 0.1)
    data = {
        "groups": [{
            "token": "GUA",
            "token_name": "GUA",
            "href": "/token/GUA",
            "route_count": 1,
            "routes": [scanner_route],
            "best_route": scanner_route,
        }],
        "summary": {},
        "rows": [scanner_route],
    }
    monkeypatch.setattr(
        server.catalog_pairs,
        "for_tokens",
        lambda *_args, **_kwargs: {
            "GUA": {
                "token": "GUA",
                "routes": [
                    scanner_route,
                    route("Bybit", 0.4, -0.2),
                    route("Aster", -0.3, 0.6),
                ],
            }
        },
    )
    monkeypatch.setattr(
        catalog_pairs.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": None, "7d": None, "30d": None},
    )

    spread = server._expand_visible_catalog_groups(data, {})
    funding = server._expand_visible_catalog_groups(data, {"funding_only": ["1"]})
    strong_funding = server._expand_visible_catalog_groups(
        data,
        {"funding_only": ["1"], "min_abs_funding_24h_pct": ["0.5"]},
    )

    assert spread["groups"][0]["route_count"] == 2
    assert {row["route_key"] for row in spread["groups"][0]["routes"]} == {"Gate", "Bybit"}
    assert funding["groups"][0]["route_count"] == 2
    assert {row["route_key"] for row in funding["groups"][0]["routes"]} == {"Gate", "Aster"}
    assert strong_funding["groups"][0]["route_count"] == 1
    assert [row["route_key"] for row in strong_funding["groups"][0]["routes"]] == [
        "Aster"
    ]


def test_catalogue_overlay_cannot_bypass_unsupported_valuation_filter(monkeypatch) -> None:
    called = False

    def unexpected(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(server.catalog_pairs, "for_tokens", unexpected)
    data = {"groups": [{"token": "GUA"}]}

    assert server._expand_visible_catalog_groups(
        data, {"min_market_cap_usd": ["1000000"]}
    ) is data
    assert called is False


def test_exact_search_recovers_from_complete_catalogue_when_scanner_group_is_empty(
    monkeypatch,
) -> None:
    route = {
        "token": "ANTHROPIC",
        "route_key": "ANTHROPIC|Bitget|Futures|Aster|Futures",
        "route_kind": "DEX-FUTURES",
        "long_venue": "Bitget",
        "long_market_type": "Futures",
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "depth_weighted_spread_pct": 3.2,
        "funding_daily_pct": 0.1,
        "quote_ts_us": 1_700_000_000_000_000,
        "age_min": 0.2,
        "freshness": "fresh",
        "spread_quote_current": True,
        "mirage_guarded": False,
    }
    monkeypatch.setattr(
        server.catalog_pairs,
        "for_tokens",
        lambda *_args, **_kwargs: {
            "ANTHROPIC": {"token": "ANTHROPIC", "route_count": 1, "routes": [route]}
        },
    )
    monkeypatch.setattr(
        catalog_pairs.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": None, "7d": None, "30d": None},
    )

    result = server._expand_visible_catalog_groups(
        {"groups": [], "rows": [], "summary": {"matching_rows": 0}},
        {"q": ["ANTHROPIC"]},
    )

    assert [group["token"] for group in result["groups"]] == ["ANTHROPIC"]
    assert result["rows"][0]["route_kind"] == "DEX-FUTURES"


def test_native_perpetual_dex_is_catalogued_but_onchain_spot_is_provider_quoted() -> None:
    assert catalog_pairs._is_onchain_spot(
        {"venue": "OKX DEX 56", "market_type": "Spot"}
    )
    assert not catalog_pairs._is_onchain_spot(
        {"venue": "Aster", "market_type": "Futures"}
    )
