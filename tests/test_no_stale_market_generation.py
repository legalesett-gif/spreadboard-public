"""The member board must come from one current, complete market generation.

The previous query-only fallback kept old expanded catalogue routes while a new
snapshot was being built.  SSE could correct routes with two resident books,
but routes outside that bounded/live-book set retained old matched spreads.  In
production OPENAI Bitget->Ourbit therefore displayed the group-wide +4.69%
while the exact current API row was about +0.61%.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from spreadboard import server


def test_snapshot_change_builds_the_current_generation_instead_of_serving_old_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = Path("board.jsonl")
    generation = {"signature": 1}
    builds: list[int] = []

    monkeypatch.setattr(server, "_MARKET_CACHE", {})
    monkeypatch.setattr(server, "_MARKET_CACHE_INFLIGHT", {})
    monkeypatch.setattr(server, "_file_signature", lambda _path: generation["signature"])
    monkeypatch.setattr(Path, "resolve", lambda self: self)

    def build(**_kwargs):
        builds.append(generation["signature"])
        return {"ok": True, "rows": [], "groups": [], "generation": builds[-1]}

    monkeypatch.setattr(server.api_spreads, "load_spreads", build)

    first = server.api_market_spreads(board, {})
    generation["signature"] = 2
    second = server.api_market_spreads(board, {})

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert builds == [1, 2]


def test_market_request_path_contains_no_stale_while_revalidate_fallback() -> None:
    source = inspect.getsource(server.api_market_spreads)

    assert "allow_stale" not in inspect.signature(server.api_market_spreads).parameters
    assert "_market_cache_stale_get" not in source
    assert "_rebuild_market_cache" not in source
    assert not hasattr(server, "_MARKET_STALE_CACHE")
