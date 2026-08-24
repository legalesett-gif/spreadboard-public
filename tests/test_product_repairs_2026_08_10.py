from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

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


def test_new_funding_schema_discards_unverifiable_legacy_aggregates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = tmp_path / "funding.json"
    cache.write_text(
        json.dumps(
            {
                "schema": "spreadboard.venue_funding_history.v1",
                "next_cursor": 0,
                "legs": {"Bybit|OLD/USDT:USDT": {"1d": 1.0, "7d": 2.0, "30d": 3.0}},
            }
        )
    )
    now = 1_800_000_000_000
    entries = [
        {"timestamp": now - index * 8 * 3_600_000, "fundingRate": 0.0001} for index in range(91)
    ]
    monkeypatch.setattr(venue_funding_history.time, "time", lambda: now / 1000)
    monkeypatch.setattr(
        venue_funding_history,
        "leg_history_outcome",
        lambda *_a, **_k: {"status": "ok", "entries": entries},
    )

    result = venue_funding_history.build(
        [("Gate", "NEW/USDT:USDT")],
        cache_path=cache,
        budget_seconds=5,
    )
    payload = json.loads(cache.read_text())

    assert "Bybit|OLD/USDT:USDT" not in result
    assert "Gate|NEW/USDT:USDT" in result
    assert payload["schema"] == "spreadboard.venue_funding_history.v5"
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
    html = (
        server.render_position_dialog()
        + server.render_position_edit_dialog()
        + server.render_position_action_dialog()
    )
    assert html.count('type="button" data-dialog-cancel') == 6
    assert '<button value="cancel"' not in html
    script = server.render_account_script()
    assert "dialog?.close('cancel')" in script


def test_research_consent_names_component_costs_and_current_version() -> None:
    html = server.render_position_edit_dialog()
    assert "total and component lifecycle-cost percentages" in html
    assert "fees, borrow, gas, transfer and measured slippage" in html
    assert "portfolio_research_v2" in html
    assert 'name="research_consent_version" value="portfolio_research_v2"' in html
    methodology = server.render_methodology_page()
    assert "median total and component lifecycle-cost percentages" in methodology
    assert "5,000 labeled 24-hour outcomes" in methodology
    assert "Eight-hour labels are monitored separately" in methodology


def test_portfolio_uses_edit_and_exact_funding_instead_of_manual_funding_button() -> None:
    item = {
        "id": 8,
        "token": "BTW",
        "status": "open",
        "quote_status": "live",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_quantity": 13530,
        "long_entry_price": 0.2017,
        "long_mark_price": 0.21,
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "short_quantity": 13530,
        "short_entry_price": 0.2026,
        "short_mark_price": 0.211,
        "long_mark_basis": "bid_ask_midpoint",
        "short_mark_basis": "markPrice",
        "current_funding": {},
        "funding_sync_status": "exact",
        "funding_event_count": 1,
        "funding_synced_at": "2026-08-12T00:05:00Z",
        "funding_income_usd": 0.49,
        "price_pnl_usd": 20,
        "fees_usd": 0,
        "total_pnl_usd": 20.49,
    }
    html = server.render_position_card(item)
    assert "Mark movement" in html
    assert "Current marked spread" in html
    assert "Settled funding" in html
    assert "Actual fees" in html
    assert "Borrow costs" in html
    assert "Gas costs" in html
    assert "Transfer costs" in html
    assert "Slippage evidence · in fills" in html
    assert "bid/ask midpoint" in html
    assert "venue mark" in html
    assert "Exact private exchange ledger" in html
    assert "Edit position" in html
    assert "Add funding" not in html

    allocated = server.render_position_card(
        {
            **item,
            "funding_sync_status": "exact_allocated",
            "funding_event_count": 2,
            "funding_allocation_method": "quantity_pro_rata",
        }
    )
    assert "allocated by saved tranche quantity during overlap" in allocated


def test_closed_position_card_labels_realised_exit_basis() -> None:
    html = server.render_position_card(
        {
            "id": 9,
            "token": "DONE",
            "status": "closed",
            "quote_status": "closed",
            "market_status": "closed",
            "long_venue": "A",
            "long_market_type": "Spot",
            "long_quantity": 1,
            "long_entry_price": 10,
            "long_mark_price": 11,
            "long_mark_basis": "stored_exit",
            "short_venue": "B",
            "short_market_type": "Futures",
            "short_quantity": 1,
            "short_entry_price": 12,
            "short_mark_price": 11,
            "short_mark_basis": "stored_exit",
            "current_marked_spread_pct": 0,
            "alert_rules": [],
        }
    )

    assert "Realised exit spread" in html
    assert "stored exit fill" in html


def test_position_suggestions_ignore_stale_out_of_order_responses() -> None:
    script = server.render_account_script()
    assert "suggestionVersion=0" in script
    assert "requestVersion=++suggestionVersion" in script
    assert script.count("if(requestVersion!==suggestionVersion)return") == 2


def test_chart_catalogue_position_suggestions_never_fabricate_zero_evidence() -> None:
    """A missing quote is not a zero spread and a missing age is not live now.

    Production GUA had no current parsed route at this instant, so the endpoint
    correctly returned chart-catalogue combinations with null spread, prices
    and age. JavaScript's ``Number(null)`` then advertised every combination
    as ``0.000%`` and ``live · 0.0 min``. Keep catalogue identity useful for
    prefilling a journal entry without inventing market evidence.
    """

    script = server.render_account_script()

    assert "const optionalNumber=value=>" in script
    assert "value===null||value===undefined||value===''" in script
    assert "const spread=optionalNumber(route.entry_spread_pct)" in script
    assert "const age=optionalNumber(route.age_min)" in script
    assert "route.source==='live public books'" in script
    assert "chart-catalogue combination" in script
    assert "require your actual fills" in script


def test_position_suggestions_include_chart_catalogue_dex_long_pairs(monkeypatch) -> None:
    monkeypatch.setattr(server.telegram_queries, "client_visible_payload", lambda: {})
    monkeypatch.setattr(server.api_spreads, "load_spreads", lambda **_k: {"rows": []})
    monkeypatch.setattr(
        server.chart_catalog,
        "load",
        lambda: {
            "generated_at": "now",
            "markets": [
                {
                    "token": "GUA",
                    "venue": "OKX DEX 56",
                    "market_type": "Spot",
                    "symbol": "GUA/USDT",
                },
                {
                    "token": "GUA",
                    "venue": "Gate",
                    "market_type": "Futures",
                    "symbol": "GUA/USDT:USDT",
                },
                {"token": "GUA", "venue": "Mexc", "market_type": "Spot", "symbol": "GUA/USDT"},
            ],
        },
    )

    data = server.api_position_suggestions(Path("missing"), {"q": ["GUA"], "limit": ["50"]})

    assert any(
        route["long_market_type"] == "DEX" and route["short_market_type"] == "Futures"
        for route in data["routes"]
    )
    assert len(data["routes"]) > 1


def test_watchlist_uses_live_then_retained_then_chart_context(monkeypatch) -> None:
    live = {
        "token": "GUA",
        "route_key": "gua-live",
        "route_kind": "SPOT-FUTURES",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "executable_spread_pct": 2.5,
        "funding_24h_pct": 0.8,
        "freshness": "fresh",
        "age_min": 0.1,
    }
    cooled = {
        "token": "ESPORTS",
        "route_key": "esports-old",
        "route_kind": "DEX-FUTURES",
        "long_venue": "OKX DEX 56",
        "long_market_type": "Spot",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "radar_historical": True,
        "radar_windows": {"1d": 0.7},
    }
    gua_cooled = {
        "token": "GUA",
        "route_key": "gua-funding",
        "route_kind": "SPOT-FUTURES",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "radar_historical": True,
        "radar_windows": {"1d": 1.2, "7d": 3.5},
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
    assert by_symbol["GUA"]["research_score"]["reasons"][0].startswith(
        "Multi-horizon expected funding +0.882%"
    )
    assert (
        by_symbol["GUA"]["opportunities"]["funding"]["route"]["route_line"]
        == "Mexc Spot → Aster Futures"
    )
    assert (
        by_symbol["GUA"]["opportunities"]["spread"]["route"]["route_line"]
        == "Mexc Spot → Gate Futures"
    )
    assert "DEX DEX" not in by_symbol["ESPORTS"]["routes"][0]["route_line"]
    assert by_symbol["ESPORTS"]["status"] == "cooled funding radar"
    assert by_symbol["ESPORTS"]["routes"][0]["pair_url"].startswith("/charts?")
    assert by_symbol["CHARTONLY"]["status"] == "chart markets available"


def test_product_guide_and_fair_price_explain_every_major_tool() -> None:
    guide = server.render_guide_page()
    for label in (
        "Membership",
        "Arbitrage",
        "Funding",
        "Fair price",
        "Charts",
        "Intel",
        "Watchlist",
        "Portfolio",
        "Alerts and Telegram",
    ):
        assert label in guide
    assert "How to use SpreadBoard" in guide

    fair = server.render_fair_price_page()
    assert "mean-reversion scanner" in fair
    assert "not guaranteed true value" in fair


def test_long_telegram_bot_handle_can_wrap_on_mobile() -> None:
    assert "/assets/app.css" in server.shell("Test", "telegram", "")
    assert ".telegram-command-grid article { min-width:0;" in server.APP_CSS
    assert ".telegram-command-grid code {" in server.APP_CSS
    assert "overflow-wrap:anywhere" in server.APP_CSS


def test_telegram_link_uses_same_tab_and_has_confirmation_fallback(tmp_path: Path) -> None:
    script = server.render_account_script()
    assert "location.assign(data.url)" in script
    assert "window.open(data.url" not in script
    assert "/api/telegram/status" in script
    db = tmp_path / "accounts.sqlite3"
    accounts.initialize(db)
    assert "data-telegram-fallback" in server.render_account_settings(
        _user("alex@spreadarbitrage.ink", "admin"),
        db,
    )


def test_close_position_uses_full_atomic_cost_and_consent_editor() -> None:
    script = server.render_account_script()
    dialog = server.render_position_edit_dialog()

    assert "openPositionEditor(actionPosition,true)" in script
    assert "Record completed journal position" in script
    assert "Save completed journal entry" in script
    assert "close:'<label><span>Long exit price" not in script
    for field in (
        "long_exit_price",
        "short_exit_price",
        "exit_fees_usd",
        "borrow_costs_usd",
        "gas_costs_usd",
        "transfer_costs_usd",
        "slippage_costs_usd",
        "research_matched_notional_usd",
        "research_costs_complete",
        "research_cost_consent",
        "transfer_started_at",
        "transfer_credited_at",
        "research_transfer_consent",
        "research_consent_version",
    ):
        assert f'name="{field}"' in dialog


def test_completed_position_evidence_callout_is_private_and_consent_neutral() -> None:
    html = server.render_research_evidence_callout(
        [
            {
                "status": "closed",
                "long_market_type": "DEX",
                "short_market_type": "Futures",
                "research_costs_complete": 0,
                "transfer_started_at": None,
                "transfer_credited_at": None,
            },
            {"status": "open", "research_costs_complete": 0},
        ]
    )

    assert "1 completed position without confirmed final lifecycle costs" in html
    assert "1 completed DEX position without observed transfer timing" in html
    assert "sharing is separate and always optional" in html
    assert server.render_research_evidence_callout(
        [
            {
                "status": "closed",
                "long_market_type": "Spot",
                "short_market_type": "Futures",
                "research_costs_complete": 1,
            }
        ]
    ) == ""


def test_member_size_quote_reports_requested_depth_and_directional_dex_cost() -> None:
    now_us = int(time.time() * 1_000_000)
    row = {
        "route_key": "GUA|OKX DEX 56|Spot|Gate|Futures",
        "token": "GUA",
        "long_venue": "OKX DEX 56",
        "long_market_type": "Spot",
        "long_market_symbol": "GUA/USDC",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
        "dex_chain": 56,
        "dex_contract": "0xabc",
        "requires_transfer": True,
        "deliverable": True,
        "long_withdraw_enabled": True,
        "short_deposit_enabled": True,
        "executable_spread_pct": 1.4,
        "depth_weighted_spread_pct": 1.1,
        "notes": {
            "route_inputs": {
                "long": {
                    "ask": 1.0,
                    "ask_vwap": 1.01,
                    "quote_ts_us": now_us,
                    "quote_source": "okx_dex_quote",
                    "quote_notional_usd": 2500,
                    "gas_estimate_usd": 0.5,
                    "price_impact_pct": 0.18,
                    "slippage_bps": 50,
                    "mev_protection": "not_enabled_quote_only",
                },
                "short": {
                    "bid": 1.02,
                    "bid_vwap": 1.015,
                    "quote_ts_us": now_us,
                    "quote_source": "ccxt_order_book",
                },
            }
        },
    }

    payload = server._member_size_quote_payload(
        row, target_notional_usd=2500, duration_ms=123
    )

    assert payload["mode"] == "isolated_member_size_quote"
    assert payload["depth_proven_at_target"] is True
    assert payload["target_notional_usd"] == 2500
    assert payload["matched_spread_pct"] == pytest.approx(1.1)
    assert payload["estimated_opening_gas_usd"] == pytest.approx(0.5)
    assert payload["gas_adjusted_matched_spread_pct"] == pytest.approx(1.08)
    assert payload["legs"]["long"]["matched_vwap"] == pytest.approx(1.01)
    assert payload["legs"]["short"]["matched_vwap"] == pytest.approx(1.015)
    assert payload["dex_evidence"][0]["contract"] == "0xabc"
    assert payload["dex_evidence"][0]["mev_protection"] == "not_enabled_quote_only"
    assert f"isolated from the standard {server.PROBE_LABEL} rankings" in " ".join(payload["limitations"])


def test_member_size_quote_rejects_a_missing_directional_vwap() -> None:
    """An exact-size quote is not proven when the route's required sell-side
    bid VWAP is missing.  Production previously returned ``ok: true`` and
    ``depth_proven_at_target: true`` while ``matched_spread_pct`` was null.
    """

    row = {
        "route_key": "OSMO|Mexc|Spot|Kucoin|Spot",
        "token": "OSMO",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_market_symbol": "OSMO/USDT",
        "short_venue": "Kucoin",
        "short_market_type": "Spot",
        "short_market_symbol": "OSMO/USDT",
        "executable_spread_pct": -0.9,
        "depth_weighted_spread_pct": None,
        "notes": {
            "route_inputs": {
                "long": {"ask": 0.04098, "ask_vwap": 0.04369},
                "short": {"bid": 0.0406, "bid_vwap": None},
            }
        },
    }

    payload = server._member_size_quote_payload(
        row, target_notional_usd=1000, duration_ms=1200
    )

    assert payload["ok"] is False
    assert payload["error"] == "exact_route_target_depth_unavailable"
    assert payload["depth_proven_at_target"] is False
    assert payload["matched_spread_pct"] is None
    assert payload["legs"]["long"]["matched_vwap"] == pytest.approx(0.04369)
    assert payload["legs"]["short"]["matched_vwap"] is None


def test_member_size_quote_cache_single_flights_identical_route_and_size(monkeypatch) -> None:
    calls: list[float] = []

    def quote(_row: dict[str, object], notional: float) -> dict[str, object]:
        calls.append(notional)
        return {"ok": True, "target_notional_usd": notional}

    monkeypatch.setattr(server, "_run_member_size_quote", quote)
    with server._SIZE_QUOTE_LOCK:
        server._SIZE_QUOTE_CACHE.clear()
        server._SIZE_QUOTE_INFLIGHT.clear()
    first = server._cached_member_size_quote(("route", 2500.0), {"route_key": "route"}, 2500)
    second = server._cached_member_size_quote(("route", 2500.0), {"route_key": "route"}, 2500)

    assert first["ok"] is True
    assert second["cached"] is True
    assert calls == [2500]
    with server._SIZE_QUOTE_LOCK:
        server._SIZE_QUOTE_CACHE.clear()
        server._SIZE_QUOTE_INFLIGHT.clear()


def test_member_size_quote_ui_requotes_without_changing_canonical_board() -> None:
    html = server.render_pair_size_quote_panel({"route_key": "GUA|DEX|Gate"})
    calculator = server.render_net_edge_dialog()
    stylesheet = Path(server.__file__).read_text(encoding="utf-8")

    assert "Quote this route at your intended size" in html
    assert "/api/size-quote/" in html
    assert "has not changed the board rankings or chart history" in html
    assert "no order, transfer or wallet action is sent" in html
    assert "Quote current books at this size" in calculator
    assert f"standardized {server.PROBE_LABEL} matched quote" in calculator
    assert "/api/size-quote/" in calculator
    assert "data.depth_proven_at_target !== true" in html
    assert "!numeric(data.matched_spread_pct)" in html
    assert ".pair-cockpit { display: grid; gap: 14px; min-width: 0; max-width: 100%;" in server.APP_CSS
    assert ".pair-page, .pair-cockpit, .pair-cockpit-head, .pair-cockpit-grid" in stylesheet


def test_member_size_quote_rejects_unbounded_notional(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "_find_canonical_route", lambda *_args: {"route_key": "route"})
    with pytest.raises(ValueError, match="notional_usd_must_be_between"):
        server.api_size_quote("route", tmp_path / "board.json", {"notional_usd": ["100001"]})
