"""Atomically published, exact-ranked Funding navigation views.

The complete funding catalogue is intentionally large.  Ranking it inside an
HTTP request made a correct page take seconds (and historical windows much
longer).  The collector publishes the twelve principal farm/window lanes here;
web readers only verify, slice and render the last complete generation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from spreadboard import materialized_views

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(
    os.environ.get(
        "SPREADBOARD_FUNDING_NAVIGATION_ROOT",
        str(Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data"))) / "funding-navigation"),
    )
)
KINDS = ("FUTURES", "FUTURES-SPOT-PAIR", "DEX-FUTURES")
WINDOWS = ("now", "1d", "7d", "30d")
MAX_TOKENS = 6_000


def query(kind: str, window: str) -> dict[str, list[str]]:
    result = {
        "funding_only": ["1"],
        "kind": [kind],
        "sort": ["funding"],
        "direction": ["desc"],
        "limit": [str(MAX_TOKENS)],
        "offset": ["0"],
    }
    if window != "now":
        result["funding_window"] = [window]
    return result


QUERIES: tuple[dict[str, list[str]], ...] = tuple(
    query(kind, window) for kind in KINDS for window in WINDOWS
)


_STORE: materialized_views.Store | None = None


def store() -> materialized_views.Store:
    global _STORE
    if _STORE is None or _STORE.root != DEFAULT_ROOT:
        _STORE = materialized_views.Store(DEFAULT_ROOT)
    return _STORE


def payload_for(
    request: dict[str, list[str]], *, board_path: Path | str | None = None
) -> dict[str, Any] | None:
    return store().payload_for(request, board_path=board_path)


def status() -> dict[str, Any]:
    state = store().status()
    return {
        **state,
        "expected_view_count": len(QUERIES),
        "complete": bool(state.get("ready"))
        and int(state.get("view_count") or 0) == len(QUERIES),
    }
