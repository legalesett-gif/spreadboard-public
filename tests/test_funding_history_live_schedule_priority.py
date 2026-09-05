"""Visible expiry and the refresh queue must agree after a cadence change."""

import json

import pytest

from spreadboard import bulk_quotes
from spreadboard import venue_funding_history as history


@pytest.mark.parametrize('hours_since_last, expected_fetches', [(1, 0), (3, 1)])
def test_real_build_refreshes_a_window_expired_by_the_live_schedule(
    monkeypatch, tmp_path, hours_since_last, expected_fetches,
):
    latest = 1_700_000_000_000
    now = latest + hours_since_last * 3_600_000
    next_settlement = latest + (2 if hours_since_last == 1 else 4) * 3_600_000
    path = tmp_path / 'history.json'
    key = 'OKX|ICX/USDT:USDT'
    values = {'1d': 1.0, '7d': 7.0, '30d': 30.0}
    status = {
        'status': 'ok', 'last_attempt_status': 'ok', 'deep_history_checked_at': 'yes',
        'latest_event_at': latest,
        'window_details': {label: {
            'complete': True, 'latest_event_at': latest, 'inferred_interval_hours': 4,
        } for label in values},
    }
    path.write_text(json.dumps({'schema': history.SCHEMA, 'legs': {key: values},
                                'leg_status': {key: status}, 'leg_updated_at': {}}))
    monkeypatch.setattr(history, 'DEFAULT_CACHE_PATH', path)
    monkeypatch.setattr(history, '_CACHE', {
        'stamp': None, 'legs': {}, 'current_legs': {}, 'leg_status': {},
        'leg_updated_at': {}, 'current_until_ms': None, 'funding_stamp': None,
    })
    monkeypatch.setattr(history.time, 'time', lambda: now / 1000)
    monkeypatch.setattr(bulk_quotes, 'load_funding', lambda: {key: {
        'interval_hours': 2, 'next_funding_ts_us': next_settlement * 1000,
    }})
    calls = []

    def fetch(venue, symbol, **kwargs):
        calls.append((venue, symbol))
        settled = next_settlement - 2 * 3_600_000
        return {'status': 'ok', 'entries': [
            {'timestamp': settled - i * 2 * 3_600_000, 'fundingRate': .0001}
            for i in range(373)
        ]}

    monkeypatch.setattr(history, 'leg_history_outcome', fetch)
    # Establish the actual public reader's expiry before exercising scheduling.
    assert (history.load(cache_path=path)[key]['1d'] is None) == bool(expected_fetches)
    history.build([('OKX', 'ICX/USDT:USDT')], priority_legs=[('OKX', 'ICX/USDT:USDT')],
                  priority_only=True, cache_path=path, budget_seconds=10)
    assert len(calls) == expected_fetches
    assert history.load(cache_path=path)[key]['1d'] is not None
