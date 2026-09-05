"""Native coverage must work without a CCXT bulk method and exclude dead listings."""
import ccxt
import pytest

from spreadboard import bulk_quotes, fast_quotes


def _bitmart(**changes):
    return {'symbol': 'BTCUSDT', 'base_currency': 'BTC', 'quote_currency': 'USDT',
            'product_type': 1, 'expire_timestamp': 0, 'status': 'Trading',
            'funding_rate': '.005', 'expected_funding_rate': '0',
            'funding_interval_hours': 4, 'funding_time': 1788652800000,
            'index_price': '79900', **changes}


def _coinbase(**changes):
    return {'symbol': 'BTC-PERP', 'base_asset_name': 'BTC', 'quote_asset_name': 'USDC',
            'type': 'PERP', 'trading_state': 'TRADING', 'funding_interval': '3600000000000',
            'quote': {'predicted_funding': '-0.000004', 'index_price': '79900'}, **changes}


def _source(monkeypatch, venue, rows):
    calls = []
    def public(url):
        calls.append(url)
        assert url == fast_quotes.NATIVE_FUNDING_SOURCES[venue]['url']
        return {'data': {'symbols': rows}} if venue == 'BitMart' else rows
    def unavailable(*args):
        raise AssertionError('Native coverage must not depend on a CCXT metadata adapter')
    monkeypatch.setattr(fast_quotes, '_json_url', public)
    monkeypatch.setattr(fast_quotes.FastQuoteRefresher, '_client', unavailable)
    return calls


@pytest.mark.parametrize('reader', ['bulk', 'point'])
def test_bitmart_current_zero_and_four_hour_schedule_survive_missing_adapter(monkeypatch, tmp_path, reader):
    calls = _source(monkeypatch, 'BitMart', [_bitmart(), _bitmart(symbol='OLDUSDT', base_currency='OLD', status='Delisted')])
    if reader == 'point':
        result = fast_quotes._native_current_funding('BitMart', 'BTC/USDT:USDT')
        assert result['current_funding_pct'] == 0
        assert result['funding_interval_hours'] == 4
    else:
        path = tmp_path/'funding.json'
        result = bulk_quotes.sweep_funding(['BitMart'], cache_path=path)
        cache = bulk_quotes.load_funding(cache_path=path)
        assert result['legs'] == 1
        assert set(cache) == {'BitMart|BTC/USDT:USDT'}
        assert cache['BitMart|BTC/USDT:USDT']['rate_pct'] == 0
        assert cache['BitMart|BTC/USDT:USDT']['interval_hours'] == 4
        assert cache['BitMart|BTC/USDT:USDT']['interval_assumed'] is False
    assert len(calls) == 1


@pytest.mark.parametrize('changes', [
    {'status':'Delisted'}, {'product_type':2}, {'expire_timestamp':1788652800000},
    {'expected_funding_rate':None}, {'funding_interval_hours':None},
    {'base_currency':''}, {'quote_currency':'BTC'}, {'symbol':'ETHUSDT'},
])
def test_bitmart_cannot_publish_dead_or_unresolved_contracts(monkeypatch, changes):
    _source(monkeypatch, 'BitMart', [_bitmart(**changes)])
    assert fast_quotes.FastQuoteRefresher()._bulk_funding_rates('BitMart') == {}


@pytest.mark.parametrize('nanos,hours', [('3600000000000',1),('14400000000000',4)])
def test_coinbase_bulk_projection_uses_native_nanosecond_schedule(monkeypatch, tmp_path, nanos, hours):
    calls = _source(monkeypatch, 'Coinbase International', [_coinbase(funding_interval=nanos), _coinbase(symbol='OLD-PERP', base_asset_name='OLD', trading_state='DELISTED')])
    path=tmp_path/'funding.json'
    result=bulk_quotes.sweep_funding(['Coinbase International'],cache_path=path)
    cache=bulk_quotes.load_funding(cache_path=path)
    assert result['legs']==1
    entry=cache['Coinbase International|BTC/USDC:USDC']
    assert entry['rate_pct']==pytest.approx(-.0004)
    assert entry['interval_hours']==hours
    assert entry['interval_assumed'] is False
    assert len(calls)==1


@pytest.mark.parametrize('changes', [
    {'type':'SPOT'}, {'trading_state':'HALT'}, {'funding_interval':'-9223372036854775808'},
    {'funding_interval':None}, {'quote':None}, {'quote':{'predicted_funding':None}},
    {'base_asset_name':''}, {'quote_asset_name':'EUR'},
])
def test_coinbase_only_active_perpetuals_with_known_current_projection(monkeypatch, changes):
    _source(monkeypatch, 'Coinbase International', [_coinbase(**changes)])
    assert fast_quotes.FastQuoteRefresher()._bulk_funding_rates('Coinbase International') == {}


@pytest.mark.parametrize('unavailable', [None, 'USDT-FUTURES', 'USDC-FUTURES'])
def test_bitget_collects_both_linear_families_with_live_cadence(monkeypatch, unavailable):
    client=ccxt.bitget()
    client.set_markets([{'id':f'BTC{quote}','symbol':f'BTC/{quote}:{quote}', 'type':'swap',
                        'base':'BTC','quote':quote,'settle':quote,'swap':True,'spot':False,
                        'linear':True,'inverse':False,'info':{'fundInterval':'8'}} for quote in ['USDT','USDC']])
    calls=[]
    def response(params):
        product=params['productType'];calls.append(product)
        if product==unavailable:raise OSError('public family unavailable')
        quote=product.split('-')[0]
        return {'code':'00000','data':[{'symbol':f'BTC{quote}','fundingRate':'0.0001',
                'fundingRateInterval':'1','nextUpdate':'1788652800000'}]}
    monkeypatch.setattr(client,'publicMixGetV2MixMarketCurrentFundRate',response)
    # The pre-fix ticker path proves the USDT-only default without making HTTP requests.
    monkeypatch.setattr(client,'publicMixGetV2MixMarketTickers',response)
    monkeypatch.setattr(fast_quotes.FastQuoteRefresher,'_client',lambda *a:client)
    rates=fast_quotes.FastQuoteRefresher()._bulk_funding_rates('Bitget')
    assert calls==['USDT-FUTURES','USDC-FUTURES']
    assert set(rates)=={f'BTC/{q}:{q}' for q in ['USDT','USDC'] if f'{q}-FUTURES'!=unavailable}
    for entry in rates.values():
        assert entry['funding_interval_hours']==1
        assert entry['funding_interval_assumed'] is False
        assert entry['next_funding_ts_us']==1788652800000000


@pytest.mark.parametrize('quote',['USD','USDC'])
def test_bitmart_quote_does_not_replace_catalogue_settlement(monkeypatch, quote):
    _source(monkeypatch,'BitMart',[_bitmart(symbol=f'BTC{quote}',quote_currency=quote,expire_timestamp=None)])
    rates=fast_quotes.FastQuoteRefresher()._bulk_funding_rates('BitMart')
    assert set(rates)=={f'BTC/{quote}:USDT'}


@pytest.mark.parametrize('rate',['NaN','Infinity','-Infinity'])
@pytest.mark.parametrize('venue',['BitMart','Coinbase International'])
def test_nonfinite_native_rates_are_unavailable(monkeypatch, venue, rate):
    row=_bitmart(expected_funding_rate=rate) if venue=='BitMart' else _coinbase(quote={'predicted_funding':rate})
    _source(monkeypatch,venue,[row])
    assert fast_quotes.FastQuoteRefresher()._bulk_funding_rates(venue)=={}
