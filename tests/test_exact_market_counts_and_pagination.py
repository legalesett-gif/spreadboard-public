"""Tokens and route pages are distinct units; every exact route stays reachable."""
import time

import pytest

from spreadboard import api_spreads, catalog_pairs, server, warm_query_projection


def route(index, kind='FUTURES', token='ONE', asset_class='crypto'):
    return {
        'token': token, 'route_key': f'{token}-{index}', 'route_kind': kind,
        'long_venue': f'Long{index}', 'short_venue': 'Short',
        'long_market_type': 'Spot' if kind == 'SPOT-FUTURES' else 'Futures',
        'short_market_type': 'Spot' if kind == 'FUTURES-SPOT' else 'Futures',
        'long_market_symbol': f'{token}/USDT:USDT', 'short_market_symbol': f'{token}/USDT:USDT',
        'long_quote': 'USDT', 'short_quote': 'USDT',
        'long_price': 1, 'short_price': 1.01, 'long_ask': 1, 'short_bid': 1.01,
        'executable_spread_pct': 1, 'depth_weighted_spread_pct': 1,
        'displayed_open_spread_pct': 1, 'matched_size_notional_usd': 500,
        'target_notional_usd': 500, 'depth_usd': 500, 'depth_unverified': False,
        'long_volume_24h_usd': 1000000, 'short_volume_24h_usd': 1000000,
        'funding_daily_pct': .1, 'asset_class': asset_class,
        'freshness': 'fresh', 'quote_ts_us': int(time.time()*1000000),
        'age_min': 0, 'spread_quote_current': True, 'deliverable': True,
        'catalog_history_loaded': True, 'settled_funding_windows': {},
    }


@pytest.fixture
def exact_catalog(monkeypatch):
    rows = [route(i) for i in range(30)] + [route(i, 'SPOT-FUTURES') for i in range(30,35)] + [route(i, 'FUTURES-SPOT') for i in range(35,40)]
    monkeypatch.setattr(catalog_pairs, 'for_token', lambda *a, **k: {'token':'ONE', 'fresh_market_count':41, 'routes':rows, 'route_count':40})
    monkeypatch.setattr(warm_query_projection.LIVE_UNIVERSE, 'target_rows', lambda **k: ([], {'ready':False}))
    return rows


def test_all_exact_routes_are_reachable_and_are_not_called_assets(exact_catalog):
    first = server._exact_catalog_market_projection({'q':['ONE']}, limit=25, offset=0)
    last = server._exact_catalog_market_projection({'q':['ONE']}, limit=25, offset=25)
    assert first['summary']['matching_tokens'] == last['summary']['matching_tokens'] == 1
    assert first['pagination']['has_more'] is True
    assert first['pagination']['has_previous'] is False
    assert last['pagination']['has_more'] is False
    assert last['pagination']['has_previous'] is True
    assert {r['route_key'] for p in (first,last) for r in p['rows']} == {r['route_key'] for r in exact_catalog}
    assert len(first['rows']) == 25 and len(last['rows']) == 15
    assert first['groups'][0]['route_count'] == last['groups'][0]['route_count'] == 40
    html = server.render_market_pagination({'q':['ONE']}, first['pagination'])
    assert '1-25 of 40 routes' in html
    assert 'offset=25' in html and '>Next</a>' in html
    assert '40 assets' not in html


def test_facets_count_distinct_token_beyond_first_page_and_pair_directions(exact_catalog):
    result = server._exact_catalog_market_projection({'q':['ONE']}, limit=25, offset=0)
    assert result['asset_class_counts'] == {'crypto':1}
    assert result['route_kind_token_counts'] == {'FUTURES':1, 'SPOT-FUTURES':1, 'FUTURES-SPOT':1}
    assert result['route_kind_counts']['FUTURES'] == 30
    assert result['lane_token_counts']['FUTURES-SPOT'] == 1
    assert server.market_kind_count('FUTURES-SPOT-PAIR',result['route_kind_token_counts'],result['summary'],result['lane_token_counts']) == 1


def test_exact_filters_keep_facets_and_page_membership_consistent(exact_catalog):
    pair = server._exact_catalog_market_projection({'q':['ONE'],'kind':['FUTURES-SPOT-PAIR']},limit=25,offset=0)
    assert len(pair['rows']) == 10
    assert pair['summary']['expanded_visible_route_count'] == 10
    assert pair['summary']['verified_route_count'] + pair['summary']['research_route_count'] == 10
    assert pair['summary']['matching_rows'] == pair['groups'][0]['route_count'] == 10
    assert pair['route_kind_token_counts']['FUTURES'] == 1
    empty = server._exact_catalog_market_projection({'q':['ONE'],'asset_class':['tokenized']},limit=25,offset=0)
    assert empty['rows'] == []
    assert empty['asset_class_counts'] == {'crypto':1}
    assert empty['route_kind_counts'] == {}


def test_live_headline_asset_counts_do_not_grow_with_venue_permutations():
    rows = [route(i) for i in range(40)] + [route(41, token='TWO')] + [route(42, token='STOCK',asset_class='tokenized')]
    result = warm_query_projection._build_headlines(tuple(rows),{},now=time.time())
    assert result['asset_class_counts'] == {'crypto':2,'tokenized':1}


def test_asset_count_accepts_native_guard_classification_without_counting_routes():
    rows = [{'token':'STOCK','tokenized_guard':{'asset_class':'tokenized'}}]*20 + [{'token':'ONE'}]*10
    assert api_spreads.asset_token_counts(rows) == {'tokenized':1,'crypto':1}


def test_exact_sort_is_applied_before_pagination(exact_catalog):
    for index, row in enumerate(exact_catalog):
        row['funding_daily_pct'] = index / 100
    low = server._exact_catalog_market_projection({'q':['ONE'],'sort':['funding'],'direction':['asc']},limit=5,offset=0)
    high = server._exact_catalog_market_projection({'q':['ONE'],'sort':['funding'],'direction':['desc']},limit=5,offset=0)
    assert [r['funding_daily_pct'] for r in low['rows']] == [0,.01,.02,.03,.04]
    assert [r['funding_daily_pct'] for r in high['rows']] == [.39,.38,.37,.36,.35]


def test_positive_carry_negative_basis_survives_group_render(exact_catalog):
    exact_catalog[:] = [route(0)]
    row = exact_catalog[0]
    row.update(short_price=.99,short_bid=.99,executable_spread_pct=-1,
               displayed_open_spread_pct=-1,depth_weighted_spread_pct=-1)
    page = server._exact_catalog_market_projection({'q':['ONE']},limit=25,offset=0)
    assert len(page['rows']) == 1
    assert 'ONE' in server.render_market_token_group(page['groups'][0])
    assert api_spreads.spread_evidence_state(row) == 'excluded'


@pytest.mark.parametrize('override',[
    {'funding_daily_pct':0}, {'funding_daily_pct':-.1},
    {'identity_mismatch':True}, {'thin_book':True},
    {'quote_ts_us':1}, {'long_quote':'BTC'},
])
def test_carry_renderer_preserves_exclusions(override):
    row = route(0)
    row.update(short_price=.99,short_bid=.99,executable_spread_pct=-1,
               displayed_open_spread_pct=-1,depth_weighted_spread_pct=-1)
    row.update(override)
    assert server.render_market_token_group({'token':'ONE','best_route':row,'routes':[row]}) == ''


@pytest.mark.parametrize('name,field,value,threshold',[
    ('min_market_cap_usd','market_cap_usd',100,101),
    ('max_market_cap_usd','market_cap_usd',100,99),
    ('min_fdv_usd','fdv_usd',200,201),
    ('max_fdv_usd','fdv_usd',200,199),
    ('max_listing_age_days','listing_age_days',10,9),
    ('min_volume_24h_usd','short_volume_24h_usd',1000,1001),
])
def test_exact_advanced_filters_exclude_nonmatching_and_unknown(exact_catalog,name,field,value,threshold):
    exact_catalog[:] = [route(0),route(1)]
    exact_catalog[0][field]=value
    exact_catalog[1].pop(field,None)
    result=server._exact_catalog_market_projection({'q':['ONE'],name:[str(threshold)]},limit=25,offset=0)
    assert result['rows']==[]
    assert result['summary']['matching_rows']==result['summary']['funding_rows']==0
    assert result['filters'][name]==threshold
    assert result['exact_token_found'] is True


def test_exact_persistence_uses_settled_windows(exact_catalog,monkeypatch):
    monkeypatch.setattr(api_spreads.venue_funding_history,'route_windows',lambda row: {'1d':1,'7d':-1,'30d':None})
    result=server._exact_catalog_market_projection({'q':['ONE'],'persistence':['persistent']},limit=25,offset=0)
    assert result['rows']==[]
    mixed=server._exact_catalog_market_projection({'q':['ONE'],'persistence':['mixed']},limit=25,offset=0)
    assert mixed['summary']['matching_rows']==40


def test_exact_headlines_use_all_matching_routes_and_correct_group_shape(exact_catalog):
    for i,row in enumerate(exact_catalog):row['funding_daily_pct']=i/100
    first=server._exact_catalog_market_projection({'q':['ONE'],'sort':['funding'],'direction':['asc']},limit=5,offset=0)
    last=server._exact_catalog_market_projection({'q':['ONE'],'sort':['funding'],'direction':['asc']},limit=5,offset=35)
    for page in (first,last):
        assert page['summary']['funding_rows']==39
        assert page['summary']['max_depth_weighted_spread_pct']==1
        assert page['top_funding'][0]['best_funding_route']['route_key']=='ONE-39'
        assert page['top_funding'][0]['best_funding_24h_pct']==.39
        assert '+0.390%' in server.render_market_mini(page['top_funding'][0],'funding')


def test_empty_exact_filter_does_not_fall_back_to_broad_cache(exact_catalog,monkeypatch,tmp_path):
    monkeypatch.setattr(server,'_market_cache_lookup',lambda *a,**k:pytest.fail('empty exact result fell back'))
    result=server.api_market_spreads(tmp_path/'unused.json',{'q':['ONE'],'min_market_cap_usd':['1']})
    assert result['mode']=='exact_token_complete_catalogue'
    assert result['rows']==[]


def test_volume_sort_uses_thinner_leg_volume_not_probe_depth(exact_catalog):
    from types import SimpleNamespace
    exact_catalog[:] = [route(0),route(1)]
    exact_catalog[0].update(depth_usd=500000,short_volume_24h_usd=10000)
    exact_catalog[1].update(depth_usd=500,long_volume_24h_usd=2000000,short_volume_24h_usd=1000000)
    page=server._exact_catalog_market_projection({'q':['ONE'],'sort':['depth']},limit=25,offset=0)
    assert page['rows'][0]['route_key']=='ONE-1'
    assert api_spreads._route_dict_sort_value(exact_catalog[0],'depth')==10000
    assert api_spreads._sort_value(SimpleNamespace(**exact_catalog[0]),'depth')==10000
    assert api_spreads._group_sort_value({'routes':exact_catalog},'depth')==1000000
    missing={**exact_catalog[0],'short_volume_24h_usd':None}
    assert api_spreads._route_dict_sort_value(missing,'depth')==0


@pytest.mark.parametrize('guard', [
    {'mirage_guarded': True}, {'identity_mismatch': True},
    {'quote_mismatch': True}, {'deliverable': False},
    {'tokenized_guard': {'rankable': False}},
])
def test_final_exact_funding_count_and_leader_share_identity_guards(exact_catalog, monkeypatch, tmp_path, guard):
    exact_catalog[:] = [route(0), route(1)]
    exact_catalog[0].update(guard)
    # The excluded high rate must neither inflate the KPI nor win the sidebar.
    monkeypatch.setattr(api_spreads, 'live_route_updates_for', lambda *a, **k: {
        'ONE-0': (None, 99.0, None), 'ONE-1': (None, .2, None),
    })
    result = server.api_market_spreads(tmp_path/'unused.json', {'q':['ONE'],'limit':['25']})
    assert result['summary']['funding_rows'] == 1
    assert result['top_funding'][0]['best_funding_route']['route_key'] == 'ONE-1'
    assert '+0.200%' in server.render_market_lane('Top Funding Pairs', result['top_funding'], 'funding')


def test_final_exact_count_uses_live_overlay_off_page_and_deduplicates(exact_catalog, monkeypatch, tmp_path):
    exact_catalog[:] = [route(i) for i in range(30)]
    monkeypatch.setattr(api_spreads, 'live_route_updates_for', lambda *a, **k: {
        f'ONE-{i}': (None, .3 if i == 29 else 0.0, None) for i in range(30)
    })
    result = server.api_market_spreads(tmp_path/'unused.json', {'q':['ONE'],'limit':['25']})
    assert result['summary']['funding_rows'] == 1
    assert len(result['rows']) == 25
    assert result['top_funding'][0]['best_funding_route']['route_key'] == 'ONE-29'
