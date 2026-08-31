"""Retained previous-generation rows bypassed the per-token cap.

`_cap_rows_per_token` runs while the snapshot is being built, but
`_merge_previous_rows` then adds rows carried over from the previous generation
and re-applies only the GLOBAL row ceiling. So a token could hold its capped
allowance of fresh routes plus a second full allowance of retained ones.

Production showed exactly that: a configured cap of 32 with 485 tokens above it
and a maximum of 64 -- precisely twice the configured value -- while 19,562 of
the snapshot's 30,000 rows were retained. The surplus was 5,419 rows (18%) of
depth nobody asked for, on a box that was OOM-killing three times a day.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from spreadarb.api_discovery import runner


def _row(token: str, index: int, *, ts: int) -> dict[str, Any]:
    return {
        "token": token,
        "route_key": f"{token}-{index}",
        "long_venue": f"V{index % 7}",
        "short_venue": f"W{index % 5}",
        "quote_ts_us": ts,
    }


def _snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "api_discovered_rows": rows,
        "dex_discovered_rows": [],
        "source_refresh": {},
    }


def test_retained_rows_cannot_double_a_token_allowance(monkeypatch) -> None:
    monkeypatch.setattr(runner, "MAX_SNAPSHOT_ROWS_PER_TOKEN", 8)

    current = [_row("AAA", i, ts=200) for i in range(8)]
    # A previous generation holding an entirely different set of routes for the
    # same token: none of these match on identity, so all are retained.
    previous = [_row("AAA", 100 + i, ts=100) for i in range(8)]

    merged = runner._merge_previous_rows(
        _snapshot(list(current)),
        _snapshot(list(previous)),
        row_limit=1000,
        retain_unmatched=True,
    )

    per_token = Counter(r["token"] for r in merged["api_discovered_rows"])
    assert per_token["AAA"] <= 8, (
        f"token carried {per_token['AAA']} rows against a configured cap of 8; "
        "retained rows bypassed the per-token cap"
    )


def test_retention_still_happens_within_the_cap(monkeypatch) -> None:
    """The cap must not switch retention off.

    Retention exists so a route with a fresher previous quote is not lost when
    the current pass misses it. Capping the merged set is only legitimate if
    retained rows still compete for the allowance.
    """

    monkeypatch.setattr(runner, "MAX_SNAPSHOT_ROWS_PER_TOKEN", 8)

    current = [_row("AAA", i, ts=200) for i in range(3)]
    previous = [_row("AAA", 100 + i, ts=100) for i in range(3)]

    merged = runner._merge_previous_rows(
        _snapshot(list(current)),
        _snapshot(list(previous)),
        row_limit=1000,
        retain_unmatched=True,
    )

    keys = {r["route_key"] for r in merged["api_discovered_rows"]}
    assert len(keys) == 6, "retention stopped working once the cap was applied"
    assert merged["source_refresh"]["previous_snapshot_rows_retained"] == 3


def test_no_token_is_lost_to_the_cap(monkeypatch) -> None:
    """Depth is what the cap removes. Breadth is never negotiable.

    Measured against the real production snapshot, applying the configured cap
    of 32 freed 18.1% of rows while losing 0 tokens and 0 venues.
    """

    monkeypatch.setattr(runner, "MAX_SNAPSHOT_ROWS_PER_TOKEN", 4)

    current = [_row(t, i, ts=200) for t in ("AAA", "BBB", "CCC") for i in range(6)]
    previous = [_row(t, 100 + i, ts=100) for t in ("AAA", "BBB", "CCC") for i in range(6)]

    merged = runner._merge_previous_rows(
        _snapshot(list(current)),
        _snapshot(list(previous)),
        row_limit=1000,
        retain_unmatched=True,
    )

    tokens = {r["token"] for r in merged["api_discovered_rows"]}
    assert tokens == {"AAA", "BBB", "CCC"}
