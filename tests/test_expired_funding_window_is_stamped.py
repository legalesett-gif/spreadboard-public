"""An expired window shows its real figure with a timestamp, not a dash.

A window fails closed the instant it crosses its next settlement. That is
right for "is this current" and wrong for "what did this pay": 97.1% of legs
hold a stored exact aggregate while only about a third are still inside their
expiry, so the board was blanking data it already had and reading as ~30%
accurate to the owner.

The figure shown is still an exact official settlement total -- never a current
rate, a partial sum or a synthetic zero. The stamp is what keeps it honest: an
UNLABELLED stale number is what the dash existed to prevent; a labelled one is
older news, which is what someone investigating a farm actually wants.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from spreadboard import server, venue_funding_history


def _row(**overrides):
    row = {
        "token": "T",
        "settled_funding_windows": {"1d": None, "7d": None, "30d": None},
        "settled_funding_windows_last_complete": {},
    }
    row.update(overrides)
    return row


def test_a_current_window_renders_plainly() -> None:
    html = server.render_carry_windows(
        _row(settled_funding_windows={"1d": 0.664929, "7d": None, "30d": None})
    )

    assert "0.665" in html
    assert "as of" not in html, "a current figure needs no stamp"


def test_an_expired_window_shows_its_figure_with_a_stamp() -> None:
    html = server.render_carry_windows(
        _row(
            settled_funding_windows={"1d": None, "7d": None, "30d": None},
            settled_funding_windows_last_complete={
                "1d": {"net": -0.5159, "asof_ms": 1_788_000_000_000}
            },
        )
    )

    assert "0.516" in html, "the exact total we already hold must be shown"
    assert "as of" in html and "UTC" in html, "and it must be stamped"
    assert "carry-stale" in html


def test_a_window_we_do_not_hold_is_still_a_dash() -> None:
    """No settlement history and flat carry remain opposite conclusions."""

    html = server.render_carry_windows(_row())

    assert "&mdash;" in html
    assert "as of" not in html


def test_an_unknown_settlement_time_is_labelled_not_invented() -> None:
    html = server.render_carry_windows(
        _row(settled_funding_windows_last_complete={"1d": {"net": 1.25, "asof_ms": None}})
    )

    assert "1.250" in html
    assert "last complete" in html, "never fabricate a timestamp"


def test_a_current_figure_is_never_replaced_by_a_stale_one() -> None:
    """Precedence matters: current wins whenever both exist."""

    html = server.render_carry_windows(
        _row(
            settled_funding_windows={"1d": 2.0},
            settled_funding_windows_last_complete={"1d": {"net": 9.9, "asof_ms": 1}},
        )
    )

    assert "2.000" in html
    assert "9.900" not in html


def test_the_raw_file_is_not_reparsed_on_every_call(tmp_path, monkeypatch):
    """An 11.5MB parse per board row is what this cache exists to prevent.

    Without it a single render re-reads the whole funding document once per
    stale row, which is how this timed out in production before the cache.
    """

    cache = tmp_path / "venue_funding_history.json"
    cache.write_text(
        json.dumps({"schema": venue_funding_history.SCHEMA, "legs": {}, "leg_status": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(venue_funding_history, "_RAW_CACHE", {"stamp": None, "path": None, "payload": {}})

    reads = []
    real_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    for _ in range(5):
        venue_funding_history._load_raw(cache_path=cache)
    assert len(reads) == 1, f"re-parsed the file {len(reads)} times for 5 calls"

    # A genuinely newer file must still be picked up: staleness here would
    # freeze the board on whichever generation happened to be read first.
    cache.write_text(
        json.dumps(
            {
                "schema": venue_funding_history.SCHEMA,
                "legs": {"Gate|X/USDT:USDT": {"1d": 0.5}},
                "leg_status": {},
            }
        ),
        encoding="utf-8",
    )
    os.utime(cache, (time.time() + 5, time.time() + 5))
    refreshed = venue_funding_history._load_raw(cache_path=cache)
    assert refreshed.get("legs", {}).get("Gate|X/USDT:USDT") == {"1d": 0.5}
    assert len(reads) == 2
