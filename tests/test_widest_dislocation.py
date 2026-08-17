"""Show the widest dislocation, labelled — the way the reference product does.

A route we cannot depth-verify was hidden entirely. Ourbit and Mexc futures
tickers publish no top-of-book size, so nothing could verify them, so UNITREE
carried 24 real routes -- Gate->Hyperliquid at 4.40%, Mexc->Ourbit at 2.65% --
and displayed 0.000%.

Hiding them is defensible only if the alternative is presenting them as
executable. It is not: the Depth cell already renders "unverified" per row, so
the honest option is to show the dislocation and say what is known about it.
"""

from __future__ import annotations

import inspect

from spreadboard import api_spreads, server


def test_unverified_routes_are_visible_by_default() -> None:
    signature = inspect.signature(api_spreads.load_spreads)
    assert signature.parameters["include_unverified"].default is True


def test_the_board_pages_do_not_re_hide_them() -> None:
    source = inspect.getsource(server)
    assert "include_unverified=False" not in source, (
        "a page passing False re-hides the widest dislocation"
    )


def test_an_unverified_row_is_labelled_not_disguised() -> None:
    """Showing the spread is only honest while the row says what backs it."""
    table = server.render_pro_market_table([{
        "route_key": "AAA|FUTURES|Mexc|Futures|Ourbit|Futures",
        "token": "AAA", "long_venue": "Mexc", "short_venue": "Ourbit",
        "depth_unverified": True,
    }])

    assert "unverified" in table


def test_a_verified_row_still_says_matched() -> None:
    table = server.render_pro_market_table([{
        "route_key": "BBB|FUTURES|Gate|Futures|Bybit|Futures",
        "token": "BBB", "long_venue": "Gate", "short_venue": "Bybit",
        "depth_unverified": False, "depth_usd": 50,
    }])

    assert "matched" in table


def test_the_mirage_guard_is_untouched() -> None:
    """Unverified means unmeasured. Mirage means known-false, and stays hidden."""
    source = inspect.getsource(api_spreads)
    assert "_is_mirage_guarded" in source
    assert "price_ratio_implausible" in source
