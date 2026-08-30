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
    monkeypatch: pytest.MonkeyPatch,
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

    captured: dict[str, Any] = {}

    def _history(*, priority_routes=None, extra_priority_legs=None):
        captured["extra"] = list(extra_priority_legs or [])
        raise RuntimeError("stop after the call under test")

    monkeypatch.setattr(service, "_refresh_venue_funding_history", _history)

    # The function under test guards broadly, so the sentinel may be swallowed;
    # the assertions below are what actually decide the result.
    service._refresh_funding_windows()

    assert ("Gate", "T/USDT:USDT") in captured.get("extra", []), (
        "a futures leg shown on the spread board must enter the fast lane"
    )
    assert ("Bybit", "T/USDT:USDT") in captured.get("extra", [])


def test_a_spot_leg_is_not_queued_for_funding_history() -> None:
    """Spot pays no funding; queueing it would spend the budget on nothing."""

    import inspect

    source = inspect.getsource(service._refresh_funding_windows)
    assert '!= "Futures"' in source or '== "Futures"' in source, (
        "the board-leg collection must filter to futures legs"
    )
