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


def test_ourbit_columnar_klines_become_ohlcv_rows() -> None:
    """Ourbit has no ccxt adapter and returns parallel arrays, not rows.

    Its legs are 1,337 of the board's futures legs -- the single largest
    backfill blocker -- and every one of them is a futures leg, so the spot
    endpoint would not have helped.
    """
    import json as _json
    from unittest.mock import patch

    payload = {
        "success": True,
        "data": {
            "time": [1786039260, 1786039320, 1786039380],
            "open": [0.4354, 0.4345, 0.4340],
            "high": [0.4354, 0.4345, 0.4346],
            "low": [0.4345, 0.4340, 0.4338],
            "close": [0.4345, 0.4340, 0.4346],
            "vol": [59.2, 33.8, 192.0],
        },
    }

    class _Response:
        def read(self) -> bytes:
            return _json.dumps(payload).encode()

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    with patch("urllib.request.urlopen", return_value=_Response()):
        candles = historical_spreads._fetch_ourbit_leg("BP/USDT:USDT", "1m", 1786039260 * 1000)

    assert len(candles) == 3
    # ccxt layout: [ms, open, high, low, close, volume]
    assert candles[0][0] == 1786039260 * 1000
    assert candles[0][4] == pytest.approx(0.4345)
    assert candles[-1][4] == pytest.approx(0.4346)


def test_the_ourbit_symbol_is_translated() -> None:
    assert historical_spreads._ourbit_symbol("BP/USDT:USDT") == "BP_USDT"
    assert historical_spreads._ourbit_symbol("1000TOSHI/USDT") == "1000TOSHI_USDT"


def test_ourbit_candles_before_the_window_are_dropped() -> None:
    """Their API rounds `start` down, so it can return points we did not ask for."""
    import json as _json
    from unittest.mock import patch

    payload = {"data": {"time": [1000, 2000, 3000], "open": [1, 1, 1],
                        "high": [1, 1, 1], "low": [1, 1, 1], "close": [1, 2, 3], "vol": [1, 1, 1]}}

    class _Response:
        def read(self) -> bytes:
            return _json.dumps(payload).encode()

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    with patch("urllib.request.urlopen", return_value=_Response()):
        candles = historical_spreads._fetch_ourbit_leg("BP/USDT:USDT", "1m", 2000 * 1000)

    assert [c[0] for c in candles] == [2000 * 1000, 3000 * 1000]


def test_history_only_venues_do_not_touch_the_price_path() -> None:
    """VENUE_IDS drives live quoting; widening it would reprice the board."""
    from spreadboard.fast_quotes import VENUE_IDS

    assert "Upbit" not in VENUE_IDS
    assert "Lighter" not in VENUE_IDS
    assert historical_spreads._history_exchange_id("Upbit") == "upbit"
    assert historical_spreads._history_exchange_id("Lighter") == "lighter"
    # ...and a venue nobody can serve stays unmapped.
    assert historical_spreads._history_exchange_id("Nonesuch") is None
    # Venues already on the price path keep their existing id.
    assert historical_spreads._history_exchange_id("Binance") == VENUE_IDS["Binance"]


def test_a_leg_with_no_symbol_is_not_fetched() -> None:
    assert historical_spreads._fetch_leg({"long_venue": "Binance"}, "long", "1m", 0) == []


def test_a_venue_without_ccxt_can_still_be_quoted_live() -> None:
    """Ourbit legs could never be repriced, so their charts sat frozen.

    _leg_quote rejected anything outside VENUE_IDS before it reached the native
    book fetcher below it -- which is the fetcher that exists precisely for
    venues with no ccxt adapter.
    """
    from spreadboard import fast_quotes

    assert "Ourbit" not in fast_quotes.VENUE_IDS
    assert "Ourbit" in fast_quotes.NATIVE_FUTURES_VENUES
    assert fast_quotes.supports_native_order_book("Ourbit", "Futures") is True
    # Spot is a different set, and Ourbit serves no spot legs on this board.
    assert fast_quotes.supports_native_order_book("Ourbit", "Spot") is False
    assert fast_quotes.supports_native_order_book("Nonesuch", "Futures") is False


def test_the_ccxt_fallback_is_not_attempted_without_an_adapter() -> None:
    """Reaching for a client that cannot exist would raise on every quote."""
    import inspect

    from spreadboard import fast_quotes

    source = inspect.getsource(fast_quotes.FastQuoteRefresher._leg_quote)
    guard = "if native_book is None and venue not in VENUE_IDS:"
    assert guard in source
    # The guard has to come before the client is constructed.
    assert source.index(guard) < source.index("client = self._client(venue, market_type)")


def test_the_book_is_deep_enough_to_price_the_probe() -> None:
    """Twenty levels could not fill $50 on a thin contract.

    Gate held $41.67 on the bid and $47.26 on the ask for BP across twenty
    levels, so depth_weighted_price returned None, the leg quote failed, and
    the chart reported "Stream sampler unavailable" on a route the board was
    still listing. Fifty levels of that same book held $107 and $233.
    """
    from spreadboard import fast_quotes

    assert fast_quotes.BOOK_DEPTH_LEVELS >= 50
    source = Path("spreadboard/fast_quotes.py").read_text(encoding="utf-8")
    # No book request may quietly keep the old shallow limit.
    assert '"limit": 20' not in source
    assert '"sz": 20' not in source
    assert "limit=20" not in source


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("not_applicable", "no candle history exists for a DEX leg"),
        ("warming", "loading window history"),
        ("unavailable", "publishes no candles"),
    ],
)
def test_a_thin_window_says_why(status: str, expected: str) -> None:
    """"0% window coverage" alone reads as a fault rather than as a fact.

    A DEX leg has no candles anywhere -- that is a property of the venue, not a
    failure of the board, and the member deciding whether to trust the chart
    needs to know which it is.
    """
    meta = server._history_coverage_meta([], 1.0, {"status": status, "rows": []})
    assert meta["historical_proxy_status"] == status

    source = Path("spreadboard/server.py").read_text(encoding="utf-8")
    assert expected in source
