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
        lambda board_path, query: {
            "BINANCE:spot:BTC/USDT>OKX:swap:BTC/USDT:USDT": (
                1.25,
                0.031,
                "top_book",
            )
        },
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
        assert payload["max_spread_pct"] is None
        assert payload["max_funding_pct"] == 0.031
        route = payload["routes"][0]
        assert route["spread_pct"] == 1.25
        assert route["funding_pct"] == 0.031
        assert route["spread_basis"] == "top_book"
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


def test_stream_does_not_erase_a_current_quote_when_fast_books_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bulk/DEX truth remains visible while the websocket lane is warming."""
    from spreadboard import server

    route = {
        "route_key": "GUA|Mexc|Spot|Gate|Futures",
        "depth_weighted_spread_pct": 1.125,
        "matched_size_notional_usd": api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD,
        "executable_spread_pct": 1.2,
        "funding_daily_pct": 0.4,
    }
    monkeypatch.setattr(server, "api_market_spreads", lambda *_args: {"groups": [{"routes": [route]}]})
    monkeypatch.setattr(server.api_spreads, "live_prices_for", lambda _routes: {})
    monkeypatch.setattr(server.api_spreads, "spread_quote_current", lambda _route: True)

    rows = server._board_stream_rows(tmp_path / "board.jsonl", {})

    assert rows[route["route_key"]] == (1.125, 0.4, "retained_matched_vwap")


def test_public_stream_reprices_only_its_preapproved_visible_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The free SSE lane must not scan the member-only board every tick."""
    from spreadboard import server

    visible = {
        "route_key": "GUA|visible",
        "depth_weighted_spread_pct": 1.25,
        "funding_daily_pct": 0.4,
    }
    seen: list[list[str]] = []
    monkeypatch.setattr(
        server.warm_query_projection.LIVE_UNIVERSE,
        "target_rows",
        lambda **kwargs: (
            [visible],
            {"ready": True, "requested": sorted(kwargs["route_keys"])},
        ),
    )
    monkeypatch.setattr(
        server,
        "api_market_spreads",
        lambda *_args, **_kwargs: pytest.fail(
            "resident visible keys must avoid a full board projection"
        ),
    )

    def live_updates(routes, **_kwargs):
        seen.append([str(route["route_key"]) for route in routes])
        return {"GUA|visible": (1.5, 0.5, 10_000_000, "matched_vwap")}

    monkeypatch.setattr(server.api_spreads, "live_route_updates_for", live_updates)

    rows = server._board_stream_rows(
        tmp_path / "board.jsonl",
        {},
        only_keys={"GUA|visible"},
    )

    assert seen == [["GUA|visible"]]
    assert rows == {"GUA|visible": (1.5, 0.5, "matched_vwap")}


def test_stream_updates_the_label_when_a_live_tick_is_only_top_book() -> None:
    """Production WKC changed the main number over SSE but left the adjacent
    ``$500 VWAP`` label behind. A current top-book tick must change both so the
    page never presents an unmeasured edge as matched-size evidence.
    """

    from spreadboard import server

    source = server.render_board_stream_script({})

    assert "route.spread_basis" in source
    assert "[data-live-spread-basis]" in source


def test_funding_headline_tracks_the_same_live_ticks_as_its_rows() -> None:
    """A live row must not outrun the page's advertised largest carry."""
    from spreadboard import server

    source = server.render_board_stream_script({})

    assert "payload.max_funding_pct" in source
    assert "[data-live-max-funding]" in source


def test_group_leader_tick_does_not_overwrite_expanded_route_rows() -> None:
    """A group and its best child intentionally share one route key.

    The updater used ``row.querySelectorAll`` on the outer ``details`` node,
    which replaced every expanded child's spread with the leader's value.  A
    group match must be scoped to its direct summary; each child is updated by
    its own independent ``data-route-key`` match.
    """
    from spreadboard import server

    source = server.render_board_stream_script({})

    assert 'row.matches("details")' in source
    assert 'row.querySelector(":scope > summary")' in source
    assert 'const liveScope = ' in source
    assert 'liveScope.querySelectorAll("[data-live-spread]")' in source
    assert 'liveScope.querySelectorAll("[data-live-funding]")' in source
