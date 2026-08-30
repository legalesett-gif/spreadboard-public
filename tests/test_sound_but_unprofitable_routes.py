"""A pair we trust that is not paying is different from one we do not trust.

`spread_evidence_state` returns `excluded` for both, and the two verdicts
could not be further apart. On a futures-futures pair you cross the bid-ask on
BOTH legs, so a tight pair reads negative in either direction until the price
gap exceeds the combined spread -- VELVET Mexc/Bybit measured -0.1021% each
way. The external comparator lists exactly those pairs, and hiding them makes
complete coverage look like a gap.

They stay off the RANKED board deliberately. Measured across ten tokens: 2,246
sound-but-unprofitable routes against 936 shown, mostly -0.01% to -0.04%.
Ranking them would treble the board with rows nobody can trade, and grow the
structures this host already cannot hold.
"""

from __future__ import annotations

import time

from spreadboard import api_spreads


def _route(**overrides):
    row = {
        "token": "VELVET",
        "route_kind": "FUTURES",
        "long_venue": "Mexc",
        "long_market_type": "Futures",
        "long_market_symbol": "VELVET/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": "VELVET/USDT:USDT",
        "displayed_open_spread_pct": -0.1021,
        "executable_spread_pct": -0.1021,
        "quote_ts_us": int(time.time() * 1_000_000),
        "long_ask": 1.0,
        "short_bid": 0.998979,
        "deliverable": True,
    }
    row.update(overrides)
    return row


def test_a_trusted_pair_that_is_not_paying_is_recognised() -> None:
    assert api_spreads.spread_is_sound_but_unprofitable(_route())


def test_a_profitable_pair_is_not_in_this_class() -> None:
    """This class exists only for the negative case; positives rank normally."""

    assert not api_spreads.spread_is_sound_but_unprofitable(
        _route(displayed_open_spread_pct=0.5, executable_spread_pct=0.5)
    )


def test_a_stale_quote_is_never_sound() -> None:
    old = int((time.time() - 86_400) * 1_000_000)
    assert not api_spreads.spread_is_sound_but_unprofitable(_route(quote_ts_us=old))


def test_a_mirage_is_never_promoted_by_being_negative() -> None:
    """The whole risk of this change: an untrustworthy row must stay out.

    Surfacing every `excluded` row would put identity mismatches and thin books
    in front of a member. Only the trusted-and-negative ones are surfaced.
    """

    assert not api_spreads.spread_is_sound_but_unprofitable(
        _route(identity_mismatch=True)
    )
    assert not api_spreads.spread_is_sound_but_unprofitable(_route(thin_book=True))


def test_a_cross_currency_pair_is_never_sound() -> None:
    assert not api_spreads.spread_is_sound_but_unprofitable(
        _route(long_quote="BTC", short_quote="USDT")
    )


def test_it_stays_out_of_the_ranked_evidence_states() -> None:
    """The ranked board reads spread_evidence_state; that must not change."""

    assert api_spreads.spread_evidence_state(_route()) == "excluded"


def test_the_ranked_board_still_drops_unprofitable_routes() -> None:
    """`filtered` must keep its default: the board lists opportunities."""

    from spreadboard import catalog_pairs

    payload = {"routes": [_route(), _route(displayed_open_spread_pct=0.5,
                                           executable_spread_pct=0.5)]}

    kept = catalog_pairs.filtered(payload, limit=None).get("routes") or []

    assert len(kept) == 1
    assert kept[0]["displayed_open_spread_pct"] == 0.5


def test_the_token_view_can_ask_to_keep_them() -> None:
    """Opting in is what makes the one-token answer complete."""

    from spreadboard import catalog_pairs

    payload = {"routes": [_route(), _route(displayed_open_spread_pct=0.5,
                                           executable_spread_pct=0.5)]}

    kept = catalog_pairs.filtered(
        payload, limit=None, include_unprofitable=True
    ).get("routes") or []

    assert len(kept) == 2, "the pair that is not paying must survive for that view"
