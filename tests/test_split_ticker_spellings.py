"""One asset, two tickers: merge it only when the books agree on the price.

Tokenised equities carry different tickers depending on venue -- Mexc lists
Apple as AAPLSTOCK while twelve other venues list AAPL -- and the catalogue
kept those as two tokens with NO venue in common, so no cross-venue route for
them could ever be built. Measured on production: 221 such assets, 1,935
markets stranded, and the external comparator reported MSTRSTOCK whitebit ->
mexc as an unmatched alias because of it.

Stripping the suffix blindly would be far worse than the gap. 18 of the 221
are ticker COLLISIONS with unrelated crypto, and three of those sit INSIDE the
3x mirage guard:

    HDSTOCK  $332.9 vs HD  $201.0   1.66x
    JPMSTOCK $357.9 vs JPM $1063.1  2.97x
    QNTSTOCK $50.7  vs QNT $61.8    1.22x

Those would have printed plausible 22-66% spreads that nothing downstream
catches. The price gate is what separates the two cases.
"""

from __future__ import annotations

from spreadboard import catalog_pairs, live_book_cache


def _catalog(*entries):
    return {
        "markets": [
            {"token": t, "venue": v, "market_type": "Futures", "symbol": f"{t}/USDT:USDT"}
            for t, v in entries
        ]
    }


def _books(monkeypatch, prices: dict[str, float]):
    def _load(venue, market_type, symbol, **_kw):
        token = str(symbol).split("/")[0].upper()
        price = prices.get(token)
        if price is None:
            return None
        return live_book_cache.CachedBook(
            bids=[[price * 0.999, 10.0]],
            asks=[[price * 1.001, 10.0]],
            quote_ts_us=1,
            source="test",
        )

    monkeypatch.setattr(live_book_cache, "load_live_book", _load)


def test_a_tokenised_equity_merges_with_its_plain_ticker(monkeypatch) -> None:
    """AAPLSTOCK on one venue and AAPL on another are one asset."""

    _books(monkeypatch, {"AAPLSTOCK": 232.10, "AAPL": 232.55})
    catalog = _catalog(("AAPLSTOCK", "Mexc"), ("AAPL", "WhiteBIT"))

    assert catalog_pairs._same_asset_spellings("AAPL", catalog) == {"AAPL", "AAPLSTOCK"}
    assert catalog_pairs._same_asset_spellings("AAPLSTOCK", catalog) == {
        "AAPL",
        "AAPLSTOCK",
    }


def test_a_collision_inside_the_mirage_guard_is_still_refused(monkeypatch) -> None:
    """JPMorgan against a crypto called JPM: 2.97x, under the 3x guard.

    This is the case that makes the price gate necessary rather than merely
    tidy. Nothing downstream would have rejected it.
    """

    _books(monkeypatch, {"JPMSTOCK": 357.9, "JPM": 1063.125})
    catalog = _catalog(("JPMSTOCK", "Mexc"), ("JPM", "Aster"))

    assert catalog_pairs._same_asset_spellings("JPMSTOCK", catalog) == {"JPMSTOCK"}


def test_a_far_apart_collision_is_refused(monkeypatch) -> None:
    """Brinker at $231 against a token called EAT at $0.0038."""

    _books(monkeypatch, {"EATSTOCK": 231.58, "EAT": 0.0038})
    catalog = _catalog(("EATSTOCK", "Mexc"), ("EAT", "Kraken"))

    assert catalog_pairs._same_asset_spellings("EATSTOCK", catalog) == {"EATSTOCK"}


def test_no_book_on_either_side_never_merges(monkeypatch) -> None:
    """Absence of a price is not evidence that two tickers are one asset."""

    _books(monkeypatch, {"AAPL": 232.55})
    catalog = _catalog(("AAPLSTOCK", "Mexc"), ("AAPL", "WhiteBIT"))

    assert catalog_pairs._same_asset_spellings("AAPL", catalog) == {"AAPL"}


def test_a_token_with_no_other_spelling_is_untouched(monkeypatch) -> None:
    _books(monkeypatch, {"BTC": 60000.0})
    catalog = _catalog(("BTC", "Binance"))

    assert catalog_pairs._same_asset_spellings("BTC", catalog) == {"BTC"}


def test_the_gate_is_tighter_than_the_mirage_guard() -> None:
    """A pair guard and an identity test are different questions.

    MAX_PRICE_RATIO rejects an implausible PAIR. This decides whether two
    tickers ARE one instrument, where a wrong answer manufactures the spread
    instead of failing to reject it.
    """

    assert catalog_pairs.SAME_ASSET_PRICE_RATIO < catalog_pairs.MAX_PRICE_RATIO
    assert catalog_pairs.SAME_ASSET_PRICE_RATIO <= 1.05
