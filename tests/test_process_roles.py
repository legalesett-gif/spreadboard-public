from __future__ import annotations

import inspect
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import collector_healthcheck
from scripts import run_spreadboard_service as service


def test_service_role_defaults_to_combined_and_rejects_typos(monkeypatch) -> None:
    monkeypatch.delenv("SPREADBOARD_SERVICE_ROLE", raising=False)
    assert service._service_role() == "combined"
    for role in ("web", "collector", "combined"):
        monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", role.upper())
        assert service._service_role() == role
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "collecter")
    with pytest.raises(ValueError):
        service._service_role()


def test_shared_generation_is_atomic_and_contains_no_market_data(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "market_generation.json"
    monkeypatch.setattr(service, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(service, "MARKET_GENERATION_PATH", target)

    service._publish_shared_market_generation("bulk_quotes")

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema"] == "spreadboard.market_generation.v1"
    assert payload["kind"] == "bulk_quotes"
    assert set(payload) == {"schema", "kind", "updated_at_unix", "generation_ns"}
    assert list(tmp_path.glob(".market_generation.json.*")) == []


def test_cleanup_removes_only_abandoned_discovery_temps(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(service, "RUNTIME_DIR", tmp_path)
    old = tmp_path / ".api_discovery_refresh.json.old.tmp"
    fresh = tmp_path / ".api_discovery_refresh.json.fresh.tmp"
    unrelated = tmp_path / ".accounts.sqlite3.old.tmp"
    for path in (old, fresh, unrelated):
        path.write_bytes(b"123")
    import os

    os.utime(old, (1_000.0, 1_000.0))
    os.utime(fresh, (10_000.0, 10_000.0))

    cleanup = service._cleanup_abandoned_discovery_temps(
        max_age_seconds=3_600, now=10_000.0
    )

    assert cleanup == {"removed": 1, "bytes": 3}
    assert not old.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_complete_materialized_generation_is_the_restart_warm_state(
    tmp_path, monkeypatch
) -> None:
    board_path = tmp_path / "board.jsonl"
    discovery = tmp_path / "api_discovery_latest.json"
    chart_catalog = tmp_path / "chart_market_catalog.json"
    metadata = tmp_path / "token_metadata.json"
    rails = tmp_path / "public_transfer_rails.json"
    for path in (board_path, discovery, chart_catalog, metadata, rails):
        path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("SPREADBOARD_BOARD_PATH", str(board_path))
    monkeypatch.setattr(service, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(service, "SNAPSHOT_PATH", discovery)
    monkeypatch.setattr(
        service.api_spreads.token_metadata, "DEFAULT_CACHE_PATH", metadata
    )
    monkeypatch.setattr(service.api_spreads.public_rails, "DEFAULT_CACHE_PATH", rails)

    def signature(path: Path) -> list[int]:
        stat = path.stat()
        return [stat.st_mtime_ns, stat.st_size]

    class Store:
        pointer_path = tmp_path / "current.json"

        @staticmethod
        def status() -> dict[str, object]:
            return {
                "ready": True,
                "source_signature": {
                    "board_path": str(board_path.resolve()),
                    "board": signature(board_path),
                    "discovery": signature(discovery),
                    "chart_catalog": signature(chart_catalog),
                    "metadata": signature(metadata),
                    "rails": signature(rails),
                },
            }

    monkeypatch.setattr(service.materialized_views, "default_store", Store)

    assert service._materialized_sources_current() is True
    watcher = service.SharedArtifactWatcher(
        threading.Event(), initial_warm_delay_seconds=0
    )
    assert watcher.initial_warm_requested is True


def test_duplicate_structural_event_does_not_rebuild_a_generation_just_published(
    monkeypatch,
) -> None:
    builds: list[bool] = []
    monkeypatch.setattr(service, "_materialized_generation_ready", lambda: True)
    monkeypatch.setattr(service, "_refresh_live_route_index", lambda: True)
    monkeypatch.setattr(
        service, "_refresh_materialized_views", lambda *, force: builds.append(force)
    )
    catalogues: list[bool] = []
    monkeypatch.setattr(
        service,
        "_refresh_complete_funding_catalog",
        lambda *, force: catalogues.append(force),
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(), initial_warm_delay_seconds=3600
    )
    watcher.warm_pending = True

    watcher._drain_warms()

    assert builds == []
    assert catalogues == [False]


def test_missing_materialized_fallback_is_built_after_fast_route_index(
    monkeypatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        service, "_refresh_live_route_index", lambda: calls.append("live") or True
    )
    monkeypatch.setattr(service, "_materialized_generation_ready", lambda: False)
    monkeypatch.setattr(
        service,
        "_refresh_materialized_views",
        lambda *, force: calls.append(("materialized", force)),
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(), initial_warm_delay_seconds=3600
    )
    watcher.warm_pending = True

    watcher._drain_warms()

    assert calls == ["live", ("materialized", True)]


def test_coalesced_structural_and_funding_handoff_preserves_funding_refresh(
    monkeypatch,
) -> None:
    builds: list[bool] = []
    catalogues: list[bool] = []
    monkeypatch.setattr(service, "_refresh_live_route_index", lambda: True)
    monkeypatch.setattr(service, "_materialized_generation_ready", lambda: True)
    monkeypatch.setattr(
        service, "_refresh_materialized_views", lambda *, force: builds.append(force)
    )
    monkeypatch.setattr(
        service,
        "_refresh_complete_funding_catalog",
        lambda *, force: catalogues.append(force),
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(), initial_warm_delay_seconds=3600
    )
    watcher.warm_pending = True
    watcher.funding_warm_pending = True

    watcher._drain_warms()

    assert builds == []
    assert catalogues == [False]


def test_web_watcher_invalidates_prices_and_warms_structural_changes(
    tmp_path, monkeypatch
) -> None:
    generation = tmp_path / "market_generation.json"
    snapshot = tmp_path / "api_discovery_latest.json"
    monkeypatch.setattr(service, "MARKET_GENERATION_PATH", generation)
    monkeypatch.setattr(service, "SNAPSHOT_PATH", snapshot)
    invalidations: list[bool] = []
    live_refreshes: list[bool] = []
    full_builds: list[bool] = []
    catalogues: list[bool] = []
    monkeypatch.setattr(
        service, "_invalidate_market_price_caches", lambda: invalidations.append(True)
    )
    monkeypatch.setattr(
        service, "_refresh_materialized_views", lambda *, force: full_builds.append(force)
    )
    monkeypatch.setattr(
        service,
        "_refresh_live_route_index",
        lambda: live_refreshes.append(True) or True,
    )
    monkeypatch.setattr(service, "_materialized_generation_ready", lambda: True)
    monkeypatch.setattr(
        service,
        "_refresh_complete_funding_catalog",
        lambda *, force: catalogues.append(force),
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(),
        initial_warm_delay_seconds=3600,
        invalidation_interval_seconds=120,
    )

    generation.write_text("{}", encoding="utf-8")
    watcher.check_once()
    assert invalidations == [True]

    snapshot.write_text("{}", encoding="utf-8")
    watcher.check_once()
    assert watcher.warm_thread is not None
    watcher.warm_thread.join(timeout=2)
    assert live_refreshes == [True]
    assert full_builds == []
    assert catalogues == [False]


def test_web_watcher_refreshes_only_funding_catalog_after_funding_generation(
    tmp_path, monkeypatch
) -> None:
    generation = tmp_path / "market_generation.json"
    snapshot = tmp_path / "api_discovery_latest.json"
    monkeypatch.setattr(service, "MARKET_GENERATION_PATH", generation)
    monkeypatch.setattr(service, "SNAPSHOT_PATH", snapshot)
    invalidations: list[bool] = []
    funding_warms: list[bool] = []
    monkeypatch.setattr(
        service, "_invalidate_market_price_caches", lambda: invalidations.append(True)
    )
    monkeypatch.setattr(
        service,
        "_refresh_complete_funding_catalog",
        lambda *, force: funding_warms.append(force),
    )
    monkeypatch.setattr(
        service,
        "_refresh_materialized_views",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("bulk funding must not rebuild the whole site")
        ),
    )
    monkeypatch.setattr(
        service, "_restore_telegram_from_materialized_generation", lambda: False
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(),
        initial_warm_delay_seconds=3600,
        invalidation_interval_seconds=120,
    )

    generation.write_text(
        json.dumps({"kind": "bulk_funding"}), encoding="utf-8"
    )
    watcher.check_once()
    assert watcher.warm_thread is not None
    watcher.warm_thread.join(timeout=2)

    assert invalidations == [True]
    assert funding_warms == [False]


def test_web_watcher_recovers_a_missing_funding_snapshot_when_spreads_are_live(
    monkeypatch,
) -> None:
    from spreadboard import telegram_queries

    monkeypatch.setattr(
        telegram_queries,
        "payload_status",
        lambda: {"ready": True, "funding_ready": False},
    )
    monkeypatch.setattr(
        service, "_restore_telegram_from_materialized_generation", lambda: False
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(),
        initial_warm_delay_seconds=3600,
        telegram_recovery_interval_seconds=30,
    )
    watcher.next_telegram_recovery_at = 0.0
    spread_warms: list[bool] = []
    funding_warms: list[bool] = []
    monkeypatch.setattr(watcher, "request_warm", lambda: spread_warms.append(True))
    monkeypatch.setattr(
        watcher, "request_funding_warm", lambda: funding_warms.append(True)
    )

    watcher._recover_telegram_snapshot_if_due()

    assert spread_warms == []
    assert funding_warms == [True]


def test_market_evidence_catch_up_is_start_to_start(monkeypatch) -> None:
    class StopAfterSweep:
        def __init__(self) -> None:
            self.stopped = False
            self.waits: list[float] = []

        def wait(self, seconds: float) -> bool:
            self.waits.append(seconds)
            if len(self.waits) > 1:
                self.stopped = True
            return self.stopped

        def is_set(self) -> bool:
            return self.stopped

    stop = StopAfterSweep()
    loop = service.MarketEvidenceLoop(stop)  # type: ignore[arg-type]
    loop.INITIAL_DELAY_SECONDS = 0.0
    loop.INTERVAL_SECONDS = 600.0
    monkeypatch.setattr(loop, "_sweep_once", lambda: None)
    ticks = iter((100.0, 160.0))
    monkeypatch.setattr(service.time, "monotonic", lambda: next(ticks))

    loop.run()

    assert stop.waits == [0.0, 540.0]


def test_token_ranking_defers_while_market_evidence_owns_memory_slot(
    monkeypatch,
) -> None:
    workers: list[bool] = []
    monkeypatch.setattr(service, "_LAST_TOKEN_RANKING_AT", 0.0)
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda *_args, **_kwargs: workers.append(True),
    )
    assert service._BACKGROUND_ANALYTICS_LOCK.acquire(blocking=False)
    try:
        service._refresh_token_rankings(force=True)
    finally:
        service._BACKGROUND_ANALYTICS_LOCK.release()

    assert workers == []


def test_web_watcher_coalesces_continuous_collector_generations(
    tmp_path, monkeypatch
) -> None:
    generation = tmp_path / "market_generation.json"
    snapshot = tmp_path / "api_discovery_latest.json"
    monkeypatch.setattr(service, "MARKET_GENERATION_PATH", generation)
    monkeypatch.setattr(service, "SNAPSHOT_PATH", snapshot)
    invalidations = []
    monkeypatch.setattr(
        service, "_invalidate_market_price_caches", lambda: invalidations.append(True)
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(),
        initial_warm_delay_seconds=3600,
        invalidation_interval_seconds=120,
    )

    generation.write_text("one", encoding="utf-8")
    watcher.check_once()
    generation.write_text("two-longer", encoding="utf-8")
    watcher.check_once()
    assert invalidations == [True]
    assert watcher.invalidation_pending is True

    watcher.last_invalidation_at -= 121
    watcher.check_once()
    assert invalidations == [True, True]
    assert watcher.invalidation_pending is False


def test_collector_republishes_complete_live_pair_index_at_bounded_cadence(
    tmp_path, monkeypatch
) -> None:
    generation = tmp_path / "market_generation.json"
    snapshot = tmp_path / "api_discovery_latest.json"
    monkeypatch.setattr(service, "MARKET_GENERATION_PATH", generation)
    monkeypatch.setattr(service, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "collector")
    monkeypatch.setattr(service, "_invalidate_market_price_caches", lambda: None)
    watcher = service.SharedArtifactWatcher(
        threading.Event(),
        initial_warm_delay_seconds=3600,
        invalidation_interval_seconds=1,
        live_route_refresh_interval_seconds=120,
    )
    watcher.next_live_route_refresh_at = 0.0
    route_warms: list[bool] = []
    monkeypatch.setattr(watcher, "request_warm", lambda: route_warms.append(True))

    generation.write_text(json.dumps({"kind": "bulk_quotes"}), encoding="utf-8")
    watcher.check_once()
    generation.write_text(
        json.dumps({"kind": "bulk_quotes", "generation": 2}), encoding="utf-8"
    )
    watcher.check_once()

    assert route_warms == [True]
    assert watcher.next_live_route_refresh_at > 0.0


def test_network_discovery_does_not_monopolize_heavy_publication_slot(
    tmp_path, monkeypatch
) -> None:
    snapshot = tmp_path / "api_discovery_latest.json"
    refresh = tmp_path / "api_discovery_refresh.json"
    snapshot.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(service, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(service, "REFRESH_SNAPSHOT_PATH", refresh)
    loop = service.RefreshLoop(300)
    monkeypatch.setattr(
        loop, "_refresh_verified_identity_registry", lambda **_kwargs: None
    )
    publication_slot_available: list[bool] = []

    def discovery(*_args, **_kwargs):
        acquired = service._COLLECTOR_HEAVY_LOCK.acquire(blocking=False)
        publication_slot_available.append(acquired)
        if acquired:
            service._COLLECTOR_HEAVY_LOCK.release()
        return service.WorkerResult(1, "", "bounded test stop", False)

    monkeypatch.setattr(service, "_run_worker", discovery)

    loop.refresh_once()

    assert publication_slot_available == [True]


def test_completed_bulk_books_request_current_pair_publication(monkeypatch) -> None:
    requests: list[bool] = []

    class Publisher:
        @staticmethod
        def request() -> int:
            requests.append(True)
            return len(requests)

    loop = service.BulkQuoteLoop(
        threading.Event(),
        route_index_publisher=Publisher(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda *_args, **_kwargs: service.WorkerResult(
            0,
            json.dumps(
                {
                    "quotes": {
                        "quotes": 23_000,
                        "venues": 18,
                        "seconds": 80.0,
                    }
                }
            ),
            "",
            False,
        ),
    )
    monkeypatch.setattr(service, "_publish_shared_market_generation", lambda _kind: None)
    monkeypatch.setattr(service, "_invalidate_market_price_caches", lambda: None)
    monkeypatch.setattr(service, "_schedule_token_rankings", lambda: None)

    loop._sweep_once()

    assert requests == [True]


def test_live_pair_publisher_coalesces_and_never_installs_in_collector(
    monkeypatch,
) -> None:
    lifecycle: list[str] = []

    class Refresh:
        @staticmethod
        def pause_websocket_worker() -> None:
            lifecycle.append("pause")

        @staticmethod
        def resume_websocket_worker() -> None:
            lifecycle.append("resume")

    monkeypatch.setattr(
        service,
        "_refresh_live_route_index",
        lambda *, install: lifecycle.append(f"publish:{install}") or True,
    )
    publisher = service.LiveRouteIndexPublisher(
        threading.Event(),
        refresh_loop=Refresh(),  # type: ignore[arg-type]
        min_interval_seconds=30,
    )
    publisher.request()
    publisher.request()

    result = publisher.check_once()

    assert result["status"] == "published"
    assert result["requested_generation"] == 2
    assert result["published_generation"] == 2
    assert lifecycle == ["pause", "publish:False", "resume"]


def test_live_pair_publisher_backs_off_after_a_failed_child(monkeypatch) -> None:
    attempts: list[bool] = []
    clock = [100.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        service,
        "_refresh_live_route_index",
        lambda *, install: attempts.append(install) or False,
    )
    publisher = service.LiveRouteIndexPublisher(
        threading.Event(),
        min_interval_seconds=120,
    )
    publisher.request()

    failed = publisher.check_once()
    waiting = publisher.check_once()

    assert failed["status"] == "publish_failed"
    assert failed["retry_in_seconds"] == 30.0
    assert waiting["status"] == "cadence_wait"
    assert attempts == [False]


def test_web_watcher_self_heals_a_missing_telegram_snapshot(monkeypatch) -> None:
    """A lost resident snapshot must not wait for the next broad discovery."""
    from spreadboard import telegram_queries

    restores: list[bool] = []
    monkeypatch.setattr(
        telegram_queries,
        "payload_status",
        lambda: {"ready": False},
    )
    monkeypatch.setattr(
        service,
        "_restore_telegram_from_materialized_generation",
        lambda: restores.append(True) or True,
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(),
        initial_warm_delay_seconds=3600,
        telegram_recovery_interval_seconds=60,
    )
    watcher.next_telegram_recovery_at = 0.0

    watcher.check_once()
    assert watcher.warm_thread is None
    assert restores == [True]


@pytest.mark.parametrize(
    ("role", "expected_catalogue_reloads"),
    (("combined", 1), ("collector", 0)),
)
def test_materialized_builder_is_an_isolated_low_priority_worker(
    monkeypatch, role: str, expected_catalogue_reloads: int
) -> None:
    seen = []
    catalogue_reloads = []
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", role)
    monkeypatch.setattr(service, "_LAST_MATERIALIZED_VIEW_AT", 0.0)
    monkeypatch.setattr(service, "_MATERIALIZED_VIEW_RETRY_AFTER", 0.0)
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda command, **kwargs: seen.append((command, kwargs))
        or service.WorkerResult(
            0,
            '{"status":"ok","generation":"g1","views":19,"routes":100}\n',
            "",
            False,
        ),
    )
    from spreadboard import server

    monkeypatch.setattr(server._MATERIALIZED_VIEW_STORE, "invalidate", lambda: None)
    monkeypatch.setattr(server, "restore_materialized_route_index", lambda _path: 100)
    monkeypatch.setattr(server, "restore_materialized_intel", lambda _path: True)
    monkeypatch.setattr(server, "mark_historical_dex_archive_ready", lambda: None)
    monkeypatch.setattr(
        service.funding_catalog,
        "reload_persisted_cache",
        lambda: catalogue_reloads.append(True)
        or {"ready": True, "token_count": 100},
    )

    assert service._refresh_materialized_views(force=True) is True

    command, options = seen[0]
    assert command[:3] == service._low_priority_prefix()
    assert any(str(item).endswith("materialized_view_worker.py") for item in command)
    assert options["timeout"] == 1800.0
    assert len(catalogue_reloads) == expected_catalogue_reloads


def test_web_role_never_owns_materialized_navigation_build(monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda *_args, **_kwargs: pytest.fail(
            "web must only install collector-published navigation"
        ),
    )

    assert service._refresh_materialized_views(force=True) is False


def test_market_evidence_cycle_does_not_wait_for_navigation_materialization(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda *_args, **_kwargs: service.WorkerResult(
            0, '{"status":"ok","artifact":"market_evidence"}\n', "", False
        ),
    )
    monkeypatch.setattr(
        service,
        "_refresh_materialized_views",
        lambda **_kwargs: pytest.fail(
            "exact-history cadence must not block on the multi-view builder"
        ),
    )

    service.MarketEvidenceLoop(threading.Event())._run_isolated_sweep()


def test_structural_discovery_waits_for_initial_exact_evidence(
    tmp_path, monkeypatch
) -> None:
    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(service, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setenv("SPREADBOARD_STARTUP_EVIDENCE_WAIT_SECONDS", "2")
    loop = service.RefreshLoop(30)
    ready = threading.Event()
    loop.startup_evidence_ready = ready
    finished = threading.Event()

    waiter = threading.Thread(
        target=lambda: (loop._wait_for_startup_evidence(), finished.set()),
        daemon=True,
    )
    waiter.start()

    assert not finished.wait(0.05)
    ready.set()
    assert finished.wait(1.0)
    waiter.join(timeout=1.0)


def test_market_evidence_releases_startup_gate_after_first_sweep(
    monkeypatch,
) -> None:
    stop = threading.Event()
    loop = service.MarketEvidenceLoop(stop)
    monkeypatch.setattr(loop, "INITIAL_DELAY_SECONDS", 0.0)

    def complete_once() -> None:
        stop.set()

    monkeypatch.setattr(loop, "_sweep_once", complete_once)

    loop.run()

    assert loop.first_sweep_done.is_set()


def test_overdue_market_evidence_cycle_yields_to_structural_work(
    monkeypatch,
) -> None:
    stop = threading.Event()
    loop = service.MarketEvidenceLoop(stop)
    waits: list[float] = []
    monkeypatch.setattr(loop, "INITIAL_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(loop, "INTERVAL_SECONDS", 300.0)
    monotonic_values = iter((100.0, 405.0))
    monkeypatch.setattr(service.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(loop, "_sweep_once", lambda: None)

    def record_wait(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) > 1:
            stop.set()
        return len(waits) > 1

    monkeypatch.setattr(stop, "wait", record_wait)

    loop.run()

    assert waits == [0.0, 5.0]


def test_complete_funding_catalog_is_an_isolated_bounded_worker(
    tmp_path, monkeypatch
) -> None:
    seen = []
    monkeypatch.setattr(service, "_FUNDING_CATALOG_RETRY_AFTER", 0.0)
    monkeypatch.setattr(
        service.funding_catalog,
        "status",
        lambda **_kwargs: {
            "ready": False,
            "age_seconds": None,
            "path": str(tmp_path / "missing.json"),
        },
    )
    monkeypatch.setattr(
        service.funding_catalog,
        "reload_persisted_cache",
        lambda: {"ready": True, "token_count": 900},
    )
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda command, **kwargs: seen.append((command, kwargs))
        or service.WorkerResult(
            0,
            '{"status":"ok","tokens":900,"bytes":1000,"seconds":12.5,"max_rss_mb":500}\n',
            "",
            False,
        ),
    )

    assert service._refresh_complete_funding_catalog(force=False) is True

    command, options = seen[0]
    assert command[:3] == service._low_priority_prefix()
    assert any(
        str(item).endswith("complete_funding_catalog_worker.py")
        for item in command
    )
    assert options["timeout"] == 1200.0


def test_collector_publishes_funding_catalog_without_retaining_giant_decode(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "collector")
    monkeypatch.setattr(service, "_FUNDING_CATALOG_RETRY_AFTER", 0.0)
    monkeypatch.setattr(
        service.funding_catalog,
        "persisted_status",
        lambda: {
            "ready": False,
            "age_seconds": None,
            "path": str(tmp_path / "complete.json"),
        },
    )
    monkeypatch.setattr(
        service.funding_catalog,
        "reload_persisted_cache",
        lambda: (_ for _ in ()).throw(
            AssertionError("collector must not retain the 1.5 GB decoded catalogue")
        ),
    )
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda *_args, **_kwargs: service.WorkerResult(
            0,
            '{"status":"ok","tokens":5335,"bytes":211930101,"seconds":26.7,"max_rss_mb":1559.3}\n',
            "",
            False,
        ),
    )

    assert service._refresh_complete_funding_catalog(force=True) is True


def test_collector_artifact_watcher_never_decodes_complete_catalogue(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "collector")
    path = tmp_path / "complete.json"
    monkeypatch.setattr(service.funding_catalog, "DEFAULT_CACHE_PATH", path)
    watcher = service.SharedArtifactWatcher(threading.Event())
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        service.funding_catalog,
        "reload_persisted_cache",
        lambda: (_ for _ in ()).throw(
            AssertionError("collector watcher must not decode the catalogue")
        ),
    )

    watcher.check_once()


def test_failed_materialized_build_has_a_bounded_retry_cooldown(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(service, "_LAST_MATERIALIZED_VIEW_AT", 0.0)
    monkeypatch.setattr(service, "_MATERIALIZED_VIEW_RETRY_AFTER", 0.0)
    monkeypatch.setattr(service.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda command, **kwargs: calls.append((command, kwargs))
        or service.WorkerResult(1, '{"status":"failed"}\n', "", False),
    )

    assert service._refresh_materialized_views(force=True) is False
    assert service._refresh_materialized_views(force=True) is False

    assert len(calls) == 1
    assert service._MATERIALIZED_VIEW_RETRY_AFTER == (
        1_000.0 + service.MATERIALIZED_VIEW_FAILURE_RETRY_SECONDS
    )


def _write_live_books(path: Path, *, quote_ts_us: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE live_books (
                cache_key TEXT PRIMARY KEY,
                quote_ts_us INTEGER NOT NULL,
                source TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO live_books (cache_key, quote_ts_us, source) VALUES (?, ?, ?)",
            ("Gate|Futures|BTC/USDT:USDT", quote_ts_us, "bulk_ticker"),
        )


def _healthy_collector_artifacts(path: Path, *, now: float) -> None:
    (path / "api_discovery_latest.json").write_text("{}", encoding="utf-8")
    (path / "market_generation.json").write_text(
        json.dumps(
            {
                "schema": "spreadboard.market_generation.v1",
                "kind": "bulk_quotes",
                "updated_at_unix": now,
                "generation_ns": int(now * 1_000_000_000),
            }
        ),
        encoding="utf-8",
    )
    completed = datetime.fromtimestamp(now, tz=UTC).isoformat()
    (path / "api_discovery_fast_quotes.json").write_text(
        json.dumps(
            {
                "fast_quote_refresh": {
                    "last_completed_cycle": {"updated_at": completed}
                }
            }
        ),
        encoding="utf-8",
    )
    _write_live_books(
        path / "spreadboard_live_books.sqlite3",
        quote_ts_us=int(now * 1_000_000),
    )


def test_collector_health_requires_all_independent_data_paths(tmp_path) -> None:
    now = time.time()
    _healthy_collector_artifacts(tmp_path, now=now)

    result = collector_healthcheck.collector_health(
        tmp_path, now=now, min_current_bulk_books=1
    )

    assert result["status"] == "ok"
    assert all(result["checks"].values())
    assert result["live_book_count"] == 1


def test_collector_health_fails_when_generation_stops_even_if_books_exist(
    tmp_path,
) -> None:
    now = time.time()
    _healthy_collector_artifacts(tmp_path, now=now)
    generation = json.loads(
        (tmp_path / "market_generation.json").read_text(encoding="utf-8")
    )
    generation["updated_at_unix"] = now - 181
    (tmp_path / "market_generation.json").write_text(
        json.dumps(generation), encoding="utf-8"
    )

    result = collector_healthcheck.collector_health(
        tmp_path, now=now, min_current_bulk_books=1
    )

    assert result["status"] == "stale"
    assert result["checks"]["generation_current"] is False
    assert result["checks"]["live_books_current"] is True


def test_websocket_leader_cannot_hide_a_dead_bulk_catalogue(tmp_path) -> None:
    now = time.time()
    _healthy_collector_artifacts(tmp_path, now=now)
    with sqlite3.connect(tmp_path / "spreadboard_live_books.sqlite3") as connection:
        connection.execute(
            "UPDATE live_books SET source = 'public_websocket'"
        )

    result = collector_healthcheck.collector_health(
        tmp_path, now=now, min_current_bulk_books=1
    )

    assert result["checks"]["live_books_current"] is True
    assert result["checks"]["bulk_books_current"] is False
    assert result["status"] == "stale"


def test_collector_role_contains_no_subscriber_or_payment_workers() -> None:
    source = inspect.getsource(service._run_collector_service)
    assert "RefreshLoop" in source
    assert "BulkQuoteLoop" in source
    assert "BulkFundingLoop" in source
    assert "MarketEvidenceLoop" in source
    for forbidden in (
        "SpreadBoardServer",
        "crypto_watcher",
        "MembershipWorker",
        "subscription_lifecycle",
        "PublicFeedWorker",
    ):
        assert forbidden not in source


def test_web_warm_never_runs_slow_market_evidence_in_the_http_process() -> None:
    source = inspect.getsource(service._warm_board_cache)
    guard = source.split('if _service_role() != "web":', 1)
    assert len(guard) == 2
    before_guard, after_guard = guard
    assert "_refresh_funding_windows()" not in before_guard
    assert "_refresh_funding_windows()" in after_guard


def test_market_evidence_is_an_isolated_low_priority_worker(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda command, **kwargs: seen.append((command, kwargs))
        or service.WorkerResult(0, '{"status":"ok"}\n', "", False),
    )
    loop = service.MarketEvidenceLoop(threading.Event())

    loop._sweep_once()

    command, options = seen[0]
    assert any(str(item).endswith("market_evidence_worker.py") for item in command)
    assert command[:3] == service._low_priority_prefix()
    assert options["timeout"] == loop.TIMEOUT_SECONDS
    assert Path("scripts/market_evidence_worker.py").exists()


def test_market_evidence_pauses_optional_websocket_fast_lane(monkeypatch) -> None:
    events: list[str] = []

    class Refresh:
        def pause_websocket_worker(self) -> None:
            events.append("pause")

        def resume_websocket_worker(self) -> None:
            events.append("resume")

    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda *_args, **_kwargs: service.WorkerResult(-1, "", "timeout", True),
    )
    loop = service.MarketEvidenceLoop(
        threading.Event(),
        refresh_loop=Refresh(),  # type: ignore[arg-type]
    )

    loop._sweep_once()

    assert events == ["pause", "resume"]


def test_market_evidence_yields_to_pending_complete_route_index(monkeypatch) -> None:
    workers: list[bool] = []

    class PendingPublisher:
        def has_pending(self) -> bool:
            return True

    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda *_args, **_kwargs: workers.append(True),
    )
    loop = service.MarketEvidenceLoop(
        threading.Event(),
        route_index_publisher=PendingPublisher(),  # type: ignore[arg-type]
    )

    loop._sweep_once()

    assert workers == []


def test_refresh_loop_pause_releases_websocket_process() -> None:
    class Process:
        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            assert timeout == 10
            return 0

    loop = service.RefreshLoop(300)
    process = Process()
    loop.websocket_process = process  # type: ignore[assignment]

    loop.pause_websocket_worker()

    assert process.terminated is True
    assert loop.websocket_process is None
    assert loop.websocket_paused.is_set() is True


def test_production_compose_assigns_separate_roles_and_secret_sets() -> None:
    source = Path("compose.production.yml").read_text(encoding="utf-8")
    assert 'SPREADBOARD_SERVICE_ROLE: "web"' in source
    assert 'SPREADBOARD_SERVICE_ROLE: "collector"' in source
    assert "/opt/spreadboard/secrets/collector.env" in source
    collector = source.split("\n  collector:\n", 1)[1].split(
        "\n  accounting-worker:\n", 1
    )[0]
    assert "/opt/spreadboard/runtime:/app/runtime" in collector
    assert "accounting-public.pem" not in collector
    assert "collector_healthcheck.py" in collector
    assert "ports:" not in collector


def test_slow_discovery_publishes_completed_source_checkpoints() -> None:
    source = inspect.getsource(service.RefreshLoop.refresh_once)

    assert "staging_seed_signature" in source
    assert "publishing completed-source partial" in source
    timeout_guard = source.split("if result.timed_out:", 1)[1].split(
        "if result.returncode != 0:", 1
    )[0]
    assert "_artifact_signature(REFRESH_SNAPSHOT_PATH)" in timeout_guard
    assert "refresh timeout before any source completed" in timeout_guard


def test_production_discovery_allows_full_broad_scan_window() -> None:
    source = Path("compose.production.yml").read_text(encoding="utf-8")

    assert 'SPREADBOARD_REFRESH_TIMEOUT_SECONDS: "3600"' in source


def test_web_role_flushes_analytics_outside_request_threads() -> None:
    source = inspect.getsource(service.main)
    assert "accounts.PageViewWorker" in source
    assert "page_view_worker.start()" in source
    assert "page_view_worker.stop()" in source
