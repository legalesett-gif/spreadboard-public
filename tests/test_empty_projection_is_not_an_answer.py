"""An empty board over a healthy universe is a build artefact, not an answer.

Production served `/free` as "0 tokens and 0 priced routes" while
`/api/health` reported the live universe `ready=True` with 123,362 routes at
generation 115, unchanged across every sample. The same stable universe
produced 522, 429, 468 and then 0 matching tokens within three minutes, so the
collapse is in the projection's live overlay, not in the source.

`_market_payload_cacheable` already refuses this for the canonical mode -- "a
populated source producing zero current groups during a handoff is not a
reusable generation" -- but the projection branch returned True above that
check and so both cached and served the empty result. Refusing it is enough:
the caller already falls through to the persisted view and the rest of the
fallback chain.
"""

from __future__ import annotations

from spreadboard import server

PROJECTION = "materialized_live_query_projection"


def _projection(groups: list, *, ready: bool = True, routes: int = 123_362) -> dict:
    return {
        "mode": PROJECTION,
        "groups": groups,
        "materialized_live_universe": {"ready": ready, "route_count": routes},
    }


def test_an_empty_projection_over_a_ready_universe_is_refused() -> None:
    assert server._market_payload_cacheable(_projection([])) is False


def test_a_populated_projection_is_kept() -> None:
    assert server._market_payload_cacheable(_projection([{"token": "BP"}])) is True


def test_an_empty_projection_over_an_empty_universe_is_a_real_answer() -> None:
    """A query that genuinely matches nothing must still be a successful query."""

    assert server._market_payload_cacheable(_projection([], routes=0)) is True
    assert server._market_payload_cacheable(_projection([], ready=False)) is True


def test_an_empty_projection_falls_through_to_the_persisted_view(
    tmp_path, monkeypatch
) -> None:
    """The point of refusing it: the visitor gets the last complete board."""

    persisted = {"mode": "materialized_view", "groups": [{"token": "BP"}], "which": "persisted"}
    monkeypatch.setattr(
        server.warm_query_projection,
        "project",
        lambda *a, **k: _projection([]),
    )
    monkeypatch.setattr(
        server.warm_query_projection.LIVE_UNIVERSE, "template", lambda: {"ok": True}
    )
    monkeypatch.setattr(
        server._MATERIALIZED_VIEW_STORE, "payload_for", lambda *a, **k: persisted
    )
    monkeypatch.setattr(server, "_exact_catalog_market_projection", lambda *a, **k: None)
    monkeypatch.setattr(server, "_sync_telegram_client_universe", lambda payload: payload)
    with server._MARKET_CACHE_LOCK:
        server._MARKET_CACHE.clear()

    served = server.api_market_spreads(tmp_path / "board.json", {})

    assert served.get("which") == "persisted"


def test_a_search_that_matches_nothing_is_still_a_complete_answer() -> None:
    """The reader asked for a subset, and a subset is allowed to be empty."""

    searched = _projection([])
    searched["filters"] = {"q": "does-not-exist", "sort": "edge", "limit": 500}

    assert server._market_payload_cacheable(searched) is True


def test_paging_and_sorting_do_not_count_as_narrowing() -> None:
    """Otherwise every board would look filtered and nothing would be refused."""

    unfiltered = _projection([])
    unfiltered["filters"] = {
        "q": None,
        "exchange": None,
        "source": "public_api",
        "evidence": "all",
        "include_stale": False,
        "sort": "edge",
        "direction": "desc",
        "offset": 0,
        "limit": 500,
    }

    assert server._market_payload_cacheable(unfiltered) is False
