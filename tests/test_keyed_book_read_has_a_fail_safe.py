"""A partial book read must not reclassify priced routes as funding-only.

`live_route_updates_for` loads books through `load_live_books_by_keys` ->
`get_many`, and whatever that single SQLite read returns becomes truth. A route
whose book is missing from the result falls to a funding-only tuple.

`_live_books()` guards the identical hazard about 350 lines earlier and its
comment names it: "A venue-sized writer transaction can briefly leave a reader
with an incomplete generation." The path feeding the live route universe never
got that protection.

Measured on production: the universe pinned at 128,848 routes and the book store
steady at ~26,000, while `current_priced_route_count` swung 332 to 92,299 with
`funding_only_route_count` moving in exact opposition and the total constant.
Routes reclassified, not lost. Staleness is ruled out -- the window is >=90s
against a 2-3s pass.

Each retained book is still bounded by its OWN `quote_ts_us`, so this cannot
widen the freshness contract; it only stops a momentary read gap from emptying
the board.
"""

from __future__ import annotations

import time

from spreadboard import api_spreads, bulk_quotes, live_book_cache


def _book(price: float, *, age_seconds: float = 0.0) -> live_book_cache.CachedBook:
    return live_book_cache.CachedBook(
        bids=[[price, 100_000.0]],
        asks=[[price * 1.001, 100_000.0]],
        quote_ts_us=int((time.time() - age_seconds) * 1_000_000),
    )


def _route() -> dict:
    return {
        "route_key": "GUA|Gate|Futures|Mexc|Futures",
        "token": "GUA",
        "route_kind": "FUTURES",
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "long_market_symbol": "GUA/USDT:USDT",
        "short_venue": "Mexc",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
    }


def _keys(route: dict) -> tuple[str, str]:
    return (
        live_book_cache.cache_key(
            route["long_venue"], route["long_market_type"], route["long_market_symbol"]
        ),
        live_book_cache.cache_key(
            route["short_venue"], route["short_market_type"], route["short_market_symbol"]
        ),
    )


def _priced(update: tuple) -> bool:
    """A priced tuple carries a spread and a quote timestamp; funding-only does not."""

    return bool(update) and update[0] is not None and len(update) > 2 and update[2] is not None


def _install(monkeypatch, responses: list[dict]) -> None:
    api_spreads.reset_keyed_book_fallback()
    calls = {"n": 0}

    def fake_load(_keys, **_kwargs):
        index = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[index]

    monkeypatch.setattr(live_book_cache, "load_live_books_by_keys", fake_load)
    monkeypatch.setattr(bulk_quotes, "load_funding", dict)
    monkeypatch.setattr(api_spreads, "_fast_quote_updates_for", lambda _routes: {})


def test_a_full_read_prices_the_route(monkeypatch) -> None:
    route = _route()
    long_key, short_key = _keys(route)
    _install(monkeypatch, [{long_key: _book(1.0), short_key: _book(1.02)}])

    updates = api_spreads.live_route_updates_for([route])

    assert _priced(updates[route["route_key"]])


def test_a_partial_read_keeps_the_route_priced(monkeypatch) -> None:
    """This is the swing: one leg missing turned the whole board funding-only."""

    route = _route()
    long_key, short_key = _keys(route)
    _install(
        monkeypatch,
        [
            {long_key: _book(1.0), short_key: _book(1.02)},
            {long_key: _book(1.0)},  # the writer transaction hid the short leg
        ],
    )

    api_spreads.live_route_updates_for([route])
    updates = api_spreads.live_route_updates_for([route])

    assert _priced(updates[route["route_key"]]), (
        "a momentary read gap reclassified a priced route as funding-only"
    )


def test_a_retained_book_still_expires_on_its_own_timestamp(monkeypatch) -> None:
    """The fail-safe must not widen the freshness contract."""

    route = _route()
    long_key, short_key = _keys(route)
    stale = api_spreads.LIVE_BOOK_MAX_AGE_SECONDS + 5
    _install(
        monkeypatch,
        [
            {long_key: _book(1.0), short_key: _book(1.02, age_seconds=stale)},
            {long_key: _book(1.0)},
        ],
    )

    api_spreads.live_route_updates_for([route])
    updates = api_spreads.live_route_updates_for([route])

    assert not _priced(updates[route["route_key"]])


def test_a_fresh_read_wins_over_the_retained_copy() -> None:
    """Retention is a floor, never a ceiling: a current book must take priority.

    Asserted on the merge itself rather than through a spread, so it states the
    priority rule directly instead of depending on how one number happens to
    respond.
    """

    api_spreads.reset_keyed_book_fallback()
    key = live_book_cache.cache_key("Gate", "Futures", "GUA/USDT:USDT")
    stale_but_current, fresh = _book(1.0), _book(2.0)

    api_spreads._with_retained_books({key: stale_but_current}, {key})
    merged = api_spreads._with_retained_books({key: fresh}, {key})

    assert merged[key] is fresh


def test_a_key_nobody_asked_for_is_not_retained() -> None:
    """Retention is scoped to the working set, not every book ever seen."""

    api_spreads.reset_keyed_book_fallback()
    wanted = live_book_cache.cache_key("Gate", "Futures", "GUA/USDT:USDT")
    other = live_book_cache.cache_key("Mexc", "Futures", "OTHER/USDT:USDT")

    api_spreads._with_retained_books({wanted: _book(1.0), other: _book(9.0)}, {wanted})
    merged = api_spreads._with_retained_books({}, {wanted, other})

    assert wanted in merged
    assert other not in merged
