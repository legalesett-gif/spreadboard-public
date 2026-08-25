from __future__ import annotations

from spreadboard import chart_warm_demand


def test_chart_demand_is_persisted_newest_first_and_widens_horizon(tmp_path) -> None:
    path = tmp_path / "chart-demand.json"

    assert chart_warm_demand.enqueue(["A", "B"], hours=24, path=path) == 2
    assert chart_warm_demand.enqueue(["A"], hours=720, path=path) == 0

    assert chart_warm_demand.requests(path=path) == [("A", 720.0), ("B", 24.0)]
    assert chart_warm_demand.route_keys(path=path) == ["A", "B"]
