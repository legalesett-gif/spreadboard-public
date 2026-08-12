from __future__ import annotations

import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from spreadboard import (
    accounts,
    alerts,
    intel,
    portfolio,
    research_score,
    server,
    venue_funding_history,
)


def _user(db_path: Path, email: str, name: str = "Member") -> dict:
    return accounts.create_user(
        email=email,
        display_name=name,
        password="member-password-is-long",
        subscription_status="active",
        subscription_days=30,
        db_path=db_path,
    )


def test_pushover_save_validates_and_clears_inactive_device(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "app-token")
    accounts.initialize(db_path)
    user = _user(db_path, "push@example.test")
    monkeypatch.setattr(
        alerts,
        "validate_pushover_user",
        lambda **_kwargs: {"ok": True, "status": 200, "devices": ["active_phone"]},
    )

    result = server.save_pushover_preferences(
        user["id"],
        {
            "pushover_user_key": "K" * 30,
            "pushover_device": "old_phone",
            "pushover_enabled": True,
            "pushover_sound": "pushover",
        },
        accounts_path=db_path,
    )

    assert result["warning"] == "pushover_device_cleared"
    assert result["active_device_count"] == 1
    assert result["preferences"]["pushover_device"] == ""
    assert "K" * 30 not in json.dumps(result)
    assert accounts.notification_credentials(user["id"], db_path=db_path)["user_key"] == "K" * 30


def test_portfolio_funding_is_exact_leg_data_even_when_route_is_not_ranked(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user_row = _user(db_path, "portfolio@example.test")
    accounts.create_position(
        user_row["id"],
        {
            "token": "ANYTOKEN",
            "long_venue": "Example Spot",
            "long_market_type": "Spot",
            "long_symbol": "ANYTOKEN/USDT",
            "long_quantity": 100,
            "long_entry_price": 1,
            "short_venue": "Example Futures",
            "short_market_type": "Futures",
            "short_symbol": "ANYTOKEN/USDT:USDT",
            "short_quantity": 100,
            "short_entry_price": 1,
        },
        db_path=db_path,
    )
    monkeypatch.setattr(portfolio.api_spreads, "load_spreads", lambda **_kwargs: {"rows": []})
    monkeypatch.setattr(portfolio, "_live_books", lambda: {})
    monkeypatch.setattr(
        portfolio.bulk_quotes,
        "load_funding",
        lambda: {
            "Example Futures|ANYTOKEN/USDT:USDT": {
                "rate_pct": 0.12,
                "interval_hours": 8,
            }
        },
    )
    user = accounts.get_user_object(user_row["id"], db_path=db_path)

    snapshot = portfolio.portfolio_snapshot(
        user,
        board_path=tmp_path / "missing.jsonl",
        accounts_path=db_path,
        evaluate_alerts=False,
    )
    position = snapshot["positions"][0]
    assert position["market_status"] == "unavailable"
    assert position["current_funding"]["short"]["rate_pct"] == pytest.approx(0.12)
    assert position["current_net_funding_24h_pct"] == pytest.approx(0.36)


def test_manual_position_marks_use_resident_books_without_exchange_calls() -> None:
    books = {
        "A|Spot|X/USDT": SimpleNamespace(bids=[[1.1, 20]], asks=[[1.2, 20]], quote_ts_us=1),
        "B|Futures|X/USDT:USDT": SimpleNamespace(bids=[[1.3, 20]], asks=[[1.4, 20]], quote_ts_us=2),
    }
    result = portfolio._quote_position(
        {
            "route_key": "X|A|Spot|B|Futures",
            "token": "X",
            "long_venue": "A",
            "long_market_type": "Spot",
            "long_symbol": "X/USDT",
            "long_quantity": 10,
            "short_venue": "B",
            "short_market_type": "Futures",
            "short_symbol": "X/USDT:USDT",
            "short_quantity": 10,
        },
        books=books,
    )
    assert result["long_bid"] == 1.1
    assert result["short_ask"] == 1.4
    assert result["position_quote_source"] == "resident_book_midpoint"


def test_watchlist_score_is_explainable_and_model_free() -> None:
    result = research_score.evaluate(
        {
            "funding_projected_24h_pct": 0.4,
            "depth_usd": 25_000,
            "executable_spread_pct": -0.2,
            "freshness": "fresh",
            "long_market_type": "Spot",
            "short_market_type": "Futures",
            "long_market_symbol": "X/USDT",
            "short_market_symbol": "X/USDT:USDT",
            "blockers": [],
        },
        windows={"1d": 0.3, "7d": 1.4, "30d": 4.5},
    )
    assert 0 <= result["score"] <= 100
    assert 0 <= result["confidence"] <= 100
    assert set(result["components"]) == {
        "carry",
        "persistence",
        "liquidity",
        "execution",
        "integrity",
        "risk",
    }
    assert result["method"] == "deterministic_dual_opportunity_evidence_v4"
    assert 0 <= result["funding_opportunity"]["score"] <= 100
    assert 0 <= result["spread_opportunity"]["score"] <= 100
    assert "spread_contribution" in result["funding_opportunity"]["components"]
    assert "funding_contribution" in result["spread_opportunity"]["components"]
    assert "Rule-based collateral reserve" in result["planning_buffer_label"]
    assert "Model stress reserve" not in result["planning_buffer_label"]
    assert "not personalized" in result["disclaimer"]


def test_funding_and_spread_scores_are_separate_with_explicit_cross_effects() -> None:
    base_route = {
        "route_kind": "SPOT-FUTURES",
        "long_market_type": "Spot",
        "short_market_type": "Futures",
        "depth_usd": 25_000,
        "executable_spread_pct": 1.0,
        "freshness": "fresh",
        "long_market_symbol": "X/USDT",
        "short_market_symbol": "X/USDT:USDT",
        "blockers": [],
    }

    def history(*, converging: bool) -> list[dict]:
        rows = []
        for hour in range(80):
            basis = 1.8 + (-0.012 if converging else 0.012) * hour
            rows.append(
                {
                    "quote_ts_us": 1_800_000_000_000_000 + hour * 3_600_000_000,
                    "long_price": 100.0,
                    "short_price": 100.0 * (1.0 + basis / 100.0),
                    "executable_spread_pct": basis,
                    "funding_daily_pct": 0.4,
                    "sample_source": "live_chart_exact_route",
                }
            )
        return rows

    positive_funding = research_score.evaluate(
        {**base_route, "funding_24h_pct": 0.4},
        windows={"1d": 0.4, "7d": 2.8, "30d": 12.0},
        history=history(converging=True),
    )
    negative_funding = research_score.evaluate(
        {**base_route, "funding_24h_pct": -0.4},
        windows={"1d": -0.4, "7d": -2.8, "30d": -12.0},
        history=history(converging=True),
    )
    adverse_basis = research_score.evaluate(
        {**base_route, "funding_24h_pct": 0.4},
        windows={"1d": 0.4, "7d": 2.8, "30d": 12.0},
        history=history(converging=False),
    )
    cooled_spread = research_score.evaluate(
        {**base_route, "executable_spread_pct": -0.2, "funding_24h_pct": 0.4},
        windows={"1d": 0.4, "7d": 2.8, "30d": 12.0},
        history=history(converging=True),
    )

    assert (
        positive_funding["spread_opportunity"]["score"]
        > negative_funding["spread_opportunity"]["score"]
    )
    assert (
        positive_funding["spread_opportunity"]["components"]["funding_contribution"]["value"]
        > negative_funding["spread_opportunity"]["components"]["funding_contribution"]["value"]
    )
    assert (
        positive_funding["funding_opportunity"]["score"]
        > adverse_basis["funding_opportunity"]["score"]
    )
    assert positive_funding["spread_opportunity"]["convergence"]["convergence_probability"] == 1.0
    assert 8 <= positive_funding["spread_opportunity"]["convergence"]["samples"] <= 11
    assert positive_funding["route_economics"]["expected_convergence_capture_pct"] > 0
    assert adverse_basis["route_economics"]["expected_convergence_capture_pct"] < 0
    # A negative entry now gets no current opening-edge credit, while the
    # separate historical convergence factor remains visible for radar value.
    assert cooled_spread["spread_opportunity"]["components"]["entry_edge"]["value"] == 0
    assert cooled_spread["spread_opportunity"]["components"]["convergence_history"]["value"] > 0
    assert cooled_spread["route_economics"]["expected_convergence_capture_pct"] is None


def test_spread_funding_cross_effect_uses_current_and_settled_horizons() -> None:
    route = {
        "route_kind": "SPOT-FUTURES",
        "long_market_type": "Spot",
        "short_market_type": "Futures",
        "funding_projected_24h_pct": -0.10,
        "funding_24h_pct": 0.30,
        "depth_usd": 25_000,
        "executable_spread_pct": 0.5,
        "freshness": "fresh",
        "long_market_symbol": "X/USDT",
        "short_market_symbol": "X/USDT:USDT",
        "blockers": [],
    }
    mixed = research_score.evaluate(
        route,
        windows={"1d": 0.30, "7d": 2.8, "30d": 9.0},
    )
    persistent_negative = research_score.evaluate(
        {**route, "funding_24h_pct": -0.30},
        windows={"1d": -0.30, "7d": -2.8, "30d": -9.0},
    )

    outlook = mixed["spread_opportunity"]["funding_outlook"]
    assert outlook["regime"] == "current_negative_historical_positive"
    assert outlook["regime_conflict"] is True
    assert outlook["expected_24h_pct"] > 0
    assert outlook["expected_24h_pct"] < outlook["historical_daily_pct"]
    assert mixed["spread_opportunity"]["components"]["funding_contribution"]["value"] > 7.5
    assert (
        mixed["spread_opportunity"]["components"]["funding_contribution"]["value"]
        > persistent_negative["spread_opportunity"]["components"]["funding_contribution"]["value"]
    )


def test_funding_outlook_renormalises_missing_windows_without_fake_zeroes() -> None:
    outlook = research_score.assess_funding_outlook(
        {"funding_projected_24h_pct": 0.4},
        windows={"7d": 1.4},
    )
    assert set(outlook["horizons"]) == {"current", "7d"}
    assert sum(outlook["weights"].values()) == pytest.approx(1.0)
    assert "1d" not in outlook["weights"]
    assert outlook["expected_24h_pct"] == pytest.approx((0.4 * 0.35 + 0.2 * 0.25) / 0.60)


def test_retained_radar_outlook_never_relabels_last_live_projection_as_current() -> None:
    outlook = research_score.assess_funding_outlook(
        {
            "radar_historical": True,
            "freshness": "historical",
            "funding_projected_24h_pct": 9.9,
        },
        windows={"1d": 0.3, "7d": 1.4, "30d": 3.0},
    )
    assert "current" not in outlook["horizons"]
    assert outlook["regime_conflict"] is False
    assert outlook["expected_24h_pct"] == pytest.approx(
        (0.3 * 0.30 + 0.2 * 0.25 + 0.1 * 0.10) / 0.65
    )


def test_dex_score_exposes_size_cost_and_identity_evidence() -> None:
    result = research_score.evaluate(
        {
            "route_kind": "DEX-FUTURES",
            "long_market_type": "DEX",
            "short_market_type": "Futures",
            "funding_projected_24h_pct": 0.4,
            "executable_spread_pct": 1.0,
            "gas_adjusted_spread_pct": 0.9,
            "matched_size_notional_usd": 50.0,
            "dex_gas_estimate_usd": 0.05,
            "dex_price_impact_pct": 0.2,
            "dex_chain": "56",
            "dex_contract": "0xabc",
            "dex_quote_source": "okx_dex_quote",
            "dex_route_plan": ["router"],
            "freshness": "fresh",
            "long_market_symbol": "0xabc",
            "short_market_symbol": "X/USDT:USDT",
            "blockers": [],
        },
        windows={"1d": 0.3},
    )

    assert result["dex_evidence"]["status"] == "size_and_cost_evidenced"
    assert result["dex_evidence"]["entry_gas_pct"] == pytest.approx(0.1)
    assert result["route_economics"]["known_dex_entry_gas_pct"] == pytest.approx(0.1)
    assert "Gas-adjusted opening basis" in result["spread_opportunity"]["components"]["entry_edge"]["detail"]


def test_watchlist_collateral_reserve_uses_volatility_and_tail_risk() -> None:
    route = {
        "route_kind": "SPOT-FUTURES",
        "long_market_type": "Spot",
        "short_market_type": "Futures",
        "funding_24h_pct": 0.4,
        "depth_usd": 25_000,
        "executable_spread_pct": 0.2,
        "freshness": "fresh",
        "long_market_symbol": "X/USDT",
        "short_market_symbol": "X/USDT:USDT",
        "blockers": [],
    }

    def history(amplitude: float) -> list[dict]:
        rows = []
        for hour in range(24 * 10):
            common = 100 * (1 + 0.0002 * hour)
            short = common * (1 + amplitude * math.sin(hour * 1.7))
            rows.append(
                {
                    "quote_ts_us": 1_800_000_000_000_000 + hour * 3_600_000_000,
                    "long_price": common,
                    "short_price": short,
                    "executable_spread_pct": (short / common - 1) * 100,
                    "funding_daily_pct": 0.4,
                    "sample_source": "live_chart_exact_route",
                }
            )
        return rows

    calm = research_score.evaluate(
        route,
        windows={"1d": 0.4, "7d": 2.8, "30d": 12},
        history=history(0.001),
    )
    volatile = research_score.evaluate(
        route,
        windows={"1d": 0.4, "7d": 2.8, "30d": 12},
        history=history(0.12),
    )
    assert calm["risk_estimate"]["data_quality"]["grade"] == "strong"
    assert volatile["planning_buffer_pct"] > calm["planning_buffer_pct"]
    assert volatile["components"]["risk"]["value"] < calm["components"]["risk"]["value"]
    assert volatile["risk_estimate"]["futures_legs"]["short"]["parametric_99_pct"] > 0


def test_spot_only_route_does_not_invent_a_futures_margin_number() -> None:
    result = research_score.evaluate(
        {
            "route_kind": "SPOT",
            "long_market_type": "Spot",
            "short_market_type": "Spot",
            "funding_24h_pct": 0.0,
            "depth_usd": 5_000,
            "executable_spread_pct": 1.0,
            "freshness": "fresh",
            "long_market_symbol": "X/USDT",
            "short_market_symbol": "X/USDT",
            "blockers": [],
        }
    )
    assert result["planning_buffer_pct"] is None
    assert result["risk_estimate"]["status"] == "not_applicable"


def test_collateral_stress_uses_intraday_peak_not_only_24h_endpoint() -> None:
    route = {
        "route_kind": "SPOT-FUTURES",
        "long_market_type": "Spot",
        "short_market_type": "Futures",
        "depth_usd": 25_000,
        "executable_spread_pct": 0.1,
        "long_market_symbol": "X/USDT",
        "short_market_symbol": "X/USDT:USDT",
    }
    history = []
    for hour in range(24 * 10 + 1):
        within_day = hour % 24
        # Every window ends back at 100, but the short futures leg spikes 30%
        # intraday. Endpoint-only returns would incorrectly report no stress.
        short_price = 130.0 if within_day == 12 else 100.0
        history.append(
            {
                "quote_ts_us": 1_800_000_000_000_000 + hour * 3_600_000_000,
                "long_price": 100.0,
                "short_price": short_price,
                "executable_spread_pct": short_price - 100.0,
                "sample_source": "live_chart_exact_route",
            }
        )

    result = research_score.assess_route_risk(route, history=history)

    assert result["futures_legs"]["short"]["adverse_24h_p95_pct"] == pytest.approx(30.0)


def test_selected_chart_exposes_live_spread_and_funding_alerts() -> None:
    route_key = "X|FUTURES|A|Futures|B|Futures"
    html = server.render_selected_chart(
        {
            "route_key": route_key,
            "token": "X",
            "route_kind": "FUTURES",
            "long_venue": "A",
            "long_market_type": "Futures",
            "short_venue": "B",
            "short_market_type": "Futures",
            "displayed_open_spread_pct": 1.2,
            "funding_24h_pct": 0.3,
        },
        {"legs": {}},
        [],
        "1h",
    )
    assert html.count('class="route-alert-btn js-alert-draft"') == 2
    assert 'data-alert-type="token_spread"' in html
    assert 'data-alert-type="funding"' in html
    assert "Spread alert" in html
    assert "Funding alert" in html


def test_watchlist_is_hard_limited_to_ten_per_member(tmp_path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = _user(db_path, "watch@example.test")
    saved = accounts.replace_watchlist(
        user["id"], [f"TOKEN{i}" for i in range(20)], db_path=db_path
    )
    assert len(saved) == 10
    assert accounts.list_watchlist(user["id"], db_path=db_path) == saved


def test_bot_attention_event_contains_no_identity_or_raw_message(tmp_path) -> None:
    path = tmp_path / "attention.jsonl"
    assert intel.record_token_attention(
        "GUA", "funding", source="private_bot", events_path=path, now=1_700_000_000
    )
    event = json.loads(path.read_text())
    serialized = json.dumps(event).casefold()
    assert event["parsed"]["symbol"] == "GUA"
    for forbidden in (
        "chat_id",
        "user_id",
        "message_id",
        "username",
        "email",
        "display_name",
        '"text"',
    ):
        assert forbidden not in serialized


def test_member_intel_hides_internal_missing_pipeline_cards() -> None:
    html = server.render_intel_source_grid(
        {
            "telegram_events": {"status": "fresh", "age_min": 2},
            "board": {"status": "fresh", "age_min": 1},
            "preflight_candidates": {"status": "missing"},
            "strategy_prompts": {"status": "missing"},
        }
    )
    assert "Bot attention" in html and "Market routes" in html
    assert "0 paid AI calls" in html
    assert "Preflight" not in html and "Prompts" not in html

    waiting_html = server.render_intel_source_grid(
        {
            "telegram_events": {"status": "missing"},
            "board": {"status": "fresh", "age_min": 1},
        }
    )
    assert "waiting" in waiting_html


def test_member_intel_uses_one_activation_state_until_bot_attention_is_fresh(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "api_intel",
        lambda *_args, **_kwargs: {
            "source_freshness": {
                "telegram_events": {"status": "missing"},
                "board": {"status": "fresh", "age_min": 1},
            }
        },
    )
    html = server.render_intel_page(Path("/missing.json"), {}, {})
    assert "Intel activates from the next @SpreadArbitrageBot lookup" in html
    assert "https://t.me/SpreadArbitrageBot" in html
    assert "Latest Brief" not in html
    assert "What's Hot" not in html
    assert "Recent Feed" not in html


def test_priority_funding_legs_are_attempted_before_rotating_catalog(tmp_path, monkeypatch) -> None:
    attempted = []
    monkeypatch.setattr(
        venue_funding_history,
        "leg_history_outcome",
        lambda venue, symbol: {
            "status": "ok",
            "entries": attempted.append((venue, symbol))
            or [{"timestamp": 1_700_000_000_000, "fundingRate": 0.001}],
        },
    )
    monkeypatch.setattr(venue_funding_history.time, "time", lambda: 1_700_000_000)
    venue_funding_history.build(
        [("A", "ONE"), ("B", "TWO")],
        priority_legs=[("P", "PINNED")],
        cache_path=tmp_path / "funding.json",
        budget_seconds=30,
    )
    assert attempted[0] == ("P", "PINNED")


def test_position_mutations_cannot_cross_user_boundary(tmp_path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    owner = _user(db_path, "owner@example.test")
    attacker = _user(db_path, "attacker@example.test")
    position = accounts.create_position(
        owner["id"],
        {
            "token": "PRIVATE",
            "long_venue": "A",
            "long_market_type": "Spot",
            "long_quantity": 1,
            "long_entry_price": 1,
            "short_venue": "B",
            "short_market_type": "Futures",
            "short_quantity": 1,
            "short_entry_price": 1,
        },
        db_path=db_path,
    )
    with pytest.raises(ValueError, match="open_position_not_found"):
        accounts.close_position(
            attacker["id"],
            position["id"],
            {"long_exit_price": 1, "short_exit_price": 1},
            db_path=db_path,
        )
    assert accounts.list_positions(attacker["id"], db_path=db_path) == []
    assert accounts.list_positions(owner["id"], db_path=db_path)[0]["status"] == "open"


def test_canonical_discovery_file_is_reported_as_a_fresh_market_source(tmp_path) -> None:
    board_path = tmp_path / "api_discovery_latest.json"
    board_path.write_text("{}")
    fast_path = tmp_path / "api_discovery_fast_quotes.json"
    fast_path.write_text("{}")
    old = os.stat(board_path).st_mtime - (180 * 60)
    os.utime(board_path, (old, old))
    now = os.stat(fast_path).st_mtime + 30
    source = intel.build_source_freshness(
        board_path=board_path,
        now=now,
        events_path=tmp_path / "events.jsonl",
        brief_dir=tmp_path / "briefs",
        preflight_candidates_path=tmp_path / "preflight.jsonl",
        strategy_queue_path=tmp_path / "queue.jsonl",
        strategy_prompts_path=tmp_path / "prompts.jsonl",
        private_preflight_path=tmp_path / "private.jsonl",
        digest_path=tmp_path / "digest.json",
    )
    assert source["board"]["exists"] is True
    assert source["board"]["status"] == "fresh"


def test_missing_bot_attention_cannot_render_legacy_community_rows(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_PUBLIC_MODE", "1")
    monkeypatch.setattr(
        server.intel,
        "build_intel",
        lambda **_kwargs: {
            "source_freshness": {
                "telegram_events": {"status": "missing"},
                "board": {"status": "fresh"},
            },
            "hot_symbols": [{"symbol": "LEGACY"}],
            "action_queue": [{"symbol": "LEGACY"}],
            "route_reality": [{"symbol": "LEGACY"}],
            "recent_events": {"alerts": [{"symbol": "LEGACY"}]},
            "question_patterns": [{"category": "legacy"}],
            "latest_brief": {"title": "legacy"},
            "alert_preview": {"cards": [{"key": "source_freshness"}]},
            "change_digest": {"recent_event_count": 9},
        },
    )
    result = server.api_intel(
        tmp_path / "canonical.json",
        {"window_hours": ["5.123"], "limit": ["7"]},
    )
    assert result["hot_symbols"] == []
    assert result["action_queue"] == []
    assert result["route_reality"] == []
    assert result["recent_events"] == {}
    assert result["latest_brief"] == {}


def test_watchlist_and_intel_hide_internal_source_cards_and_follow_dark_theme() -> None:
    assert 'if (card.key === "source_freshness") continue;' in server.WATCHLIST_SCRIPT
    html = server.shell("Intel", "intel", '<section class="intel-page"></section>')
    assert (
        ".intel-section, .change-digest, .side-card, .hot-card, .reality-card, .feed-card { background: var(--terminal-panel)"
        in html
    )
    assert ".change-counts article { display: grid;" in html
    assert "background: var(--terminal-row); border: 1px solid var(--terminal-line)" in html


def test_fresh_bot_attention_joins_the_warmed_member_market(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        server.intel,
        "build_intel",
        lambda **_kwargs: {
            "source_freshness": {
                "telegram_events": {"status": "fresh"},
                "board": {"status": "fresh"},
            },
            "hot_symbols": [{"symbol": "GUA", "event_count": 1}],
            "change_digest": {},
        },
    )
    monkeypatch.setattr(
        server.telegram_queries,
        "client_visible_payload",
        lambda: {
            "groups": [
                {
                    "token": "GUA",
                    "best_route": {
                        "route_key": "GUA|A|Spot|B|Futures",
                        "route_kind": "FUTURES-SPOT",
                        "long_venue": "A",
                        "long_market_type": "Spot",
                        "short_venue": "B",
                        "short_market_type": "Futures",
                        "executable_spread_pct": 0.4,
                        "funding_24h_pct": 0.6,
                        "freshness": "fresh",
                    },
                    "routes": [],
                }
            ]
        },
    )
    result = server.api_intel(
        tmp_path / "missing.json", {"refresh": ["1"], "window_hours": ["5.321"]}
    )
    assert result["hot_symbols"][0]["best_board"]["route_line"] == "A Spot → B Futures"
    assert result["action_queue"][0]["symbol"] == "GUA"
    assert result["action_queue"][0]["funding_24h_pct"] == 0.6
