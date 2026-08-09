from __future__ import annotations

from dataclasses import replace
import json

from spreadboard import api_spreads, server, token_metadata


def _row(**changes):
    base = api_spreads.SpreadTerminalRow(
        token="TEST",
        token_name="Test Asset",
        route_key="TEST|A|Spot|B|Futures",
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
        href="/token/TEST",
        long_volume_24h_usd=2_000_000,
        short_volume_24h_usd=1_500_000,
        long_market_symbol="TEST/USDC",
        short_market_symbol="TEST/USDT:USDT",
        long_quote="USDC",
        short_quote="USDT",
        market_cap_usd=50_000_000,
        fdv_usd=80_000_000,
        listing_age_days=12,
        listing_age_source="scanner_first_seen",
    )
    return replace(base, **changes)


def test_advanced_filters_require_route_specific_evidence(monkeypatch):
    row = _row()
    assert api_spreads._filter_rows([row], quote="USDC") == [row]
    assert api_spreads._filter_rows([row], quote="EUR") == []
    assert api_spreads._filter_rows([row], min_volume_24h_usd=1_400_000) == [row]
    assert api_spreads._filter_rows([row], min_volume_24h_usd=1_600_000) == []
    assert api_spreads._filter_rows([row], min_market_cap_usd=40_000_000) == [row]
    assert api_spreads._filter_rows([row], max_fdv_usd=70_000_000) == []
    assert api_spreads._filter_rows([row], max_listing_age_days=14) == [row]
    assert api_spreads._filter_rows([row], max_listing_age_days=7) == []

    monkeypatch.setattr(
        api_spreads.venue_funding_history,
        "route_windows",
        lambda route: {"1d": 0.1, "7d": 0.4, "30d": None},
    )
    assert api_spreads._filter_rows([row], persistence="persistent") == [row]
    assert api_spreads._filter_rows([row], persistence="mixed") == []


def test_unknown_metadata_is_only_excluded_when_filter_is_active():
    row = _row(market_cap_usd=None, fdv_usd=None, listing_age_days=None)
    assert api_spreads._filter_rows([row]) == [row]
    assert api_spreads._filter_rows([row], min_market_cap_usd=1) == []
    assert api_spreads._filter_rows([row], max_listing_age_days=30) == []


def test_market_filter_ui_labels_scanner_age_honestly():
    html = server.render_market_filter_bar(
        {"summary": {}, "route_kind_token_counts": {}, "lane_token_counts": {}},
        {"quote": ["USDC"], "max_listing_age_days": ["30"]},
    )
    assert "Advanced discovery filters" in html
    assert "Min thinner-leg 24h volume" in html
    assert "Max scanner age" in html
    assert "not presented as an exchange's official listing date" in html
    assert 'value="USDC" selected' in html
    assert 'value="USD1"' in html


def test_metadata_refresh_preserves_first_seen_and_adds_market_metrics(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "updated_at": "2020-01-01T00:00:00Z",
                "tokens": {"BTC": {"first_seen_at": "2019-01-01T00:00:00Z"}},
            }
        ),
        encoding="utf-8",
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        if "/coins/list" in request.full_url:
            return Response([{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}])
        return Response(
            [
                {
                    "id": "bitcoin",
                    "market_cap": 1_000_000,
                    "fully_diluted_valuation": 1_100_000,
                    "total_volume": 200_000,
                }
            ]
        )

    monkeypatch.setattr(token_metadata, "urlopen", fake_urlopen)
    payload = token_metadata.refresh_token_metadata(
        {"BTC"}, path=path, force=True, now=1_700_000_000
    )
    entry = payload["tokens"]["BTC"]
    assert payload["schema"] == "spreadboard.token_metadata.v2"
    assert entry["first_seen_at"] == "2019-01-01T00:00:00Z"
    assert entry["market_cap_usd"] == 1_000_000
    assert entry["fdv_usd"] == 1_100_000
    assert entry["market_volume_24h_usd"] == 200_000
