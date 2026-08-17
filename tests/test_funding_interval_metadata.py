"""A venue that publishes its schedule must never be assumed into the default.

The interval multiplies every carry number: a rate on a 4h contract pays six
times a day, not three, so defaulting to 8h halves it. Bitget's bulk
`fetch_funding_rates` returns `interval: None` for all 759 contracts while the
singular call returns "4h", so 693 legs sat on an assumed 8h default. Its own
market metadata carries the answer per contract -- 427 at 8h, 395 at 4h, one at
1h and one at 2h -- so the schedule was available the whole time.

This reads that metadata generically rather than special-casing one venue: any
exchange whose markets publish the interval now gets it.
"""

from __future__ import annotations

from spreadboard.fast_quotes import _market_interval_hours


def test_bitget_publishes_the_interval_as_fund_interval() -> None:
    """The exact shape Bitget returns: a string count of hours."""
    assert _market_interval_hours({"info": {"fundInterval": "4"}}) == 4.0


def test_an_eight_hour_contract_on_the_same_venue_still_reads_eight() -> None:
    """Bitget is mixed: assuming either value for all of it is wrong."""
    assert _market_interval_hours({"info": {"fundInterval": "8"}}) == 8.0


def test_the_one_and_two_hour_contracts_are_not_rounded_away() -> None:
    assert _market_interval_hours({"info": {"fundInterval": "1"}}) == 1.0
    assert _market_interval_hours({"info": {"fundInterval": "2"}}) == 2.0


def test_other_spellings_of_the_same_field_are_understood() -> None:
    assert _market_interval_hours({"info": {"fundingIntervalHours": 4}}) == 4.0
    assert _market_interval_hours({"info": {"funding_interval_hours": "6"}}) == 6.0


def test_a_market_without_a_published_schedule_returns_none() -> None:
    """None lets the caller fall back and flag the value as assumed."""
    assert _market_interval_hours({"info": {}}) is None
    assert _market_interval_hours({}) is None
    assert _market_interval_hours(None) is None


def test_an_impossible_schedule_is_refused_rather_than_trusted() -> None:
    """No perpetual settles daily; 24 is a misread field."""
    assert _market_interval_hours({"info": {"fundInterval": "24"}}) is None
    assert _market_interval_hours({"info": {"fundInterval": "0"}}) is None
    assert _market_interval_hours({"info": {"fundInterval": ""}}) is None
    assert _market_interval_hours({"info": {"fundInterval": "abc"}}) is None


# --------------------------------------------------------------------------
# Saying which of the two it was
# --------------------------------------------------------------------------


def test_a_published_interval_is_not_labelled_an_assumption() -> None:
    """The badge is the reader's only signal that a carry number is a guess.

    Reading Bitget's real 4h schedule while still flagging it "assumed" trades
    one wrong statement for another: the arithmetic becomes right and the
    provenance becomes wrong.
    """
    from spreadboard.fast_quotes import _funding_fields

    fields = _funding_fields(0.00005, interval_hours=4.0, interval_assumed=False)

    assert fields["funding_interval_hours"] == 4.0
    assert fields["funding_interval_assumed"] is False


def test_a_defaulted_interval_is_still_labelled_an_assumption() -> None:
    from spreadboard.fast_quotes import _funding_fields

    fields = _funding_fields(0.00005, interval_hours=8.0, interval_assumed=True)

    assert fields["funding_interval_assumed"] is True
