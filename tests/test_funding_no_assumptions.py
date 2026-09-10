"""Public projections need exact, finite current inputs on both legs."""
from types import SimpleNamespace

import pytest

from spreadboard import api_spreads, fast_quotes, funding_catalog


@pytest.mark.parametrize('venue', ['Binance', 'Bybit', 'Bingx', 'Bitget', 'Mexc', 'Gate', 'Phemex', 'XT', 'WhiteBIT', 'Hyperliquid', 'Aster'])
@pytest.mark.parametrize('side', ['long', 'short'])
def test_any_assumed_leg_withholds_pair_projection(venue, side):
    route = {f'{s}_{key}': value for s in ('long','short') for key,value in [('venue',venue if s==side else 'Other'),('market_type','Futures'),('market_symbol','ONE/USDT:USDT')]}
    rates = {f'{v}|ONE/USDT:USDT': {'rate_pct': .1,'interval_hours':8,'interval_assumed':v==venue} for v in (venue,'Other')}
    assert funding_catalog._live_current_value(route,rates) is None


def test_fresh_rate_without_interval_cannot_reuse_old_scan_schedule():
    raw = {'long_venue':'Bybit','long_market_type':'Futures','notes':{'route_inputs':{'long':{'symbol':'ONE','funding_interval_hours':8,'projected_24h_pct':99}}}}
    row = api_spreads._apply_live_funding(raw,legs={'Bybit|ONE':{'rate_pct':.1}})
    assert row['notes']['route_inputs']['long'].get('projected_24h_pct') is None


@pytest.mark.parametrize('value', [float('nan'),float('inf'),float('-inf')])
def test_nonfinite_rate_rejected(value):
    assert fast_quotes._funding_fields(value,interval_hours=1) == {}


@pytest.mark.parametrize('value', [None,0,-1,24,float('inf')])
def test_invalid_schedule_is_explicitly_unknown(value):
    fields=fast_quotes._funding_fields(.001,interval_hours=value)
    assert fields['funding_interval_hours'] is None
    assert fields['funding_interval_assumed'] is True


@pytest.mark.parametrize('venue', ['Binance','Aster'])
def test_schedule_endpoint_failure_does_not_promote_ccxt_default(monkeypatch,venue):
    client=SimpleNamespace(has={'fetchFundingRates':True},markets={'ONE':{'id':'ONE'}},fetch_funding_rates=lambda:{'ONE':{'symbol':'ONE','fundingRate':.001,'interval':'8h'}})
    r=fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(r,'_client',lambda *_:client)
    monkeypatch.setattr(r,'_bulk_funding_interval_overrides',lambda _:None)
    assert r._bulk_funding_rates_raw(venue)['ONE']['funding_interval_assumed'] is True


def test_coinex_current_uses_next_rate_and_never_last_settlement(monkeypatch):
    client=SimpleNamespace(has={'fetchFundingRates':True},markets={'ONE':{'id':'ONE'}},fetch_funding_rates=lambda:{'ONE':{'symbol':'ONE','fundingRate':-.002,'nextFundingRate':.001,'interval':'8h','fundingTimestamp':100,'nextFundingTimestamp':200}})
    r=fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(r,'_client',lambda *_:client)
    fields=r._bulk_funding_rates_raw('CoinEx')['ONE']
    assert fields['current_funding_pct'] == .1
    assert fields['next_funding_ts_us'] == 200000


def test_phemex_metadata_seconds_are_converted_per_contract(monkeypatch):
    symbol='ONE/USDT:USDT'
    market={'swap':True,'symbol':symbol,'info':{'fundingInterval':14400}}
    client=SimpleNamespace(markets={symbol:market},markets_by_id={'ONEUSDT':[market]})
    r=fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(r,'_client',lambda *_:client)
    monkeypatch.setattr(fast_quotes,'_json_url',lambda _: {'result':[{'symbol':'ONEUSDT','fundingRateRr':.001}]})
    fields=r._native_bulk_funding_rates('Phemex')[symbol]
    assert fields['funding_interval_hours'] == 4
    assert fields['funding_interval_assumed'] is False


def test_normalised_public_row_never_annualises_an_assumption():
    row=SimpleNamespace(long_market_type='Spot',short_market_type='Futures',short_funding_interval_assumed=True,short_funding_pct=.1,short_funding_interval_hours=8)
    assert api_spreads.normalised_funding(row) == (None,None)


@pytest.mark.parametrize('hours', [None,0,-1,24])
def test_public_per_day_has_no_default_interval(hours):
    assert api_spreads._per_day(.1,hours) is None


@pytest.mark.parametrize('side',['long','short'])
def test_missing_or_assumed_sampled_leg_is_not_zero_funding(side):
    row={'long_market_type':'Futures','short_market_type':'Futures','funding_daily_pct':99}
    quotes={'long':{'current_funding_pct':.1,'funding_interval_hours':1},'short':{'current_funding_pct':.2,'funding_interval_hours':1}}
    quotes[side]={}
    fast_quotes._sync_quoted_funding(row,quotes['long'],quotes['short'])
    assert row['funding_daily_pct'] is None
    quotes[side]={'current_funding_pct':.1,'funding_interval_hours':8,'funding_interval_assumed':True}
    fast_quotes._sync_quoted_funding(row,quotes['long'],quotes['short'])
    assert row['funding_daily_pct'] is None
    assert row[f'{side}_funding_interval_assumed'] is True


def test_new_sample_missing_schedule_does_not_inherit_old_interval():
    row={'long_market_type':'Futures','long_venue':'Bybit','long_market_symbol':'ONE','long_funding_interval_hours':8}
    quote={'current_funding_pct':.1,'funding_interval_hours':None,'funding_interval_assumed':True}
    fast_quotes._carry_forward_funding(row,'long',quote)
    assert quote['funding_interval_hours'] is None


def test_book_only_update_never_revives_discovery_funding(monkeypatch):
    from spreadboard import bulk_quotes
    monkeypatch.setattr(bulk_quotes,'load_funding',dict)
    row={'long_market_type':'Futures','long_venue':'Bybit','long_market_symbol':'ONE','long_funding_interval_hours':8,'long_funding_pct':99}
    quote={}
    fast_quotes._carry_forward_funding(row,'long',quote)
    assert not quote
