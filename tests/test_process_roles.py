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


def test_web_watcher_invalidates_prices_and_warms_structural_changes(
    tmp_path, monkeypatch
) -> None:
    generation = tmp_path / "market_generation.json"
    snapshot = tmp_path / "api_discovery_latest.json"
    monkeypatch.setattr(service, "MARKET_GENERATION_PATH", generation)
    monkeypatch.setattr(service, "SNAPSHOT_PATH", snapshot)
    invalidations: list[bool] = []
    warms: list[bool] = []
    monkeypatch.setattr(
        service, "_invalidate_market_price_caches", lambda: invalidations.append(True)
    )
    monkeypatch.setattr(
        service, "_warm_board_cache", lambda *, force=False: warms.append(force)
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
    assert warms == [True]


def test_web_watcher_rebuilds_only_funding_views_after_funding_generation(
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
        service, "_warm_funding_cache", lambda: funding_warms.append(True)
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
