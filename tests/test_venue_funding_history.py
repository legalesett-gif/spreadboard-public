"""Realised funding windows from each venue's settlement history.

Deriving these from our own samples was honest but empty -- routes rotate
through a 150-route sampling set, so 73 of 78 cells showed a dash. Venues
publish what actually settled, roughly 30 days of it, in well under a second.
"""

from __future__ import annotations

import time

import pytest

from spreadboard import venue_funding_history as vfh


def _settlements(count: int, rate: float, *, every_hours: int = 8, now_ms: int) -> list[dict]:
    step = every_hours * 3_600_000
    return [
        {"timestamp": now_ms - index * step, "fundingRate": rate}
        for index in range(count)
    ]


def test_a_window_is_the_sum_of_what_settled_inside_it() -> None:
    now = int(time.time() * 1000)
    # 0.01% every 8h for 30 days. The window edge is inclusive, so a day holds
    # the three settlements inside it plus the one exactly on the boundary.
    entries = _settlements(90, 0.0001, now_ms=now)

    windows = vfh.realised_windows(entries, now_ms=now)

    assert abs(windows["1d"] - 0.04) < 0.005, windows
    assert abs(windows["7d"] - 0.22) < 0.02, windows
    assert abs(windows["30d"] - 0.90) < 0.02, windows


def test_a_window_the_venue_does_not_reach_reports_nothing() -> None:
    """Bybit returns 20 days where Binance returns 30.

    Summing 20 days and calling it 30 would under-report the month.
    """
    now = int(time.time() * 1000)
    entries = _settlements(30, 0.0001, now_ms=now)  # 10 days

    windows = vfh.realised_windows(entries, now_ms=now)

    assert windows["1d"] is not None
    assert windows["7d"] is not None
    assert windows["30d"] is None


def test_no_history_is_not_a_zero() -> None:
    assert vfh.realised_windows([], now_ms=int(time.time() * 1000)) == {
        "1d": None,
        "7d": None,
        "30d": None,
    }


def test_a_spot_leg_contributes_zero_not_unknown(monkeypatch) -> None:
    """A spot leg pays no funding, so the pair is determined by its futures leg."""
    monkeypatch.setattr(
        vfh, "load", lambda **_kw: {"Bybit|COTI/USDT:USDT": {"1d": 0.5, "7d": 2.0, "30d": 8.0}}
    )
    route = {
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "long_market_symbol": "COTI/USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "COTI/USDT:USDT",
    }

    assert vfh.route_windows(route) == {"1d": 0.5, "7d": 2.0, "30d": 8.0}


def test_net_is_short_minus_long(monkeypatch) -> None:
    monkeypatch.setattr(
        vfh,
        "load",
        lambda **_kw: {
            "Gate|X/USDT:USDT": {"1d": 0.1, "7d": 0.7, "30d": 3.0},
            "Bybit|X/USDT:USDT": {"1d": 0.4, "7d": 2.1, "30d": 9.0},
        },
    )
    route = {
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "long_market_symbol": "X/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "X/USDT:USDT",
    }

    net = vfh.route_windows(route)
    assert net["1d"] == pytest.approx(0.3)
    assert net["7d"] == pytest.approx(1.4)
    assert net["30d"] == pytest.approx(6.0)


def test_a_leg_we_have_no_history_for_leaves_the_route_unknown(monkeypatch) -> None:
    monkeypatch.setattr(vfh, "load", lambda **_kw: {})
    route = {
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "long_market_symbol": "X/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "X/USDT:USDT",
    }

    assert vfh.route_windows(route) == {"1d": None, "7d": None, "30d": None}


def test_bitmart_native_history_is_parsed(monkeypatch) -> None:
    """CCXT has no funding history for BitMart, and it appears in top rows."""
    payload = {
        "data": {
            "list": [
                {"symbol": "FWDIUSDT", "funding_rate": "-0.0012", "funding_time": 1785000000000},
                {"symbol": "FWDIUSDT", "funding_rate": "0.0004", "funding_time": 1785028800000},
            ]
        }
    }

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            import json as _json

            return _json.dumps(payload).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _Response())

    entries = vfh._native_leg_history("BitMart", "FWDI/USDT:USDT")

    assert [e["fundingRate"] for e in entries] == [-0.0012, 0.0004]
    assert entries[0]["timestamp"] == 1785000000000


def test_a_venue_without_a_native_endpoint_returns_nothing() -> None:
    assert vfh._native_leg_history("Binance", "BTC/USDT:USDT") == []


def test_bitmart_http_success_with_provider_error_remains_retryable(monkeypatch) -> None:
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"code":30012,"message":"service unavailable"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _Response())

    outcome = vfh._native_leg_history_outcome("BitMart", "BTC/USDT:USDT")

    assert outcome["status"] == "api_error"
    assert outcome["error_type"] == "BitMartResponseError"


def test_coverage_distinguishes_unattempted_from_an_honest_empty_result(tmp_path) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        '{"schema":"spreadboard.venue_funding_history.v4",'
        '"leg_status":{"A|ONE":{"status":"ok"},'
        '"B|TWO":{"status":"no_history_rows"}}}'
    )

    summary = vfh.coverage_summary(
        [("A", "ONE"), ("B", "TWO"), ("C", "THREE")], cache_path=path
    )

    assert summary == {
        "catalog_leg_count": 3,
        "attempted_leg_count": 2,
        "pending_leg_count": 1,
        "retryable_error_leg_count": 0,
        "coverage_pct": 66.67,
        "catch_up_complete": False,
    }


def test_retryable_provider_failure_is_pending_not_completed(tmp_path) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        '{"schema":"spreadboard.venue_funding_history.v4",'
        '"leg_status":{"A|ONE":{"status":"client_unavailable",'
        '"last_attempt_status":"client_unavailable"}}}'
    )

    summary = vfh.coverage_summary([("A", "ONE")], cache_path=path)

    assert summary["attempted_leg_count"] == 0
    assert summary["pending_leg_count"] == 1
    assert summary["retryable_error_leg_count"] == 1
    assert not summary["catch_up_complete"]


def test_legacy_v3_empty_statuses_are_reclassified(tmp_path) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        '{"schema":"spreadboard.venue_funding_history.v3",'
        '"leg_status":{"A|ONE":{"status":"no_history_rows"}}}'
    )

    summary = vfh.coverage_summary([("A", "ONE")], cache_path=path)

    assert summary["attempted_leg_count"] == 0
    assert summary["pending_leg_count"] == 1


def test_empty_catalog_is_already_caught_up(tmp_path) -> None:
    assert vfh.coverage_summary([], cache_path=tmp_path / "missing.json") == {
        "catalog_leg_count": 0,
        "attempted_leg_count": 0,
        "pending_leg_count": 0,
        "retryable_error_leg_count": 0,
        "coverage_pct": 100.0,
        "catch_up_complete": True,
    }


def test_leg_history_outcome_distinguishes_source_failures() -> None:
    class Unsupported:
        has = {"fetchFundingRateHistory": False}
        symbols = ["ONE"]

    class MissingSymbol:
        has = {"fetchFundingRateHistory": True}
        symbols = []

    class Broken:
        has = {"fetchFundingRateHistory": True}
        symbols = ["ONE"]

        def fetch_funding_rate_history(self, *_args, **_kwargs):
            raise TimeoutError("provider timeout")

    assert vfh.leg_history_outcome(
        "Binance", "ONE", client_factory=lambda _exchange: Unsupported()
    )["status"] == "unsupported_history_api"
    assert vfh.leg_history_outcome(
        "Binance", "ONE", client_factory=lambda _exchange: MissingSymbol()
    )["status"] == "symbol_not_indexed"
    broken = vfh.leg_history_outcome(
        "Binance", "ONE", client_factory=lambda _exchange: Broken()
    )
    assert broken["status"] == "api_error"
    assert broken["error_type"] == "TimeoutError"


def test_failed_cached_client_is_retried_after_backoff(monkeypatch) -> None:
    exchange_id = "retry-test"
    vfh._CLIENTS.pop(exchange_id, None)
    vfh._CLIENT_ERRORS.pop(exchange_id, None)
    vfh._CLIENT_FAILURE_AT[exchange_id] = 100.0
    monotonic = iter((120.0, 161.0, 161.0))
    monkeypatch.setattr(vfh.time, "monotonic", lambda: next(monotonic))

    assert vfh._client(exchange_id) is None
    # After the one-minute backoff the adapter is attempted again. This test
    # exchange has no adapter, so it remains unavailable rather than cached
    # forever as a permanent failure.
    assert vfh._client(exchange_id) is None
    assert vfh._CLIENT_FAILURE_AT[exchange_id] == 161.0


def test_retryable_refresh_retains_v4_classification_and_cached_windows(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        '{"schema":"spreadboard.venue_funding_history.v4",'
        '"legs":{"A|ONE":{"1d":1.0,"7d":2.0,"30d":3.0}},'
        '"leg_updated_at":{"A|ONE":"earlier"},'
        '"leg_status":{"A|ONE":{"status":"ok"}}}'
    )
    monkeypatch.setattr(
        vfh,
        "leg_history_outcome",
        lambda *_args, **_kwargs: {
            "status": "api_error", "entries": [], "error_type": "TimeoutError"
        },
    )

    result = vfh.build([("A", "ONE")], cache_path=path, budget_seconds=5)
    payload = __import__("json").loads(path.read_text())

    assert result["A|ONE"] == {"1d": 1.0, "7d": 2.0, "30d": 3.0}
    assert payload["leg_status"]["A|ONE"]["status"] == "ok_cached"
    assert payload["leg_status"]["A|ONE"]["last_attempt_status"] == "api_error"
    assert payload["catalog_attempted_leg_count"] == 1
    assert payload["catalog_pending_leg_count"] == 0


def test_retryable_refresh_does_not_verify_legacy_cached_windows(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        '{"schema":"spreadboard.venue_funding_history.v3",'
        '"legs":{"A|ONE":{"1d":1.0,"7d":2.0,"30d":3.0}},'
        '"leg_updated_at":{"A|ONE":"earlier"},'
        '"leg_status":{"A|ONE":{"status":"ok"}}}'
    )
    monkeypatch.setattr(
        vfh,
        "leg_history_outcome",
        lambda *_args, **_kwargs: {
            "status": "client_unavailable",
            "entries": [],
            "error_type": "TimeoutError",
        },
    )

    result = vfh.build([("A", "ONE")], cache_path=path, budget_seconds=5)
    payload = __import__("json").loads(path.read_text())

    assert result["A|ONE"] == {"1d": 1.0, "7d": 2.0, "30d": 3.0}
    assert payload["leg_status"]["A|ONE"]["status"] == "unclassified_cached"
    assert payload["catalog_attempted_leg_count"] == 0
    assert payload["catalog_pending_leg_count"] == 1
