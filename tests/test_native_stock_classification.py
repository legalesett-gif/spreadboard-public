"""Native market identity must survive catalogue and discovery serialization."""
import time
from types import SimpleNamespace

import ccxt
import pytest
from spreadarb.market_status import native_market_asset_class

from spreadarb.api_discovery import sources
from spreadboard import (
    api_spreads,
    catalog_pairs,
    chart_catalog,
    funding_catalog,
    live_book_cache,
    tokenized_assets,
)


@pytest.mark.parametrize('venue,info', [('Bitget', {'isRwa':'YES'}), ('Binance', {'underlyingType':'EQUITY'})])
def test_exact_stock_market_survives_catalogue_pairs_and_cannot_bypass_guard(monkeypatch, venue, info):
    market = {'id':'BNCUSDT','base':'BNC','symbol':'BNC/USDT:USDT','quote':'USDT','settle':'USDT','swap':True,'active':True,'contractSize':1,'info':info}
    client = SimpleNamespace(load_markets=lambda: {market['symbol']:market})
    monkeypatch.setattr(ccxt, chart_catalog.VENUE_IDS[venue], lambda *a, **kw: client)
    items = chart_catalog._load_venue(venue, 'Futures')
    assert items[0]['asset_class'] == 'tokenized'
    other = {**items[0], 'venue':'Gate'}
    other.pop('asset_class')
    monkeypatch.setattr(chart_catalog, 'load', lambda: {'markets':[items[0],other]})
    book = live_book_cache.CachedBook(bids=[[4.0,10000]],asks=[[4.001,10000]],quote_ts_us=int(time.time()*1e6))
    monkeypatch.setattr(live_book_cache, 'load_live_book', lambda *a, **kw:book)
    monkeypatch.setattr(catalog_pairs.bulk_quotes, 'load_funding', dict)
    monkeypatch.setattr(catalog_pairs.public_rails, 'load_public_rails', dict)
    monkeypatch.setattr(catalog_pairs.venue_funding_history, 'route_windows', lambda r:{'1d':None,'7d':None,'30d':None})
    monkeypatch.setattr(tokenized_assets, 'load_registry', lambda *a, **kw:{})
    rows = catalog_pairs.for_token('BNC', use_cache=False)['routes']
    assert rows
    for row in rows:
        assert row['asset_class'] == 'tokenized'
        assert row['mirage_guarded'] is True
        assert row['tokenized_guard']['status'] == 'blocked'
        assert not funding_catalog._common_eligible(row, route_kind=None,symbol=None,exchange=None,quote=None)
    assert native_market_asset_class(venue, {**market,'swap':False,'spot':True}) is None
    assert tokenized_assets.classify({'token':'BNC','long_market_symbol':'BNC/USDT','short_market_symbol':'BNC/USDT'})['asset_class'] == 'crypto'


def test_discovery_ticker_metadata_survives_notes_and_public_row(monkeypatch):
    symbol = 'BNC/USDT:USDT'
    market = {'symbol':symbol,'swap':True,'quote':'USDT','info':{'isRwa':'YES'}}
    exchange = SimpleNamespace(markets={symbol:market})
    monkeypatch.setattr(sources, '_fetch_tickers', lambda *a:{symbol:{'bid':4.0,'ask':4.01}})
    monkeypatch.setattr(sources, '_resolve_quote_identity', lambda **kw:SimpleNamespace(market_identity=None,identity_key=None,blockers=[]))
    quote = sources._ticker_quotes_for_symbols(exchange=exchange,venue='Bitget',market_type='Futures',source_name='fixture',symbol_map={'BNC':symbol},funding_rates={},errors=[],context=None)[0]
    raw = {'token':'BNC','long_venue':'Bitget','short_venue':'Binance','long_market_type':'Futures','short_market_type':'Futures','quote_ts_us':int(time.time()*1e6),'notes':sources._quote_pair_notes(quote,quote),'source_kind':'api_discovered'}
    monkeypatch.setattr(tokenized_assets, 'load_registry', lambda *a, **kw:{})
    row = api_spreads._row_from_api(raw,bucket='api_discovered_rows',now=time.time(),live_funding={})
    assert row.asset_class == 'tokenized'
    public = api_spreads._public_row(row)
    assert public['tokenized_guard']['asset_class'] == 'tokenized'
    assert public['tokenized_guard']['status'] == 'blocked'
