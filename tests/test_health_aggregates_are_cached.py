"""The health endpoint must not recompute collector coverage per request.

`/api/health` is polled by the container HEALTHCHECK every thirty seconds, by
Caddy, and by anything watching the site. Each call rebuilt two aggregates:
`funding_history_health` walks the whole chart catalogue and summarises funding
coverage for every futures leg, and `research_evidence_health` reads the
calibration store. Production measured 3.2s to 14.7s per health request, and a
py-spy dump caught the request thread inside each of them.

Both summarise artefacts the collector rewrites on a minutes-long cadence, so a
short memo costs nothing in freshness.
"""

from __future__ import annotations

import pytest

from spreadboard import server


@pytest.fixture(autouse=True)
def _empty_cache():
    server._reset_health_aggregates()
    yield
    server._reset_health_aggregates()


def test_a_repeated_call_reuses_the_aggregate() -> None:
    calls: list[int] = []

    def build() -> dict[str, int]:
        calls.append(1)
        return {"n": len(calls)}

    first = server._cached_health_aggregate("probe", build)
    second = server._cached_health_aggregate("probe", build)

    assert first == second == {"n": 1}
    assert len(calls) == 1


def test_the_aggregate_is_rebuilt_once_it_expires(monkeypatch) -> None:
    monkeypatch.setattr(server, "_HEALTH_AGGREGATE_TTL_SECONDS", 0.0)
    calls: list[int] = []

    def build() -> dict[str, int]:
        calls.append(1)
        return {"n": len(calls)}

    server._cached_health_aggregate("probe", build)
    server._cached_health_aggregate("probe", build)

    assert len(calls) == 2


def test_distinct_aggregates_do_not_share_an_entry() -> None:
    server._cached_health_aggregate("funding_history", lambda: {"which": "funding"})
    research = server._cached_health_aggregate(
        "research_evidence", lambda: {"which": "research"}
    )

    assert research == {"which": "research"}


def test_health_reads_both_aggregates_through_the_cache(tmp_path, monkeypatch) -> None:
    """The call site is the point; a helper nobody calls saves nothing."""

    built: list[str] = []
    monkeypatch.setattr(
        server, "funding_history_health", lambda: built.append("funding") or {}
    )
    monkeypatch.setattr(
        server, "research_evidence_health", lambda: built.append("research") or {}
    )
    board_path = tmp_path / "board.json"
    board_path.write_text("{}", encoding="utf-8")

    for _ in range(3):
        server.api_health(board_path, {}, None)

    assert built == ["funding", "research"]
