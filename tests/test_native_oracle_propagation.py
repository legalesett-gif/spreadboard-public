"""Native indexes must reach the existing spot/perpetual mismatch guard."""
import time
from types import SimpleNamespace

import pytest

from spreadboard import api_spreads, fast_quotes


@pytest.mark.parametrize('venue,native,key', [
    ('Kucoin Futures', 'OPENAIUSDTM', 'indexPrice'),
    ('Phemex', 'OPENAIUSDT', 'indexRp'),
])
def test_native_funding_retains_its_own_index(monkeypatch, venue, native, key):
    spec = fast_quotes.NATIVE_FUNDING_SOURCES[venue]
    row = {spec['symbol']: native, spec['rate']: '0', key: '1400'}
    payload = {spec['path'][0]: [row]}
    market = {'id': native, 'symbol': 'OPENAI/USDT:USDT', 'swap': True}
    client = SimpleNamespace(markets={market['symbol']: market}, markets_by_id={native: market})
    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, '_client', lambda *args: client)
    monkeypatch.setattr(fast_quotes, '_json_url', lambda url: payload)
    fields = refresher._native_bulk_funding_rates(venue)[market['symbol']]
    assert fields['current_funding_pct'] == 0
    assert fields['index_price'] == 1400
    assert api_spreads.spot_disagrees_with_perp_index({
        'long_market_type': 'Spot', 'long_price': 866,
        'short_market_type': 'Futures', 'short_index_price': fields['index_price'],
    })


@pytest.mark.parametrize('venue,native,index_key,stamp_key', [
    ('Mexc', 'OPENAI_USDT', 'indexPrice', 'timestamp'),
    ('HTX', 'OPENAI-USDT', 'index_price', 'index_ts'),
    ('Bitget', 'OPENAIUSDT', 'indexPrice', 'ts'),
])
def test_funding_only_feed_joins_fresh_native_index_without_changing_carry(
    monkeypatch, venue, native, index_key, stamp_key,
):
    symbol = 'OPENAI/USDT:USDT'
    rate = {'current_funding_pct': 0.005, 'funding_interval_hours': 4, 'funding_interval_assumed': False}
    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, '_client', lambda *args: SimpleNamespace(
        has={'fetchFundingRates': True},
        fetch_funding_rates=lambda **kwargs: {symbol: {'symbol': symbol, 'fundingRate': 0.00005, 'interval': '4h'}},
        markets={
            symbol: {'id': native, 'swap': True, 'settle': 'USDT'},
            'OPENAI/USDT': {'id': native, 'spot': True},
        },
    ))
    calls = []
    def fetch(url):
        calls.append(url)
        return {'data': [
            {'symbol': native, 'contract_code': native, index_key: '1400', stamp_key: time.time()*1000},
            {'symbol': 'UNKNOWN', 'contract_code': 'UNKNOWN', index_key: '9000', stamp_key: time.time()*1000},
        ]}
    monkeypatch.setattr(fast_quotes, '_json_url', fetch)
    result = refresher._bulk_funding_rates(venue)
    assert result == {symbol: {**rate, 'index_price': 1400}}
    assert len(calls) == (2 if venue == 'Bitget' else 1)


@pytest.mark.parametrize('index,age', [('nan', 0), ('inf', 0), ('-1', 0), ('1400', 91), ('1400', -2)])
def test_bad_or_stale_oracle_cannot_be_stamped_as_current(monkeypatch, index, age):
    symbol = 'OPENAI/USDT:USDT'
    rates = {symbol: {'current_funding_pct': 0.005}}
    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, '_bulk_funding_rates_raw', lambda venue: rates)
    monkeypatch.setattr(refresher, '_client', lambda *args: SimpleNamespace(markets={
        symbol: {'id': 'OPENAI_USDT', 'swap': True},
    }))
    monkeypatch.setattr(fast_quotes, '_json_url', lambda url: {'data': [{
        'symbol': 'OPENAI_USDT', 'indexPrice': index, 'timestamp': (time.time()-age)*1000,
    }]})
    assert refresher._bulk_funding_rates('Mexc') == {symbol: {'current_funding_pct': 0.005}}


def test_failed_oracle_feed_preserves_live_funding(monkeypatch):
    symbol = 'OPENAI/USDT:USDT'
    rates = {symbol: {'current_funding_pct': 0.0, 'funding_interval_hours': 8}}
    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, '_bulk_funding_rates_raw', lambda venue: rates)
    monkeypatch.setattr(refresher, '_client', lambda *args: SimpleNamespace(markets={symbol: {'id': 'OPENAI_USDT', 'swap': True}}))
    def unavailable(url):
        raise TimeoutError()
    monkeypatch.setattr(fast_quotes, '_json_url', unavailable)
    assert refresher._bulk_funding_rates('Mexc') == rates


@pytest.mark.parametrize('index', ['nan', 'inf', '-inf'])
def test_unified_funding_does_not_publish_nonfinite_index(index):
    assert 'index_price' not in fast_quotes._funding_fields('0.00005', index_price=index)
