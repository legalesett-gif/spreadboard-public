from __future__ import annotations

import json
from pathlib import Path

from spreadboard import funding_history_demand


def test_exact_leg_demand_is_persisted_and_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "demand.json"

    added = funding_history_demand.enqueue(
        [("Gate", "GUA/USDT:USDT"), ("Aster", "GUA/USDT:USDT")],
        path=path,
    )

    assert added == 2
    assert set(funding_history_demand.legs(path=path)) == {
        ("Gate", "GUA/USDT:USDT"),
        ("Aster", "GUA/USDT:USDT"),
    }
    assert json.loads(path.read_text())["schema"] == (
        "spreadboard.funding_history_demand.v1"
    )


def test_expired_demand_is_not_returned(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "demand.json"
    monkeypatch.setattr(funding_history_demand.time, "time", lambda: 100_000.0)
    funding_history_demand.enqueue([("Gate", "GUA")], path=path)
    monkeypatch.setattr(
        funding_history_demand.time,
        "time",
        lambda: 100_000.0 + funding_history_demand._TTL_SECONDS + 1,
    )

    assert funding_history_demand.legs(path=path) == []
