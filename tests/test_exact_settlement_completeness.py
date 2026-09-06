"""An exact rolling total cannot omit a known scheduled settlement."""
import json

import pytest

from spreadboard import venue_funding_history as history


@pytest.mark.parametrize('days,hours', [(1, 1), (7, 8), (30, 8)])
def test_one_missing_settlement_never_becomes_a_complete_total(days, hours):
    now = 1_800_000_000_000
    count = days * 24 // hours
    rows = [{'timestamp': now - i * hours * 3_600_000, 'fundingRate': 0.0001}
            for i in range(count)]
    label = f'{days}d'
    full = history.realised_window_details(rows, now_ms=now)
    assert full['windows'][label] == pytest.approx(count * 0.01)
    del rows[count // 2]
    partial = history.realised_window_details(rows, now_ms=now)
    assert partial['windows'][label] is None
    assert not partial['window_details'][label]['complete']


def test_cached_partial_totals_are_rejected_without_discarding_valid_windows(tmp_path, monkeypatch):
    now = 1_800_000_000_000
    rows = [{'timestamp': now - i * 3_600_000, 'fundingRate': 0.0001}
            for i in range(720)]
    details = history.realised_window_details(rows, now_ms=now)
    # Mimic an old publisher that called 23/24 payments complete.
    details['window_details']['1d'].update(event_count=23, coverage_pct=95.83, max_gap_hours=2)
    details['windows']['1d'] = 0.23
    key = 'Gate|AAA/USDT:USDT'
    path = tmp_path / 'history.json'
    path.write_text(json.dumps({'schema': history.SCHEMA, 'legs': {key: details['windows']},
                               'leg_status': {key: {'status': 'ok', 'window_details': details['window_details']}}}))
    monkeypatch.setattr(history.time, 'time', lambda: now / 1000)
    monkeypatch.setitem(history._CACHE, 'stamp', None)
    current = history.load(cache_path=path)
    assert current[key]['1d'] is None
    assert current[key]['7d'] == pytest.approx(1.68)
    assert current[key]['30d'] == pytest.approx(7.2)
    monkeypatch.setattr(history, 'load', lambda: current)
    status = history.route_history_status({'long_venue': 'Gate', 'long_market_type': 'Futures',
                                          'long_market_symbol': 'AAA/USDT:USDT', 'short_market_type': 'Spot'})
    assert 'fewer exact settlements' in status['window_notes']['1d']


def test_extra_fast_settlements_do_not_hide_a_gap_in_slow_schedule():
    now = 1_800_000_000_000
    rows = [{'timestamp': now - i * 8 * 3_600_000, 'fundingRate': 0.0001}
            for i in range(90)]
    # Additional faster settlements inflate the count above the ordinary
    # schedule, but do not repair a missing event elsewhere in the month.
    rows.extend({'timestamp': now - (i * 8 + 4) * 3_600_000, 'fundingRate': 0.0001}
                for i in range(20))
    assert history.realised_window_details(rows, now_ms=now)['windows']['30d'] is not None
    del rows[60]
    assert history.realised_window_details(rows, now_ms=now)['windows']['30d'] is None
