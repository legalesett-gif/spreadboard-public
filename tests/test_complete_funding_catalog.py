"""Funding pages rank the complete pair universe before token pagination."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from scripts import complete_funding_catalog_worker
from spreadboard import funding_catalog, funding_radar, server


def test_dedicated_worker_publishes_only_the_complete_catalogue(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "complete-funding.json"
    path.write_bytes(b"{}")
    monkeypatch.setattr(
        complete_funding_catalog_worker.funding_catalog,
        "refresh_cache",
        lambda: {"GUA": {"routes": []}},
    )
    monkeypatch.setattr(
        complete_funding_catalog_worker.funding_catalog,
        "status",
        lambda: {"ready": True, "path": str(path), "persist_error": None},
    )

    summary = complete_funding_catalog_worker.build()

    assert summary["status"] == "ok"
    assert summary["tokens"] == 1
    assert summary["bytes"] == 2


def test_catalog_rebuild_serves_last_complete_generation_to_concurrent_reader(
    monkeypatch, tmp_path,
) -> None:
    old = {"OLD": {"routes": [{"token": "OLD"}]}}
    new = {"NEW": {"routes": [{"token": "NEW"}]}}
    entered = threading.Event()
    release = threading.Event()
    build_options: dict = {}
    prior_payloads = funding_catalog._CACHE_PAYLOADS
    prior_at = funding_catalog._CACHE_AT
    prior_building = funding_catalog._CACHE_BUILDING
    monkeypatch.setattr(
        funding_catalog, "DEFAULT_CACHE_PATH", tmp_path / "complete-funding.json"
    )

    monkeypatch.setattr(
        funding_catalog.chart_catalog,
        "load",
        lambda: {"markets": [{"token": "NEW"}]},
    )

    def slow_build(*_args, **kwargs):
        build_options.update(kwargs)
        entered.set()
        assert release.wait(timeout=2)
        return new

    monkeypatch.setattr(funding_catalog.catalog_pairs, "for_tokens", slow_build)
    funding_catalog._CACHE_PAYLOADS = old
    # The explicit background refresh owns a new generation even while the
    # previous one is still inside its ordinary reader TTL.
    funding_catalog._CACHE_AT = time.monotonic()
    funding_catalog._CACHE_BUILDING = False
    funding_catalog._CACHE_BUILD_DONE.set()
    result: list[dict] = []
    worker = threading.Thread(
        target=lambda: result.append(funding_catalog.refresh_cache())
    )
    try:
        worker.start()
        assert entered.wait(timeout=1)
        assert funding_catalog._complete_payloads() is old
        release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert result == [new]
        assert funding_catalog._complete_payloads() is new
        assert build_options["include_history"] is False
    finally:
        release.set()
        worker.join(timeout=2)
        funding_catalog._CACHE_PAYLOADS = prior_payloads
        funding_catalog._CACHE_AT = prior_at
        funding_catalog._CACHE_BUILDING = prior_building
        funding_catalog._CACHE_BUILD_DONE.set()


def test_background_persisted_catalog_can_be_installed_after_initial_miss(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "complete-funding.json"
    old = {"OLD": {"routes": [{"token": "OLD"}]}}
    new = {"NEW": {"routes": [{"token": "NEW"}]}}
    prior = (
        funding_catalog._CACHE_PAYLOADS,
        funding_catalog._CACHE_AT,
        funding_catalog._CACHE_RESTORE_ATTEMPTED,
        funding_catalog._CACHE_SAVED_AT,
        funding_catalog._CACHE_PERSIST_ERROR,
    )
    monkeypatch.setattr(funding_catalog, "DEFAULT_CACHE_PATH", path)
    funding_catalog._CACHE_PAYLOADS = old
    funding_catalog._CACHE_AT = 0.0
    funding_catalog._CACHE_RESTORE_ATTEMPTED = True
    funding_catalog._CACHE_SAVED_AT = None
    funding_catalog._CACHE_PERSIST_ERROR = None
    funding_catalog._persist_cache(new)
    try:
        state = funding_catalog.reload_persisted_cache()

        assert funding_catalog._CACHE_PAYLOADS == new
        assert state["ready"] is True
        assert state["token_count"] == 1
        assert state["persist_error"] is None
    finally:
        (
            funding_catalog._CACHE_PAYLOADS,
            funding_catalog._CACHE_AT,
            funding_catalog._CACHE_RESTORE_ATTEMPTED,
            funding_catalog._CACHE_SAVED_AT,
            funding_catalog._CACHE_PERSIST_ERROR,
        ) = prior


def test_persisted_complete_catalog_restores_without_a_cold_build(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "complete-funding.json"
    payloads = {
        "GUA": {
            "routes": [
                {
                    "token": "GUA",
                    "route_key": "GUA|FUTURES|Long|Futures|Short|Futures",
                }
            ]
        }
    }
    prior = (
        funding_catalog._CACHE_PAYLOADS,
        funding_catalog._CACHE_AT,
        funding_catalog._CACHE_RESTORE_ATTEMPTED,
        funding_catalog._CACHE_SAVED_AT,
    )
    monkeypatch.setattr(funding_catalog, "DEFAULT_CACHE_PATH", path)
    funding_catalog._persist_cache(payloads)
    funding_catalog._CACHE_PAYLOADS = {}
    funding_catalog._CACHE_AT = 0.0
    funding_catalog._CACHE_RESTORE_ATTEMPTED = False
    funding_catalog._CACHE_SAVED_AT = None
    monkeypatch.setattr(
        funding_catalog.catalog_pairs,
        "for_tokens",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reader owned cold build")
        ),
    )
    try:
        restored = funding_catalog._complete_payloads()
        state = funding_catalog.status()
        assert restored == payloads
        assert state["ready"] is True
        assert state["token_count"] == 1
        assert state["age_seconds"] is not None
    finally:
        (
            funding_catalog._CACHE_PAYLOADS,
            funding_catalog._CACHE_AT,
            funding_catalog._CACHE_RESTORE_ATTEMPTED,
            funding_catalog._CACHE_SAVED_AT,
        ) = prior


def test_reader_never_owns_refresh_after_complete_generation_is_invalidated(
    monkeypatch,
) -> None:
    old = {"OLD": {"routes": [{"token": "OLD"}]}}
    prior_payloads = funding_catalog._CACHE_PAYLOADS
    prior_at = funding_catalog._CACHE_AT
    prior_building = funding_catalog._CACHE_BUILDING
    builds: list[bool] = []
    monkeypatch.setattr(
        funding_catalog.catalog_pairs,
        "for_tokens",
        lambda *_args, **_kwargs: builds.append(True) or {},
    )
    funding_catalog._CACHE_PAYLOADS = old
    funding_catalog._CACHE_AT = time.monotonic()
    funding_catalog._CACHE_BUILDING = False
    funding_catalog._CACHE_BUILD_DONE.set()
    try:
        funding_catalog.clear_cache()
        assert funding_catalog._complete_payloads() is old
        assert builds == []
    finally:
        funding_catalog._CACHE_PAYLOADS = prior_payloads
        funding_catalog._CACHE_AT = prior_at
        funding_catalog._CACHE_BUILDING = prior_building
        funding_catalog._CACHE_BUILD_DONE.set()


def test_fresh_process_reader_returns_warming_without_owning_catalog_build(
    monkeypatch,
) -> None:
    prior_payloads = funding_catalog._CACHE_PAYLOADS
    prior_at = funding_catalog._CACHE_AT
    prior_building = funding_catalog._CACHE_BUILDING
    builds: list[bool] = []
    monkeypatch.setattr(
        funding_catalog.catalog_pairs,
        "for_tokens",
        lambda *_args, **_kwargs: builds.append(True) or {},
    )
    funding_catalog._CACHE_PAYLOADS = {}
    funding_catalog._CACHE_AT = 0.0
    funding_catalog._CACHE_BUILDING = False
    funding_catalog._CACHE_BUILD_DONE.set()
    try:
        started = time.monotonic()
        page = funding_catalog.page(route_kind="FUTURES", window="7d")
        elapsed = time.monotonic() - started

        assert page["status"] == "warming"
        assert page["matching_token_count"] is None
        assert builds == []
        assert elapsed < 0.1
    finally:
        funding_catalog._CACHE_PAYLOADS = prior_payloads
        funding_catalog._CACHE_AT = prior_at
        funding_catalog._CACHE_BUILDING = prior_building
        funding_catalog._CACHE_BUILD_DONE.set()


def _route(
    token: str,
    key: str,
    *,
    current: float,
    one_day: float | None,
    seven_day: float | None = None,
    thirty_day: float | None = None,
    route_kind: str = "FUTURES",
) -> dict:
    return {
        "token": token,
        "route_key": key,
        "route_kind": route_kind,
        "long_venue": "Long",
        "long_market_type": "Futures",
        "long_market_symbol": f"{token}/USDT:USDT",
        "short_venue": "Short",
        "short_market_type": "Futures",
        "short_market_symbol": f"{token}/USDT:USDT",
        "funding_daily_pct": current,
        "funding_projected_24h_pct": current,
        "settled_funding_windows": {
            "1d": one_day,
            "7d": seven_day,
            "30d": thirty_day,
        },
        "catalog_history_loaded": True,
        "deliverable": True,
        "mirage_guarded": False,
    }


def test_complete_funding_reader_never_uses_a_persisted_rank_after_history_moves(
    monkeypatch,
) -> None:
    fresh = _route("FRESH", "fresh", current=1.0, one_day=2.5)

    class StaleNavigationStore:
        def payload_for(self, *_args, **_kwargs):
            raise AssertionError("historical Funding must not use a materialized ordering")

    monkeypatch.setattr(server, "_MATERIALIZED_VIEW_STORE", StaleNavigationStore())
    monkeypatch.setattr(server, "_MARKET_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_CACHE_INFLIGHT", {})
    monkeypatch.setattr(server, "_exact_catalog_market_projection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_funding_catalog_seed_payload",
        lambda *_args, **_kwargs: {
            "ok": True,
            "filters": {"funding_only": True, "sort": "funding", "direction": "desc"},
            "summary": {},
            "pagination": {},
            "source_health": {"canonical_api": {"status": "fresh"}},
            "top_edges": [],
            "top_funding": [],
            "groups": [],
            "rows": [],
        },
    )
    monkeypatch.setattr(
        funding_catalog,
        "page",
        lambda **_kwargs: {
            "ok": True,
            "mode": "complete_funding_catalogue_ranked_before_pagination",
            "window": "1d",
            "window_value_kind": "aggregate_exact_settlements",
            "window_duration_days": 1,
            "now_is_independent": True,
            "groups": [
                {
                    "token": "FRESH",
                    "routes": [fresh],
                    "best_funding_route": fresh,
                    "best_funding_window_pct": 2.5,
                    "route_count": 1,
                }
            ],
            "rows": [fresh],
            "matching_token_count": 1,
            "matching_route_count": 1,
            "returned_token_count": 1,
            "returned_route_count": 1,
            "offset": 0,
            "limit": 25,
            "largest_value": 2.5,
            "window_route_counts": {"1d": 1, "7d": 1, "30d": 1},
            "window_token_counts": {"1d": 1, "7d": 1, "30d": 1},
        },
    )
    monkeypatch.setattr(server, "_sync_telegram_client_universe", lambda value: value)

    payload = server.api_market_spreads(
        Path("board.json"),
        {
            "funding_only": ["1"],
            "kind": ["FUTURES"],
            "funding_window": ["1d"],
            "sort": ["funding"],
            "direction": ["desc"],
            "limit": ["25"],
        },
    )

    assert [group["token"] for group in payload["groups"]] == ["FRESH"]
    assert payload["funding_catalog"]["largest_value"] == 2.5


def test_current_ranking_happens_before_token_pagination(monkeypatch) -> None:
    """A strong route outside the bounded scanner must still lead Funding."""
    weak = _route("WEAK", "weak", current=0.2, one_day=0.1)
    strong = _route("STRONG", "strong", current=4.2, one_day=0.5)
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {
            "WEAK": {"routes": [weak]},
            "STRONG": {"routes": [strong]},
        },
    )

    page = funding_catalog.page(route_kind="FUTURES", window="now", limit=1)

    assert page["matching_token_count"] == 2
    assert page["matching_route_count"] == 2
    assert [group["token"] for group in page["groups"]] == ["STRONG"]
    assert page["groups"][0]["best_funding_route"]["route_key"] == "strong"


def test_restored_catalog_now_uses_current_exact_leg_carry(monkeypatch) -> None:
    from spreadboard import warm_query_projection

    stale = _route("GUA", "gua", current=0.1, one_day=0.2)
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"GUA": {"routes": [stale]}},
    )
    monkeypatch.setattr(
        warm_query_projection.LIVE_UNIVERSE,
        "update_snapshot",
        lambda: ({}, {"ready": True}),
    )
    monkeypatch.setattr(
        funding_catalog.bulk_quotes,
        "load_funding",
        lambda: {
            "Long|GUA/USDT:USDT": {
                "rate_pct": 0.0,
                "interval_hours": 8.0,
                "age_seconds": 12.0,
            },
            "Short|GUA/USDT:USDT": {
                "rate_pct": 0.5,
                "interval_hours": 8.0,
                "age_seconds": 42.0,
            },
        },
    )
    monkeypatch.setattr(
        funding_catalog.funding_radar,
        "window_value",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Now must not read historical windows")
        ),
    )

    page = funding_catalog.page(route_kind="FUTURES", window="now")

    assert page["largest_value"] == 1.5
    assert page["groups"][0]["best_funding_route"]["funding_daily_pct"] == 1.5
    assert page["groups"][0]["best_funding_route"]["funding_age_min"] == 0.7
    assert page["window_route_counts"] == {}
    assert page["window_token_counts"] == {}


def test_populated_live_cache_never_backfills_a_missing_leg_from_stale_catalog(
    monkeypatch,
) -> None:
    from spreadboard import warm_query_projection

    stale = _route("GUA", "gua", current=9.9, one_day=0.2)
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"GUA": {"routes": [stale]}},
    )
    monkeypatch.setattr(
        warm_query_projection.LIVE_UNIVERSE,
        "update_snapshot",
        lambda: ({}, {"ready": True}),
    )
    monkeypatch.setattr(
        funding_catalog.bulk_quotes,
        "load_funding",
        lambda: {
            "Short|GUA/USDT:USDT": {
                "rate_pct": 0.5,
                "interval_hours": 8.0,
            }
        },
    )

    page = funding_catalog.page(route_kind="FUTURES", window="now")

    assert page["groups"] == []
    assert page["matching_route_count"] == 0


def test_production_historical_window_reads_current_exact_archive(monkeypatch) -> None:
    stale = _route("GUA", "gua", current=0.1, one_day=9.9)
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"GUA": {"routes": [stale]}},
    )
    monkeypatch.setattr(funding_catalog, "_resident_live_overlay", lambda rows: rows)
    archive = {
        "Long|GUA/USDT:USDT": {"1d": 0.0, "7d": 0.0, "30d": 0.0},
        "Short|GUA/USDT:USDT": {"1d": 1.25, "7d": 2.5, "30d": 5.0},
    }
    loads = []
    monkeypatch.setattr(
        funding_catalog.venue_funding_history,
        "load",
        lambda: loads.append(True) or archive,
    )
    monkeypatch.setattr(
        funding_catalog.funding_radar,
        "window_value",
        lambda _route, label, **kwargs: (
            kwargs["exact_legs"]["Short|GUA/USDT:USDT"][label]
            - kwargs["exact_legs"]["Long|GUA/USDT:USDT"][label]
        ),
    )

    page = funding_catalog.page(route_kind="FUTURES", window="1d")

    assert page["largest_value"] == 1.25
    assert page["groups"][0]["best_funding_window_pct"] == 1.25
    assert loads == [True]


def test_every_eligible_route_for_a_returned_token_is_preserved(monkeypatch) -> None:
    routes = [
        {
            **_route("FULL", f"full-{index}", current=2.0 - index / 100, one_day=1.0),
            "short_venue": f"Short {index}",
        }
        for index in range(75)
    ]
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"FULL": {"routes": routes}},
    )

    page = funding_catalog.page(route_kind="FUTURES", window="now", limit=25)

    assert page["matching_route_count"] == 75
    assert page["groups"][0]["route_count"] == 75
    assert len(page["groups"][0]["routes"]) == 75


def test_live_and_retained_legacy_keys_dedupe_by_exact_economic_legs(monkeypatch) -> None:
    live = _route("SAME", "legacy-key", current=1.0, one_day=2.0)
    retained = {
        **live,
        "route_key": "CUSTOM|new-key-format",
        "radar_historical": True,
    }
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"SAME": {"routes": [live]}},
    )
    monkeypatch.setattr(funding_radar, "routes_for", lambda *args, **kwargs: [retained])

    page = funding_catalog.page(route_kind="FUTURES", window="1d")

    assert page["matching_token_count"] == 1
    assert page["matching_route_count"] == 1
    assert page["groups"][0]["route_count"] == 1
    assert page["groups"][0]["best_funding_route"]["route_key"] == "legacy-key"


def test_historical_ranking_uses_all_exact_routes_not_only_live_leaders(
    monkeypatch,
) -> None:
    scanner_leader = _route("SCANNER", "scanner", current=3.0, one_day=0.4)
    historical_leader = _route("HISTORY", "history", current=-0.1, one_day=5.0)
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {
            "SCANNER": {"routes": [scanner_leader]},
            "HISTORY": {"routes": [historical_leader]},
        },
    )
    monkeypatch.setattr(funding_radar, "routes_for", lambda *args, **kwargs: [])

    page = funding_catalog.page(route_kind="FUTURES", window="1d", limit=1)

    assert [group["token"] for group in page["groups"]] == ["HISTORY"]
    assert page["groups"][0]["best_funding_window_pct"] == 5.0


def test_historical_windows_are_aggregate_totals_not_daily_averages(monkeypatch) -> None:
    aggregate = _route(
        "TOTAL",
        "total",
        current=99.0,
        one_day=1.0,
        seven_day=7.0,
        thirty_day=30.0,
    )
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"TOTAL": {"routes": [aggregate]}},
    )
    monkeypatch.setattr(funding_radar, "routes_for", lambda *args, **kwargs: [])

    seven = funding_catalog.page(route_kind="FUTURES", window="7d")
    thirty = funding_catalog.page(route_kind="FUTURES", window="30d")

    assert seven["groups"][0]["best_funding_window_aggregate_pct"] == 7.0
    assert seven["largest_value"] == 7.0
    assert seven["window_value_kind"] == "aggregate_exact_settlements"
    assert seven["window_duration_days"] == 7
    assert seven["now_is_independent"] is True
    assert thirty["largest_value"] == 30.0


def test_now_changes_cannot_rewrite_a_historical_total(monkeypatch) -> None:
    route = _route("ISOLATED", "isolated", current=1.0, one_day=0.5, seven_day=3.5)
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"ISOLATED": {"routes": [route]}},
    )
    monkeypatch.setattr(funding_radar, "routes_for", lambda *args, **kwargs: [])

    before = funding_catalog.page(route_kind="FUTURES", window="7d")["largest_value"]
    route["funding_daily_pct"] = 500.0
    route["funding_projected_24h_pct"] = 500.0
    after = funding_catalog.page(route_kind="FUTURES", window="7d")["largest_value"]

    assert before == after == 3.5


def test_a_retained_route_can_fill_a_cooled_market_gap(monkeypatch) -> None:
    live = _route("LIVE", "live", current=0.2, one_day=0.3)
    retained = {
        **_route("COOLED", "cooled", current=0.0, one_day=4.0),
        "radar_historical": True,
        "radar_windows": {"1d": 4.0, "7d": None, "30d": None},
    }
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"LIVE": {"routes": [live]}},
    )
    monkeypatch.setattr(funding_radar, "routes_for", lambda *args, **kwargs: [retained])

    page = funding_catalog.page(route_kind="FUTURES", window="1d", limit=25)

    assert [group["token"] for group in page["groups"]] == ["COOLED", "LIVE"]
    assert page["groups"][0]["best_funding_route"]["radar_historical"] is True


def test_missing_exact_thirty_day_history_stays_blank(monkeypatch) -> None:
    incomplete = _route("NEW", "new", current=1.0, one_day=0.5, thirty_day=None)
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"NEW": {"routes": [incomplete]}},
    )
    monkeypatch.setattr(funding_radar, "routes_for", lambda *args, **kwargs: [])

    page = funding_catalog.page(route_kind="FUTURES", window="30d", limit=25)

    assert page["groups"] == []
    assert page["matching_route_count"] == 0


def test_exact_token_detail_keeps_the_same_route_universe_in_every_window(
    monkeypatch,
) -> None:
    routes = [
        {
            **_route(
                "GUA",
                f"gua-{index}",
                current=current,
                one_day=one_day,
                seven_day=seven_day,
                thirty_day=thirty_day,
            ),
            "short_venue": f"Short {index}",
        }
        for index, (current, one_day, seven_day, thirty_day) in enumerate(
            (
                (0.5, 0.4, 2.0, 6.0),
                (-0.2, 0.1, 1.0, None),
                (0.0, None, None, None),
            )
        )
    ]
    monkeypatch.setattr(
        funding_catalog,
        "_complete_payloads",
        lambda: {"GUA": {"routes": routes}, "GUARD": {"routes": []}},
    )
    monkeypatch.setattr(funding_radar, "routes_for", lambda *args, **kwargs: [])

    pages = {
        window: funding_catalog.page(
            route_kind="FUTURES", window=window, symbol="GUA"
        )
        for window in ("now", "1d", "7d", "30d")
    }

    for page in pages.values():
        assert page["exact_symbol_detail"] is True
        assert page["matching_route_count"] == 3
        assert {route["route_key"] for route in page["groups"][0]["routes"]} == {
            "gua-0",
            "gua-1",
            "gua-2",
        }
    assert pages["30d"]["groups"][0]["best_funding_window_pct"] == 6.0
    assert pages["30d"]["groups"][0]["routes"][-1][
        "settled_funding_windows"
    ]["30d"] is None


def test_negative_futures_funding_can_form_an_inventory_backed_reverse_pair() -> None:
    from spreadboard.catalog_pairs import Leg, _directions

    spot = object.__new__(Leg)
    object.__setattr__(spot, "market_type", "Spot")
    future = object.__new__(Leg)
    object.__setattr__(future, "market_type", "Futures")

    assert _directions(spot, future) == [(spot, future)]
    assert _directions(spot, future, include_short_spot=True) == [
        (spot, future),
        (future, spot),
    ]


def test_server_replaces_scanner_groups_with_complete_funding_page(monkeypatch) -> None:
    complete_group = {
        "token": "COMPLETE",
        "route_count": 75,
        "routes": [_route("COMPLETE", "complete", current=5.0, one_day=2.0)],
        "best_funding_route": _route("COMPLETE", "complete", current=5.0, one_day=2.0),
        "best_funding_24h_pct": 5.0,
    }
    monkeypatch.setattr(
        funding_catalog,
        "page",
        lambda **_kwargs: {
            "mode": "complete_funding_catalogue_ranked_before_pagination",
            "window": "now",
            "window_value_kind": "current_rate_projected_24h",
            "window_duration_days": None,
            "now_is_independent": True,
            "groups": [complete_group],
            "rows": complete_group["routes"],
            "matching_token_count": 814,
            "matching_route_count": 9103,
            "returned_token_count": 1,
            "returned_route_count": 1,
            "offset": 0,
            "limit": 25,
            "largest_value": 5.0,
            "window_route_counts": {"1d": 4000, "7d": 3900, "30d": 1200},
            "window_token_counts": {"1d": 600, "7d": 590, "30d": 300},
        },
    )
    bounded = {
        "ok": True,
        "groups": [{"token": "SCANNER", "routes": []}],
        "rows": [],
        "summary": {"matching_tokens": 25, "matching_rows": 100},
        "pagination": {},
    }

    result = server._expand_complete_funding_groups(
        bounded,
        {"funding_only": ["1"], "kind": ["FUTURES"]},
        offset=0,
        limit=25,
    )

    assert [group["token"] for group in result["groups"]] == ["COMPLETE"]
    assert result["summary"]["matching_tokens"] == 814
    assert result["summary"]["matching_rows"] == 9103
    assert result["coverage_mode"] == "complete_funding_catalogue_ranked_before_pagination"
    assert result["funding_catalog"]["window_value_kind"] == "current_rate_projected_24h"
    assert result["funding_catalog"]["now_is_independent"] is True


def test_history_health_cannot_be_false_green_when_windows_are_partial(monkeypatch) -> None:
    monkeypatch.setattr(
        server.chart_catalog,
        "load",
        lambda: {
            "markets": [
                {
                    "venue": "Gate",
                    "symbol": "GUA/USDT:USDT",
                    "market_type": "Futures",
                }
            ]
        },
    )
    monkeypatch.setattr(
        server.venue_funding_history,
        "coverage_summary",
        lambda _legs: {
            "catalog_leg_count": 1,
            "pending_leg_count": 0,
            "retryable_error_leg_count": 0,
            "deep_history_pending_leg_count": 0,
            "fully_complete_leg_count": 0,
            "window_leg_counts": {"1d": 1, "7d": 0, "30d": 0},
        },
    )

    health = server.funding_history_health()

    assert health["status"] == "operational_partial_history"
    assert health["window_value_kind"] == "aggregate_exact_settlements"
    assert health["now_is_independent"] is True


def test_historical_page_requests_a_distinct_complete_window(monkeypatch) -> None:
    captured = {}

    def fake_api(_path, query):
        captured.update(query)
        return {
            "ok": True,
            "groups": [],
            "summary": {"matching_tokens": 0, "matching_rows": 0},
            "source_health": {"canonical_api": {"status": "fresh"}},
            "funding_catalog": {
                "matching_token_count": 0,
                "matching_route_count": 0,
                "largest_value": None,
                "window_token_counts": {"1d": 0, "7d": 0, "30d": 0},
            },
        }

    monkeypatch.setattr(server, "api_market_spreads", fake_api)
    monkeypatch.setattr(
        server,
        "funding_history_health",
        lambda: (_ for _ in ()).throw(AssertionError("warning-only health lookup")),
    )
    monkeypatch.setattr(
        server.bulk_quotes,
        "funding_health",
        lambda: {"status": "fresh", "p95_age_seconds": 30.0},
    )

    html = server.render_funding_page(
        Path("board.json"),
        {},
        {"rank": ["7d"], "farm": ["futures-futures"]},
    )

    assert captured["funding_window"] == ["7d"]
    assert "Ranking uses the complete pair catalogue before this token page is sliced" in html
    assert "7d total" in html
    assert "funding-radar-note" not in html
    assert "Blank history is explicit" not in html
    assert "Complete trailing windows:" not in html
    assert "Settlement archive coverage:" not in html
    assert "/api/stream/board" not in html


def test_historical_dex_page_and_export_request_the_same_selected_window(monkeypatch) -> None:
    captured = {}

    def fake_api(_path, query):
        captured.update(query)
        return {
            "ok": True,
            "groups": [],
            "summary": {"matching_tokens": 41, "matching_rows": 63},
            "source_health": {"canonical_api": {"status": "fresh"}},
            "funding_catalog": {
                "window": "7d",
                "matching_token_count": 41,
                "matching_route_count": 63,
                "largest_value": 7.5,
                "window_token_counts": {},
            },
        }

    monkeypatch.setattr(server, "api_market_spreads", fake_api)
    monkeypatch.setattr(
        server,
        "funding_history_health",
        lambda: {
            "attempted_leg_count": 1,
            "catalog_leg_count": 1,
            "classified_leg_count": 1,
            "pending_leg_count": 0,
            "retryable_error_leg_count": 0,
        },
    )
    monkeypatch.setattr(
        server.bulk_quotes,
        "funding_health",
        lambda: {"status": "fresh", "p95_age_seconds": 30.0},
    )

    html = server.render_funding_page(
        Path("board.json"),
        {},
        {
            "rank": ["7d"],
            "farm": ["futures-dex"],
            "offset": ["20"],
            "limit": ["20"],
        },
    )

    assert captured["funding_window"] == ["7d"]
    assert captured["offset"] == ["20"]
    assert captured["limit"] == ["20"]
    assert "funding_window=7d" in html
    assert "/api/stream/board" not in html
    assert (
        'data-export-url="/api/spreads?offset=20&amp;limit=20&amp;funding_only=1'
        in html
    )


def test_live_overlay_does_not_reselect_or_remove_historical_leaders(monkeypatch) -> None:
    leader = _route("HISTORY", "history", current=-0.2, one_day=5.0)
    group = {
        "token": "HISTORY",
        "routes": [leader],
        "best_funding_route": leader,
        "best_funding_window_pct": 5.0,
        "route_count": 1,
    }
    payload = {
        "groups": [group],
        "rows": [leader],
        "top_edges": [],
        "top_funding": [],
        "filters": {"funding_only": True, "sort": "funding", "direction": "desc"},
        "funding_catalog": {"window": "1d"},
        "summary": {"max_funding_24h_pct": 5.0},
    }
    monkeypatch.setattr(
        server.api_spreads,
        "live_route_updates_for",
        lambda *_args, **_kwargs: {},
    )

    server._apply_spread_freshness(payload)

    assert payload["groups"] == [group]
    assert group["best_funding_route"]["route_key"] == "history"
    assert group["best_funding_window_pct"] == 5.0


def test_complete_funding_request_skips_the_bounded_scanner(monkeypatch) -> None:
    route = _route("FAST", "fast", current=1.0, one_day=0.5)
    group = {
        "token": "FAST",
        "routes": [route],
        "best_funding_route": route,
        "best_funding_24h_pct": 1.0,
        "route_count": 1,
    }
    monkeypatch.setattr(
        server,
        "_funding_catalog_seed_payload",
        lambda *_args, **_kwargs: {
            "ok": True,
            "filters": {"funding_only": True, "sort": "funding", "direction": "desc"},
            "summary": {},
            "pagination": {},
            "source_health": {"canonical_api": {"status": "fresh"}},
            "top_edges": [],
            "top_funding": [],
            "groups": [],
            "rows": [],
        },
    )
    monkeypatch.setattr(
        funding_catalog,
        "page",
        lambda **_kwargs: {
            "mode": "complete_funding_catalogue_ranked_before_pagination",
            "window": "now",
            "groups": [group],
            "rows": [route],
            "matching_token_count": 1,
            "matching_route_count": 1,
            "returned_token_count": 1,
            "returned_route_count": 1,
            "offset": 0,
            "limit": 25,
            "largest_value": 1.0,
            "window_route_counts": {"1d": 1, "7d": 0, "30d": 0},
            "window_token_counts": {"1d": 1, "7d": 0, "30d": 0},
        },
    )
    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("scanner should not run")),
    )
    monkeypatch.setattr(
        server.api_spreads,
        "live_route_updates_for",
        lambda *_args, **_kwargs: {},
    )

    payload = server.api_market_spreads(
        Path("board.json"),
        {
            "funding_only": ["1"],
            "kind": ["FUTURES"],
            "sort": ["funding"],
            "direction": ["desc"],
            "limit": ["25"],
            "no_cache": ["1"],
        },
    )

    assert [item["token"] for item in payload["groups"]] == ["FAST"]
    assert payload["coverage_mode"] == "complete_funding_catalogue_ranked_before_pagination"
