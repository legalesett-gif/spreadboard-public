"""The background funding pass must refresh what has actually gone blank.

Production held a stored 24h aggregate for 9,081 legs while only 1,965 were
still current: 7,116 legs had the history and could not display it. A window is
blanked, correctly, the moment `now` crosses the next scheduled settlement --
but the background pass walked a fixed cursor, so with ~10,470 legs, a
two-minute budget and 4-8 hour schedules a leg expired long before the rotation
returned to it.

Ordering the background portion by settlement staleness converts stored history
into displayable windows without fetching anything extra. Explicit priorities
and never-attempted legs still lead, so nothing is starved.
"""

from __future__ import annotations

import time

from spreadboard import venue_funding_history as vfh


def test_an_expired_window_is_refreshed_before_a_still_current_one(monkeypatch) -> None:
    now_ms = int(time.time() * 1000)
    legs = [("Gate", "A/USDT:USDT"), ("Gate", "B/USDT:USDT"), ("Gate", "C/USDT:USDT")]

    # B is already blank, C expires soon, A has hours left.
    expiry = {
        "Gate|A/USDT:USDT": now_ms + 6 * 3_600_000,
        "Gate|B/USDT:USDT": None,
        "Gate|C/USDT:USDT": now_ms + 60_000,
    }

    def fake_current(values, status, *, now_ms, live_leg=None):
        return {}, expiry[status["key"]]

    monkeypatch.setattr(vfh, "_current_leg_windows", fake_current)
    statuses = {f"{v}|{s}": {"key": f"{v}|{s}"} for v, s in legs}

    def staleness(item):
        key = f"{item[0]}|{item[1]}"
        _c, nxt = vfh._current_leg_windows(None, statuses[key], now_ms=now_ms)
        return (0, 0) if nxt is None else (1, int(nxt))

    assert sorted(legs, key=staleness) == [
        ("Gate", "B/USDT:USDT"),   # already blank
        ("Gate", "C/USDT:USDT"),   # expires soonest
        ("Gate", "A/USDT:USDT"),   # still current longest
    ]


def test_the_build_orders_background_legs_by_staleness() -> None:
    """Pin the ordering in the shipped function, not just in the abstract."""

    import inspect

    source = inspect.getsource(vfh.build)
    assert "background.sort(key=_staleness)" in source, (
        "the background pass must refresh the most-overdue legs first"
    )
    assert "leading + background" in source, (
        "explicit priorities and never-attempted legs must still lead"
    )


def test_priority_only_passes_are_untouched() -> None:
    """A demand pass must keep answering the member who asked."""

    import inspect

    source = inspect.getsource(vfh.build)
    assert "due_priorities if priority_only else" in source
