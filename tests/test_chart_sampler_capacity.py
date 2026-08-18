"""A DEX chart must be able to collect its first observation.

Measured in production: one DEX chart sample takes ~19 seconds, because the
on-chain quote is slow. The sampler allowed two at a time and waited 1.5s for a
slot, so a member opening a DEX chart routinely found both slots held.

That alone would only delay them. What made the chart permanently empty is that
the capacity failure was written into the sample cache like a real result:

    result = {"status": "busy", ...}
    _CHART_SAMPLE_CACHE[route_key] = (time.monotonic(), result)

and the scheduler returns a cached entry without re-attempting for the whole
sample interval. So one unlucky poll suppressed the next several, the UI showed
"Stream sampler unavailable", and the chart sat on "Collecting the first
exact-route observation" indefinitely.

A failure to start work is not an observation and must never be cached.
"""

from __future__ import annotations

import inspect

from spreadboard import server


def test_a_capacity_failure_is_not_cached_as_a_result() -> None:
    source = inspect.getsource(server._refresh_chart_route)
    busy = source.index("chart_sampler_capacity")
    tail = source[busy:busy + 900]

    assert "_CHART_SAMPLE_CACHE[route_key]" not in tail, (
        "a busy result is still being cached, which suppresses the retry"
    )


def test_the_waiting_poll_is_still_told_what_happened() -> None:
    """Not caching it must not mean silently returning nothing."""
    source = inspect.getsource(server._refresh_chart_route)

    assert "chart_sampler_capacity" in source
    # The in-flight event must still be released or waiters hang to their timeout.
    busy = source.index("chart_sampler_capacity")
    assert "event.set()" in source[busy:busy + 900]


def test_there_is_room_for_more_than_one_slow_dex_quote() -> None:
    """Two slots against a 19s sample is one member blocking another."""
    assert server._CHART_SAMPLE_SLOTS._initial_value >= 4


def test_a_slot_is_waited_for_longer_than_a_moment() -> None:
    """1.5s against a 19s sample means giving up almost immediately."""
    source = inspect.getsource(server._refresh_chart_route)
    match = [
        line for line in source.splitlines()
        if "_CHART_SAMPLE_SLOTS.acquire" in line
    ]
    assert match, "slot acquisition not found"
    assert "timeout=1.5" not in match[0]
