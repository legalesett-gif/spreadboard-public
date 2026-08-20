"""A row that prints both leg prices must print the spread between them.

Live markup, verbatim:

    Hyperliquid Futures  Sell  Coinbase International Futures
    $0.136375 → $0.1362    —    refreshing both legs

Both prices are on screen. The spread is their ratio. Withholding it because a
freshness gate tripped is arbitrary: the inputs are already published, and the
caveat ("refreshing both legs") sits directly beside the number, so nobody is
misled about how current it is. 37 of 478 spread cells on the markets page were
dashes of exactly this shape.

The rule is the same one the feed and the board already follow: never show a
hole where a number can be derived, and always say what the number is.
"""

from __future__ import annotations

from spreadboard import server


def _row(**kw):
    base = {
        "route_key": "T|A|Futures|B|Futures",
        "token": "T",
        "long_venue": "Hyperliquid", "short_venue": "Coinbase International",
        "long_market_type": "Futures", "short_market_type": "Futures",
        "long_price": 0.136375, "short_price": 0.1362,
        "executable_spread_pct": None, "depth_weighted_spread_pct": None,
        # Old enough that spread_quote_current() is False.
        "age_min": 99.0,
    }
    base.update(kw)
    return base


def test_a_stale_row_still_shows_the_spread_its_prices_imply() -> None:
    html = server.render_market_group_route(_row())

    # (0.1362 / 0.136375 - 1) * 100 = -0.128%, rendered at one decimal.
    assert "-0.1%" in html, "the row prints both prices but withholds their ratio"


def test_the_stale_row_still_says_it_is_refreshing() -> None:
    """Deriving the number must not imply it is current."""
    html = server.render_market_group_route(_row())

    assert "refreshing" in html.casefold()


def test_a_current_row_prefers_its_measured_spread() -> None:
    """The derived value is a fallback, never a replacement."""
    html = server.render_market_group_route(
        _row(age_min=0.2, executable_spread_pct=1.25, depth_weighted_spread_pct=1.20)
    )

    assert "1.2%" in html
    assert "refreshing both legs" not in html
    assert "data-live-spread-basis" in html


def test_a_stale_row_prefers_its_stored_spread_over_the_derived_one() -> None:
    """If a real measurement exists, show that rather than recomputing."""
    html = server.render_market_group_route(
        _row(executable_spread_pct=0.42, depth_weighted_spread_pct=None)
    )

    assert "0.4%" in html


def test_a_row_without_prices_has_nothing_to_derive() -> None:
    """No invention: a dash is right when there is genuinely no input."""
    html = server.render_market_group_route(_row(long_price=None, short_price=None))

    assert "—" in html


def test_a_zero_price_is_not_divided_by() -> None:
    html = server.render_market_group_route(_row(long_price=0.0, short_price=0.5))

    assert "—" in html


def test_spot_legs_do_not_render_fabricated_zero_funding_rates() -> None:
    """The complete catalogue encodes a Spot leg's neutral contribution as
    zero for mixed-route arithmetic. That internal identity must not be shown
    as if the Spot market actually settled a 0.0000% perpetual funding rate.
    """

    html = server.render_market_group_route(
        _row(
            long_market_type="Spot",
            short_market_type="Spot",
            long_funding_pct=0.0,
            short_funding_pct=0.0,
        )
    )

    assert "n/a / n/a" in html
    assert "+0.0000% / +0.0000%" not in html


# --------------------------------------------------------------------------
# The group headline has the same obligation
# --------------------------------------------------------------------------


def _group(**route_kw):
    route = _row(**route_kw)
    return {
        "token": "T", "token_name": "Token",
        "best_route": route, "routes": [route],
        "best_edge_pct": None, "route_count": 1,
    }


def test_a_group_headline_derives_its_spread_from_the_prices_it_shows() -> None:
    """Three live groups showed "waiting for a two-leg matched quote" on a
    route that was publishing both of its leg prices."""
    html = server.render_market_token_group(_group())

    assert "-0.1%" in html
    assert "waiting for a two-leg matched quote" not in html


def test_a_group_headline_with_no_prices_still_says_it_is_waiting() -> None:
    html = server.render_market_token_group(_group(long_price=None, short_price=None))

    assert "waiting for a two-leg matched quote" in html
