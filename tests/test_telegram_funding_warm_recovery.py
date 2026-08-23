from __future__ import annotations

import threading
from pathlib import Path

from scripts import run_spreadboard_service as service
from spreadboard import server, telegram_queries


def test_telegram_funding_warm_retries_only_contended_lanes(monkeypatch) -> None:
    attempts: dict[str, int] = {}

    def fake_market_spreads(_board_path, query):
        kind = str((query.get("kind") or ["all"])[0])
        attempts[kind] = attempts.get(kind, 0) + 1
        if kind == "FUTURES" and attempts[kind] == 1:
            return {"status": "warming", "groups": []}
        return {"groups": [{"token": kind, "routes": []}]}

    monkeypatch.setattr(server, "api_market_spreads", fake_market_spreads)
    monkeypatch.setattr(service, "_yield_to_requests", lambda: None)

    payloads = service._complete_telegram_funding_payloads(
        Path("board.json"), attempts=2, retry_seconds=0
    )

    assert len(payloads) == 4
    assert attempts["FUTURES"] == 2
    assert attempts["FUTURES-SPOT-PAIR"] == 1
    assert attempts["DEX-FUTURES"] == 1
    assert attempts["all"] == 1


def test_telegram_funding_warm_never_installs_a_partial_generation(
    monkeypatch,
) -> None:
    def fake_market_spreads(_board_path, query):
        kind = str((query.get("kind") or ["all"])[0])
        if kind == "DEX-FUTURES":
            return {"status": "warming", "groups": []}
        return {"groups": [{"token": kind, "routes": []}]}

    monkeypatch.setattr(server, "api_market_spreads", fake_market_spreads)
    monkeypatch.setattr(service, "_yield_to_requests", lambda: None)

    assert service._complete_telegram_funding_payloads(
        Path("board.json"), attempts=2, retry_seconds=0
    ) == []


def test_web_watcher_restores_persisted_snapshots_before_rebuilding(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        telegram_queries,
        "payload_status",
        lambda: {"ready": True, "funding_ready": True},
    )
    monkeypatch.setattr(
        telegram_queries,
        "restore_persisted_payloads",
        lambda: {"spread": True, "funding": True},
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
    assert funding_warms == []
