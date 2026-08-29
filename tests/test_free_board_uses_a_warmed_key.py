"""The public landing board must be served from a pre-built view.

``FREE_BOARD_QUERY`` was an empty dict. An empty query canonicalises to its own
cache key, which is not among the warmed materialized views, so /free rebuilt
the entire unbounded board on every cache miss -- to display two complete rows
and six teasers. Measured live: /free 20.8s time-to-first-byte against /login
1.5s on the same host.

The WARM_QUERIES comments already record this exact defect for
/funding?farm=futures-spot, which sat at 27s while /funding answered in 0.20s,
because warming a different key leaves the page cold.
"""

from __future__ import annotations

from scripts import run_spreadboard_service as service
from spreadboard import materialized_views, server


def test_the_free_board_query_is_bounded() -> None:
    assert server.FREE_BOARD_QUERY.get("limit"), (
        "an unbounded query builds the whole board to show a handful of rows"
    )


def test_the_free_board_query_matches_a_warmed_view() -> None:
    """It must canonicalise to a key the collector actually materializes."""

    free_key = materialized_views.canonical_query(dict(server.FREE_BOARD_QUERY))
    warmed = {
        materialized_views.canonical_query(dict(query))
        for query in service.WARM_QUERIES
    }
    assert free_key in warmed, (
        f"/free canonicalises to {free_key}, which is not warmed; it will "
        f"rebuild the board on every cache miss. Warmed keys: {sorted(warmed)[:3]}"
    )


def test_an_empty_query_would_not_match_a_warmed_view() -> None:
    """Pin the regression: the previous value really was a cold key."""

    empty_key = materialized_views.canonical_query({})
    warmed = {
        materialized_views.canonical_query(dict(query))
        for query in service.WARM_QUERIES
    }
    assert empty_key not in warmed, (
        "if an empty query is ever warmed this test should be revisited, but "
        "the free page must still not depend on an unbounded build"
    )


def test_the_free_query_stays_pinned_and_narrow() -> None:
    """A public surface must not be widened, only narrowed."""

    limit = int(server.FREE_BOARD_QUERY["limit"][0])
    shown = server.FREE_TOKEN_LIMIT + server.FREE_TEASER_ROWS
    assert limit >= shown, "the limit must still cover every row the page renders"
    assert "q" not in server.FREE_BOARD_QUERY, "no request-controlled filter may leak in"
