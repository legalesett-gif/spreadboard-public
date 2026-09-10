"""Stock aliases stay searchable without taking duplicate global list slots."""
import time
from dataclasses import replace

import pytest
from test_tokenized_lane_and_executor_boundary import _row

from spreadboard import api_spreads as a
from spreadboard import funding_catalog as f
from spreadboard import tokenized_assets


def stock(token='GPRO', short='IO-GPRO/USDC:USDC'):
    return _row(token=token, asset_class='tokenized', route_kind='FUTURES',
        route_key=token+'|'+short, long_venue='Bybit', short_venue='Hyperliquid',
        long_market_type='Futures', short_market_type='Futures',
        long_market_symbol='GPRO/USDT:USDT', short_market_symbol=short,
        quote_ts_us=int(time.time()*1e6), long_quote='USDT', short_quote='USDC')


def test_spreads_global_groups_and_alias_detail_preserve_exact_contracts(monkeypatch):
    row=stock(); alias=replace(row,token='GPROSTOCK',route_key='alias')
    different=replace(row,short_market_symbol='XYZ-GPRO/USDC:USDC',route_key='other')
    monkeypatch.setattr(a,'tokenized_route_rankable',lambda row:True)
    groups=a._group_rows([alias,row,different])
    assert sum(len(g['routes']) for g in groups)==2
    assert {g['token'] for g in groups}=={'GPRO'}
    detail=a._group_rows([alias])
    assert detail[0]['token']=='GPROSTOCK'
    assert len(detail[0]['routes'])==1


@pytest.fixture
def funding_aliases(monkeypatch):
    base=stock().to_dict()
    base.update(funding_daily_pct=.3,mirage_guarded=False,settled_funding_windows={'1d':.1,'7d':2.,'30d':None})
    alias={**base,'token':'GPROSTOCK','route_key':'alias'}
    payloads={'GPROSTOCK':{'routes':[alias]},'GPRO':{'routes':[base]}}
    monkeypatch.setattr(f,'_complete_payloads',lambda:payloads)
    monkeypatch.setattr(f.funding_radar,'routes_for',lambda *a,**kw:[])
    monkeypatch.setattr(f,'_resident_live_overlay',lambda rows:rows)
    monkeypatch.setattr(f,'_window_value',lambda row,label,**kw:row['settled_funding_windows'][label])
    monkeypatch.setattr(f.venue_funding_history,'load',dict)
    monkeypatch.setattr(f.bulk_quotes,'load_funding',lambda:{
        'Bybit|GPRO/USDT:USDT':{'rate_pct':0.,'interval_hours':8,'age_seconds':1},
        'Hyperliquid|IO-GPRO/USDC:USDC':{'rate_pct':.1,'interval_hours':8,'age_seconds':1},
    })
    monkeypatch.setenv('SPREADBOARD_SERVICE_ROLE','web')
    return base,alias


@pytest.mark.parametrize('surface',['page','navigation'])
@pytest.mark.parametrize('window',['now','7d'])
def test_funding_global_alias_is_counted_and_displayed_once(funding_aliases,surface,window):
    result=f.page(window=window) if surface=='page' else f.build_navigation_pages()[('FUTURES',window)]
    assert result['matching_route_count']==1
    assert result['returned_route_count']==1
    assert result['groups'][0]['token']=='GPRO'


def test_direct_alias_query_keeps_route(funding_aliases):
    page=f.page(symbol='GPROSTOCK')
    assert page['matching_route_count']==1
    assert page['groups'][0]['token']=='GPROSTOCK'


@pytest.mark.parametrize('surface',['page','navigation'])
def test_incomplete_canonical_history_does_not_hide_complete_alias(funding_aliases,surface):
    base,_alias=funding_aliases
    base['settled_funding_windows']={'1d':None,'7d':None,'30d':None}
    result=f.page(window='7d') if surface=='page' else f.build_navigation_pages()[('FUTURES','7d')]
    assert result['matching_route_count']==1
    assert result['groups'][0]['token']=='GPROSTOCK'


def test_crypto_and_incomplete_market_identity_are_not_collapsed():
    row=stock();alias=replace(row,token='GPROSTOCK')
    crypto=[replace(row,asset_class='crypto'),replace(alias,asset_class='crypto')]
    assert tokenized_assets.unique_stock_rows(crypto) is crypto
    unknown=[replace(row,short_market_symbol=None),replace(alias,short_market_symbol=None)]
    assert tokenized_assets.unique_stock_rows(unknown) is unknown


def test_matched_depth_evidence_is_preserved_over_newer_top_only_alias():
    row = stock()
    alias = replace(row, token="GPROSTOCK", depth_weighted_spread_pct=None,
                    quote_ts_us=row.quote_ts_us + 1)
    assert tokenized_assets.unique_stock_rows([alias, row]) == [row]


def test_exact_spot_future_aliases_dedupe_without_collapsing_other_spot_contracts():
    row = replace(stock(), long_market_type="Spot", long_market_symbol="GPRO/USDT")
    alias = replace(row, token="GPROSTOCK")
    other = replace(row, long_market_symbol="GPROX/USDT")
    assert tokenized_assets.unique_stock_rows([alias, row, other]) == [row, other]
