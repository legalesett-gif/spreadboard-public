"""The legs on screen must be the legs whose funding history gets refreshed.

A 1d/7d/30d window fails closed the moment it crosses its next settlement, so
a leg must be refreshed every 4-8 hours to stay displayable. The refresh sweep
walks ~9,400 catalogue legs by staleness, while the spread board displays a few
hundred -- and only the FUNDING lane's routes were fed into the priority lane.

Measured on production 2026-08-30: 1,687 legs held a live window, but of the 84
distinct futures legs actually on the board only 5 (6%) did. A route needs BOTH
legs, so 6% x 6% left the columns empty on every row while the sweep refreshed
legs nobody was looking at.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts import run_spreadboard_service as service
from spreadboard import server as spreadboard_server


def test_visible_board_legs_are_passed_to_the_priority_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    board_row = {
        "route_key": "T|Gate|Futures|Bybit|Futures",
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "long_market_symbol": "T/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "T/USDT:USDT",
    }

    def _spreads(_path: Any, query: dict[str, list[str]]) -> dict[str, Any]:
        if query.get("funding_only"):
            return {"groups": [{"routes": [dict(board_row)]}]}
        return {"rows": [dict(board_row)]}

    monkeypatch.setattr(spreadboard_server, "api_market_spreads", _spreads)
    monkeypatch.setattr(service, "WARM_QUERIES", [{"funding_only": True}, {"kind": "FUTURES"}])
    monkeypatch.setattr(service, "FUNDING_ARCHIVE_QUERIES", [])
    monkeypatch.setattr(service, "_refresh_complete_funding_catalog", lambda **_k: None)

    demand = service.funding_history_demand
    monkeypatch.setattr(demand, "DEFAULT_PATH", tmp_path / "demand.json")

    def stop_after_enqueue(*args):
        raise RuntimeError("stop after the call under test")

    monkeypatch.setattr(service.market_history, "write_funding_windows", stop_after_enqueue)
    service._refresh_funding_windows()
    # The separate worker reads this exact durable queue, so a process restart
    # between selection and collection cannot lose the displayed priorities.
    assert set(demand.legs()) == {("Gate", "T/USDT:USDT"), ("Bybit", "T/USDT:USDT")}


def test_a_spot_leg_is_not_queued_for_funding_history() -> None:
    """Spot pays no funding; queueing it would spend the budget on nothing."""

    import inspect

    source = inspect.getsource(service._refresh_funding_windows)
    assert '!= "Futures"' in source or '== "Futures"' in source, (
        "the board-leg collection must filter to futures legs"
    )
