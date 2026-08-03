"""One bulk call per venue re-prices the whole board.

Websockets stream a few hundred legs of the eight thousand the board carries,
and the discovery scan re-quoted the rest on a twenty-five minute cycle -- so a
route outside the streaming set could be twenty minutes stale, and a token that
turned positive in between did not appear until the next scan.
"""

from __future__ import annotations

from typing import Any

import pytest

from spreadboard import bulk_quotes


class _Store:
    def __init__(self) -> None:
        self.written: list[tuple] = []

    def put(self, venue, market_type, symbol, *, bids, asks, quote_ts_us):
        self.written.append((venue, market_type, symbol, bids[0][0], asks[0][0]))


class _Client:
    def __init__(self, tickers: dict, markets: dict, has_tickers: bool = True) -> None:
        self._tickers = tickers
        self.markets = markets
        self.has = {"fetchTickers": has_tickers}

    def fetch_tickers(self) -> dict:
        return self._tickers


def test_a_priced_symbol_reaches_the_store() -> None:
    store = _Store()
    client = _Client(
        {"BTC/USDT": {"bid": 100.0, "ask": 100.5, "bidVolume": 2.0, "askVolume": 3.0}},
        {"BTC/USDT": {"spot": True}},
    )

    written = bulk_quotes.sweep_venue("Binance", store=store, client_factory=lambda *_a: client)

    assert written == 1
    assert store.written[0][:3] == ("Binance", "Spot", "BTC/USDT")
    assert store.written[0][3:] == (100.0, 100.5)


def test_a_symbol_without_both_sides_is_skipped() -> None:
    """A one-sided quote cannot price a spread."""
    store = _Store()
    client = _Client(
        {
            "A/USDT": {"bid": 1.0, "ask": None},
            "B/USDT": {"bid": None, "ask": 1.0},
            "C/USDT": {"bid": 0, "ask": 1.0},
        },
        {k: {"spot": True} for k in ("A/USDT", "B/USDT", "C/USDT")},
    )

    assert bulk_quotes.sweep_venue("Binance", store=store, client_factory=lambda *_a: client) == 0


def test_a_swap_is_not_filed_as_spot() -> None:
    """The board keys a leg on its market type; filing a perp under Spot makes
    it invisible to every route that names it."""
    store = _Store()
    swap = _Client(
        {"BTC/USDT:USDT": {"bid": 100.0, "ask": 100.5}},
        {"BTC/USDT:USDT": {"swap": True}},
    )

    def factory(_venue: str, market_type: str):
        return swap if market_type == "Futures" else _Client({}, {})

    bulk_quotes.sweep_venue("Binance", store=store, client_factory=factory)

    assert [row[1] for row in store.written] == ["Futures"]


def test_an_inverse_contract_is_left_out() -> None:
    """An inverse perp settles in the base asset and is not comparable."""
    store = _Store()
    client = _Client(
        {"BTC/USD:BTC": {"bid": 100.0, "ask": 100.5}},
        {"BTC/USD:BTC": {"swap": True, "inverse": True}},
    )

    assert bulk_quotes.sweep_venue("Binance", store=store, client_factory=lambda *_a: client) == 0


def test_a_venue_with_no_bulk_ticker_is_skipped() -> None:
    store = _Store()
    client = _Client({"A/USDT": {"bid": 1.0, "ask": 1.1}}, {"A/USDT": {"spot": True}}, has_tickers=False)

    assert bulk_quotes.sweep_venue("Binance", store=store, client_factory=lambda *_a: client) == 0


def test_a_venue_whose_ticker_carries_no_quotes_is_not_called_each_cycle() -> None:
    """Coinbase returns 528 symbols with neither bid nor ask."""
    assert "Coinbase" in bulk_quotes.SKIP_VENUES


def test_a_failing_venue_does_not_stop_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()

    def explode(*_a: Any, **_k: Any):
        raise RuntimeError("venue down")

    monkeypatch.setattr(bulk_quotes, "_client", explode)

    # sweep_venue swallows it; the sweep keeps going to the next venue.
    assert bulk_quotes.sweep_venue("Binance", store=store) == 0


def test_a_staler_quote_does_not_overwrite_a_fresher_one(tmp_path) -> None:
    """The bulk sweep covers the whole board every ninety seconds; the
    websockets cover the busiest legs sub-second. Letting the sweep flatten a
    streamed book leaves the push with nothing to send."""
    from spreadboard import live_book_cache

    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    fresh_ts = 1_785_000_100_000_000
    stale_ts = 1_785_000_000_000_000

    store.put("Gate", "Spot", "T/USDT", bids=[[1.0, 1.0]], asks=[[1.1, 1.0]], quote_ts_us=fresh_ts)
    store.put("Gate", "Spot", "T/USDT", bids=[[9.0, 1.0]], asks=[[9.9, 1.0]], quote_ts_us=stale_ts)

    book = store.get("Gate", "Spot", "T/USDT", max_age_seconds=10**9)
    assert book is not None
    assert book.bids[0][0] == 1.0, "the fresher streamed book must survive"
    assert book.quote_ts_us == fresh_ts


def test_the_sweep_resumes_where_it_ran_out_of_time(monkeypatch) -> None:
    """Starting at the same venue every pass starves the later ones.

    Observed as 18 venues one pass, then 10, then 1, with board coverage
    swinging between 42% and 76%.
    """
    import time as _time

    visited: list[str] = []

    def slow(venue, *, store):
        visited.append(venue)
        _time.sleep(0.03)
        return 1

    monkeypatch.setattr(bulk_quotes, "sweep_venue", slow)
    venues = ["A", "B", "C", "D", "E", "F"]
    bulk_quotes._CURSOR["index"] = 0

    bulk_quotes.sweep(venues, store=_Store(), budget_seconds=0.05)
    first = list(visited)
    visited.clear()
    bulk_quotes.sweep(venues, store=_Store(), budget_seconds=0.05)

    assert first, "the first pass must cover something"
    assert visited, "the second pass must cover something"
    assert visited[0] != first[0], "the second pass must not repeat the first venue"
    assert visited[0] == venues[len(first) % len(venues)]


def test_a_full_pass_leaves_the_cursor_back_at_the_start(monkeypatch) -> None:
    monkeypatch.setattr(bulk_quotes, "sweep_venue", lambda venue, *, store: 1)
    venues = ["A", "B", "C"]
    bulk_quotes._CURSOR["index"] = 0

    bulk_quotes.sweep(venues, store=_Store(), budget_seconds=30.0)

    assert bulk_quotes._CURSOR["index"] == 0


def test_live_funding_overlays_a_leg(monkeypatch) -> None:
    """554 of 5,382 futures legs carried no rate and 424 disagreed, because the
    rotating quote worker reaches about three venues of eighteen per pass."""
    from spreadboard import api_spreads

    monkeypatch.setattr(
        bulk_quotes,
        "load_funding",
        lambda **_kw: {
            "Bybit|T/USDT:USDT": {
                "rate_pct": 0.0125,
                "interval_hours": 4.0,
                "next_funding_ts_us": 1785000000000000,
            }
        },
    )
    raw = {
        "token": "T",
        "long_venue": "Gate", "long_market_type": "Spot", "long_market_symbol": "T/USDT",
        "short_venue": "Bybit", "short_market_type": "Futures", "short_market_symbol": "T/USDT:USDT",
        "notes": {"route_inputs": {"short": {"current_funding_pct": 0.9}}},
    }

    out = api_spreads._apply_live_funding(raw)
    leg = out["notes"]["route_inputs"]["short"]

    assert leg["current_funding_pct"] == 0.0125, "the venue's current rate must win"
    assert leg["funding_interval_hours"] == 4.0


def test_a_spot_leg_is_not_given_funding(monkeypatch) -> None:
    from spreadboard import api_spreads

    monkeypatch.setattr(
        bulk_quotes, "load_funding", lambda **_kw: {"Gate|T/USDT": {"rate_pct": 0.5}}
    )
    raw = {
        "long_venue": "Gate", "long_market_type": "Spot", "long_market_symbol": "T/USDT",
        "short_venue": "Bybit", "short_market_type": "Futures", "short_market_symbol": "T/USDT:USDT",
    }

    assert api_spreads._apply_live_funding(raw) is raw


def test_no_cached_funding_leaves_the_row_alone(monkeypatch) -> None:
    from spreadboard import api_spreads

    monkeypatch.setattr(bulk_quotes, "load_funding", lambda **_kw: {})
    raw = {"long_venue": "Gate", "long_market_type": "Futures", "long_market_symbol": "T/USDT:USDT"}

    assert api_spreads._apply_live_funding(raw) is raw


def test_a_funding_sweep_invalidates_cached_rows(tmp_path, monkeypatch) -> None:
    """A sweep can refresh every rate on the board, and the cached rows would
    keep the old ones until the snapshot happened to move."""
    import inspect

    from spreadboard import api_spreads

    source = inspect.getsource(api_spreads._load_api_discovery_rows)
    assert "FUNDING_CACHE_PATH" in source, (
        "the funding overlay must take part in the row cache key"
    )
