"""Only finished surfaces are advertised.

An empty tab reads worse to a paying subscriber than no tab at all: it looks
like something broke rather than something is coming. These pages are real work
in progress, so they stay reachable by direct link for the operator and stay out
of the navigation until they carry content.

/portfolio is separate: it never existed as a route at all, while the product,
the emails and the operator all call that surface "Portfolio". A 404 on the name
of your own feature is the worst of the three outcomes.
"""

from __future__ import annotations

from spreadboard import server

# Advertised in navigation and expected to carry real content.
LIVE_TABS = {"markets", "funding", "rankings", "fair", "charts", "watchlist", "profile"}

# Built, reachable, but not yet carrying enough to show a subscriber.
PARKED_TABS = {"intel", "signals", "triage", "community", "proof", "learn", "status"}


def _nav_keys(nav) -> set[str]:
    return {key for key, _href, _label in nav}


def test_parked_tabs_are_not_advertised_to_members() -> None:
    advertised = _nav_keys(server._MEMBER_NAV) | _nav_keys(server._MOBILE_SECONDARY_NAV)
    still_shown = advertised & PARKED_TABS
    assert not still_shown, f"unfinished surfaces still in navigation: {sorted(still_shown)}"


def test_the_finished_tabs_are_still_advertised() -> None:
    """Guard against hiding the product while hiding the stubs."""
    advertised = _nav_keys(server._MEMBER_NAV)
    missing = LIVE_TABS - advertised
    assert not missing, f"working surfaces dropped from navigation: {sorted(missing)}"


def test_a_parked_page_is_hidden_but_not_deleted() -> None:
    """Hidden means unadvertised, not unreachable: the work continues behind it."""
    source = server.__dict__
    assert "_MEMBER_NAV" in source
    # The routes must still exist so the operator can keep building them.
    import inspect

    handler = inspect.getsource(server.SpreadBoardHandler)
    for path in ("/intel", "/signals", "/triage", "/community"):
        assert f'"{path}"' in handler, f"{path} route was removed rather than hidden"


def test_portfolio_has_a_route_of_its_own() -> None:
    """The product calls it Portfolio; /portfolio must not 404."""
    import inspect

    handler = inspect.getsource(server.SpreadBoardHandler)
    assert '"/portfolio"' in handler


def test_the_portfolio_nav_entry_points_at_a_real_path() -> None:
    hrefs = {key: href for key, href, _label in server._MEMBER_NAV}
    assert hrefs.get("profile") in {"/account", "/portfolio"}


def test_scanner_members_keep_their_reduced_navigation() -> None:
    """Tier gating must survive the reshuffle."""
    import inspect

    source = inspect.getsource(server.render_primary_nav)
    assert "research_pro" in source
