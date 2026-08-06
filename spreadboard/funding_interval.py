"""One place that decides how often a contract pays funding.

The interval is a multiplier on every carry number the board shows: a rate of
0.01% is 0.03%/day on an 8-hour contract and 0.24%/day on a 1-hour one, so
getting it wrong is an eightfold error in the APR, not a rounding difference.

Three things were going wrong, all of them here now:

1. **Float noise.** Intervals arriving as a division came through as
   3.9999999999999996 and 7.999999999999999 -- 1,102 legs on Kucoin Futures --
   which are the same as 4 and 8 but group separately and print oddly.
2. **Guesses outranking measurements.** A quarter of futures legs (3,901 of
   18,788) carried an *assumed* interval. An assumption must never overwrite a
   value the venue published, and must never silently look like one.
3. **Impossible values.** A single Mexc leg reported 24 hours. No perpetual
   settles daily; that is a misread field, and annualising from it understates
   the carry threefold.
"""

from __future__ import annotations

from typing import Any

#: What perpetual venues actually use. Every major venue settles on one of
#: these; anything else is a misread field or a unit mix-up.
KNOWN_INTERVALS_HOURS: tuple[float, ...] = (1.0, 2.0, 4.0, 6.0, 8.0, 12.0)

#: How far a reported value may sit from a known interval and still be taken as
#: that interval. Covers float noise, not a genuinely different schedule.
SNAP_TOLERANCE_HOURS = 0.05

#: Used only when a venue publishes a rate with no interval and nothing else can
#: be derived. Eight hours is the most common schedule, and the value is flagged
#: as assumed so nothing downstream can mistake it for a measurement.
DEFAULT_INTERVAL_HOURS = 8.0


def normalise(value: Any) -> float | None:
    """Snap a reported interval to the schedule it is obviously meant to be.

    Returns None for anything that is not a usable interval, so the caller can
    fall back rather than annualise from a number no venue uses.
    """
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    if hours <= 0 or hours != hours or hours in (float("inf"), float("-inf")):
        return None
    for known in KNOWN_INTERVALS_HOURS:
        if abs(hours - known) <= SNAP_TOLERANCE_HOURS:
            return known
    if hours > max(KNOWN_INTERVALS_HOURS):
        # 24h and above is not a perpetual schedule. Refuse rather than guess.
        return None
    return round(hours, 4)


def from_schedule(next_ts_us: Any, previous_ts_us: Any) -> float | None:
    """The interval two consecutive settlement times imply.

    This is a measurement, not a guess: if a venue tells us when the next two
    payments land, the gap between them is the schedule.
    """
    try:
        gap_hours = (float(next_ts_us) - float(previous_ts_us)) / 3_600_000_000.0
    except (TypeError, ValueError):
        return None
    return normalise(gap_hours)


def resolve(
    *,
    published: Any = None,
    scheduled: Any = None,
    assumed: Any = None,
) -> tuple[float, bool]:
    """Settle on one interval and say whether it was measured or assumed.

    Order is deliberate: what the venue published, then what its own schedule
    implies, then a default. Returns (hours, assumed).
    """
    for candidate in (published, scheduled):
        hours = normalise(candidate)
        if hours is not None:
            return hours, False
    hours = normalise(assumed)
    if hours is not None:
        return hours, True
    return DEFAULT_INTERVAL_HOURS, True


def per_day(rate_pct: Any, interval_hours: Any) -> float | None:
    """A single funding print expressed per day, or None if it cannot be.

    Refuses rather than guesses: annualising a rate against an interval we do
    not trust is how a 1-hour contract gets shown as an 8-hour one.
    """
    try:
        rate = float(rate_pct)
    except (TypeError, ValueError):
        return None
    hours = normalise(interval_hours)
    if hours is None or hours <= 0:
        return None
    return rate * 24.0 / hours
