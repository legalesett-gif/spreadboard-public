"""Exercise WhiteBIT's real CCXT misclassification through cache and carry."""
import json
import time

import ccxt
import pytest

from spreadboard import bulk_quotes, chart_catalog, fast_quotes, funding_catalog


def _source(base='AAPL', quote='USDT', **changes):
    return {'ticker_id': f'{base}_PERP', 'stock_currency': base,
            'money_currency': quote, 'product_type': 'Perpetual',
            'funding_rate': '-0.0001', 'funding_interval_minutes': 480,
            'next_funding_rate_timestamp': '1788652800000',
            'index_price': '232.50', **changes}


def _client(monkeypatch, rows):
    client = ccxt.whitebit()
    definitions = [{'name': row['ticker_id'], 'stock': row.get('stock_currency') or 'AAPL',
                    'money': row.get('money_currency') or 'USDT', 'stockPrec': '3',
                    'moneyPrec': '2', 'type': 'tradfiFutures', 'tradesEnabled': True,
                    'isCollateral': True} for row in rows]
    client.set_markets(client.parse_markets(definitions))
    monkeypatch.setattr(client, 'v4PublicGetFutures', lambda *a: {'result': rows})
    monkeypatch.setattr(fast_quotes.FastQuoteRefresher, '_client', lambda *a: client)
    return client


@pytest.mark.parametrize('quote', ['USD', 'USDT', 'USDC'])
@pytest.mark.parametrize('rate', ['-0.0001', '0', '0.0002'])
def test_native_perpetual_identity_survives_real_ccxt_into_cache_and_carry(monkeypatch, tmp_path, quote, rate):
    row = _source(quote=quote, funding_rate=rate)
    client = _client(monkeypatch, [row])
    # Reproduce the adapter bug: it labels a tradfi perpetual as a spot pair.
    assert client.fetch_funding_rates()[f'AAPL/{quote}']['symbol'] == f'AAPL/{quote}'
    catalogue = chart_catalog._load_whitebit_venue('Futures', fetcher=lambda _url: {'result': [row]})
    symbol = catalogue[0]['symbol']
    path = tmp_path / 'funding.json'
    report = bulk_quotes.sweep_funding(['WhiteBIT'], cache_path=path)
    rates = bulk_quotes.load_funding(cache_path=path)
    assert report['legs'] == 1
    assert set(rates) == {f'WhiteBIT|{symbol}'}
    entry = rates[f'WhiteBIT|{symbol}']
    assert entry['rate_pct'] == pytest.approx(float(rate)*100)
    assert entry['interval_hours'] == 8
    assert entry['interval_assumed'] is False
    fields = fast_quotes.FastQuoteRefresher()._bulk_funding_rates('WhiteBIT')[symbol]
    assert fields['next_funding_ts_us'] == 1788652800000000
    assert fields['index_price'] == 232.5
    route = {'long_venue': 'WhiteBIT', 'long_market_type': 'Futures',
             'long_market_symbol': symbol, 'short_venue': 'Bybit',
             'short_market_type': 'Futures', 'short_market_symbol': symbol}
    carry = funding_catalog._live_current_value(route, {
        **rates, f'Bybit|{symbol}': {'rate_pct': .01, 'interval_hours': 8}})
    assert carry == pytest.approx((.01-float(rate)*100)*3)


@pytest.mark.parametrize('change', [
    {'product_type': 'Spot'}, {'ticker_id': 'AAPL_USDT'},
    {'stock_currency': ''}, {'money_currency': 'BTC'},
])
def test_bulk_funding_requires_native_perpetual_identity(monkeypatch, change):
    _client(monkeypatch, [_source(**change)])
    assert fast_quotes.FastQuoteRefresher()._bulk_funding_rates('WhiteBIT') == {}


@pytest.mark.parametrize('reader', ['load', 'merge'])
def test_old_spot_shaped_funding_keys_cannot_reappear(monkeypatch, tmp_path, reader):
    path = tmp_path / 'funding.json'
    now = time.time()
    keys = ['WhiteBIT|AAPL/USDT', 'WhiteBIT|AAPL/USDT:USDT',
            'Bybit|AAPL/USDT:USDT', 'Ourbit|AAPL/USDT:USDT']
    path.write_text(json.dumps({'legs': {k: {'rate_pct': .01, 'interval_hours': 8} for k in keys},
                                'leg_updated_at': dict.fromkeys(keys, now)}))
    if reader == 'merge':
        # Even a rotation of another venue must prune the obsolete cached keys.
        bulk_quotes.sweep_funding([], cache_path=path, merge_existing=True)
        retained = json.loads(path.read_text())['legs']
    else:
        retained = bulk_quotes.load_funding(cache_path=path)
    assert set(retained) == {'WhiteBIT|AAPL/USDT:USDT', 'Bybit|AAPL/USDT:USDT'}
