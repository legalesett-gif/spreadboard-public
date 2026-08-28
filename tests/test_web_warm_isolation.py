from __future__ import annotations

import threading

from scripts import run_spreadboard_service as service


def test_web_role_never_runs_heavy_route_or_funding_builders(monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("web must not launch a market builder")
        ),
    )

    assert service._refresh_live_route_index() is False
    assert service._refresh_complete_funding_catalog(force=True) is False


def test_restored_telegram_snapshots_skip_large_view_decoding(monkeypatch) -> None:
    from spreadboard import server, telegram_queries

    monkeypatch.setattr(
        telegram_queries,
        "restore_persisted_payloads",
        lambda: {"spread": True, "funding": True},
    )
    monkeypatch.setattr(
        server._MATERIALIZED_VIEW_STORE,
        "payload_for",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("restored snapshots must not decode large views")
        ),
    )

    service._warm_telegram_payload_at_startup(service._board_path())


def test_collector_can_publish_route_index_without_loading_it(monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "collector")
    monkeypatch.setattr(
        service,
        "_run_worker",
        lambda *_args, **_kwargs: service.WorkerResult(
            0,
            '{"status":"ok","routes":123,"seconds":1.0}\n',
            "",
            False,
        ),
    )
    from spreadboard import server

    monkeypatch.setattr(
        server,
        "restore_materialized_route_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("collector publishes but does not retain the index")
        ),
    )

    assert service._refresh_live_route_index(install=False) is True


def test_web_watcher_installs_collector_funding_catalogue(tmp_path, monkeypatch) -> None:
    path = tmp_path / "complete-funding.json"
    monkeypatch.setattr(service.funding_catalog, "DEFAULT_CACHE_PATH", path)
    installed: list[bool] = []
    monkeypatch.setattr(
        service.funding_catalog,
        "reload_persisted_cache",
        lambda: installed.append(True) or {"token_count": 42},
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(),
        initial_warm_delay_seconds=3600,
        telegram_recovery_interval_seconds=3600,
    )

    path.write_text("{}", encoding="utf-8")
    watcher.check_once()

    assert installed == [True]


def test_web_watcher_installs_live_index_without_blocking_on_full_reprice(
    tmp_path, monkeypatch
) -> None:
    from spreadboard import server, warm_query_projection

    pointer = tmp_path / "live-route-index-current.json"
    monkeypatch.setattr(service, "_live_route_pointer_path", lambda: pointer)
    installed: list[bool] = []
    monkeypatch.setattr(
        server,
        "restore_materialized_route_index",
        lambda _path: installed.append(True) or 123,
    )
    monkeypatch.setattr(
        warm_query_projection.LIVE_UNIVERSE,
        "refresh",
        lambda: (_ for _ in ()).throw(
            AssertionError("the artifact watcher must not synchronously reprice")
        ),
    )
    watcher = service.SharedArtifactWatcher(
        threading.Event(),
        initial_warm_delay_seconds=3600,
        telegram_recovery_interval_seconds=3600,
    )

    pointer.write_text("{}", encoding="utf-8")
    watcher.check_once()

    assert installed == [True]
