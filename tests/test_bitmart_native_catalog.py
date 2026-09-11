"""Native listing changes and exact settlement currencies must survive adapter absence."""
import io
import json

import pytest

from spreadarb import venue_policy
from spreadboard import chart_catalog, venue_funding_history


def future(quote='USDT', **changes):
    return {'symbol': f'BTC{quote}', 'base_currency': 'BTC', 'quote_currency': quote,
            'product_type': 1, 'expire_timestamp': 0, 'status': 'Trading',
            'contract_size': '.001', **changes}


def serve(monkeypatch, rows, code=1000):
    calls = []
    def response(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps({'code': code, 'data': {'symbols': rows}}).encode())
    monkeypatch.setattr(chart_catalog, 'urlopen', response)
    return calls


def test_actual_catalogue_loader_uses_native_spot_definitions(monkeypatch):
    rows = [{'symbol': 'BTC_USDT', 'base_currency': 'BTC', 'quote_currency': 'USDT', 'trade_status': 'trading'}]
    calls = serve(monkeypatch, rows + [dict(rows[0], trade_status='pre-trade'), dict(rows[0], symbol='WRONG')])
    result = chart_catalog._load_venue('BitMart', 'Spot')
    assert calls == ['https://api-cloud.bitmart.com/spot/v1/symbols/details']
    assert len(result) == 1 and result[0]['symbol'] == 'BTC/USDT'
    assert result[0]['contract_size'] == 1


def test_actual_catalogue_loader_keeps_exact_linear_identity_and_native_asset_class(monkeypatch):
    calls = serve(monkeypatch, [future(), future('USDC', tradfi_info={'market_group': 'US_MARKET'}), future('USD'),
                               future(status='Delisted'), future(contract_size='NaN'), future(contract_size='0'),
                               future(product_type=2)])
    result = chart_catalog._load_venue('BitMart', 'Futures')
    assert calls == ['https://api-cloud-v2.bitmart.com/contract/public/details']
    assert {r['symbol'] for r in result} == {'BTC/USDT:USDT', 'BTC/USDC:USDC'}
    assert all(r['contract_size'] == .001 for r in result)
    assert next(r for r in result if r['quote'] == 'USDC')['asset_class'] == 'tokenized'


def test_provider_error_cannot_publish_an_empty_successful_catalogue(monkeypatch):
    serve(monkeypatch, [], code=500)
    with pytest.raises(RuntimeError, match='response_error'):
        chart_catalog._load_venue('BitMart', 'Futures')


@pytest.mark.parametrize('symbol', ['BTC/USD:USDT', 'BTC/USD:BTC', 'BTC/USDC:USDT'])
def test_legacy_malformed_or_inverse_contract_cannot_survive_cached_surfaces(tmp_path, monkeypatch, symbol):
    row = {'long_venue': 'BitMart', 'long_market_type': 'Futures', 'long_market_symbol': symbol,
           'short_venue': 'Binance', 'short_market_type': 'Futures', 'short_market_symbol': 'BTC/USDT:USDT'}
    assert not venue_policy.opportunity_payload_enabled({'groups': [{'routes': [row]}]})
    assert not venue_policy.opportunity_payload_enabled({'rows': [row]}, funding_only=True)
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps({'markets': [{'venue': 'BitMart', 'market_type': 'Futures', 'symbol': symbol}]}))
    monkeypatch.setattr(chart_catalog, 'dex_market_entries', list)
    assert chart_catalog.load(path)['markets'] == []
    history = tmp_path / 'history.json'
    key = 'BitMart|' + symbol
    history.write_text(json.dumps({'schema': venue_funding_history.SCHEMA, 'legs': {key: {'1d': 1, '7d': 2, '30d': 3}}}))
    assert key not in venue_funding_history.load(cache_path=history)
    def forbidden(*args, **kwargs): raise AssertionError('invalid contract contacted provider')
    monkeypatch.setattr(venue_funding_history, '_native_leg_history_outcome', forbidden)
    assert venue_funding_history.leg_history_outcome('BitMart', symbol)['status'] == 'symbol_not_indexed'
