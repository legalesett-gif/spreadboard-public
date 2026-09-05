"""A spot leg that disagrees with the perp's own index is not the same claim.

OPENAI's spot token trades at ~846 while every venue's OPENAI perp publishes an
index of ~1,390-1,461 and pays 0.005% funding per 8h against a 3% cap. The
perps are not rich to their deliverable; they settle against something else
entirely. Pairing the two produced 63%, then 72%, then 82% "spreads" that
nobody can take, and because the per-token route budget keeps the widest rows
they evicted the genuine Kucoin-to-Hyperliquid 5.69%.

The index is the venue's own statement of what the contract settles against, so
this needs no inference. It also arrives free: `fetch_funding_rates` already
returns `indexPrice` and `markPrice` for every perp on every venue, and
`_funding_fields` was discarding them.
"""

from __future__ import annotations

import time
from typing import ClassVar

from spreadboard import api_spreads, fast_quotes


def _route(**over) -> dict:
    now_us = int(time.time() * 1_000_000)
    row = {
        "token": "OPENAI",
        "route_key": "OPENAI-route",
        "route_kind": "SPOT-FUTURES",
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "short_venue": "Hyperliquid",
        "short_market_type": "Futures",
        "long_market_symbol": "OPENAI/USDT",
        "short_market_symbol": "io:OAI",
        "long_quote": "USDT",
        "short_quote": "USDT",
        "long_price": 846.0,
        "short_price": 1461.35,
        "long_bid": 845.7,
        "long_ask": 846.5,
        "short_bid": 1461.0,
        "short_ask": 1461.7,
        "long_volume_24h_usd": 381_000.0,
        "short_volume_24h_usd": 5_296_809.0,
        "executable_spread_pct": 72.4,
        "displayed_open_spread_pct": 72.4,
        "depth_weighted_spread_pct": 72.4,
        "depth_usd": 500.0,
        "target_notional_usd": 500.0,
        "quote_ts_us": now_us,
        "blockers": [],
    }
    row.update(over)
    return row


def test_a_spot_far_from_the_perp_index_is_flagged() -> None:
    row = _route(short_index_price=1461.35)

    assert api_spreads.spot_disagrees_with_perp_index(row) is True


def test_a_spot_near_the_perp_index_is_not() -> None:
    """A real basis is nothing like this. Bitcoin spot sits pennies off index."""

    row = _route(long_price=1440.0, long_bid=1439.0, long_ask=1441.0,
                 short_index_price=1461.35, executable_spread_pct=1.5)

    assert api_spreads.spot_disagrees_with_perp_index(row) is False


def test_no_index_means_no_judgement() -> None:
    """Most venues publish one; a missing value must not exclude the row."""

    assert api_spreads.spot_disagrees_with_perp_index(_route()) is False


def test_a_futures_futures_route_is_untouched() -> None:
    """Kucoin to Hyperliquid is the route this exists to protect."""

    row = _route(
        route_kind="FUTURES",
        long_market_type="Futures",
        long_price=1382.68,
        long_bid=1382.0,
        long_ask=1383.0,
        short_index_price=1461.35,
        executable_spread_pct=5.69,
    )

    assert api_spreads.spot_disagrees_with_perp_index(row) is False


def test_the_evidence_classifier_excludes_it() -> None:
    """Flagging is only useful if it keeps the row off the board."""

    assert api_spreads.spread_evidence_state(_route(short_index_price=1461.35)) == "excluded"
    assert api_spreads.spread_evidence_state(_route()) != "excluded"


def test_the_index_price_survives_the_funding_fetch(monkeypatch) -> None:
    """It is already on the wire; the bulk fetch was dropping it on the floor."""

    class _Client:
        has: ClassVar[dict] = {"fetchFundingRates": True}
        markets: ClassVar[dict] = {
            "OPENAI/USDT:USDT": {"id": "OPENAI_USDT", "swap": True}
        }

        def fetch_funding_rates(self):
            return {
                "OPENAI/USDT:USDT": {
                    "symbol": "OPENAI/USDT:USDT",
                    "fundingRate": 0.00005,
                    "interval": "8h",
                    "indexPrice": 1461.35,
                    "markPrice": 1462.4,
                }
            }

    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda *_a, **_k: _Client())

    rates = refresher._bulk_funding_rates("Gate")

    assert rates["OPENAI/USDT:USDT"]["index_price"] == 1461.35


def test_a_spot_spot_route_is_never_judged_against_an_index() -> None:
    """Only a spot paired with a PERP is measured against that perp's index."""

    row = _route(
        route_kind="SPOT",
        short_market_type="Spot",
        short_index_price=1461.35,
        short_price=846.0,
        long_price=846.0,
    )

    assert api_spreads.spot_disagrees_with_perp_index(row) is False


def test_the_index_price_reaches_the_row() -> None:
    row = {"long_market_type": "Spot", "short_market_type": "Futures"}
    fast_quotes._sync_quoted_funding(
        row, {}, {"current_funding_pct": 0.005, "index_price": 1461.35}
    )

    assert row["short_index_price"] == 1461.35


def test_the_index_is_read_from_the_leg_the_bulk_refresh_writes() -> None:
    """`_refresh_funding` merges into notes.route_inputs[side], not the row.

    Reading only the row's own field found the index on 0 of 320 board routes.
    """

    row = _route(notes={"route_inputs": {"short": {"index_price": 1461.35}}})

    assert api_spreads.spot_disagrees_with_perp_index(row) is True
    assert api_spreads.spread_evidence_state(row) == "excluded"


def test_bulk_sweep_oracle_reaches_provider_index_and_excludes_spot(tmp_path, monkeypatch):
    import json

    from scripts import live_route_index_worker as worker
    from spreadboard import bulk_quotes, live_book_cache, provider_routes

    funding_path = tmp_path / "live_funding.json"
    monkeypatch.setattr(bulk_quotes, "FUNDING_CACHE_PATH", funding_path)
    actual_load = bulk_quotes.load_funding
    monkeypatch.setattr(bulk_quotes, "load_funding", lambda: actual_load(cache_path=funding_path))
    calls = []

    def post(url, payload):
        calls.append(payload)
        if payload["type"] == "perpDexs":
            return [None, {"name": "io"}]
        if payload.get("dex") == "io":
            return [{"universe": [{"name": "io:OAI"}]}, [{
                "impactPxs": ["1461", "1462"], "oraclePx": "1461.35",
                "markPx": "1500", "funding": "0.00005",
            }]]
        return [{"universe": []}, []]

    monkeypatch.setattr(bulk_quotes, "_public_post_json", post)
    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    assert bulk_quotes.sweep_venue("Hyperliquid", store=store) == 1
    assert len(calls) == 3  # existing main + builder metadata; no extra oracle call
    rates = bulk_quotes.load_funding()
    assert rates["Hyperliquid|io:OAI"]["index_price"] == 1461.35
    assert rates["Hyperliquid|IO-OAI/USDC:USDC"]["index_price"] == 1461.35
    stamp = int(time.time() * 1e6)
    for market_type, symbol, price in [("Spot", "OPENAI/USDT", 846),
                                      ("Futures", "OPENAI/USDT:USDT", 1383)]:
        store.put("Gate", market_type, symbol, bids=[[price, 10]], asks=[[price+1, 10]], quote_ts_us=stamp)
    rows = []
    for market_type, symbol in [("Spot", "OPENAI/USDT"), ("Futures", "OPENAI/USDT:USDT")]:
        rows.append(_route(long_market_type=market_type,
            source_name="hyperliquid_builder_dex", quote_ts_us=1,
            notes={"route_inputs": {"long": {"symbol": symbol}, "short": {"symbol": "io:OAI"}}}))
    discovery = tmp_path / "discovery.json"
    snapshot = {"dex_discovered_rows": rows}
    discovery.write_text(json.dumps(snapshot))
    provider_routes.publish(snapshot, discovery)
    monkeypatch.setattr(api_spreads, "DEFAULT_API_DISCOVERY_PATH", discovery)
    monkeypatch.setattr(api_spreads.public_rails, "load_public_rails", dict)
    monkeypatch.setattr(api_spreads, "_live_books", lambda: store.load_all(max_age_seconds=90))
    monkeypatch.setattr(api_spreads, "_apply_fast_quote_delta", lambda *a, **k: [])
    monkeypatch.setattr(worker.catalog_pairs, "dex_futures_routes", lambda *a, **k: [])
    output = worker._current_dex_rows({}, metadata={}, now=time.time())
    spot = next(row for row in output if row["long_market_type"] == "Spot")
    futures = next(row for row in output if row["long_market_type"] == "Futures")
    assert spot["short_index_price"] == 1461.35
    assert api_spreads.spread_evidence_state(spot) == "excluded"
    assert api_spreads.spread_evidence_state(futures) != "excluded"


def test_idle_funding_file_expires_and_clears_index(tmp_path, monkeypatch):
    import json

    from spreadboard import bulk_quotes
    now = [10_000.0]
    monkeypatch.setattr(bulk_quotes.time, "time", lambda: now[0])
    path = tmp_path / "funding.json"
    path.write_text(json.dumps({"legs": {"Hyperliquid|io:OAI": {
        "index_price": 1461.35, "rate_pct": 0.005, "interval_hours": 1,
    }}, "leg_updated_at": {"Hyperliquid|io:OAI": now[0]}}))
    assert bulk_quotes.load_funding(cache_path=path)
    now[0] += bulk_quotes.FUNDING_MAX_AGE_SECONDS + 2
    expired = bulk_quotes.load_funding(cache_path=path)
    assert expired == {}
    raw = _route(short_index_price=1461.35,
        notes={"route_inputs": {"short": {"symbol": "io:OAI", "index_price": 1461.35}}})
    row = api_spreads._row_from_api(raw, bucket="dex_discovered_rows", now=now[0], live_funding=expired)
    assert row.short_index_price is None


def test_a_mark_price_is_not_an_index(monkeypatch):
    class Client:
        has: ClassVar[dict] = {"fetchFundingRates": True}
        markets: ClassVar[dict] = {}
        def fetch_funding_rates(self):
            return {"x": {"symbol": "X/USDT:USDT", "fundingRate": 0.001,
                          "interval": "8h", "markPrice": 1461.35}}
    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda *a: Client())
    assert "index_price" not in refresher._bulk_funding_rates("Gate")["X/USDT:USDT"]


def test_catalog_row_preserves_the_funding_oracle(monkeypatch):
    from spreadboard import catalog_pairs, live_book_cache
    stamp = int(time.time() * 1e6)
    markets = [{"token": "OPENAI", "venue": "Gate", "market_type": "Spot", "symbol": "OPENAI/USDT", "quote": "USDT", "contract_size": 1},
               {"token": "OPENAI", "venue": "Hyperliquid", "market_type": "Futures", "symbol": "IO-OAI/USDC:USDC", "quote": "USDC", "contract_size": 1}]
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", lambda: {"markets": markets})
    monkeypatch.setattr(catalog_pairs.public_rails, "load_public_rails", dict)
    monkeypatch.setattr(catalog_pairs.venue_funding_history, "route_windows", lambda row: {})
    monkeypatch.setattr(catalog_pairs.bulk_quotes, "load_funding", lambda: {"Hyperliquid|IO-OAI/USDC:USDC": {"rate_pct": 0.005, "interval_hours": 1, "index_price": 1461.35}})
    def book(venue, *a, **k):
        price = 846 if venue == "Gate" else 1461
        return live_book_cache.CachedBook(bids=[[price, 10]], asks=[[price+1, 10]], quote_ts_us=stamp)
    monkeypatch.setattr(catalog_pairs.live_book_cache, "load_live_book", book)
    rows = catalog_pairs.for_token("OPENAI", use_cache=False)["routes"]
    assert rows
    assert all(row.get("short_index_price") == 1461.35 or row.get("long_index_price") == 1461.35 for row in rows)
    assert all(api_spreads.spread_evidence_state(row) == "excluded" for row in rows)
