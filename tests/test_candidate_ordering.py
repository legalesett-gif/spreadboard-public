"""Real books must win the per-token cap.

Candidates were ordered by spread alone, so a ticker-only mirage always
outranked a tight liquid pair and the per-token cap kept the mirage. The
reference product's whole Spot-Spot lane sits between 0.10% and 0.21% -- the
band that was being evicted. 23 of its 44 listed routes were absent from our
board for this reason.
"""

from __future__ import annotations

from src.spreadarb.api_discovery.models import MarketQuote
from src.spreadarb.api_discovery.sources import quote_candidate_pairs


def _quote(venue: str, market_type: str, *, bid: float, ask: float, token: str = "TEST") -> MarketQuote:
    return MarketQuote(
        token=token,
        venue=venue,
        market_type=market_type,
        bid=bid,
        ask=ask,
        bid_vwap=bid,
        ask_vwap=ask,
        quote_ts_us=1_700_000_000_000_000,
        source_name="test",
        symbol=f"{token}/USDT",
    )


def test_a_tradeable_pair_outranks_a_wider_ticker_only_one() -> None:
    # A real two-sided book worth 0.12%.
    real_buy = _quote("Mexc", "Spot", bid=0.999, ask=1.000)
    real_sell = _quote("Kucoin", "Spot", bid=1.0012, ask=1.0022)
    # A last trade wearing a book's clothes, printing far wider.
    fake_buy = _quote("Gate", "Spot", bid=0.5, ask=0.5)
    fake_sell = _quote("Bitget", "Spot", bid=0.9, ask=0.9)

    pairs = quote_candidate_pairs(
        [real_buy, real_sell, fake_buy, fake_sell], min_spread_pct=0.05
    )

    assert pairs, "expected both pairs to be generated"
    best = pairs[0]
    assert {best.long_quote.venue, best.short_quote.venue} == {"Mexc", "Kucoin"}, (
        "the tradeable pair must come first so the per-token cap keeps it"
    )


def test_spread_still_orders_pairs_that_are_equally_tradeable() -> None:
    narrow_buy = _quote("Mexc", "Spot", bid=0.999, ask=1.000)
    narrow_sell = _quote("Kucoin", "Spot", bid=1.001, ask=1.002)
    wide_sell = _quote("Bitget", "Spot", bid=1.05, ask=1.06)

    pairs = quote_candidate_pairs(
        [narrow_buy, narrow_sell, wide_sell], min_spread_pct=0.05
    )

    assert pairs[0].depth_weighted_spread_pct >= pairs[-1].depth_weighted_spread_pct


def test_the_per_token_cap_keeps_the_route_with_a_book() -> None:
    """_row_strength decides which routes survive the cap, not pair ordering.

    Sorting candidates was the wrong layer: _cap_rows_per_token re-sorts by
    _row_strength, so a ticker-priced route printing 90% still evicted the real
    0.12% pair. 23 of the reference product's 44 routes were missing for this.
    """
    from src.spreadarb.api_discovery.sources import _row_strength

    def row(spread: float, *, book: bool) -> dict:
        quote = {"bid": 1.0, "ask": 1.01} if book else {"bid": 1.0, "ask": 1.0}
        return {
            "depth_weighted_spread_pct": spread,
            "notes": {"route_inputs": {"long": dict(quote), "short": dict(quote)}},
        }

    real = row(0.12, book=True)
    mirage = row(90.0, book=False)

    assert _row_strength(real) > _row_strength(mirage), (
        "a tradeable 0.12% route must outrank a ticker-priced 90% one"
    )


def test_spread_still_decides_between_two_booked_routes() -> None:
    from src.spreadarb.api_discovery.sources import _row_strength

    def booked(spread: float) -> dict:
        quote = {"bid": 1.0, "ask": 1.01}
        return {
            "depth_weighted_spread_pct": spread,
            "notes": {"route_inputs": {"long": dict(quote), "short": dict(quote)}},
        }

    assert _row_strength(booked(5.0)) > _row_strength(booked(0.5))
