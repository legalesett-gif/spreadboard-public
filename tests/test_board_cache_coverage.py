"""The bounded current-generation cache must hold every routinely warmed view."""

from __future__ import annotations

import re
from pathlib import Path

from spreadboard import server

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts/run_spreadboard_service.py"


def _warm_query_count() -> int:
    source = SERVICE.read_text(encoding="utf-8")
    match = re.search(r"WARM_QUERIES:[^=]*=\s*\((.*?)\n\)\n", source, re.DOTALL)
    assert match is not None, "WARM_QUERIES not found"
    body = match.group(1)
    # Each warmed view is one dict literal at the top level of the tuple.
    return len(re.findall(r"^\s*\{", body, re.MULTILINE))


def test_the_cache_holds_every_view_that_gets_warmed() -> None:
    """Warming more views than the cache can hold just evicts the earlier ones."""
    warmed = _warm_query_count()
    assert warmed > 0

    # Plus the free board and the member default, which are not in WARM_QUERIES
    # but are the two most requested views on the site.
    assert server._MARKET_CACHE_MAX_ENTRIES >= warmed + 2


def test_the_fresh_window_is_not_shorter_than_the_quote_cadence() -> None:
    """Quotes republish every few minutes and re-key every entry.

    A fresh window shorter than that means every current-generation view gets
    needlessly rebuilt between warm passes.
    """
    assert server._MARKET_CACHE_TTL_SECONDS >= 300.0
