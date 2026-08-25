"""Funding HTML stays bounded while the exact API catalogue remains complete."""

from pathlib import Path

from spreadboard import server


def _route(token: str, index: int) -> dict:
    return {
        "token": token,
        "route_key": f"{token}|Mexc|Spot|Gate|Futures|{index}",
        "route_kind": "SPOT-FUTURES",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_market_symbol": f"{token}/USDT",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": f"{token}/USDT:USDT",
        "funding_daily_pct": 1.0 - index / 10_000,
        "funding_projected_24h_pct": 1.0 - index / 10_000,
        "executable_spread_pct": 0.1,
        "depth_weighted_spread_pct": 0.1,
        "age_min": 0.2,
        "funding_age_min": 0.2,
    }


def test_complete_page_previews_three_pairs_per_token_without_bloating_html(
    monkeypatch,
) -> None:
    groups = []
    for token_number in range(25):
        token = f"T{token_number:02d}"
        routes = [_route(token, route_number) for route_number in range(100)]
        groups.append(
            {
                "token": token,
                "token_name": token,
                "routes": routes,
                "best_funding_route": routes[0],
                "best_funding_24h_pct": 1.0,
                "route_count": len(routes),
            }
        )

    monkeypatch.setattr(
        server,
        "api_market_spreads",
        lambda _path, _query: {
            "ok": True,
            "groups": groups,
            "summary": {"matching_tokens": 25, "matching_rows": 2_500},
            "source_health": {"canonical_api": {"status": "fresh"}},
            "funding_catalog": {
                "matching_token_count": 25,
                "matching_route_count": 2_500,
                "largest_value": 1.0,
                "window_token_counts": {"1d": 25, "7d": 25, "30d": 25},
            },
        },
    )
    monkeypatch.setattr(
        server,
        "funding_history_health",
        lambda: {
            "attempted_leg_count": 100,
            "catalog_leg_count": 100,
            "classified_leg_count": 100,
            "pending_leg_count": 0,
            "retryable_error_leg_count": 0,
            "window_leg_counts": {"1d": 100, "7d": 100, "30d": 100},
            "window_coverage_pct": {"1d": 100.0, "7d": 100.0, "30d": 100.0},
            "deep_history_pending_leg_count": 0,
        },
    )
    monkeypatch.setattr(
        server.bulk_quotes,
        "funding_health",
        lambda: {"status": "fresh", "p95_age_seconds": 30.0},
    )
    monkeypatch.setattr(server.api_spreads, "spread_quote_current", lambda _row: True)

    html = server.render_funding_page(Path("board.json"), {}, {})

    assert html.count('<article class="funding-pair-row') == 75
    assert html.count("Showing the best 3 of 100 exact pairs") == 25
    assert "97 more are included in this token page's Export JSON" in html
    assert "this page's Export JSON includes every pair for these tokens" in html
    assert "limit=25&amp;offset=0" in html
    assert "T00|Mexc|Spot|Gate|Futures|2" in html
    assert "T00|Mexc|Spot|Gate|Futures|3" not in html
    assert len(html.encode("utf-8")) < 250_000


def test_group_renderer_remains_complete_without_an_explicit_preview_limit(
    monkeypatch,
) -> None:
    routes = [_route("FULL", index) for index in range(5)]
    monkeypatch.setattr(server.api_spreads, "spread_quote_current", lambda _row: True)

    html = server.render_funding_token_group(
        {
            "token": "FULL",
            "token_name": "Full catalogue",
            "routes": routes,
            "best_funding_route": routes[0],
            "route_count": len(routes),
        }
    )

    assert html.count('<article class="funding-pair-row') == 5
    assert "funding-pair-overflow" not in html


def test_exact_token_search_renders_every_route_and_token_links_to_that_ledger(
    monkeypatch,
) -> None:
    routes = [_route("GUA", index) for index in range(12)]
    group = {
        "token": "GUA",
        "token_name": "GUA",
        "routes": routes,
        "best_funding_route": routes[0],
        "route_count": len(routes),
    }
    monkeypatch.setattr(
        server,
        "api_market_spreads",
        lambda _path, _query: {
            "ok": True,
            "groups": [group],
            "summary": {"matching_tokens": 1, "matching_rows": 12},
            "source_health": {"canonical_api": {"status": "fresh"}},
            "funding_catalog": {
                "matching_token_count": 1,
                "matching_route_count": 12,
                "largest_value": 1.0,
                "window_token_counts": {"1d": 1, "7d": 1, "30d": 1},
            },
        },
    )
    monkeypatch.setattr(
        server,
        "funding_history_health",
        lambda: {
            "attempted_leg_count": 1,
            "catalog_leg_count": 1,
            "classified_leg_count": 1,
            "pending_leg_count": 0,
            "retryable_error_leg_count": 0,
            "window_leg_counts": {"1d": 1, "7d": 1, "30d": 1},
            "window_coverage_pct": {"1d": 100.0, "7d": 100.0, "30d": 100.0},
            "deep_history_pending_leg_count": 0,
        },
    )
    monkeypatch.setattr(
        server.bulk_quotes,
        "funding_health",
        lambda: {"status": "fresh", "p95_age_seconds": 30.0},
    )
    monkeypatch.setattr(server.api_spreads, "spread_quote_current", lambda _row: True)

    exact = server.render_funding_page(
        Path("board.json"), {}, {"q": ["GUA"], "rank": ["now"]}
    )
    overview = server.render_funding_page(Path("board.json"), {}, {})

    assert exact.count('<article class="funding-pair-row') == 12
    assert "all exact pairs shown for the exact token match" in exact
    assert 'href="/funding?farm=futures-futures&amp;rank=now&amp;limit=25&amp;q=GUA"' in overview
    assert "Show every exact route for this token" in overview
