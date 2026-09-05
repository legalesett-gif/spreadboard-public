from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from scripts import run_spreadboard_service as service


def test_recent_discovery_snapshot_delays_restart_work(tmp_path: Path) -> None:
    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text("{}")
    os.utime(snapshot, (1_000.0, 1_000.0))

    assert service._remaining_discovery_delay_seconds(
        snapshot,
        interval_seconds=3_600.0,
        now=1_600.0,
    ) == 3_000.0


def test_missing_or_expired_discovery_snapshot_refreshes_immediately(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert service._remaining_discovery_delay_seconds(
        missing,
        interval_seconds=3_600.0,
        now=5_000.0,
    ) == 0.0

    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text("{}")
    os.utime(snapshot, (1_000.0, 1_000.0))
    assert service._remaining_discovery_delay_seconds(
        snapshot,
        interval_seconds=3_600.0,
        now=5_000.0,
    ) == 0.0


def test_production_deep_discovery_has_idle_capacity_between_full_windows() -> None:
    source = Path("compose.production.yml").read_text(encoding="utf-8")

    assert 'SPREADBOARD_REFRESH_SECONDS: "7200"' in source
    assert 'SPREADBOARD_REFRESH_TIMEOUT_SECONDS: "3600"' in source


def test_stale_member_views_are_compacted_before_deep_discovery() -> None:
    source = inspect.getsource(service.RefreshLoop.run)

    materialize = source.index("_refresh_materialized_views(force=True)")
    delay = source.index("_remaining_discovery_delay_seconds")
    discovery = source.index("self.refresh_once()")
    assert materialize < delay < discovery
    assert "self.pause_websocket_worker()" in source
    assert "self.resume_websocket_worker()" in source


def test_recent_chart_catalog_does_not_race_startup_materialization() -> None:
    source = inspect.getsource(service.RefreshLoop.run_chart_catalog)

    delay = source.index("_remaining_discovery_delay_seconds")
    build = source.index("_artifact_worker")
    assert delay < build
    assert 'RUNTIME_DIR / "chart_market_catalog.json"' in source


@pytest.mark.parametrize("configuration", ["default", "production"])
def test_two_hour_old_market_catalog_refreshes_without_waiting_four_more_hours(
    monkeypatch, tmp_path, configuration,
) -> None:
    # Aster STONKS was live while a four-hour-old catalogue
    # still omitted them. Exercise the actual refresh loop and its first wait.
    snapshot = tmp_path / "chart_market_catalog.json"
    snapshot.write_text("{}")
    os.utime(snapshot, (1000, 1000))
    monkeypatch.setattr(service, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(service.time, "time", lambda: 8200)
    monkeypatch.delenv("SPREADBOARD_CHART_CATALOG_SECONDS", raising=False)
    if configuration == "production":
        import re
        from pathlib import Path
        compose = (Path(__file__).resolve().parents[1] / "compose.production.yml").read_text()
        value = re.search(r'SPREADBOARD_CHART_CATALOG_SECONDS: "(\d+)"', compose).group(1)
        monkeypatch.setenv("SPREADBOARD_CHART_CATALOG_SECONDS", value)
    calls = []
    monkeypatch.setattr(service, "_artifact_worker", lambda *args: calls.append(args))

    class StopAfterWait:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            self.stopped = True
            calls.append(seconds)
            return True

    loop = service.RefreshLoop(7200)
    loop.stop_event = StopAfterWait()
    loop.run_chart_catalog()
    assert calls == [("chart-catalog", "--workers", "4"), 3600.0]
