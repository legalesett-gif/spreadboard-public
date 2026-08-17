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
        store=_Store(), fetch_spot=lambda: SPOT_PAYLOAD, fetch_futures=boom
    )

    assert count == 2
    assert {b["market_type"] for b in written} == {"Spot"}
