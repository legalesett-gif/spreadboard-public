"""A display shortlist must not become a permanent candidate shortlist."""
import base64
import zlib

import orjson
import pytest
from spreadboard.packed_routes import MAX_TOKEN_BYTES, PackedRoutes

from spreadboard import funding_catalog as f


@pytest.fixture
def candidate_catalog(monkeypatch, tmp_path):
    rows = [{
        "token": "CASE", "route_key": f"long-{i}", "route_kind": "FUTURES",
        "long_venue": f"Long{i}", "long_market_type": "Futures",
        "long_market_symbol": "CASE/USDT:USDT", "long_quote": "USDT",
        "short_venue": "Gate", "short_market_type": "Futures",
        "short_market_symbol": "CASE/USDT:USDT", "short_quote": "USDT",
        "funding_apr_pct": 100.0-i, "displayed_open_spread_pct": float(i),
    } for i in range(6)]
    rates = {f"Long{i}|CASE/USDT:USDT": {"rate_pct": i/100, "interval_hours": 8, "age_seconds": 1}
             for i in range(6)}
    rates['Gate|CASE/USDT:USDT'] = {"rate_pct": .1, "interval_hours": 8, "age_seconds": 1}
    monkeypatch.setattr(f, 'DEFAULT_CACHE_PATH', tmp_path/'funding.json')
    monkeypatch.setattr(f, '_CACHE_PAYLOADS', {})
    monkeypatch.setattr(f, '_CACHE_RESTORE_ATTEMPTED', True)
    monkeypatch.setattr(f, '_CACHE_BUILDING', False)
    monkeypatch.setattr(f.chart_catalog, 'load', lambda: {'markets': [{'token': 'CASE', 'market_type': 'Futures'}]})

    def build(tokens, *, route_reducer, **kwargs):
        return {'CASE': {'routes': route_reducer(rows)}}

    monkeypatch.setattr(f.catalog_pairs, 'for_tokens', build)
    monkeypatch.setattr(f.bulk_quotes, 'load_funding', lambda: rates)
    monkeypatch.setattr(f.funding_radar, 'routes_for', lambda *args, **kwargs: [])
    monkeypatch.setattr(f, '_resident_live_overlay', lambda rows: rows)
    monkeypatch.setattr(f.venue_funding_history, 'load', dict)
    monkeypatch.setattr(f, '_window_value', lambda route, label, **kwargs: (5.0 if route['long_venue'] == 'Long5' else .1) if label != '30d' else None)
    monkeypatch.setenv('SPREADBOARD_SERVICE_ROLE', 'web')
    f.refresh_cache()
    # Exercise the durable handoff, not only the in-memory callback result.
    monkeypatch.setattr(f, '_CACHE_PAYLOADS', {})
    assert f.reload_persisted_cache()['ready']
    return rows, rates


@pytest.mark.parametrize('surface', ['page', 'navigation'])
def test_later_best_long_is_not_lost_to_build_time_rates(candidate_catalog, surface):
    _, rates = candidate_catalog
    rates['Long5|CASE/USDT:USDT']['rate_pct'] = -.2
    page = f.page(window='now') if surface == 'page' else f.build_navigation_pages()[('FUTURES', 'now')]
    assert page['groups'][0]['best_funding_route']['route_key'] == 'long-5'
    assert page['largest_value'] == pytest.approx(.9)
    assert page['matching_route_count'] == 6
    assert page['returned_route_count'] == 1  # same short contract, best current hedge


@pytest.mark.parametrize('surface', ['page', 'navigation'])
def test_historical_winner_is_selected_independently_of_now(candidate_catalog, surface):
    page = f.page(window='7d') if surface == 'page' else f.build_navigation_pages()[('FUTURES', '7d')]
    assert page['groups'][0]['best_funding_route']['route_key'] == 'long-5'
    assert page['largest_value'] == 5
    assert page['matching_route_count'] == 6
    assert not (f.page(window='30d') if surface == 'page' else f.build_navigation_pages()[('FUTURES', '30d')])['rows']


def test_exchange_filter_can_select_a_previously_lower_ranked_hedge(candidate_catalog):
    page = f.page(window='now', exchange='Long5')
    assert [r['route_key'] for r in page['rows']] == ['long-5']
    assert page['largest_value'] == pytest.approx(.15)


def test_exact_token_preserves_all_distinct_long_alternatives(candidate_catalog):
    assert len(f.page(window='now', symbol='CASE')['rows']) == 6


def test_packed_rows_roundtrip_is_lossless_and_iteration_is_isolated():
    rows = [{'token': '龙虾', 'tiny': 1e-18, 'large': 1788631773123456, 'guard': {'rankable': False},
             'null': None, 'list': [1, 'USDC', False], 'route_key': 'x'}]
    packed = PackedRoutes.restore(PackedRoutes(rows).envelope())
    assert list(packed) == rows
    changed = list(packed)
    changed[0]['guard']['rankable'] = True
    assert list(packed) == rows


@pytest.mark.parametrize('corruption', ['oversize', 'count', 'zero_count', 'truncated', 'trailing', 'inflated'])
def test_invalid_packed_generation_is_rejected_without_replacing_previous(monkeypatch, tmp_path, corruption):
    old = {'OLD': {'routes': [{'route_key': 'old'}]}}
    envelope = PackedRoutes([{'route_key': 'new'}]).envelope()
    if corruption == 'oversize': envelope['raw_bytes'] = MAX_TOKEN_BYTES + 1
    if corruption == 'count': envelope['count'] = 2
    if corruption == 'zero_count': envelope['count'] = 0
    if corruption == 'truncated': envelope['data'] = envelope['data'][:-5]
    if corruption == 'trailing': envelope['data'] = base64.b64encode(base64.b64decode(envelope['data']) + b'extra').decode()
    if corruption == 'inflated': envelope['data'] = base64.b64encode(zlib.compress(b' ' * 5000)).decode()
    path = tmp_path/'cache.json'
    path.write_bytes(orjson.dumps({'schema': f.PERSISTED_SCHEMA, 'saved_at_unix': 123, 'payloads': {'NEW': {'routes': envelope}}}))
    monkeypatch.setattr(f, 'DEFAULT_CACHE_PATH', path)
    monkeypatch.setattr(f, '_CACHE_PAYLOADS', old)
    monkeypatch.setattr(f, '_CACHE_BUILDING', False)
    assert f.reload_persisted_cache()['persist_error']
    assert f._CACHE_PAYLOADS is old


def test_streamed_rank_keeps_late_winners_and_counts_before_display(monkeypatch):
    rows = [{"token": "BULK", "route_key": str(i), "route_kind": "FUTURES",
             "short_venue": f"Short{i % 15}", "short_market_type": "Futures",
             "short_market_symbol": "BULK/USDT:USDT", "long_venue": "Long",
             "funding_daily_pct": i+1, "displayed_open_spread_pct": 1} for i in range(1600)]
    monkeypatch.delenv('SPREADBOARD_SERVICE_ROLE', raising=False)
    monkeypatch.setattr(f, '_complete_payloads', lambda: {'BULK': {'routes': PackedRoutes(rows)}})
    sizes=[]
    monkeypatch.setattr(f, '_resident_live_overlay', lambda batch: sizes.append(len(batch)) or batch)
    page=f.page()
    assert page['largest_value'] == 1600
    assert page['matching_route_count'] == 1600
    assert len(page['rows']) == 7
    assert len({r['short_venue'] for r in page['rows']}) == 7
    assert max(sizes) <= 512


def test_archive_does_not_retain_rejected_candidate_dictionaries(monkeypatch):
    import weakref
    alive = weakref.WeakValueDictionary()
    peak = 0

    class Row(dict):
        pass

    def candidates(**kwargs):
        nonlocal peak
        for i in range(500):
            row = Row(route_key=str(i), funding_daily_pct=1 if i == 499 else -1)
            alive[i] = row
            peak = max(peak, len(alive))
            yield row

    monkeypatch.setattr(f, '_iter_routes', candidates)
    monkeypatch.setattr(f, '_window_value', lambda *args, **kwargs: 0)
    assert [r['route_key'] for r in f.archive_routes()] == ['499']
    assert peak <= 3
