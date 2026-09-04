"""The route-index build must not share the box with the ranking build.

Measured on the production collector, 120 samples at 10s through a full cycle:

    live_route_index_worker present            31 / 120 samples
      ...overlapping token_ranking_worker      23 / 31  (74%)
    anon peak WITH token_ranking      4050 / 3914 / 3650 MB
    anon peak WITHOUT it              2900 / 2861 / 2708 MB
    cgroup ceiling (4096MB) touched   80 / 120 samples

The worst sample read anon=4050MB against a 4096MB cap -- 46MB of headroom --
composed of live_route_index_worker 1672MB, bulk_quote_worker 776MB,
token_ranking_worker 679MB and four smaller children. That is concurrency
observed directly, not a sum of separate peaks.

`bulk_quote_worker` overlaps just as often and is deliberately left alone: it is
the price path, and parking it behind a multi-minute index build would cost the
live freshness the board is judged on. Serialising the ranking build alone takes
the peak from 4050MB to about 2900MB, which is enough.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "run_spreadboard_service",
    Path(__file__).resolve().parents[1] / "scripts" / "run_spreadboard_service.py",
)
assert _SPEC and _SPEC.loader
service = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(service)


def _command(script: str) -> list[str]:
    return ["python", f"/app/scripts/{script}", "--db-path", "/app/data/spreadarb.db"]


def test_the_ranking_build_takes_a_heavy_slot() -> None:
    assert service._is_heavy_child(_command("token_ranking_worker.py")) is True


def test_the_route_index_build_still_does() -> None:
    assert service._is_heavy_child(_command("live_route_index_worker.py")) is True


def test_the_quote_path_is_left_alone() -> None:
    """Parking the price path behind a multi-minute build costs freshness."""

    assert service._is_heavy_child(_command("bulk_quote_worker.py")) is False
    assert service._is_heavy_child(_command("fast_quote_worker.py")) is False


def test_only_one_heavy_child_runs_at_a_time() -> None:
    """The slot is what makes membership mean anything."""

    assert service._HEAVY_CHILD_SLOT._value == 1
