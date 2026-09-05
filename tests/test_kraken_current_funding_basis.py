"""Public single-market funding must agree with the bulk adapter's notional basis."""
import ccxt
import pytest

from spreadboard import fast_quotes


def test_native_kraken_funding_matches_bulk_for_the_same_public_ticker(monkeypatch):
    ticker={'symbol':'PF_LCAPUSD','fundingRate':-.0345315,'markPrice':6.998391,
            'indexPrice':6.9291,'suspended':False,'tag':'perpetual'}
    client=ccxt.krakenfutures()
    client.symbol=lambda _: 'LCAP/USD:USD'
    bulk=client.parse_funding_rate(ticker)
    monkeypatch.setattr(fast_quotes,'_json_url',lambda _: {'tickers':[ticker]})
    point=fast_quotes._native_current_funding('Kraken Futures','LCAP/USD:USD')
    assert point['current_funding_pct']==pytest.approx(bulk['fundingRate']*100)
    assert point['funding_interval_hours']==1
    assert point['index_price']==ticker['indexPrice']


@pytest.mark.parametrize('invalid', [{'suspended':True},{'markPrice':None},{'markPrice':0},
                                   {'markPrice':float('nan')},{'markPrice':float('inf')},
                                   {'fundingRate':float('nan')},{'fundingRate':float('inf')}])
def test_unknown_or_suspended_kraken_current_funding_stays_unknown(monkeypatch,invalid):
    ticker={'symbol':'PF_LCAPUSD','fundingRate':-.03,'markPrice':7,'indexPrice':6.9,
            'suspended':False,'tag':'perpetual',**invalid}
    monkeypatch.setattr(fast_quotes,'_json_url',lambda _: {'tickers':[ticker]})
    assert fast_quotes._native_current_funding('Kraken Futures','LCAP/USD:USD')=={}
