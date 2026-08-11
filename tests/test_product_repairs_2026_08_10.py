from __future__ import annotations

import json
from pathlib import Path

from spreadboard import accounts, server, venue_funding_history


def _user(email: str, role: str = "member") -> accounts.User:
    return accounts.User(
        id=1,
        email=email,
        display_name=email.split("@", 1)[0],
        role=role,
        subscription_status="active",
        subscription_expires_at=None,
        monthly_capital_usd=None,
    )


def test_alex_and_anatolij_can_operate_the_member_ledger() -> None:
    assert _user("alex@spreadarbitrage.ink", "admin").can_manage_members
    assert _user("anatolij@spreadarbitrage.ink", "admin").can_manage_members
    assert not _user("admin@spreadboard.local", "admin").can_manage_members
    assert not _user("subscriber@example.com").can_manage_members


def test_partial_funding_refresh_merges_instead_of_erasing_previous_legs(
    tmp_path: Path, monkeypatch,
) -> None:
    cache = tmp_path / "funding.json"
    cache.write_text(json.dumps({
        "schema": "spreadboard.venue_funding_history.v1",
        "next_cursor": 0,
        "legs": {"Bybit|OLD/USDT:USDT": {"1d": 1.0, "7d": 2.0, "30d": 3.0}},
    }))
    now = 1_800_000_000_000
    entries = [
        {"timestamp": now - index * 8 * 3_600_000, "fundingRate": 0.0001}
        for index in range(91)
    ]
    monkeypatch.setattr(venue_funding_history.time, "time", lambda: now / 1000)
    monkeypatch.setattr(
        venue_funding_history,
        "leg_history_outcome",
        lambda *_a, **_k: {"status": "ok", "entries": entries},
    )

    result = venue_funding_history.build(
        [("Gate", "NEW/USDT:USDT")], cache_path=cache, budget_seconds=5,
    )
    payload = json.loads(cache.read_text())

    assert "Bybit|OLD/USDT:USDT" in result
    assert "Gate|NEW/USDT:USDT" in result
    assert payload["schema"] == "spreadboard.venue_funding_history.v4"
    assert payload["leg_updated_at"]["Gate|NEW/USDT:USDT"]


def test_dex_and_spot_cards_do_not_render_fake_funding_fields() -> None:
    for market_type, venue in (("Spot", "Gate"), ("Futures", "OKX DEX 56")):
        leg = {"venue": venue, "market_type": market_type, "market_symbol": "GUA/USDT"}
        pair_html = server.render_leg_card("Long", leg)
        chart_html = server.render_chart_leg_stats("Long", leg)
        assert "Live funding" not in pair_html
        assert "Live funding" not in chart_html
        assert "not applicable" not in pair_html
        assert "not applicable" not in chart_html

    futures = {"venue": "Gate", "market_type": "Futures", "market_symbol": "GUA/USDT:USDT"}
    assert "Live funding" in server.render_leg_card("Short", futures)


def test_payout_cadence_has_no_exchange_names_and_dex_never_pays() -> None:
    row = {
        "long_venue": "OKX DEX 56",
        "long_market_type": "Futures",  # defensive against a malformed source row
        "long_funding_interval_hours": 8,
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_funding_interval_hours": 4,
    }
    assert server.funding_cadence_pair(row) == "every 4h"
    assert "Gate" not in server.funding_cadence_pair(row)
    assert not server.leg_pays_funding(row, "long")
    assert server.leg_pays_funding(row, "short")


def test_position_cancel_never_depends_on_required_field_validation() -> None:
    html = server.render_position_dialog() + server.render_position_action_dialog()
    assert html.count('type="button" data-dialog-cancel') == 4
    assert '<button value="cancel"' not in html
    script = server.render_account_script()
    assert "dialog?.close('cancel')" in script


def test_position_suggestions_ignore_stale_out_of_order_responses() -> None:
    script = server.render_account_script()
    assert "suggestionVersion=0" in script
    assert "requestVersion=++suggestionVersion" in script
    assert script.count("if(requestVersion!==suggestionVersion)return") == 2


def test_position_suggestions_include_chart_catalogue_dex_long_pairs(monkeypatch) -> None:
    monkeypatch.setattr(server.telegram_queries, "client_visible_payload", lambda: {})
    monkeypatch.setattr(server.api_spreads, "load_spreads", lambda **_k: {"rows": []})
    monkeypatch.setattr(server.chart_catalog, "load", lambda: {
        "generated_at": "now",
        "markets": [
            {"token": "GUA", "venue": "OKX DEX 56", "market_type": "Spot", "symbol": "GUA/USDT"},
            {"token": "GUA", "venue": "Gate", "market_type": "Futures", "symbol": "GUA/USDT:USDT"},
            {"token": "GUA", "venue": "Mexc", "market_type": "Spot", "symbol": "GUA/USDT"},
        ],
    })

    data = server.api_position_suggestions(Path("missing"), {"q": ["GUA"], "limit": ["50"]})

    assert any(
        route["long_market_type"] == "DEX"
        and route["short_market_type"] == "Futures"
        for route in data["routes"]
    )
    assert len(data["routes"]) > 1


def test_watchlist_uses_live_then_retained_then_chart_context(monkeypatch) -> None:
    live = {
        "token": "GUA", "route_key": "gua-live", "route_kind": "SPOT-FUTURES",
        "long_venue": "Mexc", "long_market_type": "Spot",
        "short_venue": "Gate", "short_market_type": "Futures",
        "executable_spread_pct": 2.5, "funding_24h_pct": 0.8, "freshness": "fresh",
    }
    cooled = {
        "token": "ESPORTS", "route_key": "esports-old", "route_kind": "DEX-FUTURES",
        "long_venue": "OKX DEX 56", "long_market_type": "Spot",
        "short_venue": "Gate", "short_market_type": "Futures",
        "radar_historical": True, "radar_windows": {"1d": 0.7},
    }
    gua_cooled = {
        "token": "GUA", "route_key": "gua-funding", "route_kind": "SPOT-FUTURES",
        "long_venue": "Mexc", "long_market_type": "Spot",
        "short_venue": "Aster", "short_market_type": "Futures",
        "radar_historical": True, "radar_windows": {"1d": 1.2, "7d": 3.5},
    }
    monkeypatch.setattr(server.telegram_queries, "client_visible_payload", lambda: {"rows": [live]})
    monkeypatch.setattr(
        server.funding_radar,
        "routes_for",
        lambda symbol: [cooled] if symbol == "ESPORTS" else [gua_cooled] if symbol == "GUA" else [],
    )
    monkeypatch.setattr(server.chart_catalog, "load", lambda: {"markets": [{"token": "CHARTONLY"}]})

    context = server._watchlist_market_context(Path("missing"), ["GUA", "ESPORTS", "CHARTONLY"])
    by_symbol = {item["symbol"]: item for item in context}

    assert by_symbol["GUA"]["status"] == "live routes"
    assert by_symbol["GUA"]["research_route"]["route_line"] == "Mexc Spot → Aster Futures"
    assert by_symbol["GUA"]["research_score"]["reasons"][0].startswith("Net carry +1.200%")
    assert by_symbol["GUA"]["opportunities"]["funding"]["route"]["route_line"] == "Mexc Spot → Aster Futures"
    assert by_symbol["GUA"]["opportunities"]["spread"]["route"]["route_line"] == "Mexc Spot → Gate Futures"
    assert "DEX DEX" not in by_symbol["ESPORTS"]["routes"][0]["route_line"]
    assert by_symbol["ESPORTS"]["status"] == "cooled funding radar"
    assert by_symbol["ESPORTS"]["routes"][0]["pair_url"].startswith("/charts?")
    assert by_symbol["CHARTONLY"]["status"] == "chart markets available"


def test_product_guide_and_fair_price_explain_every_major_tool() -> None:
    guide = server.render_guide_page()
    for label in (
        "Membership", "Arbitrage", "Funding", "Fair price", "Charts",
        "Intel", "Watchlist", "Portfolio", "Alerts and Telegram",
    ):
        assert label in guide
    assert "How to use SpreadBoard" in guide

    fair = server.render_fair_price_page()
    assert "mean-reversion scanner" in fair
    assert "not guaranteed true value" in fair


def test_long_telegram_bot_handle_can_wrap_on_mobile() -> None:
    css = server.shell("Test", "telegram", "")
    assert ".telegram-command-grid article { min-width:0;" in css
    assert ".telegram-command-grid code {" in css
    assert "overflow-wrap:anywhere" in css


def test_telegram_link_uses_same_tab_and_has_confirmation_fallback(tmp_path: Path) -> None:
    script = server.render_account_script()
    assert "location.assign(data.url)" in script
    assert "window.open(data.url" not in script
    assert "/api/telegram/status" in script
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    assert "data-telegram-fallback" in server.render_account_settings(
        _user("alex@spreadarbitrage.ink", "admin"), db,
    )
