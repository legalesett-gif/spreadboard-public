"""One asset, two tickers: admit per MARKET, on a live price, or not at all.

Venues disagree about tokenised-equity tickers -- Mexc lists Apple as
AAPLSTOCK while twelve other venues list AAPL -- and the catalogue keeps those
as two tokens with NO venue in common, so no cross-venue route for them can be
built. Measured: 221 assets, 1,935 markets stranded, and the comparator reports
MSTRSTOCK whitebit -> mexc as an unmatched alias because of it.

Two weaker designs were tried against production data first and both failed:

* "the first book of each spelling agrees" merged JPMorgan with an unrelated
  crypto, because whichever market answers first decides the price;
* "each spelling's own prices agree" still merged them, and says nothing about
  a spelling's UNPRICED markets, which would join silently and be priced later.

So admission is per market, requires a live price, and compares that market
against this token's own band.
"""

from __future__ import annotations

from spreadboard import catalog_pairs, live_book_cache


def _market(token: str, venue: str):
    return {
        "token": token,
        "venue": venue,
        "market_type": "Futures",
        "symbol": f"{token}/USDT:USDT",
    }


def _books(monkeypatch, prices: dict[tuple[str, str], float | None]):
    def _load(venue, market_type, symbol, **_kw):
        price = prices.get((str(venue), str(symbol).split("/")[0].upper()))
        if price is None:
            return None
        return live_book_cache.CachedBook(
            bids=[[price * 0.999, 10.0]], asks=[[price * 1.001, 10.0]],
            quote_ts_us=1, source="test",
        )

    monkeypatch.setattr(live_book_cache, "load_live_book", _load)


def test_a_tokenised_equity_admits_its_plain_ticker_markets(monkeypatch) -> None:
    catalog = {"markets": [_market("AAPLSTOCK", "Mexc"), _market("AAPL", "WhiteBIT")]}
    _books(monkeypatch, {("Mexc", "AAPLSTOCK"): 232.10, ("WhiteBIT", "AAPL"): 232.55})

    got = catalog_pairs._other_spelling_markets(
        "AAPLSTOCK", catalog, [_market("AAPLSTOCK", "Mexc")], 1800.0
    )

    assert [m["venue"] for m in got] == ["WhiteBIT"]


def test_an_unrelated_crypto_under_the_same_ticker_is_refused(monkeypatch) -> None:
    """Citigroup at $134 against a token called C at $0.06.

    On production all 18 of CSTOCK's crypto-side markets are rejected this way.
    """

    catalog = {"markets": [_market("CSTOCK", "Mexc"), _market("C", "Aster")]}
    _books(monkeypatch, {("Mexc", "CSTOCK"): 134.49, ("Aster", "C"): 0.0624})

    got = catalog_pairs._other_spelling_markets(
        "CSTOCK", catalog, [_market("CSTOCK", "Mexc")], 1800.0
    )

    assert got == []


def test_only_the_agreeing_markets_of_a_mixed_ticker_are_admitted(monkeypatch) -> None:
    """The case that broke both earlier designs.

    A ticker can carry the equity on one venue and an unrelated token on
    another. Judging the TOKEN merges both; judging each MARKET keeps only the
    one that agrees.
    """

    catalog = {
        "markets": [
            _market("HDSTOCK", "Mexc"),
            _market("HD", "WhiteBIT"),   # the equity
            _market("HD", "Aster"),      # something else entirely
        ]
    }
    _books(monkeypatch, {
        ("Mexc", "HDSTOCK"): 332.9,
        ("WhiteBIT", "HD"): 333.4,
        ("Aster", "HD"): 201.0,
    })

    got = catalog_pairs._other_spelling_markets(
        "HDSTOCK", catalog, [_market("HDSTOCK", "Mexc")], 1800.0
    )

    assert [m["venue"] for m in got] == ["WhiteBIT"], (
        "the disagreeing market must not ride in on the agreeing one"
    )


def test_an_unpriced_market_never_joins(monkeypatch) -> None:
    """No live price is not evidence of identity; it would be priced later."""

    catalog = {"markets": [_market("AAPLSTOCK", "Mexc"), _market("AAPL", "WhiteBIT")]}
    _books(monkeypatch, {("Mexc", "AAPLSTOCK"): 232.10, ("WhiteBIT", "AAPL"): None})

    got = catalog_pairs._other_spelling_markets(
        "AAPLSTOCK", catalog, [_market("AAPLSTOCK", "Mexc")], 1800.0
    )

    assert got == []


def test_no_price_on_this_token_admits_nothing(monkeypatch) -> None:
    catalog = {"markets": [_market("AAPLSTOCK", "Mexc"), _market("AAPL", "WhiteBIT")]}
    _books(monkeypatch, {("Mexc", "AAPLSTOCK"): None, ("WhiteBIT", "AAPL"): 232.55})

    got = catalog_pairs._other_spelling_markets(
        "AAPLSTOCK", catalog, [_market("AAPLSTOCK", "Mexc")], 1800.0
    )

    assert got == []


def test_a_token_with_no_other_spelling_admits_nothing(monkeypatch) -> None:
    catalog = {"markets": [_market("BTC", "Binance")]}
    _books(monkeypatch, {("Binance", "BTC"): 60000.0})

    assert catalog_pairs._other_spelling_markets(
        "BTC", catalog, [_market("BTC", "Binance")], 1800.0
    ) == []


def test_the_identity_gate_is_tighter_than_the_pair_guard() -> None:
    assert catalog_pairs.SAME_ASSET_PRICE_RATIO < catalog_pairs.MAX_PRICE_RATIO
    assert catalog_pairs.SAME_ASSET_PRICE_RATIO <= 1.05


def _book(price: float):
    return live_book_cache.CachedBook(
        bids=[[price * 0.999, 10.0]], asks=[[price * 1.001, 10.0]],
        quote_ts_us=1, source="test",
    )


def test_the_bulk_path_merges_spellings_too() -> None:
    """`for_token` alone fixed the token page and nothing the board reads.

    The live route index is built by `for_tokens`, so MSTRSTOCK showed 132
    routes when searched and 0 in the index -- and the comparator kept
    reporting it as comparator_display_alias_unmatched.
    """

    mkts = {
        "AAPLSTOCK": {("Mexc", "Futures", "AAPLSTOCK/USDT:USDT"): {"token": "AAPLSTOCK"}},
        "AAPL": {("WhiteBIT", "Futures", "AAPL/USDT:USDT"): {"token": "AAPL"}},
    }
    books = {
        live_book_cache.cache_key("Mexc", "Futures", "AAPLSTOCK/USDT:USDT"): _book(232.10),
        live_book_cache.cache_key("WhiteBIT", "Futures", "AAPL/USDT:USDT"): _book(232.55),
    }

    catalog_pairs._admit_other_spellings_bulk(mkts, books)

    assert len(mkts["AAPLSTOCK"]) == 2, "the plain ticker's market must join"
    assert len(mkts["AAPL"]) == 2, "and the merge is symmetric"


def test_the_bulk_path_refuses_a_collision() -> None:
    """Citigroup against a crypto called C, 2,155x apart."""

    mkts = {
        "CSTOCK": {("Mexc", "Futures", "CSTOCK/USDT:USDT"): {"token": "CSTOCK"}},
        "C": {("Aster", "Futures", "C/USDT:USDT"): {"token": "C"}},
    }
    books = {
        live_book_cache.cache_key("Mexc", "Futures", "CSTOCK/USDT:USDT"): _book(134.49),
        live_book_cache.cache_key("Aster", "Futures", "C/USDT:USDT"): _book(0.0624),
    }

    catalog_pairs._admit_other_spellings_bulk(mkts, books)

    assert len(mkts["CSTOCK"]) == 1
    assert len(mkts["C"]) == 1


def test_the_bulk_merge_does_not_compound_across_spellings() -> None:
    """Reading a snapshot keeps one spelling's new markets out of the other's band."""

    mkts = {
        "XSTOCK": {("Mexc", "Futures", "XSTOCK/USDT:USDT"): {"token": "XSTOCK"}},
        "X": {
            ("WhiteBIT", "Futures", "X/USDT:USDT"): {"token": "X"},
            ("Aster", "Futures", "X/USDT:USDT"): {"token": "X"},
        },
    }
    books = {
        live_book_cache.cache_key("Mexc", "Futures", "XSTOCK/USDT:USDT"): _book(100.0),
        live_book_cache.cache_key("WhiteBIT", "Futures", "X/USDT:USDT"): _book(100.5),
        live_book_cache.cache_key("Aster", "Futures", "X/USDT:USDT"): _book(10.0),
    }

    catalog_pairs._admit_other_spellings_bulk(mkts, books)

    assert len(mkts["XSTOCK"]) == 2, "only the agreeing market joins"
    keys = {k[0] for k in mkts["XSTOCK"]}
    assert "Aster" not in keys


def test_the_bulk_expansion_actually_calls_the_merge() -> None:
    """Guard the wiring: the helper passes on its own whether or not it runs.

    That is exactly how the first version of this fix shipped half-done -- the
    single-token path was wired, the bulk path that builds the index was not,
    and every test still passed.
    """

    import inspect

    source = inspect.getsource(catalog_pairs.for_tokens)
    assert "_admit_other_spellings_bulk(" in source, (
        "for_tokens builds the live route index; it must merge spellings too"
    )


def test_a_quiet_book_can_still_prove_identity(monkeypatch) -> None:
    """Identity is not a price claim, so it must not use the pricing window.

    SNDK on WhiteBIT is real and quotes at 1462.40, but that book missed the
    tight pricing cut at index-build time -- so SNDKSTOCK mexc->whitebit was
    absent in every comparator sample. A slightly older book still proves two
    tickers are one instrument; it is never used to price anything.
    """

    mkts = {
        "SNDKSTOCK": {("Mexc", "Futures", "SNDKSTOCK/USDT:USDT"): {"token": "SNDKSTOCK"}},
        "SNDK": {("WhiteBIT", "Futures", "SNDK/USDT:USDT"): {"token": "SNDK"}},
    }
    # Only the STOCK side is in the tight pricing window.
    books = {
        live_book_cache.cache_key("Mexc", "Futures", "SNDKSTOCK/USDT:USDT"): _book(1462.49),
    }
    monkeypatch.setattr(
        live_book_cache, "load_live_book",
        lambda venue, mtype, symbol, **_kw: _book(1462.40) if venue == "WhiteBIT" else None,
    )

    catalog_pairs._admit_other_spellings_bulk(mkts, books)

    assert len(mkts["SNDKSTOCK"]) == 2, "a quiet but real market must still merge"


def test_a_quiet_book_that_disagrees_is_still_refused(monkeypatch) -> None:
    mkts = {
        "CSTOCK": {("Mexc", "Futures", "CSTOCK/USDT:USDT"): {"token": "CSTOCK"}},
        "C": {("Aster", "Futures", "C/USDT:USDT"): {"token": "C"}},
    }
    books = {live_book_cache.cache_key("Mexc", "Futures", "CSTOCK/USDT:USDT"): _book(134.49)}
    monkeypatch.setattr(
        live_book_cache, "load_live_book",
        lambda venue, mtype, symbol, **_kw: _book(0.0624) if venue == "Aster" else None,
    )

    catalog_pairs._admit_other_spellings_bulk(mkts, books)

    assert len(mkts["CSTOCK"]) == 1
