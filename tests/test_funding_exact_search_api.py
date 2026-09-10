"""Exact-token HTML and export must reach the complete live Funding reader."""

from pathlib import Path

import pytest
from spreadboard.packed_routes import PackedRoutes

from spreadboard import funding_catalog, server


@pytest.mark.parametrize(
    ("kind", "farm", "long_type"),
    [("FUTURES", "futures-futures", "Futures"),
     ("FUTURES-SPOT-PAIR", "futures-spot", "Spot")],
)
def test_exact_search_keeps_all_pairs_and_their_live_funding_age(
    monkeypatch, kind, farm, long_type,
) -> None:
    rows = [{
        "token": "CASE", "route_key": f"case-{i}",
        "route_kind": "FUTURES" if long_type == "Futures" else "SPOT-FUTURES",
        "long_venue": f"Long{i}", "long_market_type": long_type,
        "long_market_symbol": "CASE/USDT:USDT" if long_type == "Futures" else "CASE/USDT",
        "long_quote": "USDT", "short_quote": "USDT",
        "short_venue": "Gate", "short_market_type": "Futures",
        "short_market_symbol": "CASE/USDT:USDT",
        "funding_daily_pct": 99.0, "age_min": 0.2,
        "displayed_open_spread_pct": 0.1,
    } for i in range(30)]
    rates = {f"Long{i}|CASE/USDT:USDT": {
        "rate_pct": 0.01, "interval_hours": 8, "age_seconds": 60,
    } for i in range(30)}
    rates["Gate|CASE/USDT:USDT"] = {
        "rate_pct": 0.1, "interval_hours": 8, "age_seconds": 180,
    }
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(funding_catalog, "_complete_payloads", lambda: {
        "CASE": {"routes": PackedRoutes(rows)},
    })
    monkeypatch.setattr(funding_catalog, "_resident_live_overlay", lambda batch: batch)
    monkeypatch.setattr(funding_catalog.bulk_quotes, "load_funding", lambda: rates)
    monkeypatch.setattr(funding_catalog.venue_funding_history, "load", dict)
    monkeypatch.setattr(funding_catalog.funding_radar, "routes_for", lambda *a, **kw: [])
    monkeypatch.setattr(funding_catalog, "_window_value", lambda *a, **kw: None)
    # Keep the competing real API branch available: mocking the exact-token
    # projection itself would hide the early return that caused this bug.
    monkeypatch.setattr(server.catalog_pairs, "for_token", lambda *a, **kw: {
        "token": "CASE", "routes": rows, "route_count": 30, "fresh_market_count": 31,
    })
    monkeypatch.setattr(server.catalog_pairs, "filtered", lambda payload, **kw: payload)
    monkeypatch.setattr(server.warm_query_projection.LIVE_UNIVERSE, "target_rows",
                        lambda **kw: ([], {"ready": False}))
    monkeypatch.setattr(server, "_funding_catalog_seed_payload", lambda *a, **kw: {
        "ok": True, "filters": {}, "summary": {}, "pagination": {},
        "source_health": {"canonical_api": {"status": "fresh"}},
    })
    monkeypatch.setattr(server, "_sync_telegram_client_universe", lambda payload: payload)
    monkeypatch.setattr(server.bulk_quotes, "funding_health", lambda: {
        "status": "fresh", "p95_age_seconds": 180,
    })
    monkeypatch.setattr(server.funding_history_demand, "enqueue", lambda *a, **kw: None)
    monkeypatch.setattr(server.chart_warm_demand, "enqueue", lambda *a, **kw: None)
    monkeypatch.setattr(server, "funding_history_health", dict)

    query = {"q": ["CASE"], "no_cache": ["1"], "limit": ["25"]}
    payload = server.api_market_spreads(Path("board.json"), {
        **query, "kind": [kind], "funding_only": ["1"], "sort": ["funding"],
    })
    assert len(payload["rows"]) == 30
    assert payload["funding_catalog"]["matching_route_count"] == 30
    assert payload["funding_catalog"]["returned_route_count"] == 30
    assert {row["route_key"] for row in payload["rows"]} == {f"case-{i}" for i in range(30)}
    for row in payload["rows"]:
        assert row["funding_age_min"] == 3.0
        assert row["funding_daily_pct"] == pytest.approx(0.27 if long_type == "Futures" else 0.3)

    html = server.render_funding_page(Path("board.json"), {}, {**query, "farm": [farm]})
    assert html.count('<article class="funding-pair-row') == 30
    assert "all exact pairs shown for the exact token match" in html
    assert "Live now · funding 3 min old" in html
    assert "Live now · funding — old" not in html
