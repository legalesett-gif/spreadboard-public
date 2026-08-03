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
    # 0.01% every 8h for 30 days. The window edge is inclusive, so a day holds
    # the three settlements inside it plus the one exactly on the boundary.
    entries = _settlements(90, 0.0001, now_ms=now)

    windows = vfh.realised_windows(entries, now_ms=now)

    assert abs(windows["1d"] - 0.04) < 0.005, windows
    assert abs(windows["7d"] - 0.22) < 0.02, windows
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
