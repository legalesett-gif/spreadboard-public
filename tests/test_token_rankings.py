from __future__ import annotations

from pathlib import Path
import time

from spreadboard import server, token_rankings


def _market() -> dict:
    current_us = int(time.time() * 1_000_000)
    return {
        "source_health": {"canonical_api": {"status": "fresh", "updated_at": "now", "age_min": 0.1}},
        "groups": [
            {
                "token": "GUA",
                "token_name": "GUA",
                "routes": [{"token": "GUA", "route_key": "GUA|A|Futures|B|Futures", "long_venue": "A", "long_market_type": "Futures", "long_market_symbol": "GUA/USDT:USDT", "short_venue": "B", "short_market_type": "Futures", "short_market_symbol": "GUA/USDT:USDT", "depth_weighted_spread_pct": 2.0, "funding_projected_24h_pct": 0.5, "age_min": 0.1, "quote_ts_us": current_us}],
                "venues": ["A", "B"],
                "route_kinds": ["FUTURES"],
                "best_edge_pct": 2.0,
                "best_route": {"route_key": "GUA|A|Futures|B|Futures", "long_venue": "A", "short_venue": "B", "depth_weighted_spread_pct": 2.0},
                "best_funding_24h_pct": 0.5,
                "best_funding_24h_basis": "projected_current_rate",
                "best_funding_route": {"route_key": "GUA|A|Futures|B|Futures", "long_venue": "A", "short_venue": "B", "funding_projected_24h_pct": 0.5},
                "age_min": 0.1,
            }
        ],
    }


def test_rankings_union_full_catalog_live_and_cooled(monkeypatch, tmp_path: Path) -> None:
    catalog = {
        "markets": [
            {"token": "GUA", "venue": "A", "market_type": "Futures", "symbol": "GUA/USDT:USDT"},
            {"token": "GUA", "venue": "B", "market_type": "Futures", "symbol": "GUA/USDT:USDT"},
            {"token": "ONLYCAT", "venue": "C", "market_type": "Spot", "symbol": "ONLYCAT/USDT"},
        ]
    }
    radar = [{"token": "OLD", "route_key": "OLD|A|Futures|B|Futures", "token_name": "Old", "radar_windows": {"1d": 1.2, "7d": 2.3, "30d": 3.4}}]
    monkeypatch.setattr(
        token_rankings.funding_radar,
        "window_value",
        lambda route, label: (route.get("radar_windows") or {}).get(label),
    )
    path = tmp_path / "token_rankings.json"
    payload = token_rankings.build(
        board_path=tmp_path / "board.jsonl",
        output_path=path,
        market_payload=_market(),
        catalog_payload=catalog,
        radar_routes=radar,
        catalogue_summaries={},
    )
    rows = {row["token"]: row for row in payload["records"]}
    assert payload["token_count"] == 3
    assert payload["live_token_count"] == 1
    assert payload["cooled_token_count"] == 1
    assert payload["catalogued_token_count"] == 1
    assert rows["GUA"]["catalog_pair_count"] == 1
    assert rows["ONLYCAT"]["status"] == "catalogued"
    assert rows["OLD"]["settled_windows"]["30d"] == 3.4
    assert token_rankings.load(path)["records"] == payload["records"]


def test_rankings_keep_current_dex_routes_outside_table_records(tmp_path: Path) -> None:
    market = _market()
    dex = {
        "token": "GUA",
        "route_key": "dex-route",
        "route_kind": "DEX-FUTURES",
        "long_venue": "OKX DEX 56",
        "short_venue": "Gate",
        "quote_ts_us": 1_700_000_000_000_000,
    }
    market["groups"][0]["routes"].append(dex)
    payload = token_rankings.build(
        board_path=tmp_path / "board.jsonl",
        output_path=tmp_path / "rankings.json",
        market_payload=market,
        catalog_payload={"markets": []},
        radar_routes=[],
        catalogue_summaries={},
    )
    assert payload["current_dex_routes"]["GUA"] == [dex]
    assert "current_dex_routes" not in payload["records"][0]
    assert token_rankings.dex_routes_for(payload, "GUA", now=1_700_000_100.0) == [dex]
    assert token_rankings.dex_routes_for(payload, "GUA", now=1_700_000_400.0) == []


def test_rankings_page_reads_precomputed_artifact_without_market_build(monkeypatch) -> None:
    payload = {
        "schema": token_rankings.SCHEMA,
        "generated_at": "2026-08-13T00:00:00+00:00",
        "token_count": 1,
        "live_token_count": 1,
        "cooled_token_count": 0,
        "catalogued_token_count": 0,
        "records": [{"token": "GUA", "status": "live", "best_spread_pct": 2.0, "funding_now_24h_pct": 0.5, "settled_windows": {"1d": 1.0, "7d": 2.0, "30d": 3.0}, "catalog_pair_count": 44, "catalog_venue_count": 7, "live_route_count": 18, "token_url": "/token/GUA", "chart_url": "/charts?token=GUA"}],
    }
    monkeypatch.setattr(server.token_rankings, "load", lambda: payload)
    monkeypatch.setattr(
        server,
        "api_market_spreads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cold build")),
    )
    html = server.render_rankings_page({"rank": ["spread"]})
    assert "Individual token leaderboard" in html
    assert "GUA" in html
    assert "Pair coverage" in html
    assert 'data-refresh-preserve="rankings-filters"' in html
    assert "Current-rate 24h projection" in html
    assert "Settled evidence lives only in the 24h, 7d, and 30d columns." in html


def test_ranked_metrics_are_independent() -> None:
    payload = {
        "records": [
            {"token": "SPREAD", "status": "live", "best_spread_pct": 8.0, "funding_now_24h_pct": 0.1, "settled_windows": {}},
            {"token": "FUND", "status": "live", "best_spread_pct": 1.0, "funding_now_24h_pct": 1.2, "settled_windows": {}},
        ]
    }
    assert token_rankings.ranked(payload, metric="spread")[0]["token"] == "SPREAD"
    assert token_rankings.ranked(payload, metric="funding")[0]["token"] == "FUND"


def test_non_spread_rank_only_reprices_rows_that_can_be_returned(monkeypatch) -> None:
    payload = {
        "records": [
            {
                "token": f"T{index:04d}",
                "status": "live",
                "best_spread_pct": float(index),
                "best_spread_route": {
                    "route_key": f"route-{index}",
                    "quote_ts_us": int(time.time() * 1_000_000),
                },
                "settled_windows": {"30d": float(index)},
            }
            for index in range(1000)
        ]
    }
    seen = []
    monkeypatch.setattr(
        token_rankings.api_spreads,
        "live_prices_for",
        lambda routes, include_funding=False: seen.extend(routes) or {},
    )

    rows = token_rankings.ranked(payload, metric="30d", limit=25)

    assert len(rows) == 25
    assert len(seen) == 25
    assert rows[0]["token"] == "T0999"


def test_ranked_reprices_precomputed_leader_from_current_matched_books(monkeypatch) -> None:
    payload = {
        "records": [
            {
                "token": "OLD",
                "status": "live",
                "best_spread_pct": 9.0,
                "best_spread_route": {
                    "route_key": "OLD|A|Spot|B|Futures",
                    "age_min": 10.0,
                },
            }
        ]
    }
    monkeypatch.setattr(
        token_rankings.api_spreads,
        "live_prices_for",
        lambda routes, include_funding=False: {
            "OLD|A|Spot|B|Futures": (0.25, None)
        },
    )

    row = token_rankings.ranked(payload, metric="spread")[0]

    assert row["best_spread_pct"] == 0.25
    assert row["best_spread_route"]["spread_quote_current"] is True


def test_ranked_expires_old_leader_when_no_two_leg_book_exists(monkeypatch) -> None:
    payload = {
        "records": [
            {
                "token": "OLD",
                "status": "live",
                "best_spread_pct": 9.0,
                "best_spread_route": {"route_key": "OLD|A|Spot|B|Futures", "age_min": 10.0},
            }
        ]
    }
    monkeypatch.setattr(
        token_rankings.api_spreads,
        "live_prices_for",
        lambda routes, include_funding=False: {},
    )

    row = token_rankings.ranked(payload, metric="spread")[0]

    assert row["best_spread_pct"] is None
    assert row["best_spread_route"] is None


def test_old_spread_cannot_lead_but_current_funding_remains_ranked(tmp_path: Path) -> None:
    market = _market()
    old = market["groups"][0]["routes"][0]
    old["age_min"] = 10.0
    old["quote_ts_us"] = int((time.time() - 600.0) * 1_000_000)
    old["depth_weighted_spread_pct"] = 9.0
    old["funding_projected_24h_pct"] = 0.7
    payload = token_rankings.build(
        board_path=tmp_path / "board.jsonl",
        output_path=tmp_path / "rankings.json",
        market_payload=market,
        catalog_payload={"markets": []},
        radar_routes=[],
        catalogue_summaries={},
    )
    row = payload["records"][0]
    assert row["best_spread_pct"] is None
    assert row["funding_now_24h_pct"] == 0.7


def test_unverified_top_book_cannot_replace_a_matched_spread(tmp_path: Path) -> None:
    market = _market()
    market["groups"][0]["routes"] = [
        {
            "token": "GUA", "route_key": "mirage", "long_venue": "A", "short_venue": "B",
            "depth_weighted_spread_pct": 99.0, "depth_unverified": True, "age_min": 0.1,
        },
        {
            "token": "GUA", "route_key": "verified", "long_venue": "C", "short_venue": "D",
            "depth_weighted_spread_pct": 1.25, "depth_unverified": False, "age_min": 0.1,
        },
    ]
    market["groups"][0]["best_route"] = market["groups"][0]["routes"][0]
    market["groups"][0]["best_edge_pct"] = 99.0

    payload = token_rankings.build(
        board_path=tmp_path / "board.jsonl",
        output_path=tmp_path / "rankings.json",
        market_payload=market,
        catalog_payload={"markets": []},
        radar_routes=[],
        catalogue_summaries={},
    )

    row = payload["records"][0]
    assert row["best_spread_pct"] == 1.25
    assert row["best_spread_route"]["route_key"] == "verified"


def test_identity_warned_scanner_route_cannot_lead_token_rankings(tmp_path: Path) -> None:
    market = _market()
    market["groups"][0]["routes"] = [
        {
            "token": "GUA", "route_key": "collision", "long_venue": "A", "short_venue": "B",
            "depth_weighted_spread_pct": 60.0, "mirage_guarded": True,
            "funding_projected_24h_pct": 9.0, "age_min": 0.1,
        },
        {
            "token": "GUA", "route_key": "clean", "long_venue": "C", "short_venue": "D",
            "depth_weighted_spread_pct": 1.5, "mirage_guarded": False,
            "funding_projected_24h_pct": 0.4, "age_min": 0.1,
        },
    ]
    market["groups"][0]["best_route"] = market["groups"][0]["routes"][0]
    market["groups"][0]["best_edge_pct"] = 60.0
    market["groups"][0]["best_funding_route"] = market["groups"][0]["routes"][0]
    market["groups"][0]["best_funding_24h_pct"] = 9.0

    payload = token_rankings.build(
        board_path=tmp_path / "board.jsonl", output_path=tmp_path / "rankings.json",
        market_payload=market, catalog_payload={"markets": []}, radar_routes=[],
        catalogue_summaries={},
    )

    row = payload["records"][0]
    assert row["best_spread_pct"] == 1.5
    assert row["best_spread_route"]["route_key"] == "clean"
    assert row["funding_now_24h_pct"] == 0.4
    assert row["best_funding_route"]["route_key"] == "clean"


def test_token_with_only_guarded_routes_has_no_ranked_spread(tmp_path: Path) -> None:
    market = _market()
    guarded = {
        "token": "GUA", "route_key": "collision", "long_venue": "A", "short_venue": "B",
        "depth_weighted_spread_pct": 60.0, "mirage_guarded": True, "age_min": 0.1,
    }
    market["groups"][0]["routes"] = [guarded]
    market["groups"][0]["best_route"] = guarded
    market["groups"][0]["best_edge_pct"] = 60.0

    payload = token_rankings.build(
        board_path=tmp_path / "board.jsonl", output_path=tmp_path / "rankings.json",
        market_payload=market, catalog_payload={"markets": []}, radar_routes=[],
        catalogue_summaries={},
    )

    row = payload["records"][0]
    assert row["best_spread_pct"] is None
    assert row["best_spread_route"] is None


def test_guarded_non_leader_does_not_mark_verified_ranked_token(tmp_path: Path) -> None:
    market = _market()
    clean = dict(market["groups"][0]["routes"][0])
    clean["route_key"] = "clean"
    clean["depth_weighted_spread_pct"] = 1.0
    clean["mirage_guarded"] = False
    guarded = dict(clean)
    guarded["route_key"] = "guarded"
    guarded["depth_weighted_spread_pct"] = 60.0
    guarded["mirage_guarded"] = True
    market["groups"][0]["routes"] = [guarded, clean]
    market["groups"][0]["best_route"] = guarded

    payload = token_rankings.build(
        board_path=tmp_path / "board.jsonl", output_path=tmp_path / "rankings.json",
        market_payload=market, catalog_payload={"markets": []}, radar_routes=[],
        catalogue_summaries={},
    )

    row = payload["records"][0]
    assert row["best_spread_route"]["route_key"] == "clean"
    assert row["identity_warning"] is False


def test_fresh_catalog_replaces_older_cex_leader_even_when_edge_fell(tmp_path: Path) -> None:
    current = {
        "best_spread_pct": 0.4,
        "best_spread_route": {
            "route_kind": "FUTURES",
            "long_venue": "Mexc",
            "short_venue": "Gate",
            "depth_weighted_spread_pct": 0.4,
        },
        "funding_now_24h_pct": 0.1,
        "best_funding_route": {
            "route_kind": "FUTURES",
            "long_venue": "Mexc",
            "short_venue": "Gate",
            "funding_projected_24h_pct": 0.1,
        },
        "quoteable_pair_count": 2,
        "fresh_market_count": 2,
        "fresh_venue_count": 2,
        "age_min": 0.1,
    }
    payload = token_rankings.build(
        board_path=tmp_path / "board.jsonl",
        output_path=tmp_path / "rankings.json",
        market_payload=_market(),
        catalog_payload={"markets": []},
        radar_routes=[],
        catalogue_summaries={"GUA": current},
    )
    row = payload["records"][0]
    assert row["best_spread_pct"] == 0.4
    assert row["funding_now_24h_pct"] == 0.1
    assert row["live_venue_count"] == 2
