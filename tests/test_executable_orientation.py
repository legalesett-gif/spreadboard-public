"""A route must be printed in the direction you could actually trade it.

A row with the futures leg long and the spot leg short cannot be taken as
written -- it sells spot you do not own. The board negated the carry but kept
the original leg order, so a row read "long Gate Futures, short Gate Spot"
while its +0.29%/day described the opposite trade, and the spread was never
re-derived at all: GUA showed 192.29% in a direction nobody can trade, against
-66.5% in the one they can.
"""

from __future__ import annotations

import pytest

from spreadboard.api_spreads import _mirror_if_spot_sale_required


def _gua() -> dict:
    """The real snapshot row, with its real prices."""
    return {
        "token": "GUA",
        "long_venue": "BitMart",
        "long_market_type": "Futures",
        "long_market_symbol": "GUA/USDT:USDT",
        "short_venue": "Gate",
        "short_market_type": "Spot",
        "short_market_symbol": "GUA/USDT",
        "executable_spread_pct": 192.28692634,
        "depth_weighted_spread_pct": 192.28692634,
        "funding_daily_pct": -0.1275,
        "notes": {
            "route_inputs": {
                "long": {"bid": 0.05186, "ask": 0.05186, "bid_vwap": 0.05186, "ask_vwap": 0.05186},
                "short": {"bid": 0.15158, "ask": 0.15491, "bid_vwap": 0.15158, "ask_vwap": 0.15491},
            }
        },
    }


def test_legs_are_swapped_into_the_tradeable_direction() -> None:
    mirrored = _mirror_if_spot_sale_required(_gua())

    # You buy the spot and short the futures, so spot is the long leg.
    assert mirrored["long_venue"] == "Gate"
    assert mirrored["long_market_type"] == "Spot"
    assert mirrored["short_venue"] == "BitMart"
    assert mirrored["short_market_type"] == "Futures"


def test_the_spread_is_re_derived_for_the_direction_shown() -> None:
    """192% was buying at 0.05186 and selling at 0.15158 -- the wrong way round."""
    mirrored = _mirror_if_spot_sale_required(_gua())

    # Buy Gate spot at its ask, sell BitMart futures at its bid.
    expected = (0.05186 / 0.15491 - 1.0) * 100.0
    assert mirrored["executable_spread_pct"] == pytest.approx(expected)
    assert mirrored["executable_spread_pct"] < 0
    assert mirrored["depth_weighted_spread_pct"] == pytest.approx(expected)


def test_the_carry_flips_with_the_legs() -> None:
    mirrored = _mirror_if_spot_sale_required(_gua())

    assert mirrored["funding_daily_pct"] == pytest.approx(0.1275)


def test_the_leg_books_move_with_their_legs() -> None:
    mirrored = _mirror_if_spot_sale_required(_gua())
    legs = mirrored["notes"]["route_inputs"]

    assert legs["long"]["ask"] == 0.15491, "the long leg must carry Gate's book"
    assert legs["short"]["bid"] == 0.05186, "the short leg must carry BitMart's book"


def test_a_normal_route_is_left_alone() -> None:
    """Long spot, short futures is already the direction you would trade."""
    row = {
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "short_venue": "BitMart",
        "short_market_type": "Futures",
        "executable_spread_pct": 3.0,
        "funding_daily_pct": 0.5,
    }

    assert _mirror_if_spot_sale_required(row) is row


def test_futures_to_futures_is_left_alone() -> None:
    row = {
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "funding_daily_pct": 1.0,
    }

    assert _mirror_if_spot_sale_required(row) is row


def test_every_per_side_block_moves_with_its_leg() -> None:
    """funding, identity and route_inputs all key on long/short.

    Swapping only route_inputs left Gate's funding rate on Mexc's spot leg and
    dropped the DEX leg's chain and contract off the row entirely.
    """
    row = {
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "short_venue": "Mexc",
        "short_market_type": "Spot",
        "notes": {
            "funding": {
                "long": {"rate_pct": 0.0366, "interval_hours": 4.0},
                "short": {"rate_pct": None, "interval_hours": None},
            },
            "identity": {"long": {"chain": None}, "short": {"chain": "1"}},
            "route_inputs": {"long": {"ask": 2.0}, "short": {"ask": 1.0}},
        },
    }

    notes = _mirror_if_spot_sale_required(row)["notes"]

    # The rate belongs to Gate, which is now the short leg.
    assert notes["funding"]["short"]["rate_pct"] == 0.0366
    assert notes["funding"]["long"]["rate_pct"] is None
    assert notes["identity"]["long"]["chain"] == "1"
    assert notes["route_inputs"]["long"]["ask"] == 1.0
