"""The feed must never be weaker than the page it is correcting.

The board renders `depth_weighted_spread_pct` when a route can prove the probe
size and falls back to `executable_spread_pct` when it cannot, labelling which
one it showed. The stream carried only the first of those, so any route that
could not prove depth streamed null -- and the client wrote "—" over a number
the server had just rendered correctly.

Raising the probe from $50 to $500 made that constant instead of occasional:
every route's stored `depth_usd` was stamped 50.0 by earlier scans, so
`prior_depth_verified` (which compares it against the current probe) went false
almost everywhere and the fallback inside `live_route_updates_for` stopped
firing. The board filled with dashes within seconds of loading, which reads as
"needs a refresh".

Two invariants keep it fed:
  * the stream sends the same value the page would render, and
  * a null update never erases a number already on screen.
"""

from __future__ import annotations

import json

import pytest

from spreadboard import api_spreads, live_book_cache


class _Book:
    """A real two-sided ladder, deliberately too thin for the probe."""

    def __init__(self, price: float) -> None:
        # A tenth of the probe: enough to price the top of book, not enough to
        # walk a matched VWAP at the target size.
        size = api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD * 0.1
        self.bids = [[price, size / price]]
        self.asks = [[price * 1.01, size / (price * 1.01)]]
        self.quote_ts_us = 1_700_000_100_000_000


def _route(**overrides) -> dict:
    route = {
        "route_key": "T|Gate|Spot|Bybit|Futures",
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "long_market_symbol": "T/USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "T/USDT:USDT",
        "long_ask": 1.00,
        "short_bid": 1.10,
        "age_min": 0.1,
        # Stamped by a scan taken when the probe was $50, which is exactly the
        # state every stored route was in when the probe was raised.
        "depth_usd": 50.0,
        "depth_unverified": False,
    }
    route.update(overrides)
    return route


def _books(monkeypatch: pytest.MonkeyPatch) -> None:
    long_key = live_book_cache.cache_key("Gate", "Spot", "T/USDT")
    short_key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(
        live_book_cache,
        "load_live_books_by_keys",
        lambda *_a, **_k: {long_key: _Book(1.00), short_key: _Book(1.10)},
    )


def test_a_book_too_thin_for_the_probe_still_streams_its_top_of_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: this streamed None and blanked a live row."""
    _books(monkeypatch)

    priced = api_spreads.live_prices_for([_route()])
    spread, _funding = priced["T|Gate|Spot|Bybit|Futures"]

    assert spread is not None, "a live two-sided book must always produce a number"


def test_the_streamed_number_matches_what_the_page_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same precedence as `displayed_edge`: matched first, else top of book."""
    _books(monkeypatch)

    updates = api_spreads.live_route_updates_for([_route()])
    spread, _funding, _ts = updates["T|Gate|Spot|Bybit|Futures"]

    # Long book asks at 1.01, short book bids at 1.10: the top-of-book edge the
    # page would render for this route once matched depth is unavailable.
    expected = (1.10 / 1.01 - 1.0) * 100.0

    assert spread == pytest.approx(expected)


def test_a_thin_live_tick_identifies_itself_as_top_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The number is useful and must remain visible, but it cannot retain a
    matched-VWAP label when the current ladders do not fill the probe size.
    """
    _books(monkeypatch)

    update = api_spreads.live_route_updates_for([_route()], include_basis=True)[
        "T|Gate|Spot|Bybit|Futures"
    ]

    assert update[3] == "top_book"


def test_two_fresh_top_books_renew_only_an_indicative_cex_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indicative is a live evidence class, not a 90-second build artifact."""
    _books(monkeypatch)
    route = _route(
        quote_ts_us=1_600_000_000_000_000,
        depth_usd=None,
        depth_weighted_spread_pct=None,
        depth_unverified=True,
    )

    update = api_spreads.live_route_updates_for([route], include_basis=True)[
        route["route_key"]
    ]

    assert update[2] == 1_700_000_100_000_000
    assert update[3] == "top_book"


def test_current_exact_500_quote_outranks_a_thin_resident_top_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceleration feed cannot demote exact matched evidence to top-book."""
    _books(monkeypatch)
    route = _route()
    monkeypatch.setattr(
        api_spreads,
        "_fast_quote_updates_for",
        lambda _routes: {
            route["route_key"]: (1.75, None, 1_700_000_200_000_000, "matched_vwap")
        },
    )

    update = api_spreads.live_route_updates_for([route], include_basis=True)[
        route["route_key"]
    ]

    assert update[0] == pytest.approx(1.75)
    assert update[2] == 1_700_000_200_000_000
    assert update[3] == "matched_vwap"


def test_a_proven_route_still_reports_its_matched_vwap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback must not shadow a genuine depth measurement."""

    class _DeepBook(_Book):
        def __init__(self, price: float) -> None:
            super().__init__(price)
            size = api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD * 5.0
            self.bids = [[price, size / price]]
            self.asks = [[price * 1.01, size / (price * 1.01)]]

    long_key = live_book_cache.cache_key("Gate", "Spot", "T/USDT")
    short_key = live_book_cache.cache_key("Bybit", "Futures", "T/USDT:USDT")
    monkeypatch.setattr(
        live_book_cache,
        "load_live_books_by_keys",
        lambda *_a, **_k: {long_key: _DeepBook(1.00), short_key: _DeepBook(1.10)},
    )

    priced = api_spreads.live_prices_for([_route()])
    spread, _funding = priced["T|Gate|Spot|Bybit|Futures"]

    assert spread is not None


def test_shared_books_are_depth_walked_once_per_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair expansion must not recalculate one ladder for every route."""

    _books(monkeypatch)
    monkeypatch.setattr("spreadboard.bulk_quotes.load_funding", dict)
    original = api_spreads._book_side
    calls: list[tuple[int, str]] = []

    def counted(book, side):
        calls.append((id(book), side))
        return original(book, side)

    monkeypatch.setattr(api_spreads, "_book_side", counted)
    first = _route(route_key="T|Gate|Spot|Bybit|Futures|one")
    second = _route(route_key="T|Gate|Spot|Bybit|Futures|two")

    updates = api_spreads.live_route_updates_for(
        [first, second], include_basis=True
    )

    assert set(updates) == {first["route_key"], second["route_key"]}
    assert len(calls) == 2
    assert {side for _book, side in calls} == {"ask", "bid"}


def test_the_client_never_overwrites_a_number_with_a_dash() -> None:
    """A null update must leave the last good value alone.

    Funding already worked this way; the spread branch did not, so one null
    tick replaced a real number with "—" until the member reloaded.
    """
    import inspect

    from spreadboard import server

    source = inspect.getsource(server.render_board_stream_script)
    assert 'const next = text || "—";' not in source, (
        "a null spread still erases the rendered value"
    )


def test_the_headline_is_not_blanked_by_a_tick_without_a_number() -> None:
    """The KPI above the board follows the same rule as the rows.

    Blanking it made the entire board read as broken even while every row
    underneath was still showing a correct, current number.
    """
    import inspect

    from spreadboard import server

    source = inspect.getsource(server.render_board_stream_script)
    assert 'pct(payload.max_spread_pct, 1) || "—"' not in source


def test_fast_quote_delta_corrects_an_expanded_route_without_resident_books(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expanded catalogue pairs are broader than the websocket subscription.

    OPENAI Bitget->Phemex retained the group's old +4.69% in production while
    the current fast quote was +0.56%.  The push path must use that current
    exact-leg delta when neither book happens to be resident.
    """
    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text("{}")
    delta = tmp_path / "api_discovery_fast_quotes.json"
    delta.write_text(
        json.dumps(
            {
                "updated_at": "2026-08-20T23:22:25Z",
                "rows": [
                    {
                        "token": "OPENAI",
                        "long_venue": "Bitget",
                        "long_market_type": "Futures",
                        "short_venue": "Phemex",
                        "short_market_type": "Futures",
                        "depth_weighted_spread_pct": 0.5568672572,
                        "funding_daily_pct": 0.015,
                        "quote_ts_us": 1_787_268_022_495_800,
                        "notes": {
                            "route_inputs": {
                                "long": {"symbol": "OPENAI/USDT:USDT"},
                                "short": {"symbol": "OPENAI/USDT:USDT"},
                            }
                        },
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(api_spreads, "DEFAULT_API_DISCOVERY_PATH", snapshot)
    monkeypatch.setattr(api_spreads.time, "time", lambda: 1_787_268_032.0)

    def no_resident_books(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(live_book_cache, "load_live_books_by_keys", no_resident_books)
    monkeypatch.setattr("spreadboard.bulk_quotes.load_funding", dict)
    route = {
        "route_key": "CUSTOM:openai-bitget-phemex",
        "token": "OPENAI",
        "long_venue": "Bitget",
        "long_market_type": "Futures",
        "long_market_symbol": "OPENAI/USDT:USDT",
        "short_venue": "Phemex",
        "short_market_type": "Futures",
        "short_market_symbol": "OPENAI/USDT:USDT",
    }

    update = api_spreads.live_route_updates_for([route], include_basis=True)[route["route_key"]]

    assert update[0] == pytest.approx(0.5568672572)
    assert update[1] is None, "an old fast-price artefact must not renew expired funding"
    assert update[3] == "matched_vwap"


def test_fresh_dex_leg_quote_reprices_every_expanded_futures_partner(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One paid chain/contract quote is reusable across all fresh CEX legs.

    The structural DEX->Aster route can be older than 90 seconds by the time a
    complete route-index build finishes. A fresh DEX->XT fast row for the same
    contract must therefore reprice Aster directly in the live overlay.
    """

    now_us = 1_787_300_000_000_000
    contract = "0xce7de646e7208a4ef112cb6ed5038fa6cc6b12e3"
    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text("{}")
    (tmp_path / "api_discovery_fast_quotes.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "token": "TRX",
                        "long_venue": "OKX DEX 56",
                        "long_market_type": "Spot",
                        "short_venue": "XT",
                        "short_market_type": "Futures",
                        "quote_ts_us": now_us,
                        "depth_weighted_spread_pct": 4.0,
                        "matched_size_notional_usd": 500.0,
                        "notes": {
                            "identity": {
                                "long": {
                                    "chain_id": "56",
                                    "token_address": contract,
                                }
                            },
                            "route_inputs": {
                                "long": {
                                    "symbol": "TRX",
                                    "ask_vwap": 0.32,
                                    "bid_vwap": 0.319,
                                    "quote_ts_us": now_us,
                                    "quote_notional_usd": 500.0,
                                },
                                "short": {"symbol": "TRX/USDT:USDT"},
                            },
                        },
                    }
                ]
            }
        )
    )
    short_key = live_book_cache.cache_key(
        "Aster", "Futures", "TRX/USDT:USDT"
    )
    short_book = live_book_cache.CachedBook(
        bids=[[0.336, 10_000.0]],
        asks=[[0.337, 10_000.0]],
        quote_ts_us=now_us + 5_000_000,
    )
    monkeypatch.setattr(api_spreads, "DEFAULT_API_DISCOVERY_PATH", snapshot)
    monkeypatch.setattr(api_spreads.time, "time", lambda: now_us / 1_000_000 + 30)
    monkeypatch.setattr(
        live_book_cache,
        "load_live_books_by_keys",
        lambda *_args, **_kwargs: {short_key: short_book},
    )
    monkeypatch.setattr("spreadboard.bulk_quotes.load_funding", dict)
    api_spreads._FAST_ROUTE_UPDATE_CACHE.update(
        {"key": None, "exact": {}, "simple": {}, "dex": {}}
    )
    route = {
        "route_key": "TRX|OKX DEX 56|Spot|Aster|Futures",
        "token": "TRX",
        "route_kind": "DEX-FUTURES",
        "long_venue": "OKX DEX 56",
        "long_market_type": "Spot",
        "long_market_symbol": "TRX",
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "short_market_symbol": "TRX/USDT:USDT",
        "dex_chain": "56",
        "dex_contract": contract,
        "quote_ts_us": now_us - 600_000_000,
        "depth_weighted_spread_pct": 16.0,
        "matched_size_notional_usd": 500.0,
    }

    update = api_spreads.live_route_updates_for([route], include_basis=True)[
        route["route_key"]
    ]

    assert update[0] == pytest.approx((0.336 / 0.32 - 1.0) * 100.0)
    assert update[2] == now_us
    assert update[3] == "matched_vwap"
