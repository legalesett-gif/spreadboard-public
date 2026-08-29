"""The public landing board must resolve to a view the collector materializes.

``FREE_BOARD_QUERY`` was an empty dict. On a cache miss ``api_market_spreads``
asks ``_MATERIALIZED_VIEW_STORE.payload_for(query)`` BEFORE building anything,
so a query whose canonical key is materialized is served from the pre-built
generation. An empty query canonicalises to its own key, which is not warmed,
so /free fell through to a full unbounded board build to render two complete
rows and six teasers -- 20.8s time-to-first-byte against 1.5s for /login.

The WARM_QUERIES comments already record this defect for
/funding?farm=futures-spot, which sat at 27s while /funding answered in 0.20s.

These tests pin the mechanism, not an assumption about it: that the plain board
path really does consult the materialized store, and that the free query's
canonical key is one the collector warms.
"""

from __future__ import annotations

import inspect

from scripts import run_spreadboard_service as service
from spreadboard import materialized_views, server


def test_the_plain_board_path_consults_the_materialized_store() -> None:
    """The mechanism the fix depends on. Pin it, do not assume it."""

    source = inspect.getsource(server.api_market_spreads)
    assert "_MATERIALIZED_VIEW_STORE.payload_for(query" in source, (
        "the plain board path must read the pre-built generation on a cache "
        "miss, otherwise matching a warmed key buys nothing"
    )


def test_the_free_board_query_matches_a_warmed_view() -> None:
    free_key = materialized_views.canonical_query(dict(server.FREE_BOARD_QUERY))
    warmed = {
        materialized_views.canonical_query(dict(query))
        for query in service.WARM_QUERIES
    }
    assert free_key in warmed, (
        f"/free canonicalises to {free_key}, which the collector does not "
        f"materialize, so every cache miss rebuilds the board"
    )


def test_an_empty_query_is_not_a_warmed_view() -> None:
    """Pin the regression: the previous value really was an unwarmed key."""

    warmed = {
        materialized_views.canonical_query(dict(query))
        for query in service.WARM_QUERIES
    }
    assert materialized_views.canonical_query({}) not in warmed


def test_the_limit_still_covers_every_row_the_page_renders() -> None:
    limit = int(server.FREE_BOARD_QUERY["limit"][0])
    shown = server.FREE_TOKEN_LIMIT + server.FREE_TEASER_ROWS
    assert limit >= shown, (
        f"limit {limit} cannot supply the {shown} groups the free page renders"
    )


def test_the_free_query_stays_pinned() -> None:
    """A public surface must not be widened by anything from the request."""

    assert not (set(server.FREE_BOARD_QUERY) - {"limit"}), (
        "no request-controlled filter may enter the pinned public query"
    )
