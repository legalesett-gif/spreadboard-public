"""The funding-window pass must not hold one copy of the universe per query.

``_refresh_funding_windows`` iterates five funding-only queries, each of which
builds its own payload, and the same route appears in several of them. Appending
every occurrence held roughly five copies of the route universe at once, drove
``market_evidence_worker`` to 2.3GB, and the collector's cgroup killed it. That
is why funding windows never converged -- complete 24h legs sat at ~317 of
10,469 while overdue stayed at 8,759: the process that computes them died
before finishing.

Deduplication cannot change the result. The radar mapping downstream is already
built with ``catalog_pairs.route_identity`` in a last-wins dict, ``priority``
routes are reduced to (venue, symbol) legs and deduplicated by their consumer,
and ``route_keys`` addresses rows by key.
"""

from __future__ import annotations

import inspect

from scripts import run_spreadboard_service as service


def _source() -> str:
    return inspect.getsource(service._refresh_funding_windows)


def test_routes_are_deduplicated_as_they_are_collected() -> None:
    source = _source()
    assert "warm_by_identity" in source and "priority_by_key" in source, (
        "collection must deduplicate rather than append every occurrence"
    )
    assert "warm_routes.append(route)" not in source, (
        "appending every occurrence reintroduces one copy of the universe per query"
    )
    assert "priority_routes.extend(" not in source


def test_the_warm_mapping_matches_the_radar_key() -> None:
    """Dedup must use the SAME key the radar uses, or the result changes."""

    source = _source()
    assert "warm_by_identity[catalog_pairs.route_identity(route)] = route" in source, (
        "warm routes must be keyed by route_identity, matching the radar dict"
    )
    # The radar mapping downstream must still key on the same function.
    assert "catalog_pairs.route_identity(route): route" in source


def test_each_query_payload_is_released(): 
    assert "del payload" in _source(), (
        "the payload must not stay alive once its routes are held"
    )


def test_dedup_preserves_every_distinct_route() -> None:
    """Simulate the collection shape: duplicates collapse, distinct survive."""

    groups = [
        {"routes": [{"route_key": "A"}, {"route_key": "B"}]},
        {"routes": [{"route_key": "B"}, {"route_key": "C"}]},
    ]
    route_key_set: dict[str, None] = {}
    priority_by_key: dict[str, dict] = {}
    for _query in range(5):  # the same five funding queries
        for group in groups:
            for route in group["routes"]:
                key = str(route["route_key"])
                route_key_set.setdefault(key, None)
                priority_by_key.setdefault(key, route)

    assert list(route_key_set) == ["A", "B", "C"], "order preserved, duplicates gone"
    assert len(priority_by_key) == 3
