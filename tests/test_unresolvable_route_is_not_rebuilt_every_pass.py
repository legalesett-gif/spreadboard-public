"""An unresolvable tracked route rebuilt the snapshot on every warmer pass.

`_find_canonical_route` has four cheap lookups and then falls back to
`load_spreads`, which walks the discovery snapshot to build rows before
filtering to one token. That fallback had no memory of failure, so a route key
that cannot be resolved paid the full cost again on every pass, forever.

`tracked_route_warmer.check_once` calls the resolver for each tracked route that
is not already current, and the owner holds positions -- SKHX, 龙虾 -- whose
routes are not on the board. Profiled in production: the warmer thread held the
GIL at 54% inside `_load_api_discovery_rows`, so request threads could not run
and pages took 14 seconds.

A miss is remembered per snapshot generation. When the snapshot changes the
memory is void and the route is retried, so a genuinely new route still appears.
"""

from __future__ import annotations

from pathlib import Path

from spreadboard import server


def test_a_missing_route_is_only_looked_up_once_per_generation(monkeypatch) -> None:
    calls: list[str] = []

    def counting_load_spreads(**kwargs):
        calls.append(str(kwargs.get("q")))
        return {"rows": []}

    monkeypatch.setattr(server.api_spreads, "load_spreads", counting_load_spreads)
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda _k: None)
    monkeypatch.setattr(
        server.warm_query_projection.LIVE_UNIVERSE,
        "target_rows",
        lambda **_k: ([], {"ready": False}),
    )
    server._reset_route_miss_cache()

    board = Path("/tmp/board.jsonl")
    for _ in range(5):
        assert server._find_canonical_route("GHOST|Gate|Spot|Aster|Futures", board) is None

    assert len(calls) == 1, (
        f"walked the discovery snapshot {len(calls)} times for one unresolvable "
        "route; the warmer does this on every pass"
    )


def test_a_new_snapshot_generation_retries(monkeypatch) -> None:
    """A route absent from one generation may exist in the next."""

    calls: list[str] = []
    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **k: calls.append(str(k.get("q"))) or {"rows": []},
    )
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda _k: None)
    monkeypatch.setattr(
        server.warm_query_projection.LIVE_UNIVERSE,
        "target_rows",
        lambda **_k: ([], {"ready": False}),
    )
    server._reset_route_miss_cache()

    board = Path("/tmp/board.jsonl")
    server._find_canonical_route("GHOST|Gate|Spot|Aster|Futures", board)
    server._reset_route_miss_cache()
    server._find_canonical_route("GHOST|Gate|Spot|Aster|Futures", board)

    assert len(calls) == 2, "a new generation must re-ask for a route it may now have"


def test_a_resolvable_route_is_never_negative_cached(monkeypatch) -> None:
    """Only failure is remembered. Caching a hit here would serve a stale row."""

    row = {"route_key": "REAL|Gate|Spot|Aster|Futures", "token": "REAL"}
    calls: list[str] = []
    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **k: calls.append(str(k.get("q"))) or {"rows": [row]},
    )
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda _k: None)
    monkeypatch.setattr(
        server.warm_query_projection.LIVE_UNIVERSE,
        "target_rows",
        lambda **_k: ([], {"ready": False}),
    )
    server._reset_route_miss_cache()

    board = Path("/tmp/b.jsonl")
    first = server._find_canonical_route("REAL|Gate|Spot|Aster|Futures", board)
    second = server._find_canonical_route("REAL|Gate|Spot|Aster|Futures", board)

    assert first is not None
    # The second call is the point: recording a HIT in the miss cache would make
    # a route that exists vanish on every subsequent lookup.
    assert second is not None, "a resolvable route was remembered as a miss"
    assert second.get("route_key") == "REAL|Gate|Spot|Aster|Futures"


def test_installing_a_new_generation_clears_remembered_misses() -> None:
    """The clear must happen where generations are installed, not by hand.

    A remembered miss that outlives its generation hides a route that has since
    appeared, which is a worse failure than the cost it saves.
    """

    import inspect
    import re

    source = inspect.getsource(server)
    installs = source.count("_ROUTE_COMPAT_ROWS.clear()")
    assert installs >= 2, "install sites moved; this guard needs updating"

    # Count only clears that sit WITH an install, not the standalone reset
    # helper -- otherwise deleting one install's clear still passes.
    paired = len(
        re.findall(
            r"_ROUTE_COMPAT_ROWS\.clear\(\)\s*\n(?:\s*#[^\n]*\n)*\s*_ROUTE_MISS_CACHE\.clear\(\)",
            source,
        )
    )

    assert paired == installs, (
        f"{installs} generation installs but only {paired} clear the miss "
        "memory alongside: a miss can outlive the generation that produced it"
    )


def test_a_resolved_key_is_never_written_into_the_miss_memory(monkeypatch) -> None:
    """Asserted directly on the memory, not through a second lookup.

    A second lookup is served by the compat-row cache before the miss memory is
    consulted, so it cannot see this. But compat rows are evicted under their
    own limit, and a resolvable route remembered as a miss would then vanish.
    """

    # A key no other test has resolved: a compat row left by one would answer
    # this lookup before the miss memory is ever reached.
    key = "UNIQUEHIT|Gate|Spot|Aster|Futures"
    row = {"route_key": key, "token": "UNIQUEHIT"}
    monkeypatch.setattr(server.api_spreads, "load_spreads", lambda **_k: {"rows": [row]})
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda _k: None)
    monkeypatch.setattr(
        server.warm_query_projection.LIVE_UNIVERSE,
        "target_rows",
        lambda **_k: ([], {"ready": False}),
    )
    server._reset_route_miss_cache()
    server._ROUTE_COMPAT_ROWS.pop(key, None)
    server._ROUTE_COMPAT_PATHS.pop(key, None)

    board = Path("/tmp/b.jsonl")
    assert server._find_canonical_route(key, board) is not None

    assert (key, str(board)) not in server._ROUTE_MISS_CACHE, (
        "a route that resolved was recorded as a miss; once its compat row is "
        "evicted it would disappear"
    )
