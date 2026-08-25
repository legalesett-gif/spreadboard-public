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
    # 0.01% every 8h for 30 days. Trailing windows use (since, now], so the
    # settlement exactly on the old boundary belongs to the preceding window.
    entries = _settlements(90, 0.0001, now_ms=now)

    windows = vfh.realised_windows(entries, now_ms=now)

    assert abs(windows["1d"] - 0.03) < 0.005, windows
    assert abs(windows["7d"] - 0.21) < 0.02, windows
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


def test_duplicate_future_and_sparse_rows_cannot_inflate_a_window() -> None:
    now = int(time.time() * 1000)
    complete = _settlements(90, 0.0001, now_ms=now)
    contaminated = [
        *complete,
        dict(complete[0]),
        {"timestamp": now + 3_600_000, "fundingRate": 9.0},
    ]
    details = vfh.realised_window_details(contaminated, now_ms=now)

    assert details["windows"]["1d"] == pytest.approx(0.03)
    assert details["discarded_duplicate_count"] == 1
    assert details["discarded_future_count"] == 1
    assert vfh.realised_windows(
        [complete[0], complete[-1]], now_ms=now
    ) == {"1d": None, "7d": None, "30d": None}
    assert details["window_details"]["30d"]["incomplete_reason"] is None


def test_incomplete_windows_explain_the_exact_evidence_gap() -> None:
    now = int(time.time() * 1000)
    details = vfh.realised_window_details(
        _settlements(30, 0.0001, now_ms=now), now_ms=now
    )

    assert details["window_details"]["1d"]["incomplete_reason"] is None
    assert (
        details["window_details"]["30d"]["incomplete_reason"]
        == "insufficient_event_coverage"
    )


def test_cached_total_expires_at_the_next_exact_settlement_boundary(tmp_path, monkeypatch) -> None:
    now_ms = 1_800_000_000_000
    entries = _settlements(90, 0.0001, now_ms=now_ms)
    details = vfh.realised_window_details(entries, now_ms=now_ms)
    path = tmp_path / "funding.json"
    path.write_text(
        __import__("json").dumps(
            {
                "schema": vfh.SCHEMA,
                "legs": {"Gate|ONE": details["windows"]},
                "leg_status": {
                    "Gate|ONE": {
                        "status": "ok",
                        "window_details": details["window_details"],
                    }
                },
            }
        )
    )
    monkeypatch.setitem(vfh._CACHE, "stamp", None)
    monkeypatch.setattr(vfh.time, "time", lambda: (now_ms + 8 * 3_600_000 - 1) / 1000)

    assert vfh.load(cache_path=path)["Gate|ONE"]["1d"] == pytest.approx(0.03)

    monkeypatch.setattr(vfh.time, "time", lambda: (now_ms + 8 * 3_600_000) / 1000)
    assert vfh.load(cache_path=path)["Gate|ONE"] == {
        "1d": None,
        "7d": None,
        "30d": None,
    }


def test_coverage_counts_only_current_complete_windows(tmp_path, monkeypatch) -> None:
    now_ms = 1_800_000_000_000
    details = vfh.realised_window_details(
        _settlements(90, 0.0001, now_ms=now_ms), now_ms=now_ms
    )
    path = tmp_path / "funding.json"
    path.write_text(
        __import__("json").dumps(
            {
                "schema": vfh.SCHEMA,
                "legs": {"Gate|ONE": details["windows"]},
                "leg_status": {
                    "Gate|ONE": {
                        "status": "ok",
                        "window_details": details["window_details"],
                    }
                },
            }
        )
    )
    monkeypatch.setattr(vfh.time, "time", lambda: (now_ms + 8 * 3_600_000) / 1000)

    summary = vfh.coverage_summary([("Gate", "ONE")], cache_path=path)

    assert summary["classified_leg_count"] == 1
    assert summary["window_leg_counts"] == {"1d": 0, "7d": 0, "30d": 0}
    assert summary["fully_complete_leg_count"] == 0
    assert summary["current_window_catch_up_complete"] is False
    assert summary["history_catch_up_complete"] is False


def test_live_tighter_schedule_expires_history_at_the_real_earlier_boundary() -> None:
    status = {
        "window_details": {
            label: {
                "complete": True,
                "latest_event_at": 1_000_000,
                "inferred_interval_hours": 8.0,
            }
            for label in ("1d", "7d", "30d")
        }
    }
    values = {"1d": 1.0, "7d": 7.0, "30d": 30.0}
    live_leg = {
        "interval_hours": 4.0,
        "next_funding_ts_us": (1_000_000 + 4 * 3_600_000) * 1000,
    }

    before, _expiry = vfh._current_leg_windows(
        values,
        status,
        now_ms=1_000_000 + 4 * 3_600_000 - 1,
        live_leg=live_leg,
    )
    at_boundary, _expiry = vfh._current_leg_windows(
        values,
        status,
        now_ms=1_000_000 + 4 * 3_600_000,
        live_leg=live_leg,
    )

    assert before == values
    assert at_boundary == {"1d": None, "7d": None, "30d": None}


def test_route_status_exposes_a_per_window_provider_evidence_note(monkeypatch) -> None:
    monkeypatch.setattr(
        vfh,
        "load",
        lambda **_kwargs: {"Gate|GUA/USDT:USDT": {"1d": 0.1, "7d": 0.7, "30d": None}},
    )
    monkeypatch.setitem(
        vfh._CACHE,
        "leg_status",
        {
            "Gate|GUA/USDT:USDT": {
                "status": "ok",
                "window_details": {
                    "30d": {
                        "incomplete_reason": "insufficient_event_coverage",
                        "event_count": 100,
                        "expected_event_count": 180,
                    }
                },
            }
        },
    )
    monkeypatch.setitem(
        vfh._CACHE,
        "legs",
        {"Gate|GUA/USDT:USDT": {"1d": 0.1, "7d": 0.7, "30d": None}},
    )
    route = {
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_market_symbol": "GUA/USDT",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
    }

    status = vfh.route_history_status(route)

    assert status["window_notes"]["30d"] == (
        "Gate returned fewer exact settlements than the complete window requires "
        "(100/180 events)."
    )


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


def test_two_spot_legs_have_no_settled_funding_window(monkeypatch) -> None:
    """Production LUNC rendered 0.000% for 1d, 7d and 30d because both
    non-funding legs contributed zero. Spot-Spot has no perpetual settlement
    to measure, so every window is not applicable rather than a realised zero.
    """

    monkeypatch.setattr(vfh, "load", dict)
    route = {
        "long_venue": "HTX",
        "long_market_type": "Spot",
        "long_market_symbol": "LUNC/USDT",
        "short_venue": "Bybit",
        "short_market_type": "Spot",
        "short_market_symbol": "LUNC/USDT",
    }

    assert vfh.route_windows(route) == {
        "1d": None,
        "7d": None,
        "30d": None,
    }


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


def test_preloaded_exact_legs_avoid_reopening_archive(monkeypatch) -> None:
    exact_legs = {
        "Gate|X/USDT:USDT": {"1d": 0.1, "7d": 0.7, "30d": 3.0},
        "Bybit|X/USDT:USDT": {"1d": 0.4, "7d": 2.1, "30d": 9.0},
    }
    monkeypatch.setattr(
        vfh,
        "load",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("preloaded archive must not be reopened per route")
        ),
    )
    route = {
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "long_market_symbol": "X/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "X/USDT:USDT",
    }

    assert vfh.route_windows(route, legs=exact_legs) == pytest.approx(
        {"1d": 0.3, "7d": 1.4, "30d": 6.0}
    )


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


def test_bitmart_unicode_market_is_percent_encoded(monkeypatch) -> None:
    seen = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"code":1000,"data":{"list":[]}}'

    def open_url(request, **_kwargs):
        seen.append(request.full_url)
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", open_url)

    outcome = vfh._native_leg_history_outcome("BitMart", "龙虾/USDT:USDT")

    assert outcome["status"] == "no_history_rows"
    assert "%E9%BE%99%E8%99%BEUSDT" in seen[0]
    assert "龙虾" not in seen[0]


def test_coverage_distinguishes_unattempted_from_an_honest_empty_result(tmp_path) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        f'{{"schema":"{vfh.SCHEMA}",'
        '"leg_status":{"A|ONE":{"status":"ok"},'
        '"B|TWO":{"status":"no_history_rows"}}}'
    )

    summary = vfh.coverage_summary(
        [("A", "ONE"), ("B", "TWO"), ("C", "THREE")], cache_path=path
    )

    assert summary == {
        "catalog_leg_count": 3,
        "attempted_leg_count": 2,
        "classified_leg_count": 2,
        "pending_leg_count": 1,
        "retryable_error_leg_count": 0,
        "coverage_pct": 66.67,
        "source_check_pct": 66.67,
        "window_leg_counts": {"1d": 0, "7d": 0, "30d": 0},
        "stored_window_leg_counts": {"1d": 0, "7d": 0, "30d": 0},
        "overdue_window_leg_counts": {"1d": 0, "7d": 0, "30d": 0},
        "window_coverage_pct": {"1d": 0.0, "7d": 0.0, "30d": 0.0},
        "fully_complete_leg_count": 0,
        "deep_history_pending_leg_count": 1,
        "catch_up_complete": False,
        "current_window_catch_up_complete": True,
        "history_catch_up_complete": False,
    }


def test_retryable_provider_failure_is_checked_but_not_classified(tmp_path) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        f'{{"schema":"{vfh.SCHEMA}",'
        '"leg_status":{"A|ONE":{"status":"client_unavailable",'
        '"last_attempt_status":"client_unavailable"}}}'
    )

    summary = vfh.coverage_summary([("A", "ONE")], cache_path=path)

    assert summary["attempted_leg_count"] == 1
    assert summary["classified_leg_count"] == 0
    assert summary["pending_leg_count"] == 0
    assert summary["retryable_error_leg_count"] == 1
    assert summary["coverage_pct"] == 0.0
    assert summary["source_check_pct"] == 100.0
    assert summary["catch_up_complete"]
    assert summary["history_catch_up_complete"]


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
        "classified_leg_count": 0,
        "pending_leg_count": 0,
        "retryable_error_leg_count": 0,
        "coverage_pct": 100.0,
        "source_check_pct": 100.0,
        "window_leg_counts": {"1d": 0, "7d": 0, "30d": 0},
        "stored_window_leg_counts": {"1d": 0, "7d": 0, "30d": 0},
        "overdue_window_leg_counts": {"1d": 0, "7d": 0, "30d": 0},
        "window_coverage_pct": {"1d": 100.0, "7d": 100.0, "30d": 100.0},
        "fully_complete_leg_count": 0,
        "deep_history_pending_leg_count": 0,
        "catch_up_complete": True,
        "current_window_catch_up_complete": True,
        "history_catch_up_complete": True,
    }


def test_service_reserves_separate_priority_and_catalog_budgets() -> None:
    import inspect

    from scripts import run_spreadboard_service

    source = inspect.getsource(run_spreadboard_service._refresh_venue_funding_history)

    assert "priority_only=True" in source
    assert "priority_legs=[]" in source
    assert "budget_seconds=30.0" in source
    assert "budget_seconds=90.0 if demanded_legs else 120.0" in source
    assert source.count("budget_seconds=120.0") == 1
    assert "[:120]" not in source
    assert 'before["history_catch_up_complete"]' in source


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


def test_a_paused_bingx_market_is_classified_without_inventing_history() -> None:
    class Paused:
        has = {"fetchFundingRateHistory": True}
        symbols = ["ONE"]

        def fetch_funding_rate_history(self, *_args, **_kwargs):
            raise RuntimeError('bingx {"code":109415,"msg":"ONE is pause currently"}')

    outcome = vfh.leg_history_outcome(
        "Bingx", "ONE", client_factory=lambda _exchange: Paused()
    )

    assert outcome == {"status": "market_paused", "entries": []}


def test_history_request_respects_the_strictest_public_limit() -> None:
    class Limited:
        has = {"fetchFundingRateHistory": True}
        symbols = ["ONE"]

        def fetch_funding_rate_history(self, _symbol, *, since, limit):
            assert since > 0
            assert limit == 100
            return [{"timestamp": since, "fundingRate": 0.0001}]

    outcome = vfh.leg_history_outcome(
        "WhiteBIT", "ONE", client_factory=lambda _exchange: Limited()
    )

    assert outcome["status"] == "ok"


@pytest.mark.parametrize(
    ("venue", "cursor_key", "cursor_value"),
    [
        ("Mexc", "page_num", 2),
        ("Bitget", "pageNo", 2),
        ("Gate", "until", 899),
        ("OKX", "after", "900"),
        ("WhiteBIT", "endDate", 1),
    ],
)
def test_provider_pagination_requests_an_older_second_page(
    venue: str, cursor_key: str, cursor_value: object
) -> None:
    calls: list[dict] = []

    class Paged:
        def fetch_funding_rate_history(self, _symbol, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return [
                    {"timestamp": 900, "fundingRate": 0.001},
                    {"timestamp": 1000, "fundingRate": 0.001},
                ]
            return []

    vfh._history_pages(
        Paged(), venue, "ONE", since=1, now_ms=2000, max_pages=2
    )

    assert calls[1]["params"][cursor_key] == cursor_value


def test_gate_starts_from_the_latest_page_before_walking_backwards() -> None:
    calls: list[dict] = []

    class Paged:
        def fetch_funding_rate_history(self, _symbol, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return [
                    {"timestamp": 900, "fundingRate": 0.001},
                    {"timestamp": 1000, "fundingRate": 0.001},
                ]
            return []

    vfh._history_pages(Paged(), "Gate", "ONE", since=1, now_ms=2000, max_pages=2)

    assert calls[0]["since"] is None
    assert "params" not in calls[0]
    assert calls[1]["params"]["until"] == 899


@pytest.mark.parametrize("venue", ["Bingx", "Coinbase International", "Bybit"])
def test_newest_first_adapters_use_their_bounded_native_paginators(venue: str) -> None:
    calls: list[dict] = []

    class Paginated:
        def fetch_funding_rate_history(self, _symbol, **kwargs):
            calls.append(kwargs)
            return [
                {"timestamp": index + 1, "fundingRate": 0.001}
                for index in range(250)
            ]

    rows, pages = vfh._history_pages(
        Paginated(), venue, "ONE", since=1, now_ms=2000, max_pages=3
    )

    assert len(rows) == 250
    assert pages == 3
    assert calls == [
        {
            "since": 1,
            "limit": 300,
            "params": {"paginate": True, "paginationCalls": 3},
        }
    ]


def test_xt_walks_backwards_with_the_oldest_native_row_id() -> None:
    calls: list[dict] = []

    class XTArchive:
        def fetch_funding_rate_history(self, _symbol, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return [
                    {
                        "timestamp": 900,
                        "fundingRate": 0.001,
                        "info": {"id": "oldest-id"},
                    },
                    {
                        "timestamp": 1000,
                        "fundingRate": 0.001,
                        "info": {"id": "newest-id"},
                    },
                ]
            return []

    vfh._history_pages(
        XTArchive(), "XT", "ONE", since=1, now_ms=2000, max_pages=2
    )

    assert calls[0]["since"] is None
    assert calls[1]["params"]["id"] == "oldest-id"


def test_gate_two_page_archive_proves_gua_and_siren_style_four_hour_windows() -> None:
    now_ms = 1_800_000_000_000
    interval_ms = 4 * 3_600_000
    since = now_ms - 31 * 86_400_000
    events = [
        {"timestamp": timestamp, "fundingRate": 0.0001}
        for timestamp in range(since + interval_ms, now_ms + 1, interval_ms)
    ]

    class GateArchive:
        def fetch_funding_rate_history(self, _symbol, **kwargs):
            upper = int((kwargs.get("params") or {}).get("until") or now_ms)
            eligible = [row for row in events if row["timestamp"] <= upper]
            return eligible[-vfh.HISTORY_PAGE_SIZE :]

    rows, pages = vfh._history_pages(
        GateArchive(), "Gate", "GUA/USDT:USDT", since=since, now_ms=now_ms, max_pages=10
    )
    details = vfh.realised_window_details(rows, now_ms=now_ms)

    assert pages == 2
    assert len(rows) == len(events)
    assert details["windows"] == pytest.approx({"1d": 0.06, "7d": 0.42, "30d": 1.8})
    assert all(item["complete"] for item in details["window_details"].values())


@pytest.mark.parametrize("symbol", ["SIREN/USDT:USDT", "龙虾/USDT:USDT"])
def test_aster_hourly_archive_pages_forward_to_the_latest_settlement(symbol: str) -> None:
    now_ms = 1_800_000_000_000
    interval_ms = 3_600_000
    since = now_ms - 31 * 86_400_000
    events = [
        {"timestamp": timestamp, "fundingRate": 0.00001}
        for timestamp in range(since + interval_ms, now_ms + 1, interval_ms)
    ]

    class AsterArchive:
        def fetch_funding_rate_history(self, _symbol, **kwargs):
            lower = int(kwargs["since"])
            return [row for row in events if row["timestamp"] >= lower][
                : vfh.HISTORY_PAGE_SIZE
            ]

    rows, pages = vfh._history_pages(
        AsterArchive(), "Aster", symbol, since=since, now_ms=now_ms, max_pages=10
    )
    details = vfh.realised_window_details(rows, now_ms=now_ms)

    assert pages == 8
    assert len(rows) == len(events)
    assert details["windows"] == pytest.approx({"1d": 0.024, "7d": 0.168, "30d": 0.72})
    assert all(item["complete"] for item in details["window_details"].values())


def test_incomplete_or_multi_page_history_keeps_a_deep_refresh_budget() -> None:
    assert vfh._history_page_budget(
        {"status": "ok", "history_pages": 1},
        {"1d": 1.0, "7d": None, "30d": None},
        priority=False,
    ) == vfh.PRIORITY_HISTORY_PAGES
    assert vfh._history_page_budget(
        {"status": "ok", "history_pages": 8},
        {"1d": 1.0, "7d": 7.0, "30d": 30.0},
        priority=False,
    ) == vfh.PRIORITY_HISTORY_PAGES
    assert vfh._history_page_budget(
        {"status": "ok", "history_pages": 1},
        {"1d": 1.0, "7d": 7.0, "30d": 30.0},
        priority=False,
    ) == 1


def test_priority_refresh_waits_until_the_next_settlement() -> None:
    status = {
        "status": "ok",
        "last_attempt_status": "ok",
        "deep_history_checked_at": "now",
        "window_details": {
            label: {
                "complete": True,
                "latest_event_at": 1_000_000,
                "inferred_interval_hours": 1.0,
            }
            for label in ("1d", "7d", "30d")
        },
    }
    values = {"1d": 1.0, "7d": 7.0, "30d": 30.0}

    assert not vfh._priority_refresh_due(status, values, now_ms=4_599_999)
    assert vfh._priority_refresh_due(status, values, now_ms=4_600_000)


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


def test_retryable_refresh_retains_v5_classification_and_cached_windows(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        f'{{"schema":"{vfh.SCHEMA}",'
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
    assert payload["catalog_classified_leg_count"] == 1
    assert payload["catalog_pending_leg_count"] == 0


def test_retryable_refresh_preserves_the_exact_expiry_evidence(tmp_path, monkeypatch) -> None:
    now_ms = 1_800_000_000_000
    details = vfh.realised_window_details(
        _settlements(90, 0.0001, now_ms=now_ms), now_ms=now_ms
    )
    path = tmp_path / "funding.json"
    path.write_text(
        __import__("json").dumps(
            {
                "schema": vfh.SCHEMA,
                "legs": {"A|ONE": details["windows"]},
                "leg_status": {
                    "A|ONE": {
                        "status": "ok",
                        "latest_event_at": details["latest_event_at"],
                        "window_details": details["window_details"],
                    }
                },
            }
        )
    )
    monkeypatch.setattr(
        vfh,
        "leg_history_outcome",
        lambda *_args, **_kwargs: {
            "status": "api_error",
            "entries": [],
            "error_type": "TimeoutError",
        },
    )

    vfh.build([("A", "ONE")], cache_path=path, budget_seconds=5)
    status = __import__("json").loads(path.read_text())["leg_status"]["A|ONE"]

    assert status["status"] == "ok_cached"
    assert status["window_details"] == details["window_details"]


def test_definitive_missing_market_clears_old_rolling_totals(tmp_path, monkeypatch) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        __import__("json").dumps(
            {
                "schema": vfh.SCHEMA,
                "legs": {"A|ONE": {"1d": 1.0, "7d": 2.0, "30d": 3.0}},
                "leg_status": {"A|ONE": {"status": "ok"}},
            }
        )
    )
    monkeypatch.setattr(
        vfh,
        "leg_history_outcome",
        lambda *_args, **_kwargs: {"status": "symbol_not_indexed", "entries": []},
    )

    result = vfh.build([("A", "ONE")], cache_path=path, budget_seconds=5)
    payload = __import__("json").loads(path.read_text())

    assert "A|ONE" not in result
    assert payload["leg_status"]["A|ONE"]["status"] == "symbol_not_indexed"


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

    assert "A|ONE" not in result
    assert payload["leg_status"]["A|ONE"]["status"] == "client_unavailable"
    assert payload["catalog_attempted_leg_count"] == 1
    assert payload["catalog_classified_leg_count"] == 0
    assert payload["catalog_pending_leg_count"] == 0


def test_retryable_and_unattempted_legs_run_before_healthy_maintenance(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "funding.json"
    path.write_text(
        f'{{"schema":"{vfh.SCHEMA}",'
        '"leg_status":{"A|HEALTHY":{"status":"ok"},'
        '"B|RETRY":{"status":"api_error","last_attempt_status":"api_error"}}}'
    )
    attempted: list[tuple[str, str]] = []

    def outcome(venue, symbol):
        attempted.append((venue, symbol))
        return {"status": "no_history_rows", "entries": []}

    monkeypatch.setattr(vfh, "leg_history_outcome", outcome)
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(vfh.time, "monotonic", lambda: next(ticks))

    vfh.build(
        [("A", "HEALTHY"), ("B", "RETRY"), ("C", "PENDING")],
        cache_path=path,
        budget_seconds=1,
    )

    assert attempted == [("B", "RETRY")]
