"""The bounded current-generation cache must hold every routinely warmed view."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts/run_spreadboard_service.py"


def _warm_query_count() -> int:
    source = SERVICE.read_text(encoding="utf-8")
    match = re.search(r"WARM_QUERIES:[^=]*=\s*\((.*?)\n\)\n", source, re.DOTALL)
    assert match is not None, "WARM_QUERIES not found"
    body = match.group(1)
    # Each warmed view is one dict literal at the top level of the tuple.
    return len(re.findall(r"^\s*\{", body, re.MULTILINE))


def test_disk_generation_is_larger_than_the_bounded_resident_hot_set() -> None:
    """Eviction reloads verified JSON; it must not force market recomputation."""
    warmed = _warm_query_count()
    assert warmed > 0

    compose = (ROOT / "compose.production.yml").read_text(encoding="utf-8")
    match = re.search(r'SPREADBOARD_MARKET_CACHE_ENTRIES:\s*"(\d+)"', compose)
    assert match is not None
    assert 4 <= int(match.group(1)) < warmed


def test_the_fresh_window_is_not_shorter_than_the_quote_cadence() -> None:
    """Quotes republish every few minutes and re-key every entry.

    A fresh window shorter than that means every current-generation view gets
    needlessly rebuilt between warm passes.
    """
    from spreadboard import server

    assert server._MARKET_CACHE_TTL_SECONDS >= 300.0
