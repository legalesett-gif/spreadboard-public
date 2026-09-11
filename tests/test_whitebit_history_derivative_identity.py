"""Native traditional-finance declarations survive funding-only CCXT pruning."""
import time

import ccxt
import pytest

from spreadboard import chart_catalog, tokenized_assets
from spreadboard import venue_funding_history as history


@pytest.mark.parametrize('token', ['AAOI', 'AAPL'])
def test_real_adapter_history_client_keeps_declared_tradfi_perpetual(tmp_path, monkeypatch, token):
    client = ccxt.whitebit()
    definition = {'name': token + '_PERP', 'stock': token, 'money': 'USDT',
                  'stockPrec': '2', 'moneyPrec': '2', 'type': 'tradfiFutures',
                  'tradesEnabled': True, 'isCollateral': True}
    spot = {**definition, 'name': token + '_USDT', 'type': 'spot'}
    monkeypatch.setattr(client, 'load_markets', lambda: client.set_markets(client.parse_markets([spot, definition])))
    monkeypatch.setattr(ccxt, 'whitebit', lambda *_a, **_kw: client)
    for name in ('_CLIENTS', '_CLIENT_ERRORS', '_CLIENT_FAILURE_AT', '_CLIENT_CREATION_LOCKS'):
        monkeypatch.setattr(history, name, {})
    now = int(time.time())
    latest = now // 28800 * 28800
    calls = []
    def native(request):
        calls.append(request)
        return [{'market': token + '_PERP', 'fundingTime': str(latest-i*28800),
                 'fundingRate': '-0.0001', 'rateCalculatedTime': str(latest-(i+1)*28800)}
                for i in range(96)]
    monkeypatch.setattr(client, 'v4PublicGetFundingHistoryMarket', native)
    outcome = history.leg_history_outcome('WhiteBIT', token + '/USDT:USDT',
                                         max_pages=1, event_store_path=tmp_path/'events.sqlite3')
    assert outcome['status'] == 'ok'
    assert calls and all(call['market'] == token + '_PERP' for call in calls)
    assert max(row['timestamp'] for row in outcome['entries']) == latest * 1000
    assert all(row['fundingRate'] == -.0001 for row in outcome['entries'])
    assert history.realised_windows(outcome['entries'])['1d'] == pytest.approx(-.03)
    market = client.market(token + '/USDT:USDT')
    assert market['swap'] is True and market['spot'] is False
    assert market['info']['type'] == 'tradfiFutures'
    assert token + '/USDT' not in client.symbols
    before = len(calls)
    assert history.leg_history_outcome('WhiteBIT', token + '/USDT')['status'] == 'symbol_not_indexed'
    assert len(calls) == before


def test_native_tradfi_classification_reaches_the_identity_guard(tmp_path):
    definition = {'name': 'AAOI_PERP', 'type': 'tradfiFutures', 'tradesEnabled': True}
    future = {'ticker_id': 'AAOI_PERP', 'stock_currency': 'AAOI',
              'money_currency': 'USDT', 'product_type': 'Perpetual'}
    rows = chart_catalog._load_whitebit_venue('Futures', fetcher=lambda url:
             [definition] if url.endswith('/markets') else {'result': [future]})
    assert rows[0]['asset_class'] == 'tokenized'
    guard = tokenized_assets.classify({'token': 'AAOI', 'long_asset_class': rows[0]['asset_class']},
                                     path=tmp_path/'no-registry.json')
    assert guard['asset_class'] == 'tokenized' and guard['status'] == 'blocked'


@pytest.mark.parametrize('definitions', [{}, []])
def test_missing_native_definitions_cannot_publish_perpetuals(definitions):
    future = {'ticker_id': 'AAOI_PERP', 'stock_currency': 'AAOI',
              'money_currency': 'USDT', 'product_type': 'Perpetual'}
    with pytest.raises((ValueError, TypeError)):
        chart_catalog._load_whitebit_venue('Futures', fetcher=lambda url:
            definitions if url.endswith('/markets') else {'result': [future]})


@pytest.mark.parametrize('change', [
    {'tradesEnabled': False}, {'delistedAt': 123}, {'type': 'spot'},
])
def test_native_definition_vetoes_closed_or_wrong_product(change):
    definition = {'name': 'AAOI_PERP', 'type': 'tradfiFutures',
                  'tradesEnabled': True, **change}
    future = {'ticker_id': 'AAOI_PERP', 'stock_currency': 'AAOI',
              'money_currency': 'USDT', 'product_type': 'Perpetual'}
    assert chart_catalog._load_whitebit_venue('Futures', fetcher=lambda url:
        [definition] if url.endswith('/markets') else {'result': [future]}) == []


@pytest.mark.parametrize('market_type', ['Spot', 'Futures'])
def test_announced_delisting_does_not_hide_current_market(market_type):
    name = 'ICX_USDT' if market_type == 'Spot' else 'ICX_PERP'
    definition = {'name': name, 'stock': 'ICX', 'money': 'USDT',
                  'type': 'spot' if market_type == 'Spot' else 'futures',
                  'tradesEnabled': True, 'delistedAt': int(time.time()) + 3600}
    future = {'ticker_id': name, 'stock_currency': 'ICX',
              'money_currency': 'USDT', 'product_type': 'Perpetual'}
    def fetch(url):
        return [definition] if url.endswith('/markets') else {'result': [future]}
    assert len(chart_catalog._load_whitebit_venue(market_type, fetcher=fetch)) == 1
    definition['delistedAt'] = 123
    assert chart_catalog._load_whitebit_venue(market_type, fetcher=fetch) == []


def test_spot_error_response_does_not_publish_an_empty_catalogue():
    with pytest.raises(TypeError):
        chart_catalog._load_whitebit_venue('Spot', fetcher=lambda _: {'success': False})


def test_futures_error_response_does_not_publish_an_empty_catalogue():
    with pytest.raises(TypeError):
        chart_catalog._load_whitebit_venue('Futures', fetcher=lambda url:
            [] if url.endswith('/markets') else {'success': False})
