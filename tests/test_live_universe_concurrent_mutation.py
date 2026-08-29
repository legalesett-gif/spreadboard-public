"""The resident route index must never be mutated while it is being iterated.

``restore_materialized_route_index`` installs one dictionary object into BOTH
``server._ROUTE_INDEX["rows"]`` and ``warm_query_projection.LIVE_UNIVERSE``.
The compatibility lookup for an old bookmark used to merge freshly loaded rows
into that dictionary with ``_ROUTE_INDEX["rows"].update(indexed)`` while
holding ``_ROUTE_INDEX_LOCK`` -- a different lock from the universe's own.

Production raised ``RuntimeError: dictionary changed size during iteration``
repeatedly from ``refresh_route_kinds`` and ``opportunity_rows`` between 15:47
and 19:05 UTC on 2026-08-28.  Each raise silently skipped a live refresh or an
opportunity-journal transition, so a real spread could open and close without
ever being recorded.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from spreadboard import server, warm_query_projection


@pytest.fixture(autouse=True)
def _isolated_route_index():
    """These globals are process-wide; never leak them into another test."""

    with server._ROUTE_INDEX_LOCK:
        saved = (
            server._ROUTE_INDEX["signature"],
            server._ROUTE_INDEX["rows"],
            dict(server._ROUTE_COMPAT_PATHS),
            dict(server._ROUTE_COMPAT_ROWS),
        )
    yield
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = saved[0]
        server._ROUTE_INDEX["rows"] = saved[1]
        server._ROUTE_COMPAT_PATHS.clear()
        server._ROUTE_COMPAT_PATHS.update(saved[2])
        server._ROUTE_COMPAT_ROWS.clear()
        server._ROUTE_COMPAT_ROWS.update(saved[3])


def _row(token: str, route_key: str) -> dict[str, object]:
    now_us = int(time.time() * 1_000_000)
    return {
        "token": token,
        "route_key": route_key,
        "route_kind": "FUTURES",
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "short_venue": "Mexc",
        "short_market_type": "Futures",
        "executable_spread_pct": 1.0,
        "quote_ts_us": now_us,
        "freshness": "fresh",
    }


def test_a_compat_lookup_never_mutates_the_installed_universe_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact production trigger: a bookmark for an absent route_key."""

    installed = {f"T{i}|Gate|Futures|Mexc|Futures": _row(f"T{i}", f"T{i}|Gate|Futures|Mexc|Futures") for i in range(50)}

    monkeypatch.setattr(server, "_file_signature", lambda _p: "sig")
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda _key: None)
    # The absent bookmark resolves through a token-scoped catalogue read.
    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **_kw: {
            "rows": [
                _row("OLD", "OLD|Gate|Futures|Mexc|Futures"),
                _row("OLD", "OLD|Bybit|Futures|Mexc|Futures"),
            ]
        },
    )

    board = Path("board.jsonl")
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = (str(board), "sig", "sig")
        server._ROUTE_INDEX["rows"] = installed
        server._ROUTE_COMPAT_PATHS.clear()
        server._ROUTE_COMPAT_ROWS.clear()

    before = set(installed)
    resolved = server._find_canonical_route("OLD|Gate|Futures|Mexc|Futures", board)

    assert resolved is not None, "the bookmark must still resolve"
    assert set(installed) == before, (
        "the installed generation was mutated in place; LIVE_UNIVERSE shares "
        "this exact object and iterates it under a different lock"
    )
    assert "OLD|Gate|Futures|Mexc|Futures" in server._ROUTE_COMPAT_ROWS


def test_a_concurrent_compat_lookups_do_not_break_universe_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stress the exact race that produced the production RuntimeError."""

    rows = {
        f"T{i}|Gate|Futures|Mexc|Futures": _row(f"T{i}", f"T{i}|Gate|Futures|Mexc|Futures")
        for i in range(400)
    }

    universe = warm_query_projection.LiveRouteUniverse()
    universe.install(rows)  # type: ignore[arg-type]

    monkeypatch.setattr(server, "_file_signature", lambda _p: "sig")
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda _key: None)

    counter = {"n": 0}

    def _load(**_kw):
        counter["n"] += 1
        n = counter["n"]
        return {
            "rows": [
                _row(f"OLD{n}", f"OLD{n}|Gate|Futures|Mexc|Futures"),
                _row(f"OLD{n}", f"OLD{n}|Bybit|Futures|Mexc|Futures"),
            ]
        }

    monkeypatch.setattr(server.api_spreads, "load_spreads", _load)

    board = Path("board.jsonl")
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = (str(board), "sig", "sig")
        # The production sharing relationship: one object, two owners.
        server._ROUTE_INDEX["rows"] = rows
        server._ROUTE_COMPAT_PATHS.clear()
        server._ROUTE_COMPAT_ROWS.clear()

    errors: list[BaseException] = []
    stop = threading.Event()

    def _iterate() -> None:
        try:
            while not stop.is_set():
                universe.opportunity_rows(("FUTURES",))
        except BaseException as exc:  # noqa: BLE001 - the test asserts on it.
            errors.append(exc)

    def _bookmarks() -> None:
        try:
            for i in range(300):
                server._find_canonical_route(
                    f"MISSING{i}|Gate|Futures|Mexc|Futures", board
                )
        except BaseException as exc:  # noqa: BLE001 - the test asserts on it.
            errors.append(exc)

    reader = threading.Thread(target=_iterate, daemon=True)
    writer = threading.Thread(target=_bookmarks, daemon=True)
    reader.start()
    writer.start()
    writer.join(timeout=60)
    stop.set()
    reader.join(timeout=30)

    assert not errors, f"concurrent compat lookups corrupted iteration: {errors[:2]}"
    assert len(rows) == 400, "the installed generation must keep its exact size"


def test_a_compat_row_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A member scanning many dead links must not grow web memory forever."""

    monkeypatch.setattr(server, "_file_signature", lambda _p: "sig")
    monkeypatch.setattr(server.chart_catalog, "route_from_key", lambda _k: None)
    monkeypatch.setattr(server.funding_radar, "route_for_key", lambda _key: None)
    monkeypatch.setattr(server, "_ROUTE_COMPAT_ROW_LIMIT", 25)

    counter = {"n": 0}

    def _load(**_kw):
        counter["n"] += 1
        n = counter["n"]
        return {"rows": [_row(f"OLD{n}", f"OLD{n}|Gate|Futures|Mexc|Futures")]}

    monkeypatch.setattr(server.api_spreads, "load_spreads", _load)

    board = Path("board.jsonl")
    with server._ROUTE_INDEX_LOCK:
        server._ROUTE_INDEX["signature"] = (str(board), "sig", "sig")
        server._ROUTE_INDEX["rows"] = {}
        server._ROUTE_COMPAT_PATHS.clear()
        server._ROUTE_COMPAT_ROWS.clear()

    for i in range(200):
        server._find_canonical_route(f"GONE{i}|Gate|Futures|Mexc|Futures", board)

    assert len(server._ROUTE_COMPAT_ROWS) <= 25, (
        f"compat cache grew to {len(server._ROUTE_COMPAT_ROWS)} rows"
    )
    assert len(server._ROUTE_COMPAT_PATHS) <= 25, "the path map must be evicted too"


def test_the_full_universe_scan_does_not_run_under_the_lock() -> None:
    """Holding ``_lock`` across a ~130k-row scan serialises every reader.

    ``status()`` -- which /api/health calls -- takes the same lock, so a scan
    that holds it for the whole pass makes health latency a function of the
    universe size. This is asserted structurally rather than by timing: a
    timing assertion at test scale passes with or without the bug, which would
    make it a false guard.

    Iterating outside the lock is only safe because ``install()`` replaces both
    dictionaries wholesale and nothing mutates them in place -- the property
    the other tests in this module pin.
    """

    import inspect
    import textwrap

    source = textwrap.dedent(
        inspect.getsource(warm_query_projection.LiveRouteUniverse.opportunity_rows)
    )
    lock_line = None
    for index, line in enumerate(source.splitlines()):
        if "with self._lock:" in line:
            lock_line = index
            lock_indent = len(line) - len(line.lstrip())
        if "for key, row in rows.items():" in line:
            scan_indent = len(line) - len(line.lstrip())
            assert lock_line is not None, "expected the reference grab under the lock"
            assert scan_indent <= lock_indent, (
                "the full-universe scan is nested inside `with self._lock:`; "
                "take references under the lock and iterate outside it"
            )
            return
    raise AssertionError("could not locate the scan loop")
