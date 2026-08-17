"""Ourbit prices, without a CCXT adapter to lean on.

Ourbit already supplied funding rates through a native endpoint but never
prices, because the bulk sweep iterates VENUE_IDS and CCXT has no Ourbit
adapter. The visible cost was routes that simply could not exist: the reference
product headlines UNITREE at 4.07% on Mexc->Ourbit while our board shows 0.000%
for the same token, because our widest-spread selection can only choose among
venues we actually carry.

Symbols are the trap. Ourbit spot returns "BTCUSDT" with no separator and
futures returns "BTC_USDT"; the board keys everything as "BTC/USDT" for spot and
"BTC/USDT:USDT" for a linear perpetual. Get that wrong and the legs never match,
which looks exactly like the venue having no routes.
"""

from __future__ import annotations

import pytest

from spreadboard import ourbit_quotes

SPOT_PAYLOAD = [
    {"symbol": "BTCUSDT", "bidPrice": "63567.2", "bidQty": "1.5",
     "askPrice": "63567.3", "askQty": "2.0"},
    {"symbol": "UNITREEUSDT", "bidPrice": "98.25", "bidQty": "40",
     "askPrice": "98.40", "askQty": "35"},
    {"symbol": "BROKENUSDT", "bidPrice": "0", "bidQty": "0",
     "askPrice": "0", "askQty": "0"},
]

FUTURES_PAYLOAD = {
    "success": True,
    "code": 0,
    "data": [
        {"symbol": "BTC_USDT", "bid1": 63567.2, "ask1": 63567.3,
         "volume24": 533693537, "lastPrice": 63567.3},
        {"symbol": "UNITREE_USDT", "bid1": 102.20, "ask1": 102.30,
         "volume24": 123456, "lastPrice": 102.25},
        {"symbol": "NOBID_USDT", "bid1": 0, "ask1": 0, "volume24": 0},
    ],
}


# --------------------------------------------------------------------------
# Symbol translation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTCUSDT", "BTC/USDT"),
        ("UNITREEUSDT", "UNITREE/USDT"),
        ("ETHUSDC", "ETH/USDC"),
    ],
)
def test_spot_symbols_become_board_symbols(raw, expected) -> None:
    assert ourbit_quotes.spot_symbol(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTC_USDT", "BTC/USDT:USDT"),
        ("UNITREE_USDT", "UNITREE/USDT:USDT"),
    ],
)
def test_futures_symbols_become_linear_perpetuals(raw, expected) -> None:
    assert ourbit_quotes.futures_symbol(raw) == expected


def test_an_unrecognised_quote_currency_is_refused_rather_than_guessed() -> None:
    """A mis-split symbol silently mismatches every leg; refuse instead."""
    assert ourbit_quotes.spot_symbol("SOMETHINGODD") is None
    assert ourbit_quotes.futures_symbol("NOSEPARATOR") is None


# --------------------------------------------------------------------------
# Building books
# --------------------------------------------------------------------------


def test_spot_books_carry_price_and_size() -> None:
    books = ourbit_quotes.spot_books(SPOT_PAYLOAD, now_us=1_000)

    by_symbol = {b["symbol"]: b for b in books}
    assert by_symbol["BTC/USDT"]["bids"] == [[63567.2, 1.5]]
    assert by_symbol["BTC/USDT"]["asks"] == [[63567.3, 2.0]]
    assert all(b["venue"] == "Ourbit" for b in books)
    assert all(b["market_type"] == "Spot" for b in books)


def test_futures_books_use_bid1_and_ask1() -> None:
    books = ourbit_quotes.futures_books(FUTURES_PAYLOAD, now_us=1_000)

    by_symbol = {b["symbol"]: b for b in books}
    assert by_symbol["UNITREE/USDT:USDT"]["bids"][0][0] == 102.20
    assert by_symbol["UNITREE/USDT:USDT"]["asks"][0][0] == 102.30
    assert all(b["market_type"] == "Futures" for b in books)


def test_a_zero_or_missing_quote_is_dropped_not_published() -> None:
    """A zero price would read as a 100% spread against any real venue."""
    spot = {b["symbol"] for b in ourbit_quotes.spot_books(SPOT_PAYLOAD, now_us=1)}
    fut = {b["symbol"] for b in ourbit_quotes.futures_books(FUTURES_PAYLOAD, now_us=1)}

    assert "BROKEN/USDT" not in spot
    assert "NOBID/USDT:USDT" not in fut


def test_books_are_marked_as_ticker_depth_not_real_books() -> None:
    """One level from a ticker must never be mistaken for L2 depth."""
    for book in ourbit_quotes.spot_books(SPOT_PAYLOAD, now_us=1):
        assert book["source"] == "bulk_ticker"
        assert len(book["bids"]) == 1 and len(book["asks"]) == 1


def test_a_malformed_payload_yields_nothing_rather_than_raising() -> None:
    """One bad venue response must never stop the sweep."""
    assert ourbit_quotes.spot_books("not a list", now_us=1) == []
    assert ourbit_quotes.futures_books({"data": "nonsense"}, now_us=1) == []
    assert ourbit_quotes.futures_books({}, now_us=1) == []


def test_an_unsuccessful_futures_response_is_ignored() -> None:
    assert ourbit_quotes.futures_books(
        {"success": False, "code": 500, "data": FUTURES_PAYLOAD["data"]}, now_us=1
    ) == []


def test_the_sweep_writes_every_good_book_to_the_store() -> None:
    written = []

    class _Store:
        def put_many(self, books):
            written.extend(books)
            return len(books)

    count = ourbit_quotes.sweep(
        store=_Store(),
        fetch_spot=lambda: SPOT_PAYLOAD,
        fetch_futures=lambda: FUTURES_PAYLOAD,
        with_depth=False,  # depth is covered separately and must not hit the network
    )

    assert count == 4  # 2 good spot + 2 good futures
    assert {b["venue"] for b in written} == {"Ourbit"}


def test_one_failing_endpoint_does_not_lose_the_other() -> None:
    """Spot and futures are separate calls; a dead one must not take both."""
    written = []

    class _Store:
        def put_many(self, books):
            written.extend(books)
            return len(books)

    def boom():
        raise RuntimeError("ourbit futures down")

    count = ourbit_quotes.sweep(
        store=_Store(), fetch_spot=lambda: SPOT_PAYLOAD, fetch_futures=boom,
        with_depth=False,
    )

    assert count == 2
    assert {b["market_type"] for b in written} == {"Spot"}


# --------------------------------------------------------------------------
# Real L2 depth, which the contract ticker cannot give
# --------------------------------------------------------------------------

DETAIL_PAYLOAD = {
    "success": True,
    "data": [
        {"symbol": "UNITREE_USDT", "contractSize": 0.01},
        {"symbol": "BTC_USDT", "contractSize": 0.0001},
    ],
}

DEPTH_PAYLOAD = {
    "success": True,
    "data": {
        "asks": [[97.39, 330, 1], [97.40, 1872, 1], [97.41, 486, 1]],
        "bids": [[97.30, 500, 1], [97.29, 900, 1]],
    },
}


def test_contract_sizes_are_read_in_one_call() -> None:
    sizes = ourbit_quotes.contract_sizes(DETAIL_PAYLOAD)

    assert sizes["UNITREE_USDT"] == 0.01
    assert sizes["BTC_USDT"] == 0.0001


def test_depth_levels_are_converted_from_contracts_to_base_units() -> None:
    """330 contracts at 0.01 is 3.3 tokens, not 330. Getting this wrong
    overstates liquidity a hundredfold."""
    book = ourbit_quotes.depth_book("UNITREE_USDT", DEPTH_PAYLOAD, 0.01, now_us=1)

    assert book["symbol"] == "UNITREE/USDT:USDT"
    # 330 contracts x 0.01 lands on 3.3000000000000003 in binary floating point.
    assert book["asks"][0][0] == 97.39
    assert book["asks"][0][1] == pytest.approx(3.3)
    assert book["bids"][0][1] == pytest.approx(5.0)


def test_a_depth_book_keeps_every_level_it_was_given() -> None:
    book = ourbit_quotes.depth_book("UNITREE_USDT", DEPTH_PAYLOAD, 0.01, now_us=1)

    assert len(book["asks"]) == 3
    assert len(book["bids"]) == 2


def test_a_depth_book_is_marked_as_real_depth_not_a_ticker() -> None:
    """This is the one Ourbit source that can honestly answer a size question."""
    book = ourbit_quotes.depth_book("UNITREE_USDT", DEPTH_PAYLOAD, 0.01, now_us=1)

    assert book["source"] == "public_rest_l2"


def test_a_missing_contract_size_is_refused_rather_than_assumed_one() -> None:
    """Assuming 1.0 would report 330 tokens where there are 3.3."""
    assert ourbit_quotes.depth_book("UNITREE_USDT", DEPTH_PAYLOAD, None, now_us=1) is None


def test_an_unsuccessful_depth_response_yields_nothing() -> None:
    assert ourbit_quotes.depth_book(
        "UNITREE_USDT", {"success": False, "data": {}}, 0.01, now_us=1
    ) is None


def test_depth_fetching_is_bounded_so_it_cannot_flood_the_venue() -> None:
    """711 contracts must never mean 711 requests in one sweep."""
    asked = []

    def fake_depth(symbol):
        asked.append(symbol)
        return DEPTH_PAYLOAD

    books = ourbit_quotes.depth_books(
        ["A_USDT", "B_USDT", "C_USDT", "D_USDT"],
        sizes={s: 0.01 for s in ("A_USDT", "B_USDT", "C_USDT", "D_USDT")},
        fetch_depth=fake_depth,
        limit=2,
        now_us=1,
    )

    assert len(asked) == 2
    assert len(books) == 2


def test_one_failing_depth_call_does_not_abandon_the_rest() -> None:
    def flaky(symbol):
        if symbol == "A_USDT":
            raise RuntimeError("timeout")
        return DEPTH_PAYLOAD

    books = ourbit_quotes.depth_books(
        ["A_USDT", "B_USDT"],
        sizes={"A_USDT": 0.01, "B_USDT": 0.01},
        fetch_depth=flaky,
        limit=5,
        now_us=1,
    )

    assert len(books) == 1


def test_successive_sweeps_cover_different_contracts() -> None:
    """A fixed slice would refresh the same forty contracts for ever."""
    ourbit_quotes._DEPTH_CURSOR["index"] = 0
    symbols = [f"S{i}_USDT" for i in range(10)]

    first = ourbit_quotes._depth_rotation(symbols, 4)
    second = ourbit_quotes._depth_rotation(symbols, 4)

    assert first == ["S0_USDT", "S1_USDT", "S2_USDT", "S3_USDT"]
    assert second == ["S4_USDT", "S5_USDT", "S6_USDT", "S7_USDT"]
    assert not set(first) & set(second)


def test_the_rotation_wraps_without_losing_symbols() -> None:
    ourbit_quotes._DEPTH_CURSOR["index"] = 0
    symbols = [f"S{i}_USDT" for i in range(5)]

    ourbit_quotes._depth_rotation(symbols, 4)
    wrapped = ourbit_quotes._depth_rotation(symbols, 4)

    assert len(wrapped) == 4
    assert wrapped[0] == "S4_USDT"


def test_depth_can_be_switched_off_without_losing_ticker_prices() -> None:
    written = []

    class _Store:
        def put_many(self, books):
            written.extend(books); return len(books)

    count = ourbit_quotes.sweep(
        store=_Store(), fetch_spot=lambda: SPOT_PAYLOAD,
        fetch_futures=lambda: FUTURES_PAYLOAD, with_depth=False,
    )

    assert count == 4
    assert all(b["source"] == "bulk_ticker" for b in written)


# --------------------------------------------------------------------------
# Depth goes where it is seen
# --------------------------------------------------------------------------


def test_priority_symbols_are_fetched_before_the_rotation() -> None:
    """Alphabetical order left UNITREE waiting ~28 sweeps for depth it needed
    to form a spread at all. Tokens visible on the board come first."""
    ourbit_quotes._DEPTH_CURSOR["index"] = 0
    every = [f"S{i}_USDT" for i in range(20)] + ["UNITREE_USDT"]

    picked = ourbit_quotes.depth_order(
        every, priority=["UNITREE_USDT", "S5_USDT"], count=4
    )

    assert picked[:2] == ["UNITREE_USDT", "S5_USDT"]
    assert len(picked) == 4


def test_a_priority_symbol_is_not_fetched_twice_in_one_sweep() -> None:
    ourbit_quotes._DEPTH_CURSOR["index"] = 0
    every = ["A_USDT", "B_USDT", "C_USDT"]

    picked = ourbit_quotes.depth_order(every, priority=["A_USDT"], count=3)

    assert len(picked) == len(set(picked))


def test_priority_symbols_unknown_to_the_venue_are_ignored() -> None:
    ourbit_quotes._DEPTH_CURSOR["index"] = 0
    every = ["A_USDT", "B_USDT"]

    picked = ourbit_quotes.depth_order(every, priority=["NOTLISTED_USDT"], count=2)

    assert "NOTLISTED_USDT" not in picked


def test_the_rotation_still_advances_so_nothing_starves() -> None:
    """Priority must not pin the sweep to the same few for ever."""
    ourbit_quotes._DEPTH_CURSOR["index"] = 0
    every = [f"S{i}_USDT" for i in range(10)]

    first = ourbit_quotes.depth_order(every, priority=["S0_USDT"], count=3)
    second = ourbit_quotes.depth_order(every, priority=["S0_USDT"], count=3)

    assert first != second


def test_the_board_priority_never_breaks_the_sweep(monkeypatch) -> None:
    """Priority is an optimisation; a board that will not load must not stop depth."""
    from spreadboard import bulk_quotes

    monkeypatch.setattr(
        bulk_quotes, "api_spreads", None, raising=False
    )
    assert isinstance(bulk_quotes._ourbit_depth_priority(), list)


def test_depth_priority_covers_the_whole_board_not_just_page_one() -> None:
    """A token below the first page could never earn depth, and without depth
    its spread never forms, so it could never climb. UNITREE sat in that loop."""
    import inspect

    from spreadboard import bulk_quotes

    source = inspect.getsource(bulk_quotes._ourbit_depth_priority)
    assert "limit=None" in source
    assert "[:250]" not in source


def test_priority_itself_rotates_so_no_priority_symbol_starves() -> None:
    """Taking the first N of a 250-long priority list starves everything past
    position N for ever. UNITREE was priority #100-odd and never fetched."""
    ourbit_quotes._DEPTH_CURSOR["index"] = 0
    ourbit_quotes._PRIORITY_CURSOR["index"] = 0
    every = [f"P{i}_USDT" for i in range(100)]
    priority = list(every)

    first = ourbit_quotes.depth_order(every, priority=priority, count=10)
    second = ourbit_quotes.depth_order(every, priority=priority, count=10)
    third = ourbit_quotes.depth_order(every, priority=priority, count=10)

    seen = set(first) | set(second) | set(third)
    assert len(seen) == 30, "each sweep must reach different priority symbols"
    assert not set(first) & set(second)


# --------------------------------------------------------------------------
# A one-level ticker must never overwrite a fifty-level book
# --------------------------------------------------------------------------


def test_ticker_books_do_not_overwrite_symbols_that_have_live_depth() -> None:
    """live_books is keyed venue|market_type|symbol, so the ticker sweep and the
    depth sweep write the SAME row. Depth covers 25 symbols a pass out of 711,
    so the next ticker pass was flattening every book outside that slice back to
    one level. public_rest_l2 could never grow past the last slice fetched."""
    written = []

    class _Store:
        def put_many(self, books):
            written.extend(books); return len(books)

    count = ourbit_quotes.sweep(
        store=_Store(),
        fetch_spot=lambda: SPOT_PAYLOAD,
        fetch_futures=lambda: FUTURES_PAYLOAD,
        with_depth=False,
        protected_symbols={"UNITREE/USDT:USDT"},
    )

    symbols = {b["symbol"] for b in written}
    assert "UNITREE/USDT:USDT" not in symbols, "depth was clobbered by a ticker"
    assert "BTC/USDT:USDT" in symbols, "unprotected symbols must still refresh"
    assert count == len(written)


def test_protection_only_covers_futures_not_spot() -> None:
    """Spot tickers carry real sizes and are the only source for spot."""
    written = []

    class _Store:
        def put_many(self, books):
            written.extend(books); return len(books)

    ourbit_quotes.sweep(
        store=_Store(), fetch_spot=lambda: SPOT_PAYLOAD,
        fetch_futures=lambda: FUTURES_PAYLOAD, with_depth=False,
        protected_symbols={"BTC/USDT"},
    )

    assert "BTC/USDT" in {b["symbol"] for b in written}


def test_the_store_reports_which_symbols_already_hold_depth() -> None:
    class _Conn:
        def execute(self, *_a):
            return [("Ourbit|Futures|UNITREE/USDT:USDT",)]

    class _Store:
        path = ":memory:"
        _conn = _Conn()

    found = ourbit_quotes.symbols_with_live_depth(_Store(), max_age_seconds=600)
    assert "UNITREE/USDT:USDT" in found
