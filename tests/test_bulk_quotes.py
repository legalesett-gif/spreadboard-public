"""One bulk call per venue re-prices the whole board.

Websockets stream a few hundred legs of the eight thousand the board carries,
and the discovery scan re-quoted the rest on a twenty-five minute cycle -- so a
route outside the streaming set could be twenty minutes stale, and a token that
turned positive in between did not appear until the next scan.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from pathlib import Path

import pytest

from spreadboard import bulk_quotes


class _Store:
    def __init__(self) -> None:
        self.written: list[tuple] = []
        self.quote_timestamps: list[int] = []

    def put(self, venue, market_type, symbol, *, bids, asks, quote_ts_us, source="public_websocket"):
        self.written.append((venue, market_type, symbol, bids[0][0], asks[0][0]))
        self.quote_timestamps.append(quote_ts_us)


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


def test_aster_native_bulk_prices_every_usdt_family_without_private_credentials() -> None:
    payloads = {
        "fapi": [
            {
                "symbol": "ANTHROPICUSDT",
                "bidPrice": "1888.9",
                "bidQty": "0.05",
                "askPrice": "1906.5",
                "askQty": "0.01",
                "time": 1_787_453_992_200,
            }
        ],
        "sapi": [
            {
                "symbol": "GUAUSDT",
                "bidPrice": "0.0400",
                "bidQty": "4000",
                "askPrice": "0.0401",
                "askQty": "5000",
                "time": 1_787_453_992_300,
            },
            # Unknown quote suffixes are not guessed into a unified symbol.
            {
                "symbol": "GUAUSDC",
                "bidPrice": "0.0400",
                "bidQty": "4000",
                "askPrice": "0.0401",
                "askQty": "5000",
                "time": 1_787_453_992_300,
            },
        ],
    }

    books = bulk_quotes._native_aster_books(
        fetcher=lambda url: payloads["fapi" if "fapi" in url else "sapi"]
    )

    assert [(row["market_type"], row["symbol"]) for row in books] == [
        ("Futures", "ANTHROPIC/USDT:USDT"),
        ("Spot", "GUA/USDT"),
    ]
    assert all(row["venue"] == "Aster" for row in books)
    assert all(row["source"] == "native_bulk_ticker" for row in books)


def test_binance_native_bulk_keeps_spot_and_futures_separate() -> None:
    payloads = {
        "api.binance.com": [
            {
                "symbol": "TRXUSDT",
                "bidPrice": "0.30",
                "bidQty": "1000",
                "askPrice": "0.31",
                "askQty": "900",
            }
        ],
        "fapi.binance.com": [
            {
                "symbol": "TRXUSDT",
                "bidPrice": "0.35",
                "bidQty": "800",
                "askPrice": "0.36",
                "askQty": "700",
                "time": 1_787_453_992_200,
            }
        ],
    }

    books = bulk_quotes._native_bulk_books(
        "Binance",
        fetcher=lambda url: payloads[
            "fapi.binance.com" if "fapi.binance.com" in url else "api.binance.com"
        ],
    )

    assert [(row["market_type"], row["symbol"]) for row in books] == [
        ("Spot", "TRX/USDT"),
        ("Futures", "TRX/USDT:USDT"),
    ]
    assert books[1]["bids"] == [[0.35, 800.0]]


def test_kucoin_native_bulk_applies_contract_multiplier() -> None:
    payloads = {
        "allTickers": {
            "data": [
                {
                    "symbol": "XBTUSDTM",
                    "bestBidPrice": "80000",
                    "bestBidSize": "5",
                    "bestAskPrice": "80001",
                    "bestAskSize": "6",
                    "ts": 1_787_453_992_200_000_000,
                }
            ]
        },
        "contracts": {
            "data": [
                {
                    "symbol": "XBTUSDTM",
                    "baseCurrency": "XBT",
                    "quoteCurrency": "USDT",
                    "multiplier": "0.001",
                    "isInverse": False,
                    "status": "Open",
                }
            ]
        },
    }

    books = bulk_quotes._native_bulk_books(
        "Kucoin Futures",
        fetcher=lambda url: payloads[
            "allTickers" if "allTickers" in url else "contracts"
        ],
    )

    assert books[0]["symbol"] == "BTC/USDT:USDT"
    assert books[0]["bids"] == [[80000.0, 0.005]]
    assert books[0]["asks"] == [[80001.0, 0.006]]


def test_native_snapshot_freshness_is_observation_time_not_last_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quiet but freshly returned BBO must not disappear as stale."""

    observed_at = 1_800_000_000.0
    monkeypatch.setattr(bulk_quotes.time, "time", lambda: observed_at)
    payloads = {
        "allTickers": {
            "data": [
                {
                    "symbol": "XBTUSDTM",
                    "bestBidPrice": "80000",
                    "bestBidSize": "5",
                    "bestAskPrice": "80001",
                    "bestAskSize": "6",
                    "ts": 1_700_000_000_000_000_000,
                }
            ]
        },
        "contracts": {
            "data": [
                {
                    "symbol": "XBTUSDTM",
                    "baseCurrency": "XBT",
                    "quoteCurrency": "USDT",
                    "multiplier": "0.001",
                    "isInverse": False,
                    "status": "Open",
                }
            ]
        },
    }

    books = bulk_quotes._native_bulk_books(
        "Kucoin Futures",
        fetcher=lambda url: payloads[
            "allTickers" if "allTickers" in url else "contracts"
        ],
    )

    assert books[0]["quote_ts_us"] == int(observed_at * 1_000_000)


def test_whitebit_native_snapshot_separates_real_spot_and_tradfi_futures() -> None:
    definitions = [
        {
            "name": "BTC_USDT",
            "stock": "BTC",
            "money": "USDT",
            "type": "spot",
            "tradesEnabled": True,
            "delistedAt": None,
        },
        {
            "name": "AAOI_PERP",
            "stock": "AAOI",
            "money": "USDT",
            "type": "tradfiFutures",
            "tradesEnabled": True,
            "delistedAt": None,
        },
    ]
    snapshot = [
        [1.0, 2.0, "BTC_USDT", 1, "80000", "2", "80001", "3"],
        [1.0, 2.0, "AAOI_PERP", 2, "109.8", "4", "110.0", "5"],
    ]

    books = bulk_quotes._native_bulk_books(
        "WhiteBIT",
        fetcher=lambda _url: definitions,
        whitebit_snapshotter=lambda expected: snapshot
        if expected == {"BTC_USDT", "AAOI_PERP"}
        else [],
    )

    assert [(row["market_type"], row["symbol"]) for row in books] == [
        ("Spot", "BTC/USDT"),
        ("Futures", "AAOI/USDT:USDT"),
    ]
    assert books[1]["bids"] == [[109.8, 4.0]]


def test_generic_bulk_ticker_uses_fresh_response_observation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = 1_800_000_000.0
    monkeypatch.setattr(bulk_quotes.time, "time", lambda: observed_at)
    store = _Store()
    client = _Client(
        {
            "QUIET/USDT": {
                "bid": 1.0,
                "ask": 1.1,
                "timestamp": 1_700_000_000_000,
            }
        },
        {"QUIET/USDT": {"spot": True}},
    )

    assert (
        bulk_quotes.sweep_venue(
            "Gate", store=store, client_factory=lambda *_args: client
        )
        == 1
    )
    assert store.quote_timestamps == [int(observed_at * 1_000_000)]


def test_xt_native_bulk_keeps_spot_and_futures_bbo_families_separate() -> None:
    payloads = {
        "sapi.xt.com": {
            "rc": 0,
            "result": [
                {
                    "s": "gua_usdt",
                    "t": 1_787_453_992_200,
                    "bp": "0.0400",
                    "bq": "4000",
                    "ap": "0.0401",
                    "aq": "5000",
                }
            ],
        },
        "fapi.xt.com": {
            "returnCode": 0,
            "result": [
                {
                    "s": "gua_usdt",
                    "t": 1_787_453_992_300,
                    "bp": "0.0410",
                    "ap": "0.0411",
                }
            ],
        },
    }

    books = bulk_quotes._native_bulk_books(
        "XT",
        fetcher=lambda url: payloads[
            "fapi.xt.com" if "fapi.xt.com" in url else "sapi.xt.com"
        ],
    )

    assert [(row["market_type"], row["symbol"]) for row in books] == [
        ("Spot", "GUA/USDT"),
        ("Futures", "GUA/USDT:USDT"),
    ]
    assert books[0]["bids"] == [[0.04, 4000.0]]
    assert books[1]["bids"] == [[0.041, 0.0]]


def test_kraken_futures_native_bulk_keeps_only_linear_live_perpetuals() -> None:
    books = bulk_quotes._native_bulk_books(
        "Kraken Futures",
        fetcher=lambda _url: {
            "result": "success",
            "tickers": [
                {
                    "symbol": "PF_XBTUSD",
                    "tag": "perpetual",
                    "pair": "XBT:USD",
                    "bid": 80_000,
                    "bidSize": 2,
                    "ask": 80_001,
                    "askSize": 3,
                    "suspended": False,
                },
                {
                    "symbol": "PI_XBTUSD",
                    "tag": "perpetual",
                    "pair": "XBT:USD",
                    "bid": 80_000,
                    "ask": 80_001,
                    "suspended": False,
                },
                {
                    "symbol": "PF_OLDUSD",
                    "tag": "perpetual",
                    "pair": "OLD:USD",
                    "bid": 1,
                    "ask": 2,
                    "suspended": True,
                },
            ],
        },
    )

    assert [(row["market_type"], row["symbol"]) for row in books] == [
        ("Futures", "BTC/USD:USD")
    ]
    assert books[0]["bids"] == [[80_000.0, 2.0]]
    assert books[0]["asks"] == [[80_001.0, 3.0]]


def test_native_futures_ticker_without_size_stays_indicative() -> None:
    books = bulk_quotes._native_bulk_books(
        "Phemex",
        fetcher=lambda _url: {
            "result": [
                {
                    "symbol": "TRXUSDT",
                    "bidRp": "0.35",
                    "askRp": "0.36",
                    "timestamp": 1_787_453_992_200_000_000,
                }
            ]
        },
    )

    assert books[0]["symbol"] == "TRX/USDT:USDT"
    assert books[0]["bids"] == [[0.35, 0.0]]
    assert books[0]["asks"] == [[0.36, 0.0]]


def test_hyperliquid_native_bulk_prices_main_and_builder_perpetuals() -> None:
    payloads = {
        "perpDexs": [{"name": "xyz"}],
        "main": [
            {"universe": [{"name": "BTC"}]},
            [{"impactPxs": ["80000", "80001"]}],
        ],
        "xyz": [
            {
                "universe": [
                    {"name": "xyz:TSLA"},
                    {"name": "xyz:OLD", "isDelisted": True},
                ]
            },
            [
                {"impactPxs": ["210.1", "210.2"]},
                {"impactPxs": ["1", "2"]},
            ],
        ],
    }

    def post(_url: str, payload: dict[str, Any]) -> Any:
        if payload["type"] == "perpDexs":
            return payloads["perpDexs"]
        return payloads[payload.get("dex") or "main"]

    books = bulk_quotes._native_bulk_books("Hyperliquid", poster=post)

    assert [(row["market_type"], row["symbol"]) for row in books] == [
        ("Futures", "BTC/USDC:USDC"),
        ("Futures", "XYZ-TSLA/USDC:USDC"),
    ]
    assert books[1]["bids"] == [[210.1, 0.0]]
    assert books[1]["asks"] == [[210.2, 0.0]]
    assert all(row["source"] == "native_bulk_ticker" for row in books)


def test_hyperliquid_native_futures_do_not_wait_for_spot_ccxt(monkeypatch) -> None:
    store = _Store()
    native = [
        {
            "venue": "Hyperliquid",
            "market_type": "Futures",
            "symbol": "BTC/USDC:USDC",
            "bids": [[80000.0, 0.0]],
            "asks": [[80001.0, 0.0]],
            "quote_ts_us": 1_787_453_992_200_000,
            "source": "native_bulk_ticker",
        }
    ]
    monkeypatch.setattr(bulk_quotes, "_native_bulk_books", lambda *_a, **_k: native)
    monkeypatch.setattr(
        bulk_quotes,
        "_client",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("slow fallback called")),
    )

    written = bulk_quotes.sweep_venue("Hyperliquid", store=store)

    assert written == 1
    assert store.written[0][:3] == ("Hyperliquid", "Futures", "BTC/USDC:USDC")


def test_native_bulk_venue_is_written_without_slow_ccxt_fallback() -> None:
    store = _Store()
    native = [
        {
            "venue": "Kucoin Futures",
            "market_type": "Futures",
            "symbol": "TRX/USDT:USDT",
            "bids": [[0.35, 100.0]],
            "asks": [[0.36, 100.0]],
            "quote_ts_us": 1_787_453_992_200_000,
            "source": "native_bulk_ticker",
        }
    ]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(bulk_quotes, "_native_bulk_books", lambda _venue: native)
        monkeypatch.setattr(
            bulk_quotes,
            "_client",
            lambda *_args: pytest.fail("native-complete venue must not load CCXT"),
        )
        written = bulk_quotes.sweep_venue("Kucoin Futures", store=store)

    assert written == 1
    assert store.written[0][:3] == (
        "Kucoin Futures",
        "Futures",
        "TRX/USDT:USDT",
    )


def test_a_venue_whose_ticker_carries_no_quotes_is_not_called_each_cycle() -> None:
    """Coinbase returns 528 symbols with neither bid nor ask."""
    assert "Coinbase" in bulk_quotes.SKIP_VENUES


def test_a_failing_venue_does_not_stop_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()

    def explode(*_a: Any, **_k: Any):
        raise RuntimeError("venue down")

    monkeypatch.setattr(bulk_quotes, "_client", explode)
    monkeypatch.setattr(bulk_quotes, "_native_bulk_books", lambda _venue: [])

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


def test_the_sweep_resumes_where_it_ran_out_of_time(tmp_path, monkeypatch) -> None:
    """Starting at the same venue every pass starves the later ones.

    Observed as 18 venues one pass, then 10, then 1, with board coverage
    swinging between 42% and 76%.
    """
    import time as _time

    visited: list[str] = []

    def slow(venue, **_kwargs):
        visited.append(venue)
        _time.sleep(0.03)
        return 1

    monkeypatch.setattr(bulk_quotes, "CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(bulk_quotes.fair_price, "write", lambda rows, **_k: 0)
    monkeypatch.setattr(bulk_quotes, "sweep_venue", slow)
    venues = ["A", "B", "C", "D", "E", "F"]
    bulk_quotes._CURSOR["index"] = 0

    bulk_quotes.sweep(venues, store=_Store(), budget_seconds=0.05, workers=1)
    first = list(visited)
    visited.clear()
    bulk_quotes.sweep(venues, store=_Store(), budget_seconds=0.05, workers=1)

    assert first, "the first pass must cover something"
    assert visited, "the second pass must cover something"
    assert visited[0] != first[0], "the second pass must not repeat the first venue"
    assert visited[0] == venues[len(first) % len(venues)]


def test_a_full_pass_leaves_the_cursor_back_at_the_start(tmp_path, monkeypatch) -> None:
    # The cursor is persisted now, so the starting point comes from the file.
    monkeypatch.setattr(bulk_quotes, "CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(bulk_quotes, "CACHE_WRITER", None) if False else None
    monkeypatch.setattr(bulk_quotes.fair_price, "write", lambda rows, **_k: 0)
    monkeypatch.setattr(bulk_quotes, "sweep_venue", lambda venue, **_kwargs: 1)
    venues = ["A", "B", "C"]
    bulk_quotes._CURSOR["index"] = 0

    bulk_quotes.sweep(venues, store=_Store(), budget_seconds=30.0, workers=1)

    assert bulk_quotes._CURSOR["index"] == 0


def test_independent_venues_refresh_concurrently(tmp_path, monkeypatch) -> None:
    active = 0
    maximum_active = 0
    lock = threading.Lock()
    release = threading.Event()

    def blocked(_venue, **_kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if maximum_active >= 2:
                release.set()
        assert release.wait(1.0)
        with lock:
            active -= 1
        return 1

    monkeypatch.setattr(bulk_quotes, "CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(bulk_quotes.fair_price, "write", lambda rows, **_k: 0)
    monkeypatch.setattr(bulk_quotes, "sweep_venue", blocked)
    bulk_quotes._CURSOR["index"] = 0

    result = bulk_quotes.sweep(
        ["A", "B", "C", "D"], store=_Store(), budget_seconds=30.0, workers=2
    )

    assert maximum_active == 2
    assert result["venues"] == 4


def test_a_stuck_venue_cannot_hold_the_sweep_past_its_truth_budget(
    tmp_path, monkeypatch
) -> None:
    release = threading.Event()

    def stuck(_venue, **_kwargs):
        release.wait(2.0)
        return 1

    monkeypatch.setattr(bulk_quotes, "CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(bulk_quotes.fair_price, "write", lambda rows, **_k: 0)
    monkeypatch.setattr(bulk_quotes, "sweep_venue", stuck)

    started = time.monotonic()
    result = bulk_quotes.sweep(
        ["Slow"], store=_Store(), budget_seconds=0.05, workers=1
    )
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.5
    assert result["timed_out"] is True
    assert result["pending_venues"] == ["Slow"]
    assert result["venues"] == 0


def test_normal_sweep_refreshes_aster_again_at_publication(tmp_path, monkeypatch) -> None:
    visited: list[str] = []

    def record(venue, **_kwargs):
        visited.append(venue)
        return 2 if venue == "Aster" else 1

    monkeypatch.setattr(bulk_quotes, "CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(bulk_quotes, "VENUE_IDS", {"Aster": "aster", "Binance": "binance"})
    monkeypatch.setattr(bulk_quotes.ourbit_quotes, "sweep", lambda **_kwargs: 0)
    monkeypatch.setattr(bulk_quotes.fair_price, "write", lambda rows, **_kwargs: 0)
    monkeypatch.setattr(bulk_quotes, "sweep_venue", record)

    result = bulk_quotes.sweep(store=_Store(), budget_seconds=30.0, workers=1)

    assert visited == ["Aster", "Binance", "Aster"]
    assert result["venues"] == 2
    # The count describes the published closing generation, not two writes of
    # the same Aster books.
    assert result["quotes"] == 3


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


def test_expired_funding_clears_snapshot_current_fields(monkeypatch) -> None:
    from spreadboard import api_spreads

    monkeypatch.setattr(bulk_quotes, "load_funding", lambda **_kw: {})
    raw = {
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "long_market_symbol": "T/USDT:USDT",
        "funding_daily_pct": 4.2,
        "funding_projected_24h_pct": 4.2,
        "notes": {
            "route_inputs": {
                "long": {
                    "symbol": "T/USDT:USDT",
                    "current_funding_pct": 1.4,
                    "projected_24h_pct": 4.2,
                }
            }
        },
    }

    updated = api_spreads._apply_live_funding(raw, {})

    assert updated["funding_daily_pct"] is None
    assert updated["funding_projected_24h_pct"] is None
    assert "current_funding_pct" not in updated["notes"]["route_inputs"]["long"]


def test_a_funding_sweep_invalidates_cached_rows(tmp_path, monkeypatch) -> None:
    """A sweep can refresh every rate on the board, and the cached rows would
    keep the old ones until the snapshot happened to move."""
    import inspect

    from spreadboard import api_spreads

    source = inspect.getsource(api_spreads._load_api_discovery_rows)
    assert "FUNDING_CACHE_PATH" in source, (
        "the funding overlay must take part in the row cache key"
    )


def test_the_funding_sweep_covers_venues_ccxt_cannot(monkeypatch) -> None:
    """Eight of eighteen futures venues publish no bulk funding through CCXT.

    Calling CCXT alone left Ourbit (845 legs) and XT (688) with no rate at all,
    along with Mexc, HTX, BitMart and both Kraken and Kucoin futures.
    """
    import inspect

    source = inspect.getsource(bulk_quotes.sweep_funding)
    assert "_bulk_funding_rates" in source, (
        "the sweep must go through the native-aware path, not CCXT directly"
    )


def test_the_sweep_writes_entries_keyed_by_venue_and_symbol(tmp_path, monkeypatch) -> None:
    class _Refresher:
        def _bulk_funding_rates(self, venue):
            if venue != "Ourbit":
                return {}
            return {"QNTX/USDT:USDT": {"current_funding_pct": 0.02, "funding_interval_hours": 8.0}}

        def close(self):
            pass

    monkeypatch.setattr("spreadboard.fast_quotes.FastQuoteRefresher", lambda: _Refresher())
    out = bulk_quotes.sweep_funding(["Ourbit"], cache_path=tmp_path / "f.json")

    assert out["legs"] == 1
    legs = bulk_quotes.load_funding(cache_path=tmp_path / "f.json")
    assert legs["Ourbit|QNTX/USDT:USDT"]["rate_pct"] == 0.02


def test_rotating_funding_merge_expires_an_old_rate(tmp_path, monkeypatch) -> None:
    path = tmp_path / "funding.json"
    path.write_text(json.dumps({
        "schema": "spreadboard.live_funding.v1",
        "updated_at": "2026-08-13T00:00:00+00:00",
        "legs": {"Gate|OLD/USDT:USDT": {"rate_pct": 9.0}},
        "leg_updated_at": {"Gate|OLD/USDT:USDT": 1.0},
    }))

    class _Refresher:
        def _bulk_funding_rates(self, _venue):
            return {"NEW/USDT:USDT": {"current_funding_pct": 0.02, "funding_interval_hours": 8}}

        def close(self):
            pass

    monkeypatch.setattr("spreadboard.fast_quotes.FastQuoteRefresher", lambda: _Refresher())
    monkeypatch.setattr(bulk_quotes.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(bulk_quotes, "FUNDING_MAX_AGE_SECONDS", 300.0)
    bulk_quotes.sweep_funding(
        ["Ourbit"], cache_path=path, merge_existing=True
    )

    payload = json.loads(path.read_text())
    assert "Gate|OLD/USDT:USDT" not in payload["legs"]
    assert payload["legs"]["Ourbit|NEW/USDT:USDT"]["rate_pct"] == 0.02


def test_funding_reader_refuses_stale_cache_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "funding.json"
    path.write_text(json.dumps({
        "schema": "spreadboard.live_funding.v1",
        "updated_at": "1970-01-01T00:00:01+00:00",
        "legs": {"Gate|OLD/USDT:USDT": {"rate_pct": 9.0}},
    }))
    monkeypatch.setattr(bulk_quotes.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(bulk_quotes, "FUNDING_MAX_AGE_SECONDS", 300.0)
    bulk_quotes._FUNDING_CACHE["stamp"] = None
    assert bulk_quotes.load_funding(cache_path=path) == {}


def test_the_overlay_keys_on_the_symbol_the_snapshot_actually_carries() -> None:
    """No futures leg has a top-level market_symbol.

    All 32,056 of them keep it in notes.route_inputs, so keying on the
    top-level field produced "venue|None" for every leg and the overlay matched
    nothing at all.
    """
    from spreadboard.api_spreads import _apply_live_funding

    raw = {
        "long_venue": "XT",
        "long_market_type": "Futures",
        "notes": {"route_inputs": {"long": {"symbol": "VANRY/USDT:USDT"}}},
    }
    overlay = {"XT|VANRY/USDT:USDT": {"rate_pct": -0.0212, "interval_hours": 8.0}}

    out = _apply_live_funding(raw, overlay)
    leg = out["notes"]["route_inputs"]["long"]

    assert leg["current_funding_pct"] == -0.0212
    assert leg["funding_interval_hours"] == 8.0


def test_a_leg_with_no_symbol_anywhere_is_skipped() -> None:
    from spreadboard.api_spreads import _apply_live_funding

    raw = {"long_venue": "XT", "long_market_type": "Futures", "notes": {}}

    assert _apply_live_funding(raw, {"XT|X": {"rate_pct": 1.0}}) is raw


def test_the_funding_sweep_covers_venues_with_no_ccxt_adapter() -> None:
    """Ourbit is absent from VENUE_IDS, so iterating that alone never asked for
    its rates -- 844 legs with nothing."""
    import inspect

    from spreadboard import bulk_quotes

    source = inspect.getsource(bulk_quotes.sweep_funding)
    assert "NATIVE_FUNDING_SOURCES" in source


def test_one_client_per_venue_not_one_per_market_type(monkeypatch) -> None:
    """The second client held a duplicate of the first's market metadata.

    load_markets() returns the same set whichever defaultType the client was
    built with, so caching per (venue, market type) doubled the heaviest thing
    in the process -- Binance's pair measured 303MB -- and across 21 venues it
    was enough to OOM the service.
    """
    built: list[str] = []

    class Client:
        def __init__(self, *_args, **_kwargs) -> None:
            self.options: dict[str, object] = {}
            self.markets: dict[str, object] = {}

        def load_markets(self) -> None:
            built.append("load")

    module = type("ccxt", (), {"binance": Client})
    monkeypatch.setitem(__import__("sys").modules, "ccxt", module)
    monkeypatch.setattr(bulk_quotes, "_CLIENTS", {})
    monkeypatch.setattr(bulk_quotes, "VENUE_IDS", {"Binance": "binance"})

    spot = bulk_quotes._client("Binance", "Spot")
    futures = bulk_quotes._client("Binance", "Futures")

    assert spot is futures
    assert built == ["load"]
    assert futures.options["defaultType"] == "swap"
    assert bulk_quotes._client("Binance", "Spot").options["defaultType"] == "spot"


def test_the_rotation_cursor_survives_the_process_that_advances_it(tmp_path, monkeypatch) -> None:
    """The sweep now exits after each pass, so an in-memory cursor never moves.

    Starting alphabetically every pass is what starved the later venues in the
    first place: 18 venues one pass, then 10, then 1.
    """
    monkeypatch.setattr(bulk_quotes, "CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(bulk_quotes, "_CURSOR", {"index": 0})

    swept: list[str] = []
    monkeypatch.setattr(
        bulk_quotes,
        "sweep_venue",
        lambda venue, **_kwargs: swept.append(venue) or 1,
    )
    monkeypatch.setattr(bulk_quotes.fair_price, "write", lambda rows, **_k: 0)

    venues = ["A", "B", "C", "D"]
    bulk_quotes.sweep(venues[:2], store=object(), budget_seconds=60.0)
    first = list(swept)

    # A fresh process: module state is gone, only the file remains.
    monkeypatch.setattr(bulk_quotes, "_CURSOR", {"index": 0})
    swept.clear()
    bulk_quotes.sweep(venues[:2], store=object(), budget_seconds=60.0)

    assert first == ["A", "B"]
    # Resumes where the last process stopped rather than restarting at A.
    assert (tmp_path / "cursor.json").exists()
    assert bulk_quotes._load_cursor(len(venues)) == bulk_quotes._CURSOR["index"]


def test_the_sweep_runs_as_a_worker_process_not_inside_the_server() -> None:
    """Its memory only comes back if the process that held it exits."""
    import inspect

    from scripts.run_spreadboard_service import BulkFundingLoop, BulkQuoteLoop

    source = inspect.getsource(BulkQuoteLoop)
    assert "bulk_quote_worker.py" in source
    assert "_run_worker" in source
    assert '"--funding-budget-seconds",\n                "0"' in source
    funding_source = inspect.getsource(BulkFundingLoop)
    assert '"--skip-quotes"' in funding_source
    assert '"--funding-venues"' in funding_source
    assert Path("scripts/bulk_quote_worker.py").exists()


def test_quote_loop_pause_can_fit_inside_the_current_spread_window() -> None:
    """The old hard minimum was 15s; an 85s production pass therefore had a
    100s cadence against a 90s current-spread boundary."""
    assert bulk_quotes.INTERVAL_SECONDS <= 5.0


def test_a_quote_with_an_unknown_size_is_still_a_quote(tmp_path) -> None:
    """Several venues return a bid and an ask but no volumes.

    The sweep stores the size as 0, and requiring amount > 0 discarded all 299
    Hyperliquid books on read -- silently, with the rows sitting in the table.
    That is why the operator's SKHY/SKHX chart showed no prices.
    """
    from spreadboard import live_book_cache

    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    store.put(
        "Hyperliquid",
        "Futures",
        "XYZ-SKHX/USDC:USDC",
        bids=[[1167.409, 0.0]],
        asks=[[1167.6, 0.0]],
        quote_ts_us=int(time.time() * 1_000_000),
    )

    book = store.get(
        "Hyperliquid", "Futures", "XYZ-SKHX/USDC:USDC", max_age_seconds=300.0
    )

    assert book is not None, "a sized-0 quote was dropped on read"
    assert book.bids[0][0] == 1167.409
    assert book.asks[0][0] == 1167.6


def test_futures_ticker_volume_is_normalised_by_contract_size(tmp_path) -> None:
    from spreadboard import bulk_quotes, live_book_cache

    class Client:
        has = {"fetchTickers": True}
        markets = {
            "T/USDT:USDT": {
                "swap": True,
                "inverse": False,
                "contractSize": 100.0,
            }
        }

        def fetch_tickers(self):
            return {
                "T/USDT:USDT": {
                    "bid": 0.25,
                    "ask": 0.26,
                    "bidVolume": 4.0,
                    "askVolume": 5.0,
                }
            }

    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    bulk_quotes.sweep_venue(
        "Gate",
        store=store,
        client_factory=lambda _venue, market_type: Client() if market_type == "Futures" else None,
    )
    book = store.get("Gate", "Futures", "T/USDT:USDT", max_age_seconds=300.0)

    assert book is not None
    assert book.bids == [[0.25, 400.0]]
    assert book.asks == [[0.26, 500.0]]
    assert book.source == "bulk_ticker"


@pytest.mark.parametrize("source", ["bulk_ticker", "native_bulk_ticker"])
def test_bulk_ticker_cannot_flatten_a_current_websocket_ladder(
    tmp_path, source
) -> None:
    from spreadboard import live_book_cache

    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    timestamp = int(time.time() * 1_000_000)
    store.put(
        "Gate", "Spot", "T/USDT",
        bids=[[1.0, 100.0], [0.99, 100.0]],
        asks=[[1.01, 100.0], [1.02, 100.0]],
        quote_ts_us=timestamp,
        source="public_websocket",
    )
    store.put(
        "Gate", "Spot", "T/USDT",
        bids=[[9.0, 0.0]], asks=[[9.1, 0.0]],
        quote_ts_us=timestamp + 1_000_000,
        source=source,
    )

    book = store.get("Gate", "Spot", "T/USDT", max_age_seconds=300.0)
    assert book is not None
    assert book.source == "public_websocket"
    assert len(book.bids) == 2


def test_a_book_batch_preserves_each_quote_and_source(tmp_path) -> None:
    from spreadboard import live_book_cache

    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    timestamp = int(time.time() * 1_000_000)
    written = store.put_many([
        {
            "venue": "Gate",
            "market_type": "Spot",
            "symbol": "A/USDT",
            "bids": [[1.0, 100.0]],
            "asks": [[1.01, 100.0]],
            "quote_ts_us": timestamp,
            "source": "bulk_ticker",
        },
        {
            "venue": "Mexc",
            "market_type": "Futures",
            "symbol": "B/USDT:USDT",
            "bids": [[2.0, 50.0]],
            "asks": [[2.02, 50.0]],
            "quote_ts_us": timestamp + 1,
            "source": "bulk_ticker",
        },
    ])

    assert written == 2
    books = store.load_all(max_age_seconds=300.0)
    assert set(books) == {
        "Gate|Spot|A/USDT",
        "Mexc|Futures|B/USDT:USDT",
    }
    assert all(book.source == "bulk_ticker" for book in books.values())


def test_targeted_book_load_returns_only_requested_fresh_keys(tmp_path) -> None:
    from spreadboard import live_book_cache

    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    timestamp = int(time.time() * 1_000_000)
    store.put_many([
        {
            "venue": "Gate", "market_type": "Spot", "symbol": "A/USDT",
            "bids": [[1.0, 5.0]], "asks": [[1.01, 5.0]],
            "quote_ts_us": timestamp, "source": "bulk_ticker",
        },
        {
            "venue": "Mexc", "market_type": "Spot", "symbol": "B/USDT",
            "bids": [[2.0, 5.0]], "asks": [[2.01, 5.0]],
            "quote_ts_us": timestamp, "source": "bulk_ticker",
        },
    ])

    selected = store.get_many(
        ["Gate|Spot|A/USDT", "missing"],
        max_age_seconds=300.0,
    )

    assert set(selected) == {"Gate|Spot|A/USDT"}


def test_a_zero_or_negative_price_is_still_rejected(tmp_path) -> None:
    from spreadboard import live_book_cache

    store = live_book_cache.LiveBookStore(tmp_path / "books.sqlite3")
    store.put(
        "Venue",
        "Futures",
        "X/Y",
        bids=[[0.0, 5.0]],
        asks=[[-1.0, 5.0]],
        quote_ts_us=int(time.time() * 1_000_000),
    )

    assert store.get("Venue", "Futures", "X/Y", max_age_seconds=300.0) is None
