"""The rankings leaderboard withheld the spread it was already holding.

Live /rankings, top three rows: the SPREAD NOW column rendered a dash with
"refreshing both legs" underneath, on tokens that carry a perfectly good
best_spread_pct. Same fault as the markets rows, the token headlines and the
funding page -- `x if spread_current else None` discards the value instead of
labelling it.

A leaderboard whose leading column is empty is not a leaderboard.
"""

from __future__ import annotations

from spreadboard import server


def _row(**kw):
    base = {
        "token": "AI", "token_name": "Metadata pending", "status": "live",
        "best_spread_pct": 1.23,
        "best_spread_route": {
            "route_key": "AI|Binance|Futures|OKX|Futures",
            "long_venue": "Binance", "short_venue": "OKX",
            "long_market_type": "Futures", "short_market_type": "Futures",
            "age_min": 99.0,
        },
        "best_funding_route": {}, "settled_windows": {},
    }
    base.update(kw)
    return base


def test_a_stale_row_still_shows_its_spread() -> None:
    html = server.render_token_ranking_row(1, _row())

    assert "1.2%" in html or "1.23%" in html, "the leaderboard's main column is blank"


def test_it_still_says_the_quote_is_refreshing() -> None:
    html = server.render_token_ranking_row(1, _row())

    assert "refreshing" in html.casefold()


def test_a_row_with_no_spread_still_shows_a_dash() -> None:
    html = server.render_token_ranking_row(1, _row(best_spread_pct=None))

    assert "—" in html
