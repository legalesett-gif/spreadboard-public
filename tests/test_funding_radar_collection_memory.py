"""The real evidence pass must release rich positive rows while collecting."""

import json
import weakref

from scripts import run_spreadboard_service as service
from spreadboard import funding_catalog, funding_radar, market_history, research_calibration, server


def _route(index, **overrides):
    return {
        'token': f'TOKEN{index}', 'route_key': f'catalogue-{index}',
        'route_kind': 'SPOT-FUTURES',
        'long_venue': 'Mexc', 'long_market_type': 'Spot',
        'long_market_symbol': f'TOKEN{index}/USDT', 'long_quote': 'USDT',
        'short_venue': 'Gate', 'short_market_type': 'Futures',
        'short_market_symbol': f'TOKEN{index}/USDT:USDT', 'short_quote': 'USDT',
        'funding_daily_pct': index + 1.0,
        'unrelated_token_metadata': {'text': 'large description ' * 1000},
        **overrides,
    }


def test_actual_evidence_pass_keeps_last_route_per_identity_without_rich_rows(
    monkeypatch, tmp_path,
):
    alive = weakref.WeakValueDictionary()
    peak = 0

    class Row(dict):
        pass

    def candidates(**kwargs):
        nonlocal peak
        for index in range(200):
            row = Row(_route(index))
            alive[index] = row
            peak = max(peak, len(alive))
            yield row

    warm = _route(0, route_key='warm-0', funding_daily_pct=1000)
    leader = _route(0, route_key='leader-0', funding_daily_pct=2000)
    monkeypatch.setattr(service, 'WARM_QUERIES', [{'funding_only': True}])
    monkeypatch.setattr(service, 'FUNDING_ARCHIVE_QUERIES', [])
    monkeypatch.setattr(service, '_refresh_complete_funding_catalog', lambda **kwargs: None)
    monkeypatch.setattr(service.funding_history_demand, 'enqueue', lambda legs: None)
    monkeypatch.setattr(server, 'api_market_spreads', lambda *args: {
        'groups': [{'routes': [warm], 'best_funding_route': leader}],
    })
    monkeypatch.setattr(market_history, 'write_funding_windows', lambda keys: len(keys))
    monkeypatch.setattr(research_calibration, 'capture_routes', lambda routes: {'inserted': 1})
    monkeypatch.setattr(research_calibration, 'label_matured', lambda: {'labeled': 0})
    monkeypatch.setattr(funding_catalog, '_iter_routes', candidates)
    monkeypatch.setattr(funding_catalog, '_window_value', lambda *args: 0)
    monkeypatch.setattr(funding_radar.venue_funding_history, 'route_windows', lambda route: {
        '1d': route['funding_daily_pct'] / 2, '7d': 7.0, '30d': None,
    })
    path = tmp_path / 'radar.json'
    refresh = funding_radar.refresh
    monkeypatch.setattr(funding_radar, 'refresh', lambda routes: refresh(
        routes, cache_path=path, now=1_000_000,
    ))

    service._refresh_funding_windows()

    # These assertions also detect errors swallowed by the worker's outer guard.
    records = json.loads(path.read_text())['records']
    assert len(records) == 200
    assert set(records) == {'leader-0', *(f'catalogue-{i}' for i in range(1, 200))}
    assert records['leader-0']['route']['funding_daily_pct'] == 2000
    assert records['leader-0']['windows'] == {'1d': 1000, '7d': 7.0, '30d': None}
    assert records['catalogue-199']['windows']['1d'] == 100
    assert all('unrelated_token_metadata' not in r['route'] for r in records.values())
    assert peak <= 3, f'evidence pass retained {peak} rich positive candidate rows'


def test_evidence_releases_unselected_query_cache_before_history_and_radar(monkeypatch):
    from spreadboard import api_spreads

    class CachedRow(dict):
        pass

    unselected = CachedRow(token="UNSELECTED")
    reference = weakref.ref(unselected)
    monkeypatch.setattr(api_spreads, "_ROW_CACHE", {"selection": (0, [unselected])})
    monkeypatch.setattr(api_spreads, "_RESULT_CACHE", {"selection": {"rows": [unselected]}})
    del unselected
    warm = _route(1)
    phases = []
    monkeypatch.setattr(service, "WARM_QUERIES", [{"funding_only": True}])
    monkeypatch.setattr(service, "FUNDING_ARCHIVE_QUERIES", [])
    monkeypatch.setattr(service, "_refresh_complete_funding_catalog", lambda **kw: None)
    monkeypatch.setattr(server, "api_market_spreads", lambda *a: {
        "groups": [{"routes": [warm], "best_funding_route": warm}],
    })

    def history(legs):
        assert reference() is None
        assert legs == [("Gate", "TOKEN1/USDT:USDT")]
        phases.append("history")

    def radar(routes):
        assert reference() is None
        assert len(routes) == 1 and routes[0]["token"] == "TOKEN1"
        phases.append("radar")
        return 1

    monkeypatch.setattr(service.funding_history_demand, "enqueue", history)
    monkeypatch.setattr(market_history, "write_funding_windows", lambda keys: len(keys))
    monkeypatch.setattr(funding_catalog, "archive_routes", lambda: iter(()))
    monkeypatch.setattr(funding_radar, "refresh", radar)
    monkeypatch.setattr(research_calibration, "capture_routes", lambda routes: {"inserted": 1})
    monkeypatch.setattr(research_calibration, "label_matured", lambda: {"labeled": 0})
    service._refresh_funding_windows()
    # The outer worker guard swallows failures, so both downstream phases
    # must actually complete while preserving the selected route.
    assert phases == ["history", "radar"]
