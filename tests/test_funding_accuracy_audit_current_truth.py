"""The operator's Now audit must not waive hourly or settled-tagged errors."""
import sys

import pytest

from scripts import audit_funding_accuracy as audit


def _run(monkeypatch, *, venue, rate, hours, assumed=False, shown=1.0, historical_tag=False):
    route = {'long_venue': 'Mexc', 'long_market_type': 'Spot',
             'short_venue': venue, 'short_market_type': 'Futures',
             'short_market_symbol': 'TOKEN/USDT:USDT',
             'funding_24h_source': 'settled_public_events' if historical_tag else None}
    monkeypatch.setattr(sys, 'argv', ['audit_funding_accuracy.py', '--top', '1'])
    queries = []
    def api(_path, query):
        queries.append(query)
        return {'groups': [{'token': 'TOKEN', 'best_funding_route': route,
                            'best_funding_24h_pct': shown}]} if query['kind'] == ['FUTURES'] else {'groups': []}
    monkeypatch.setattr(audit, 'api_market_spreads', api)
    class Refresher:
        def _bulk_funding_rates(self, requested):
            assert requested == venue
            return {'TOKEN/USDT:USDT': {'current_funding_pct': rate,
                    'funding_interval_hours': hours, 'funding_interval_assumed': assumed}}
        def close(self):
            pass
    monkeypatch.setattr(audit, 'FastQuoteRefresher', Refresher)
    return audit.main(), queries


@pytest.mark.parametrize('venue', ['Kraken Futures', 'Hyperliquid', 'Bybit'])
@pytest.mark.parametrize('historical_tag', [False, True])
def test_now_audit_catches_wrong_current_carry_even_with_hourly_or_history_metadata(monkeypatch, capsys, venue, historical_tag):
    status, queries = _run(monkeypatch, venue=venue, rate=0, hours=1, historical_tag=historical_tag)
    assert status == 1
    assert all(q['funding_window'] == ['now'] for q in queries)
    output = capsys.readouterr().out
    assert 'MISMATCH' in output
    assert 'FORECAST' not in output and 'MEASURED' not in output


@pytest.mark.parametrize(('hours', 'assumed'), [(None, False), (0, False), (8, True), (-1, False), (float('nan'), False)])
def test_unknown_or_invalid_schedule_is_incomplete_not_an_assumed_eight_hour_success(monkeypatch, capsys, hours, assumed):
    status, _ = _run(monkeypatch, venue='Bybit', rate=.01, hours=hours, assumed=assumed, shown=.03)
    assert status == 2
    assert 'unverifiable 1' in capsys.readouterr().out


@pytest.mark.parametrize('hours', [1, 2, 4, 8])
def test_current_projection_is_compared_on_the_actual_schedule(monkeypatch, capsys, hours):
    status, _ = _run(monkeypatch, venue='Hyperliquid', rate=.02, hours=hours, shown=.02*24/hours)
    assert status == 0
    assert 'matched 1, mismatched 0' in capsys.readouterr().out


def test_empty_audit_does_not_report_success(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['audit_funding_accuracy.py'])
    monkeypatch.setattr(audit, 'api_market_spreads', lambda *a: {'groups': []})
    class Refresher:
        def close(self):
            pass
    monkeypatch.setattr(audit, 'FastQuoteRefresher', Refresher)
    assert audit.main() == 2


@pytest.mark.parametrize('shown', [None, float('nan'), float('inf')])
def test_missing_current_board_value_does_not_become_zero(monkeypatch, shown):
    status, _ = _run(monkeypatch, venue='Bybit', rate=0, hours=1, shown=shown)
    assert status == 2


def test_zero_is_a_real_verifiable_current_rate(monkeypatch):
    status, _ = _run(monkeypatch, venue='Bybit', rate=0, hours=1, shown=0)
    assert status == 0


def test_overflowing_projection_is_unverifiable(monkeypatch):
    status, _ = _run(monkeypatch, venue='Bybit', rate=1e308, hours=1e-308)
    assert status == 2
