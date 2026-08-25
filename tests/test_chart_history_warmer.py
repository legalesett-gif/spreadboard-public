from __future__ import annotations

import threading

from scripts import run_spreadboard_service
from spreadboard import chart_warm_demand, historical_spreads, server


ROUTE = {
    "route_key": "GUA|Gate|Futures|Mexc|Futures",
    "token": "GUA",
    "long_venue": "Gate",
    "long_market_type": "Futures",
    "long_market_symbol": "GUA/USDT:USDT",
    "short_venue": "Mexc",
    "short_market_type": "Futures",
    "short_market_symbol": "GUA/USDT:USDT",
}


def test_collector_chart_loop_prioritises_persisted_member_demand(
    tmp_path, monkeypatch
) -> None:
    custom_key = server._chart_link_route_key(ROUTE)
    monkeypatch.setattr(
        chart_warm_demand,
        "requests",
        lambda: [(custom_key, 720.0)],
    )
    monkeypatch.setattr(
        run_spreadboard_service,
        "_priority_funding_chart_route_keys",
        lambda: [],
    )
    monkeypatch.setattr(server, "_find_canonical_route", lambda *_args: ROUTE)
    fetched: list[tuple[float, bool]] = []
    monkeypatch.setattr(
        historical_spreads,
        "load_or_fetch",
        lambda _row, **kwargs: fetched.append(
            (float(kwargs["hours"]), bool(kwargs["blocking"]))
        )
        or {"status": "warming", "started": True},
    )
    worker = run_spreadboard_service.ChartHistoryWarmLoop(
        threading.Event(),
        board_path=tmp_path / "board.jsonl",
        batch=4,
    )

    status = worker.check_once()

    assert fetched == [(720.0, False)]
    assert status["requested"] == 1
    assert status["started"] == 1


def test_web_history_reader_enqueues_collector_and_never_fetches(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(server, "_find_canonical_route", lambda *_args: ROUTE)
    monkeypatch.setattr(server.market_history, "load_history", lambda **_kwargs: [])
    enqueued: list[tuple[list[str], float]] = []
    monkeypatch.setattr(
        server.chart_warm_demand,
        "enqueue",
        lambda keys, **kwargs: enqueued.append((list(keys), float(kwargs["hours"])))
        or 1,
    )
    fetch_flags: list[bool] = []
    monkeypatch.setattr(
        historical_spreads,
        "load_or_fetch",
        lambda _row, **kwargs: fetch_flags.append(bool(kwargs["start_fetch"]))
        or {"status": "warming", "rows": []},
    )

    server.api_history(
        ROUTE["route_key"],
        tmp_path / "board.jsonl",
        {"hours": ["720"], "wait": ["0"]},
    )

    assert enqueued == [([server._chart_link_route_key(ROUTE)], 720.0)]
    assert fetch_flags == [False]
