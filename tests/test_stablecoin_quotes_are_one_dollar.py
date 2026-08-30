"""A USDC leg and a USDT leg are the same trade, so they must pair.

Requiring the two quote strings to match was not removing a risk -- the
stablecoin basis is a rounding error against the spreads this board ranks --
it was removing whole venues from the product. Hyperliquid quotes every
perpetual in USDC, so no Hyperliquid route could be built at all, and the
comparator then reported those pairs as coverage gaps we had failed to
generate.

What must still be rejected is a dollar against a different currency: pairing
a USDT book against a BTC- or EUR-quoted book is currency risk dressed as a
token spread.
"""

from __future__ import annotations

import pytest

from spreadboard import catalog_pairs, live_book_cache, route_taxonomy
from spreadboard import coverage_reconciliation as cr


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("USDC", "USDT"),
        ("USDT", "USDC"),
        ("USD", "USDT"),
        ("USDT", "USDT"),
        ("FDUSD", "USDC"),
        ("usdc", "UsDt"),
    ],
)
def test_two_dollars_are_interchangeable(left: str, right: str) -> None:
    assert route_taxonomy.quotes_are_interchangeable(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [("USDT", "BTC"), ("USDC", "ETH"), ("USDT", "EUR"), ("BTC", "USDT")],
)
def test_a_dollar_against_another_currency_is_still_refused(
    left: str, right: str
) -> None:
    assert not route_taxonomy.quotes_are_interchangeable(left, right)


def test_an_unknown_quote_does_not_lose_the_route() -> None:
    """A blank metadata field must not silently cost real coverage."""

    assert route_taxonomy.quotes_are_interchangeable("", "USDT")
    assert route_taxonomy.quotes_are_interchangeable("USDT", None)


def _leg(venue: str, quote: str, symbol: str, price: float) -> catalog_pairs.Leg:
    return catalog_pairs.Leg(
        token="STBL",
        venue=venue,
        market_type="Futures",
        symbol=symbol,
        quote=quote,
        contract_size=1.0,
        book=live_book_cache.CachedBook(
            bids=[[price, 1000.0]],
            asks=[[price * 1.001, 1000.0]],
            quote_ts_us=1,
            source="test",
        ),
    )


def test_the_hyperliquid_usdc_perp_now_pairs_against_a_usdt_perp() -> None:
    """The exact production case: STBL/USDC:USDC on Hyperliquid vs Gate USDT.

    The comparator listed this pair; we refused to build it and reported it
    absent.
    """

    reason = catalog_pairs._reject_reason(
        "STBL",
        _leg("Hyperliquid", "USDC", "STBL/USDC:USDC", 1.0),
        _leg("Gate", "USDT", "STBL/USDT:USDT", 1.0),
        rails={},
    )

    assert reason is None, f"the route must now be buildable, got {reason!r}"


def test_a_btc_quoted_leg_is_still_rejected() -> None:
    reason = catalog_pairs._reject_reason(
        "STBL",
        _leg("Bybit", "BTC", "STBL/BTC", 1.0),
        _leg("Gate", "USDT", "STBL/USDT:USDT", 1.0),
        rails={},
    )

    assert reason == "quote_mismatch"


@pytest.mark.parametrize(
    "symbol",
    ["XMR/USDC:USDC", "STBL/USDC:USDC", "NOM/USDT", "BAND/USDT:USDT", "X/USD"],
)
def test_the_comparator_counts_any_dollar_leg(symbol: str) -> None:
    """A built USDC route must be counted as matched, not reported absent."""

    assert cr._usdt_symbol(symbol)


@pytest.mark.parametrize("symbol", ["XMR/BTC", "XMR/ETH:ETH", "", "XMR"])
def test_the_comparator_still_rejects_a_non_dollar_leg(symbol: str) -> None:
    assert not cr._usdt_symbol(symbol)


@pytest.mark.parametrize(
    ("named", "expected"),
    [
        ("Kucoin Futures", "kucoin"),
        ("Kraken Futures", "kraken"),
        ("Kucoin", "kucoin"),
        ("Bybit Perpetual", "bybit"),
        ("Gate.io", "gate"),
        ("AsterDex", "aster"),
    ],
)
def test_a_venue_is_identified_by_its_exchange_not_its_namespace(
    named: str, expected: str
) -> None:
    """"Kraken Futures" and "kraken" are one exchange.

    The identity carries market type separately, so a namespace suffix on the
    venue can only ever cause a false mismatch. This used to need a
    hand-written alias per venue: kucoinfutures was listed, krakenfutures was
    not, and nothing reported the difference.
    """

    assert cr.normalize_venue(named) == expected


def test_okx_dex_is_not_collapsed_into_okx() -> None:
    """DEX is a different venue in the taxonomy, not OKX's other namespace."""

    assert cr.normalize_venue("OKX DEX") != cr.normalize_venue("OKX")


def _leg_at(venue: str, quote: str, symbol: str, top_size: float) -> catalog_pairs.Leg:
    return catalog_pairs.Leg(
        token="XMR",
        venue=venue,
        market_type="Futures",
        symbol=symbol,
        quote=quote,
        contract_size=1.0,
        book=live_book_cache.CachedBook(
            bids=[[100.0, top_size]],
            asks=[[100.1, top_size]],
            quote_ts_us=1,
            source="test",
        ),
    )


def test_a_venue_quoting_both_dollars_enters_the_pairing_once() -> None:
    """Bybit lists XMR as both USDT and USDC; that is one venue, not two.

    Keeping both would double every route through Bybit, differing only in
    which dollar the quote is denominated in. On the live catalogue that turned
    quote unification into +62.6% routes when only +14.1% was new venue reach.
    """

    legs = catalog_pairs._one_leg_per_venue(
        [
            _leg_at("Bybit", "USDT", "XMR/USDT:USDT", 10.0),
            _leg_at("Bybit", "USDC", "XMR/USDC:USDC", 90.0),
            _leg_at("Hyperliquid", "USDC", "XMR/USDC:USDC", 50.0),
        ]
    )

    assert len(legs) == 2, "one leg per venue, plus the USDC-only venue"
    by_venue = {leg.venue: leg for leg in legs}
    assert set(by_venue) == {"Bybit", "Hyperliquid"}
    assert by_venue["Bybit"].quote == "USDC", (
        "the deeper book must win, so the row shown is the fillable one"
    )
    assert by_venue["Hyperliquid"].quote == "USDC", (
        "a USDC-only venue must still contribute its leg -- this is the "
        "coverage the collapse must not cost"
    )


def test_a_non_dollar_market_keeps_its_own_leg() -> None:
    """A BTC-quoted market is a different market, not a duplicate quote."""

    legs = catalog_pairs._one_leg_per_venue(
        [
            _leg_at("Gate", "USDT", "XMR/USDT:USDT", 10.0),
            _leg_at("Gate", "BTC", "XMR/BTC", 10.0),
        ]
    )

    assert len(legs) == 2


def test_the_discovery_side_gate_matches_the_pairing_gate() -> None:
    """``quote_basis_mismatch`` is the twin of ``_reject_reason``.

    Correcting only the pairing gate left this one filtering out the very
    routes that gate had just started building, inside
    ``load_public_route_index`` -- the index the board serves and the external
    comparator reads. Both must answer the same question the same way.
    """

    from dataclasses import replace as _replace

    from spreadboard import api_spreads

    base = api_spreads._row_from_api(
        {
            "token": "STBL",
            "long_venue": "Hyperliquid",
            "long_market_type": "Futures",
            "long_quote": "USDC",
            "short_venue": "Gate",
            "short_market_type": "Futures",
            "short_quote": "USDT",
            "notes": {"route_inputs": {"long": {}, "short": {}}},
        },
        bucket="api_discovered",
        now=1.0,
    )

    assert not api_spreads.quote_basis_mismatch(base), (
        "a USDC perp against a USDT perp must survive the discovery filter"
    )
    assert not api_spreads.quote_basis_mismatch(
        _replace(base, long_quote="USD", short_quote="USDT")
    )
    assert api_spreads.quote_basis_mismatch(
        _replace(base, long_quote="BTC", short_quote="USDT")
    ), "a dollar against BTC is still a different basis"
    assert not api_spreads.quote_basis_mismatch(
        _replace(base, long_quote="", short_quote="USDT")
    ), "an unknown quote (DEX leg) must not be filtered out"
