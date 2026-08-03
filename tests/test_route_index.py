"""Opening a chart by route must not rebuild the whole board.

_find_canonical_route built the entire twelve-thousand-row board and scanned it
for one key, costing 14.6s on every chart opened by route -- most of the thirty
seconds a member waited.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spreadboard import server


def test_the_board_is_built_once_and_then_indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = []

    def build(**_kwargs):
        builds.append(1)
        return {"rows": [{"route_key": "A|X|Spot|Y|Spot"}, {"route_key": "B|X|Spot|Y|Spot"}]}

    monkeypatch.setattr(server.api_spreads, "load_spreads", build)
    monkeypatch.setattr(server, "_file_signature", lambda _p: "sig")
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = None

    board = Path("board.jsonl")
    first = server._find_canonical_route("A|X|Spot|Y|Spot", board)
    second = server._find_canonical_route("B|X|Spot|Y|Spot", board)

    assert first and second
    assert len(builds) == 1, "the second lookup must reuse the index"


def test_a_new_snapshot_rebuilds_the_index(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = []
    signature = {"value": "one"}

    def build(**_kwargs):
        builds.append(1)
        return {"rows": [{"route_key": "A|X|Spot|Y|Spot"}]}

    monkeypatch.setattr(server.api_spreads, "load_spreads", build)
    monkeypatch.setattr(server, "_file_signature", lambda _p: signature["value"])
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = None

    board = Path("board.jsonl")
    server._find_canonical_route("A|X|Spot|Y|Spot", board)
    signature["value"] = "two"
    server._find_canonical_route("A|X|Spot|Y|Spot", board)

    assert len(builds) == 2, "a changed snapshot must refresh the index"


def test_a_catalogue_route_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: {"route_key": "custom"})

    def explode(**_kwargs):
        raise AssertionError("must not build the board for a catalogue route")

    monkeypatch.setattr(server.api_spreads, "load_spreads", explode)

    assert server._find_canonical_route("custom", Path("board.jsonl")) == {"route_key": "custom"}
