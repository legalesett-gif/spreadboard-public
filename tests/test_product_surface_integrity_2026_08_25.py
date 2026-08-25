from __future__ import annotations

import inspect

from scripts import materialized_view_worker, run_spreadboard_service
from spreadboard import alerts, catalog_pairs, server, token_rankings


def _route(**overrides):
    row = {
        "route_key": "SPCX|Gate|Spot|Gate|Futures",
        "token": "SPCX",
        "route_kind": "SPOT-FUTURES",
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "long_market_symbol": "SPCX/USDT",
        "long_quote": "USDT",
        "long_price": 127.95,
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": "SPCX/USDT:USDT",
        "short_quote": "USDT",
        "short_price": 138.0,
        "executable_spread_pct": 7.85,
        "depth_weighted_spread_pct": 7.80,
        "funding_daily_pct": 0.005,
        "age_min": 0.0,
        "spread_quote_current": True,
    }
    row.update(overrides)
    return row


def test_tokens_tab_and_alias_are_both_reachable() -> None:
    hrefs = {key: href for key, href, _label in server._MEMBER_NAV}
    assert hrefs["rankings"] == "/tokens"
    source = inspect.getsource(server.SpreadBoardHandler)
    assert '{"/tokens", "/rankings"}' in source


def test_alert_drafts_distinguish_price_funding_and_spread() -> None:
    row = _route()
    price = server.render_alert_draft_button(row, alert_type="price")
    funding = server.render_alert_draft_button(row, alert_type="token_funding")
    spread = server.render_alert_draft_button(
        row, alert_type="token_spread", allow_unquoted=True
    )
    assert 'data-alert-type="price"' in price
    assert 'data-current-value="132.975"' in price
    assert "Token price alert" in price
    assert 'data-alert-type="token_funding"' in funding
    assert 'data-current-value="0.005"' in funding
    assert 'data-current-value="7.85"' in spread


def test_selected_chart_exposes_all_four_useful_alerts() -> None:
    source = inspect.getsource(server.render_selected_chart)
    for alert_type in ("token_spread", "funding", "price", "token_funding"):
        assert f'alert_type="{alert_type}"' in source


def test_exact_token_projection_uses_complete_catalogue(monkeypatch) -> None:
    payload = {
        "token": "SPCX",
        "fresh_market_count": 2,
        "route_count": 1,
        "routes": [_route()],
    }
    monkeypatch.setattr(catalog_pairs, "for_token", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(
        catalog_pairs,
        "filtered",
        lambda source, **_kwargs: {**source, "routes": list(source["routes"])},
    )
    result = server._exact_catalog_market_projection(
        {"q": ["SPCX"], "kind": ["FUTURES-SPOT-PAIR"]},
        limit=25,
        offset=0,
    )
    assert result is not None
    assert result["mode"] == "exact_token_complete_catalogue"
    assert result["rows"][0]["long_venue"] == "Gate"
    assert result["rows"][0]["short_venue"] == "Gate"


def test_funding_materialization_keeps_counts_but_only_html_preview() -> None:
    routes = [_route(route_key=f"route-{index}") for index in range(8)]
    payload = {
        "filters": {"funding_only": True},
        "groups": [{"token": "SPCX", "route_count": 8, "routes": routes}],
        "rows": routes,
    }
    compact = materialized_view_worker._compact_funding_navigation(payload)
    assert compact["groups"][0]["route_count"] == 8
    assert len(compact["groups"][0]["routes"]) == server.FUNDING_PAIR_PREVIEW_LIMIT
    assert len(compact["rows"]) == server.FUNDING_PAIR_PREVIEW_LIMIT


def test_collector_does_not_start_legacy_in_process_board_warm(monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "collector")
    loop = run_spreadboard_service.RefreshLoop(300)
    loop._start_board_warm()
    assert loop.warm_thread is None


def test_all_in_place_refresh_surfaces_are_silent() -> None:
    source = inspect.getsource(server)
    for class_name in (
        "alerts-page",
        "watchlist-page",
        "intel-page",
        "triage-page",
        "signals-page",
    ):
        marker = source.index(f'class="{class_name}')
        assert 'data-refresh-silent="1"' in source[marker : marker + 180]


def test_live_spread_order_can_promote_a_route_beyond_old_500_cut(monkeypatch) -> None:
    records = []
    for index in range(501):
        route = _route(
            route_key=f"route-{index}",
            token=f"T{index:03d}",
            age_min=30.0,
            executable_spread_pct=float(1000 - index),
        )
        records.append(
            {
                "token": route["token"],
                "status": "live",
                "best_spread_pct": route["executable_spread_pct"],
                "best_spread_route": route,
            }
        )

    monkeypatch.setattr(
        token_rankings.api_spreads,
        "live_prices_for",
        lambda routes, **_kwargs: {
            str(route["route_key"]): (9999.0, None)
            for route in routes
            if route["route_key"] == "route-500"
        },
    )
    ranked = token_rankings.ranked(
        {"records": records}, metric="spread", limit=1
    )
    assert ranked[0]["token"] == "T500"


def test_historical_funding_resorts_by_the_value_it_displays(monkeypatch) -> None:
    low = _route(token="LOW", route_key="low")
    high = _route(token="HIGH", route_key="high")
    values = {"low": 8.625, "high": 8.85}
    monkeypatch.setattr(
        server.funding_radar,
        "window_value",
        lambda row, _window: values[str(row.get("route_key"))],
    )
    monkeypatch.setattr(
        server.api_spreads,
        "live_route_updates_for",
        lambda *_args, **_kwargs: {},
    )
    payload = {
        "filters": {"funding_only": True, "direction": "desc"},
        "funding_catalog": {"window": "30d"},
        "groups": [
            {"token": "LOW", "best_funding_route": low, "routes": [low]},
            {"token": "HIGH", "best_funding_route": high, "routes": [high]},
        ],
        "rows": [low, high],
    }
    server._apply_spread_freshness(payload)
    assert [group["token"] for group in payload["groups"]] == ["HIGH", "LOW"]


def test_token_price_median_does_not_weight_a_market_by_pair_count() -> None:
    repeated = [
        _route(
            long_venue="Gate",
            long_market_type="Spot",
            long_market_symbol="SPCX/USDT",
            long_price=100.0,
            short_venue=f"Venue-{index}",
            short_market_symbol=f"SPCX-{index}/USDT:USDT",
            short_price=200.0 + index,
        )
        for index in range(5)
    ]
    value = alerts.token_metrics(repeated)["SPCX"]["token_price"]
    assert value == 201.5
