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


def test_complete_payload_preserves_all_exported_futures_legs(tmp_path: Path) -> None:
    path = tmp_path / "demand.json"
    payload = {
        "filters": {"funding_only": True},
        "groups": [
            {
                "best_funding_route": {
                    "long_venue": "Gate",
                    "long_market_type": "Futures",
                    "long_market_symbol": "ONE/USDT:USDT",
                    "short_venue": "Mexc",
                    "short_market_type": "Spot",
                },
                "routes": [
                    {
                        "long_venue": "Aster",
                        "long_market_type": "Futures",
                        "long_market_symbol": "ONE/USDT:USDT",
                        "short_venue": "Bybit",
                        "short_market_type": "Futures",
                        "short_market_symbol": "ONE/USDT:USDT",
                    }
                ],
            }
        ],
    }

    funding_history_demand.enqueue_payload(payload, path=path)

    assert set(funding_history_demand.legs(path=path)) == {
        ("Gate", "ONE/USDT:USDT"),
        ("Aster", "ONE/USDT:USDT"),
        ("Bybit", "ONE/USDT:USDT"),
    }
