"""The funding interval is a multiplier on every carry number on the board.

0.01% is 0.03%/day on an 8-hour contract and 0.24%/day on a 1-hour one, so
getting it wrong is an eightfold error in the APR, not a rounding difference.
"""

from __future__ import annotations

import pytest

from spreadboard import funding_interval as fi


def test_float_noise_snaps_to_the_real_schedule() -> None:
    """1,102 Kucoin legs arrived as 3.9999999999999996 and 7.999999999999999."""
    assert fi.normalise(3.9999999999999996) == 4.0
    assert fi.normalise(7.999999999999999) == 8.0
    assert fi.normalise(0.9999999) == 1.0


def test_a_schedule_no_perpetual_uses_is_refused() -> None:
    """One Mexc leg reported 24 hours. Annualising from that understates the
    carry threefold, so it is refused rather than trusted."""
    assert fi.normalise(24.0) is None
    assert fi.normalise(72) is None
    assert fi.normalise(0) is None
    assert fi.normalise(-4) is None
    assert fi.normalise(None) is None
    assert fi.normalise("nonsense") is None


def test_the_real_schedules_pass_through() -> None:
    for hours in fi.KNOWN_INTERVALS_HOURS:
        assert fi.normalise(hours) == hours


def test_a_published_interval_beats_an_assumption() -> None:
    """A quarter of futures legs carried an assumed interval; an assumption must
    never overwrite what the venue actually published."""
    assert fi.resolve(published=4, assumed=8) == (4.0, False)
    assert fi.resolve(published=1, scheduled=8, assumed=8) == (1.0, False)


def test_the_venues_own_schedule_counts_as_measured() -> None:
    """Two consecutive settlement times are a measurement, not a guess."""
    assert fi.resolve(published=None, scheduled=4.0) == (4.0, False)
    assert fi.resolve(published=24, scheduled=4) == (4.0, False)


def test_a_pure_guess_is_marked_as_one() -> None:
    hours, assumed = fi.resolve()
    assert hours == fi.DEFAULT_INTERVAL_HOURS
    assert assumed is True


def test_the_interval_can_be_read_off_the_settlement_times() -> None:
    hour = 3_600_000_000
    assert fi.from_schedule(8 * hour, 0) == 8.0
    assert fi.from_schedule(4 * hour, 0) == 4.0
    assert fi.from_schedule(hour, 0) == 1.0
    # A gap no venue uses is not a schedule.
    assert fi.from_schedule(30 * hour, 0) is None


def test_per_day_refuses_rather_than_guesses() -> None:
    assert fi.per_day(0.01, 1) == pytest.approx(0.24)
    assert fi.per_day(0.01, 8) == pytest.approx(0.03)
    assert fi.per_day(0.01, 3.9999999999999996) == pytest.approx(0.06)
    assert fi.per_day(0.01, 24) is None
    assert fi.per_day(None, 8) is None


def test_the_board_uses_it() -> None:
    import inspect

    from spreadboard import api_spreads

    assert "funding_interval" in inspect.getsource(api_spreads._per_day)
    assert "funding_interval" in inspect.getsource(api_spreads._apply_live_funding)
