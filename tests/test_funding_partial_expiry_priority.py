"""The background sweep must repair every expired period, not only wholly blank legs."""
import json

import pytest

from spreadboard import bulk_quotes
from spreadboard import venue_funding_history as history


@pytest.mark.parametrize('expiry_cause', ['rolling_boundary', 'live_schedule'])
def test_partial_expiry_precedes_empty_and_current_archives(tmp_path, monkeypatch, expiry_cause):
    now = 1_800_000_000_000
    hour = 3_600_000
    day = 24 * hour
    path = tmp_path / 'history.json'
    legs = [('Gate', f'{symbol}/USDT:USDT') for symbol in ('CURRENT', 'EMPTY', 'PARTIAL')]
    keys = {symbol: f'Gate|{symbol}/USDT:USDT' for symbol in ('CURRENT', 'EMPTY', 'PARTIAL')}
    def detail(days, latest):
        return {'complete': True, 'latest_event_at': latest,
                'earliest_event_at': latest - days * day + 4 * hour,
                'inferred_interval_hours': 4}
    statuses = {
        keys['CURRENT']: {'status': 'ok', 'window_details': {'1d': detail(1, now - 3 * hour)}},
        keys['EMPTY']: {'status': 'no_history_rows'},
        keys['PARTIAL']: {'status': 'ok', 'window_details': {
            '1d': detail(1, now - hour), '7d': detail(7, now - hour)}},
    }
    live = {}
    if expiry_cause == 'rolling_boundary':
        statuses[keys['PARTIAL']]['window_details']['7d']['earliest_event_at'] = now - 7 * day
    else:
        live[keys['PARTIAL']] = {'interval_hours': .25,
                                'next_funding_ts_us': (now + hour // 4) * 1000}
    path.write_text(json.dumps({'schema': history.SCHEMA, 'leg_status': statuses,
                               'legs': {keys['CURRENT']: {'1d': 1.0},
                                        keys['PARTIAL']: {'1d': 1.0, '7d': 7.0}}}))
    monkeypatch.setattr(history, 'DEFAULT_CACHE_PATH', path)
    monkeypatch.setattr(history.time, 'time', lambda: now / 1000)
    monkeypatch.setattr(bulk_quotes, 'load_funding', lambda: live)
    selected = []
    def fetch(items, **_kwargs):
        selected.extend(items)
        return iter(())
    monkeypatch.setattr(history, '_fetch_outcomes_in_batches', fetch)
    history.build(legs, cache_path=path, budget_seconds=10)
    assert selected == [legs[2], legs[1], legs[0]]
