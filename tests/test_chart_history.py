"""A chart that opens on a route has to draw a line.

The default window is 1H. The candle backfill refused any window shorter than
four hours, and the exact-book recorder rotates across 200k routes at roughly
one sample per route per 45 minutes -- so the 1H chart held a single point and
lightweight-charts drew nothing. Every member opening their first chart saw an
empty panel reading "1 observations, 0% window coverage".
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from spreadboard import historical_spreads, server


CEX_ROUTE = {
    "route_key": "VANRY|Gate|Spot|Binance|Spot",
    "token": "VANRY",
    "long_venue": "Gate",
    "long_market_type": "Spot",
    "short_venue": "Binance",
    "short_market_type": "Spot",
}

DEX_ROUTE = {
    "route_key": "VANRY|Gate|Spot|OKX DEX 1|Spot",
    "token": "VANRY",
    "long_venue": "Gate",
    "long_market_type": "Spot",
    "short_venue": "OKX DEX 1",
    "short_market_type": "Spot",
}


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(historical_spreads, "CACHE_DIR", tmp_path / "history_cache")
    historical_spreads._WARMING.clear()


def _candles(count: int, *, start_ms: int, step_ms: int, price: float) -> list[list[float]]:
    return [
        [start_ms + index * step_ms, price, price, price, price + index * 0.0001, 1000.0]
        for index in range(count)
    ]


def test_a_one_hour_window_is_backfilled(monkeypatch: pytest.MonkeyPatch) -> None:
    """This is the default window and the one that was always empty."""
    now_ms = int(time.time() * 1000)
    start = now_ms - 3600 * 1000

    def fake_leg(row: dict[str, Any], side: str, timeframe: str, since_ms: int) -> list[list[float]]:
        assert timeframe == "1m", "a one hour window needs minute candles"
        return _candles(60, start_ms=start, step_ms=60_000, price=1.0 if side == "long" else 0.5)

    monkeypatch.setattr(historical_spreads, "_fetch_leg", fake_leg)
    result = historical_spreads.load_or_fetch(CEX_ROUTE, hours=1.0, max_points=1200)

    assert result["status"] == "ok"
    assert len(result["rows"]) >= 50, "a one hour chart must have enough points to draw a line"


@pytest.mark.parametrize("hours", [1 / 60, 5 / 60, 0.5, 1.0, 4.0, 12.0, 24.0, 72.0, 168.0])
def test_no_window_is_refused_outright(hours: float, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every window the UI offers must at least attempt a backfill."""
    monkeypatch.setattr(
        historical_spreads,
        "_fetch_leg",
        lambda row, side, timeframe, since_ms: _candles(
            30, start_ms=int(time.time() * 1000) - 600_000, step_ms=20_000, price=2.0
        ),
    )
    result = historical_spreads.load_or_fetch(CEX_ROUTE, hours=hours, max_points=600)

    assert result["status"] != "not_applicable", f"{hours}h window was refused"


def test_the_timeframe_matches_the_window() -> None:
    """A 15m candle cannot fill a one hour window, and a 1m candle cannot span a week."""
    assert historical_spreads.timeframe_for(1 / 60) == "1m"
    assert historical_spreads.timeframe_for(1.0) == "1m"
    assert historical_spreads.timeframe_for(24.0) == "1m"
    assert historical_spreads.timeframe_for(72.0) == "5m"
    assert historical_spreads.timeframe_for(168.0) == "15m"


def test_a_dex_leg_is_still_refused() -> None:
    """OKX DEX publishes no candles; pretending otherwise would invent prices."""
    assert historical_spreads.load_or_fetch(DEX_ROUTE, hours=1.0)["status"] == "not_applicable"


def test_a_cold_chart_does_not_block_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two legs of candles take seconds; the page must not wait for them."""
    started = []

    def slow_leg(row: dict[str, Any], side: str, timeframe: str, since_ms: int) -> list[list[float]]:
        started.append(side)
        time.sleep(0.3)
        return _candles(20, start_ms=int(time.time() * 1000) - 600_000, step_ms=30_000, price=3.0)

    monkeypatch.setattr(historical_spreads, "_fetch_leg", slow_leg)

    began = time.monotonic()
    result = historical_spreads.load_or_fetch(CEX_ROUTE, hours=1.0, blocking=False)
    elapsed = time.monotonic() - began

    assert result["status"] == "warming"
    assert elapsed < 0.2, f"the non-blocking path waited {elapsed:.2f}s"

    # ...and the work really does happen, so a later poll finds it.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        follow_up = historical_spreads.load_or_fetch(CEX_ROUTE, hours=1.0, blocking=False)
        if follow_up.get("status") == "ok":
            assert follow_up["rows"], "the warmed cache landed with no rows"
            return
        time.sleep(0.1)
    pytest.fail("the background backfill never landed")


def test_one_fetch_serves_concurrent_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ten members opening the same chart must not start ten candle fetches."""
    calls: list[str] = []

    def counted_leg(row: dict[str, Any], side: str, timeframe: str, since_ms: int) -> list[list[float]]:
        calls.append(side)
        time.sleep(0.4)
        return _candles(10, start_ms=int(time.time() * 1000) - 600_000, step_ms=60_000, price=1.5)

    monkeypatch.setattr(historical_spreads, "_fetch_leg", counted_leg)
    for _ in range(10):
        historical_spreads.load_or_fetch(CEX_ROUTE, hours=1.0, blocking=False)

    time.sleep(1.2)
    assert len(calls) == 2, f"expected one fetch (two legs), got {len(calls)} leg fetches"


def test_the_chart_reports_that_history_is_still_loading() -> None:
    """Without this flag the client polls once and a cold chart stays blank."""
    warming = {"status": "warming", "rows": [], "timeframe": "1m"}
    meta = server._history_coverage_meta([], 1.0, warming)

    assert meta["historical_proxy_warming"] is True
    assert meta["historical_proxy"] is False

    settled = {"status": "ok", "rows": [], "timeframe": "1m"}
    assert server._history_coverage_meta([], 1.0, settled)["historical_proxy_warming"] is False


def test_the_client_polls_while_history_is_loading() -> None:
    """The stream only appends the newest row, so it cannot fill the window."""
    source = Path("spreadboard/server.py").read_text(encoding="utf-8")

    assert "function scheduleBackfill(payload)" in source
    assert "historical_proxy_warming" in source
    # It must be called from the fetch path and be bounded.
    assert "scheduleBackfill(payload);" in source
    assert "backfillTries >= 12" in source


def test_the_chart_draws_the_convergence_line() -> None:
    """A basis trade is closed at zero -- that level is the point of the chart."""
    source = Path("spreadboard/server.py").read_text(encoding="utf-8")

    assert "createPriceLine" in source
    assert "'Converged'" in source
    block = source.split("createPriceLine", 1)[1][:400]
    assert "price: 0" in block, "the convergence line must sit at zero"
    assert "Dashed" in block
    # ...and it has to survive a theme flip.
    assert "convergenceLine?.applyOptions" in source


def test_the_convergence_colour_is_defined_in_both_themes() -> None:
    source = Path("spreadboard/server.py").read_text(encoding="utf-8")
    assert source.count("--terminal-warning:") == 2, "light and dark both need the colour"
