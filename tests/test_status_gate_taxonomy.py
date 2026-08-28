"""/api/status must separate serving health from archive completeness.

On 2026-08-28 production served current data correctly -- /api/health reported
ok=true, 0.18 minute market age and 26,012 live books -- while /api/status
reported ok=false. Two independent reporting faults caused it:

* the DEX component read ``canonical["dex_spot_source"]``, which describes the
  retired standalone DEX-Spot product and is no longer published, so it always
  resolved to an empty mapping and reported ``row_count: 0`` even though the
  fast-quote pass was publishing 32 current DEX-FUTURES tokens; and
* the exact-settlement backlog was rendered with the contradictory detail
  "100.0% source coverage" beside a degraded status, because the percentage
  described source classification while the status described rolling-window
  completeness.

The external monitor consumed the single ``ok`` bit, so availability, current
data quality and archive completeness were indistinguishable from an outage.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from spreadboard import server


def _healthy_environment(monkeypatch: pytest.MonkeyPatch, *, funding: dict) -> None:
    monkeypatch.setattr(
        server.crypto_billing,
        "status",
        lambda: {"checkout_ready": True, "chain": "Arbitrum One", "tokens": ["USDT"]},
    )
    monkeypatch.setattr(
        server.telegram_bot,
        "status",
        lambda: {"configured": True, "community_configured": True},
    )
    monkeypatch.setattr(server.mailer, "status", lambda: {"configured": True})
    monkeypatch.setattr(server, "funding_history_health", lambda: funding)
    monkeypatch.setattr(
        server,
        "accounting_worker_status",
        lambda: {"configured": True, "running": True, "read_only": True},
    )


def _status(monkeypatch: pytest.MonkeyPatch) -> dict:
    return server.api_public_status(
        Path("board.json"),
        {},
        SimpleNamespace(running=True, poll_seconds=30),
        SimpleNamespace(running=True, poll_seconds=900),
    )


def test_a_live_dex_futures_lane_is_not_reported_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact production reading: 32 current DEX-FUTURES tokens."""

    monkeypatch.setattr(
        server,
        "api_source_health",
        lambda *_a, **_kw: {
            "ok": True,
            "canonical_api": {
                "status": "warming",
                "updated_at": "2026-08-28T19:41:50Z",
                "age_min": 0.45,
                "lane_token_counts": {"DEX-FUTURES": 32},
                "fast_quote_refresh": {
                    "status": "ok",
                    "cycle_complete": True,
                    "lane_token_counts": {"DEX-FUTURES": 32},
                    "top_25_ready": {"DEX-FUTURES": True},
                    "failure_reason_counts": {},
                },
            },
        },
    )
    _healthy_environment(monkeypatch, funding={"status": "operational", "coverage_pct": 100.0})

    payload = _status(monkeypatch)

    dex = payload["components"]["dex_quotes"]
    assert dex["status"] == "operational", (
        "a lane publishing 32 current tokens was reported degraded because the "
        "builder read the retired dex_spot_source key"
    )
    assert dex["row_count"] == 32
    assert dex["lane"] == "DEX-FUTURES"
    assert payload["ok"] is True


def test_a_filling_funding_archive_does_not_claim_the_site_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded advancing backlog is not an availability incident."""

    monkeypatch.setattr(
        server,
        "api_source_health",
        lambda *_a, **_kw: {
            "ok": True,
            "canonical_api": {
                "age_min": 0.45,
                "lane_token_counts": {"DEX-FUTURES": 32},
                "fast_quote_refresh": {
                    "status": "ok",
                    "lane_token_counts": {"DEX-FUTURES": 32},
                    "top_25_ready": {"DEX-FUTURES": True},
                },
            },
        },
    )
    _healthy_environment(
        monkeypatch,
        funding={
            "status": "archive_catching_up",
            "coverage_pct": 100.0,
            "history_catch_up_complete": False,
            "window_coverage_pct": {"1d": 29.33, "7d": 28.94, "30d": 25.76},
            # A genuinely bounded backlog: more legs current than overdue.
            "window_leg_counts": {"1d": 7000, "7d": 6900, "30d": 6000},
            "overdue_window_leg_counts": {"1d": 2400, "7d": 2300, "30d": 2100},
        },
    )

    payload = _status(monkeypatch)

    funding = payload["components"]["funding_history"]
    assert funding["status"] == "catching_up"
    assert "100.0% source coverage" != funding["detail"], (
        "the detail must not print full coverage beside a degraded status"
    )
    assert "blank" in funding["detail"], "incomplete windows must stay blank"
    assert payload["gates"]["availability"]["status"] == "operational"
    assert payload["gates"]["current_market_data"]["status"] == "operational"
    assert payload["gates"]["historical_completeness"]["status"] == "catching_up"
    assert payload["ok"] is True, (
        "an advancing archive backlog must not read as a production outage"
    )


def test_a_stale_market_still_fails_the_availability_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Separating the gates must not hide a real current-data failure."""

    monkeypatch.setattr(
        server,
        "api_source_health",
        lambda *_a, **_kw: {
            "ok": False,
            "canonical_api": {
                "age_min": 45.0,
                "lane_token_counts": {"DEX-FUTURES": 32},
                "fast_quote_refresh": {
                    "status": "ok",
                    "lane_token_counts": {"DEX-FUTURES": 32},
                    "top_25_ready": {"DEX-FUTURES": True},
                },
            },
        },
    )
    _healthy_environment(monkeypatch, funding={"status": "operational", "coverage_pct": 100.0})

    payload = _status(monkeypatch)

    assert payload["components"]["market_data"]["status"] == "degraded"
    assert payload["gates"]["availability"]["status"] == "degraded"
    assert payload["gates"]["availability"]["failing"] == ["market_data"]
    assert payload["ok"] is False


def test_a_shrinking_settlement_archive_is_not_called_catching_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact 2026-08-28 reading: overdue legs outnumbered current ones 3:1.

    ``archive_catching_up`` is the collector's word for "not complete", not
    evidence of progress. While the collector was being OOM-killed, complete
    24h windows fell 3,071 -> 2,293 in an hour and overdue rose 6,130 -> 6,914.
    Reporting that as "still filling" would have been false.
    """

    monkeypatch.setattr(
        server,
        "api_source_health",
        lambda *_a, **_kw: {
            "ok": True,
            "canonical_api": {
                "age_min": 0.45,
                "lane_token_counts": {"DEX-FUTURES": 36},
                "fast_quote_refresh": {
                    "status": "ok",
                    "lane_token_counts": {"DEX-FUTURES": 36},
                    "top_25_ready": {"DEX-FUTURES": True},
                },
            },
        },
    )
    _healthy_environment(
        monkeypatch,
        funding={
            "status": "archive_catching_up",
            "coverage_pct": 100.0,
            "history_catch_up_complete": False,
            "window_coverage_pct": {"1d": 21.9, "7d": 21.56, "30d": 19.39},
            "window_leg_counts": {"1d": 2293, "7d": 2257, "30d": 2030},
            "overdue_window_leg_counts": {"1d": 6914, "7d": 6842, "30d": 5922},
        },
    )

    payload = _status(monkeypatch)

    funding = payload["components"]["funding_history"]
    assert funding["status"] == "degraded", (
        "a backlog larger than what is current is a real fault, not catch-up"
    )
    assert "filling" not in funding["detail"], "never claim progress that is not happening"
    assert "6914" in funding["detail"] and "2293" in funding["detail"]
    assert payload["gates"]["historical_completeness"]["status"] == "degraded"
    # It is still not an availability incident: the product serves current data.
    assert payload["gates"]["availability"]["status"] == "operational"
    assert payload["ok"] is True
