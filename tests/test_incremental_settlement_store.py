from typing import ClassVar

from spreadboard import settlement_store
from spreadboard import venue_funding_history as history

HOUR = 3_600_000
NOW = 1_788_900_000_000


def events(end, count):
    return [{'timestamp': end-i*HOUR, 'fundingRate': 0.0001} for i in range(count)]


def test_overlap_deduplicates_corrects_and_prunes_without_crossing_legs(tmp_path):
    path = tmp_path/'events.sqlite3'
    settlement_store.merge(path, 'A', 'ONE', events(NOW, 770), NOW)
    settlement_store.merge(path, 'A', 'TWO', [{'timestamp': NOW, 'fundingRate': .5}], NOW)
    merged = settlement_store.merge(path, 'A', 'ONE', [{'timestamp': NOW, 'fundingRate': .0002}, {'timestamp': NOW+HOUR, 'fundingRate': .9}], NOW)
    assert len(merged) == 768
    assert merged[-1]['fundingRate'] == .0002
    assert settlement_store.read(path, 'A', 'TWO', NOW)[0]['fundingRate'] == .5


def test_second_provider_fetch_uses_overlap_and_retains_exact_month(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(history.time, 'time', lambda: clock[0]/1000)
    class Client:
        has: ClassVar[dict] = {'fetchFundingRateHistory': True}
        symbols: ClassVar[list] = ['ONE/USDT:USDT']
    requests = []
    def pages(_client, _venue, _symbol, *, since, now_ms, max_pages):
        requests.append((since,max_pages))
        return [r for r in events(now_ms, 745) if r['timestamp'] >= since], max_pages
    monkeypatch.setattr(history, '_history_pages', pages)
    kwargs = {'client_factory': lambda _: Client(), 'event_store_path': tmp_path/'events.sqlite3'}
    first = history.leg_history_outcome('Bybit', 'ONE/USDT:USDT', **kwargs)
    clock[0] += HOUR
    second = history.leg_history_outcome('Bybit', 'ONE/USDT:USDT', **kwargs)
    assert first['status'] == second['status'] == 'ok'
    assert requests[1][0] == NOW-24*HOUR
    assert requests[1][1] < requests[0][1]
    assert len(second['entries']) == 746
    totals = history.realised_windows(second['entries'], now_ms=clock[0])
    assert abs(totals['30d']-7.2) < 1e-8


def test_incomplete_archive_keeps_full_backfill_request(tmp_path, monkeypatch):
    path = tmp_path/'events.sqlite3'
    settlement_store.merge(path, 'Bybit', 'ONE', events(NOW, 12), NOW)
    monkeypatch.setattr(history.time, 'time', lambda: NOW/1000)
    class Client:
        has: ClassVar[dict] = {'fetchFundingRateHistory': True}
        symbols: ClassVar[list] = ['ONE']
    seen = []
    def pages(*args, **kwargs):
        seen.append(kwargs['since'])
        return events(NOW, 745), 8
    monkeypatch.setattr(history, '_history_pages', pages)
    history.leg_history_outcome('Bybit', 'ONE', client_factory=lambda _:Client(), event_store_path=path)
    assert seen == [NOW-31*24*HOUR]


def test_window_expires_when_oldest_payment_leaves_before_next_settlement():
    for days in (1,7,30):
        label=f'{days}d'
        status={'window_details':{label:{'complete':True,'latest_event_at':NOW,'inferred_interval_hours':8,'earliest_event_at':NOW-days*24*HOUR+HOUR}}}
        assert history._window_expiry_ms(status,label) == NOW+HOUR
