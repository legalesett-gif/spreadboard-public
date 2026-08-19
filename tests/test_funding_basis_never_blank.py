"""The funding page withheld the basis it already had.

52 cells on the default funding view rendered:

    Net 24h +0.030% at current rate · every 8h    Basis refreshing  —  —

The carry is right there. The basis was nulled because a freshness gate tripped,
exactly as it was on the markets rows and the token headlines, and for the same
reason: `x if basis_current else None` throws the stored value away instead of
labelling it.

Same rule as everywhere else: show the number, keep the caveat beside it, and
only fall back to a dash when there is genuinely nothing to show.
"""

from __future__ import annotations

from spreadboard import server


def _group(**best_kw):
    best = {
        "token": "T", "route_key": "T|A|Futures|B|Futures",
        "long_venue": "Gate", "short_venue": "HTX",
        "long_market_type": "Futures", "short_market_type": "Futures",
        "executable_spread_pct": 0.42, "depth_weighted_spread_pct": None,
        "funding_daily_pct": 0.03, "age_min": 99.0,
    }
    best.update(best_kw)
    return {"token": "T", "token_name": "Token", "best_route": best,
            "routes": [best], "route_count": 1}


def test_a_stale_funding_group_still_shows_its_basis() -> None:
    html = server.render_funding_token_group(_group())

    assert "0.4%" in html or "0.42%" in html, "the basis it holds is still withheld"


def test_it_still_says_the_basis_is_refreshing() -> None:
    html = server.render_funding_token_group(_group())

    assert "refreshing" in html.casefold()


def test_a_current_group_is_unchanged() -> None:
    html = server.render_funding_token_group(_group(age_min=0.2))

    assert "Basis refreshing" not in html


def test_a_group_with_no_basis_at_all_still_shows_a_dash() -> None:
    html = server.render_funding_token_group(
        _group(executable_spread_pct=None, long_price=None, short_price=None)
    )

    assert "—" in html
