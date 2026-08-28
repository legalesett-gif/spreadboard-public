from __future__ import annotations

import time

from spreadboard import coverage_reconciliation, live_book_cache


def _reference() -> dict:
    return coverage_reconciliation.validate_reference_payload(
        {
            "source": "uacryptoinvest.com",
            "source_url": "https://uacryptoinvest.com/arbitrage",
            "observed_at": "2026-08-28T12:00:00Z",
            "rows": [
                {
                    "token": "gua",
                    "long_venue": "Mexc",
                    "long_market_type": "Futures",
                    "short_venue": "Gate",
                    "short_market_type": "Futures",
                    "reference_spread_pct": 0.1,
                    "source_rank": 1,
                    "sample_bucket": "top",
                },
                {
                    "token": "MISSING",
                    "long_venue": "Mexc",
                    "long_market_type": "Spot",
                    "short_venue": "Gate",
                    "short_market_type": "Futures",
                    "reference_spread_pct": 0.7,
                    "source_rank": 20,
                    "sample_bucket": "tail",
                },
            ],
        }
    )


def test_reconciliation_traces_every_absence_and_flags_large_difference() -> None:
    now_us = int(time.time() * 1_000_000)
    route = {
        "route_key": "GUA-route",
        "token": "GUA",
        "route_kind": "FUTURES",
        "long_venue": "Mexc",
        "long_market_type": "Futures",
        "long_market_symbol": "GUA/USDT:USDT",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
        "executable_spread_pct": 0.8,
        "quote_ts_us": now_us,
    }
    long_key = live_book_cache.cache_key("Mexc", "Futures", "GUA/USDT:USDT")
    short_key = live_book_cache.cache_key("Gate", "Futures", "GUA/USDT:USDT")
    catalog = {
        "markets": [
            {"token": "GUA", "venue": "Mexc", "market_type": "Futures"},
            {"token": "GUA", "venue": "Gate", "market_type": "Futures"},
            {"token": "MISSING", "venue": "Mexc", "market_type": "Spot"},
        ]
    }

    status = coverage_reconciliation.reconcile(
        _reference(),
        routes=[route],
        catalog=catalog,
        fresh_books={long_key: object(), short_key: object()},
        navigation_status={"complete": True, "empty_view_count": 0},
        book_coverage_status={"status": "ok"},
    )

    assert status["exact_pair_recall_pct"] == 50.0
    assert status["unexplained_absence_count"] == 0
    assert status["spread_investigation_count"] == 1
    assert status["rows"][0]["long_market_symbol"] == "GUA/USDT:USDT"
    assert status["rows"][0]["official_book_investigation_required"] is True
    assert status["rows"][1]["reason_code"] == "missing_short_catalog_market"
    assert "exact_pair_recall_below_95_pct" in status["failures"]


def test_book_coverage_warns_after_two_low_cycles_and_is_immediately_critical(
    tmp_path,
) -> None:
    path = tmp_path / "book-health.json"
    first = coverage_reconciliation.record_book_coverage(
        {
            "book_coverage_pct": 89.0,
            "catalog_market_count": 100,
            "fresh_market_count": 89,
            "missing_book_count": 11,
        },
        path=path,
    )
    second = coverage_reconciliation.record_book_coverage(
        {"book_coverage_pct": 88.0}, path=path
    )
    critical = coverage_reconciliation.record_book_coverage(
        {"book_coverage_pct": 77.95}, path=path
    )
    recovered = coverage_reconciliation.record_book_coverage(
        {"book_coverage_pct": 96.0}, path=path
    )

    assert first["status"] == "ok"
    assert second["status"] == "warn"
    assert critical["status"] == "critical"
    assert recovered["status"] == "ok"
    assert recovered["consecutive_below_90"] == 0


def test_okx_dex_monitor_requires_identity_and_current_matched_quote() -> None:
    now_us = int(time.time() * 1_000_000)
    route = {
        "route_key": "dex-gua",
        "token": "GUA",
        "route_kind": "DEX-FUTURES",
        "long_venue": "OKX DEX 56",
        "long_market_type": "Spot",
        "long_market_symbol": "0xgua",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
        "dex_chain": "56",
        "dex_contract": "0xgua",
        "depth_weighted_spread_pct": 1.0,
        "matched_size_notional_usd": 500.0,
        "quote_ts_us": now_us,
    }

    health = coverage_reconciliation.dex_monitor([route])

    assert health["status"] == "ok"
    assert health["route_count"] == 1
    assert health["current_matched_route_count"] == 1
    assert health["target_notional_usd"] == 500.0


def test_okx_dex_monitor_uses_current_overlay_without_extending_timestamp(
    monkeypatch,
) -> None:
    now_us = int(time.time() * 1_000_000)
    stale_us = now_us - 20 * 60 * 1_000_000
    route = {
        "route_key": "dex-gua-overlay",
        "token": "GUA",
        "route_kind": "DEX-FUTURES",
        "long_venue": "OKX DEX 56",
        "short_venue": "Gate",
        "dex_chain": "56",
        "dex_contract": "0xgua",
        "depth_weighted_spread_pct": 0.1,
        "matched_size_notional_usd": 500.0,
        "quote_ts_us": stale_us,
    }
    monkeypatch.setattr(
        coverage_reconciliation.api_spreads,
        "live_route_updates_for",
        lambda rows, include_basis: {
            "dex-gua-overlay": (1.25, None, now_us, "matched_500_usd")
        },
    )

    health = coverage_reconciliation.dex_monitor([route])

    assert health["status"] == "ok"
    assert health["current_matched_route_count"] == 1
    assert route["quote_ts_us"] == stale_us
