from __future__ import annotations

from dataclasses import replace
import json

from spreadboard import api_spreads, executor_boundary, server, tokenized_assets


def _row(**changes):
    base = api_spreads.SpreadTerminalRow(
        token="SIREN",
        token_name="Siren",
        route_key="SIREN|A|Spot|B|Futures",
        route_kind="SPOT-FUTURES",
        source_group="public_api",
        source_label="Public API",
        source_name=None,
        long_venue="A",
        long_market_type="Spot",
        short_venue="B",
        short_market_type="Futures",
        executable_spread_pct=1.0,
        depth_weighted_spread_pct=0.8,
        displayed_open_spread_pct=1.0,
        funding_apr_pct=36.5,
        funding_daily_pct=0.1,
        funding_spread_pct=0.1,
        depth_usd=1_500_000,
        validation_state="ok",
        executor_status=None,
        status="live",
        blockers=[],
        next_action="research",
        freshness="fresh",
        age_min=1.0,
        quote_ts_us=1,
        href="/token/SIREN",
        long_volume_24h_usd=2_000_000,
        short_volume_24h_usd=1_500_000,
        long_market_symbol="SIREN/USDC",
        short_market_symbol="SIREN/USDT:USDT",
        long_quote="USDC",
        short_quote="USDT",
    )
    return replace(base, **changes)


def test_tokenized_ticker_is_visible_but_blocked_without_registry(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text('{"assets": {}}', encoding="utf-8")

    guard = tokenized_assets.classify(
        {
            "token": "AMZNSTOCK",
            "long_venue": "Mexc",
            "short_venue": "Hyperliquid",
            "long_market_symbol": "AMZNSTOCK/USDT:USDT",
            "short_market_symbol": "xyz:AMZN",
        },
        path=registry,
    )

    assert guard["asset_class"] == "tokenized"
    assert guard["status"] == "blocked"
    assert guard["execution_policy"] == "research_only"
    assert "tokenized_registry_missing" in guard["reasons"]


def test_complete_registry_verifies_evidence_but_never_enables_execution(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "assets": {
                    "AMZNSTOCK": {
                        "underlying_symbol": "AMZN",
                        "instrument_type": "equity_perpetual",
                        "issuer_or_market": "Venue-issued reference contract",
                        "oracle_source": "NASDAQ composite reference",
                        "trading_hours": "Venue schedule; may trade outside cash session",
                        "corporate_action_policy": "Venue contract specification applies",
                        "venues": ["Mexc", "Hyperliquid"],
                        "source_url": "https://example.com/contract-specification",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    guard = tokenized_assets.classify(
        {
            "token": "AMZNSTOCK",
            "long_venue": "Mexc",
            "short_venue": "Hyperliquid",
        },
        path=registry,
    )

    assert guard["status"] == "verified"
    assert guard["reasons"] == []
    assert guard["execution_policy"] == "research_only"


def test_crypto_and_tokenized_lanes_filter_independently(monkeypatch):
    crypto = _row()
    tokenized = _row(
        token="AMZNSTOCK",
        token_name="Amazon tokenized stock",
        route_key="AMZNSTOCK|Mexc|Futures|Hyperliquid|Futures",
        long_venue="Mexc",
        long_market_type="Futures",
        short_venue="Hyperliquid",
        short_market_type="Futures",
        long_market_symbol="AMZNSTOCK/USDT:USDT",
        short_market_symbol="xyz:AMZN",
        asset_class="tokenized",
        route_kind="DEX-FUTURES",
    )
    assert api_spreads._filter_rows([crypto, tokenized], asset_class="crypto") == [crypto]
    assert api_spreads._filter_rows([crypto, tokenized], asset_class="tokenized") == [tokenized]

    monkeypatch.setattr(
        tokenized_assets,
        "classify",
        lambda route: {
            "asset_class": "tokenized",
            "status": "blocked",
            "execution_policy": "research_only",
        },
    )
    assert api_spreads.tokenized_route_rankable(tokenized) is False
    assert api_spreads.tokenized_route_rankable(crypto) is True


def test_tokenized_guard_reaches_api_and_both_market_views(monkeypatch):
    tokenized = _row(
        token="AMZNSTOCK",
        token_name="Amazon tokenized stock",
        route_key="AMZNSTOCK|Mexc|Futures|Hyperliquid|Futures",
        long_venue="Mexc",
        long_market_type="Futures",
        short_venue="Hyperliquid",
        short_market_type="Futures",
        long_market_symbol="AMZNSTOCK/USDT:USDT",
        short_market_symbol="xyz:AMZN",
        # This test isolates the tokenized-identity badge. A cross-quote pair
        # is independently rejected by the Markets safety boundary.
        short_quote="USDC",
        asset_class="tokenized",
        route_kind="DEX-FUTURES",
        quote_ts_us=int(server.time.time() * 1_000_000),
    )
    monkeypatch.setattr(
        tokenized_assets,
        "classify",
        lambda route: {
            "asset_class": "tokenized",
            "status": "blocked",
            "underlying_symbol": "AMZN",
            "instrument_type": None,
            "oracle_source": None,
            "trading_hours": None,
            "source_url": None,
            "reasons": ["oracle_unresolved"],
            "execution_policy": "research_only",
        },
    )
    payload = api_spreads._public_row(tokenized)

    assert payload["tokenized_guard"]["status"] == "blocked"
    assert "dd pending" in server.render_tokenized_guard_badge(payload).casefold()
    detail = server.render_pair_tokenized_guard(payload)
    assert "Due diligence pending" in detail
    assert "Oracle / reference" in detail
    assert "matching equity ticker" in detail

    group = {
        "token": "AMZNSTOCK",
        "token_name": "Amazon tokenized stock",
        "venues": ["Mexc", "Hyperliquid"],
        "route_kinds": ["DEX-FUTURES"],
        "route_count": 1,
        "best_edge_pct": 0.2,
        "age_min": 1,
        "best_route": payload,
    }
    assert "Tokenized · DD pending" in server.render_market_token_group(group)


def test_market_filter_and_executor_attestation_are_explicit(monkeypatch):
    html = server.render_market_filter_bar(
        {
            "summary": {},
            "route_kind_token_counts": {},
            "lane_token_counts": {},
            "asset_class_counts": {"crypto": 12, "tokenized": 3},
        },
        {"asset_class": ["tokenized"]},
    )
    assert "Tokenized assets" in html
    assert 'name="asset_class" value="tokenized"' in html

    monkeypatch.setattr(executor_boundary.credential_crypto, "encryption_available", lambda: True)
    monkeypatch.setattr(executor_boundary.credential_crypto, "decryption_available", lambda: False)
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadboard.example")
    monkeypatch.setenv("SPREADBOARD_EXECUTOR_URL", "https://spreadboard.example/orders")
    same_origin = executor_boundary.status()
    assert same_origin["separate_origin_verified"] is False
    assert same_origin["handoff_enabled"] is False
    assert same_origin["exchange_credentials_loaded"] is False
    assert same_origin["read_only_accounting"] == {
        "browser_sealed_envelope_intake": True,
        "web_process_decryption_available": False,
        "separate_worker_required": True,
        "execution_permissions_allowed": False,
    }
    assert same_origin["order_capabilities"] == []

    monkeypatch.setenv("SPREADBOARD_EXECUTOR_URL", "https://executor.example")
    separate = executor_boundary.status()
    assert separate["separate_origin_verified"] is True
    assert separate["handoff_enabled"] is False
    assert separate["verdict"] == "separate_origin_reserved"

    page = server.render_executor_boundary_page()
    assert "0" in page and "order capabilities" in page
    assert "Research here. Execution somewhere else." in page
    assert "No handoff endpoint is exposed" in page
    assert "Read-only accounting is a separate trust path" in page
    assert "browser encrypts them before upload" in page
    assert "web process cannot decrypt them" in page
    assert "private balances or positions" not in page

    monkeypatch.setattr(executor_boundary.credential_crypto, "decryption_available", lambda: True)
    violation = executor_boundary.status()
    assert violation["verdict"] == "web_secret_boundary_violation"
    violation_page = server.render_executor_boundary_page()
    assert "Boundary violation: this web process can decrypt" in violation_page
    assert "web process cannot decrypt them" not in violation_page


def test_telegram_landing_preview_shows_reader_text_not_transport_html(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server.telegram_bot,
        "status",
        lambda: {
            "bot_username": None,
            "community_configured": False,
            "public_feed_outbound_ready": False,
            "query_snapshot_ready": True,
            "query_snapshot_age_seconds": 125.0,
            "query_snapshot_token_count": 691,
            "query_snapshot_route_count": 2_511,
        },
    )
    monkeypatch.setattr(
        server.telegram_bot,
        "render_public_digest",
        lambda **_kwargs: '<b>SpreadBoard</b>\n1. <b><a href="https://example.test">SIREN</a></b> · +1.00%',
    )

    page = server.render_telegram_landing_page(tmp_path / "board.jsonl")

    assert "SpreadBoard\n1. SIREN · +1.00%" in page
    assert "&lt;b&gt;SpreadBoard" not in page
    assert "$SIREN" in page
    assert "/token SIREN" in page
    assert "@spreadarbitragesubscription_bot SIREN" in page
    assert "Latest completed /top snapshot" in page
    assert "What /top returns now" not in page
    assert "research pro entitlement and is not connected yet" in page.lower()
    assert "already connected" not in page


def test_telegram_landing_reports_connected_forum_from_runtime_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server.telegram_bot,
        "status",
        lambda: {
            "bot_username": "spreadarbitragesubscription_bot",
            "community_configured": True,
            "public_feed_outbound_ready": False,
            "query_snapshot_ready": True,
            "query_snapshot_age_seconds": 30.0,
            "query_snapshot_token_count": 42,
            "query_snapshot_route_count": 120,
        },
    )
    monkeypatch.setattr(
        server.telegram_bot,
        "render_public_digest",
        lambda **_kwargs: "SpreadBoard latest completed snapshot\nUpdated 30s ago",
    )

    page = server.render_telegram_landing_page(tmp_path / "board.jsonl")

    assert "research pro entitlement and is connected" in page.lower()
    assert "not connected yet" not in page


def test_telegram_landing_names_a_warming_snapshot_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server.telegram_bot,
        "status",
        lambda: {
            "bot_username": "spreadarbitragesubscription_bot",
            "community_configured": True,
            "public_feed_outbound_ready": False,
            "query_snapshot_ready": False,
            "query_snapshot_age_seconds": None,
            "query_snapshot_token_count": 0,
            "query_snapshot_route_count": 0,
        },
    )
    monkeypatch.setattr(
        server.telegram_bot,
        "render_public_digest",
        lambda **_kwargs: "The Telegram snapshot is still warming.",
    )

    page = server.render_telegram_landing_page(tmp_path / "board.jsonl")

    assert "/top snapshot warming" in page
    assert "Latest completed /top snapshot" not in page
    assert "What /top returns now" not in page
