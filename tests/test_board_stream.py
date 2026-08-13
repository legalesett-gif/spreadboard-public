"""The board's push endpoint, exercised over a real socket.

The stream is the whole reason the board reads live rather than on a reload,
and it is the one code path a unit test of the renderers never touches: it
first ran in production with a NameError on the line that builds the event, so
every connection sent one keepalive and then died with a 500.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from spreadboard import api_spreads
from spreadboard.server import SpreadBoardHandler, SpreadBoardServer


def _serve(tmp_path: Path) -> SpreadBoardServer:
    server = SpreadBoardServer(
        ("127.0.0.1", 0),
        SpreadBoardHandler,
        board_path=tmp_path / "board.jsonl",
        config={},
        accounts_path=tmp_path / "accounts.sqlite3",
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_board_stream_emits_a_board_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "0")
    monkeypatch.setenv("SPREADBOARD_BOARD_STREAM_SECONDS", "1")

    # One route, priced. The first tick has nothing to diff against, so every
    # route counts as changed and the event must go out.
    monkeypatch.setattr(
        api_spreads,
        "live_prices_for",
        lambda routes: {},
    )
    monkeypatch.setattr(
        "spreadboard.server._board_stream_rows",
        lambda board_path, query: {"BINANCE:spot:BTC/USDT>OKX:swap:BTC/USDT:USDT": (1.25, 0.031)},
    )

    server = _serve(tmp_path)
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
    try:
        connection.request("GET", "/api/stream/board", headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")

        saw_event = False
        payload: dict = {}
        for _ in range(12):
            line = response.fp.readline().decode("utf-8").strip()
            if line == "event: board":
                saw_event = True
            elif saw_event and line.startswith("data:"):
                payload = json.loads(line[len("data:") :].strip())
                break

        assert saw_event, "the stream never emitted a board event"
        assert payload["updated_at"].endswith("Z")
        assert payload["max_spread_pct"] == 1.25
        route = payload["routes"][0]
        assert route["spread_pct"] == 1.25
        assert route["funding_pct"] == 0.031
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_stream_reprices_under_the_pages_own_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stream must not invent a query the page never used.

    It normalised limit/sort before re-pricing, which produced a second cache
    key. The page and its own stream then each paid a full board build every
    time the cache turned over, doubling the most expensive work on the box.
    """
    from spreadboard import server

    seen: list[dict] = []

    monkeypatch.setattr(
        server,
        "api_market_spreads",
        lambda board_path, query: seen.append(query) or {"groups": []},
    )
    monkeypatch.setattr(server.api_spreads, "live_prices_for", lambda routes: {})

    page_query = {"kind": ["FUTURES"], "limit": ["50"], "sort": ["edge"]}
    server._board_stream_rows(Path("board.jsonl"), page_query)

    assert seen == [page_query], "the stream re-priced under a different query"
