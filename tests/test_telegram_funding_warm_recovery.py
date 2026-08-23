from __future__ import annotations

from pathlib import Path

from scripts import run_spreadboard_service as service
from spreadboard import server


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
