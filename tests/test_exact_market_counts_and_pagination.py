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
