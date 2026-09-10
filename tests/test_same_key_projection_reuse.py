"""The short projection TTL must not destroy a usable revalidation fallback."""
import pytest

from spreadboard import server


def key():
    return ('board',(1,1),(2,2),(3,3),(4,4),(5,5),(6,6),(7,7),(8,8),(('limit',('20',)),))


@pytest.mark.parametrize('background_lookup_first', [False, True])
def test_expired_exact_key_returns_repriced_structure_and_requests_refresh(monkeypatch, tmp_path, background_lookup_first):
    payload={'mode':'materialized_live_query_projection','groups':[{'token':'BTC'}],
             'pagination':{'limit':20},'quote': 'old'}
    monkeypatch.setattr(server, '_MARKET_CACHE', {key():(100.0,payload)})
    monkeypatch.setattr(server, '_MARKET_CACHE_LAST_USED', {})
    monkeypatch.setattr(server, '_MARKET_CACHE_INFLIGHT', {})
    monkeypatch.setattr(server, '_LIVE_QUERY_RESULT_TTL_SECONDS', 10)
    monkeypatch.setattr(server, '_MARKET_CACHE_TTL_SECONDS', 900)
    monkeypatch.setattr(server.time, 'monotonic', lambda:120.0)
    monkeypatch.setattr(server, '_market_cache_key', lambda *a:key())
    monkeypatch.setattr(server, '_exact_catalog_market_projection', lambda *a,**kw:None)
    monkeypatch.setattr(server.warm_query_projection.LIVE_UNIVERSE, 'template', dict)
    monkeypatch.setattr(server.warm_query_projection, 'project', lambda *a,**kw:pytest.fail('foreground rebuilt instead of using the completed projection'))
    warms=[]
    monkeypatch.setattr(server, '_schedule_market_generation_warm', lambda *a:warms.append(a[2]))
    monkeypatch.setattr(server.materialized_views, 'finalize_projection', lambda p:p)
    monkeypatch.setattr(server, '_apply_spread_freshness_coalesced', lambda p:p.update(quote='fresh'))
    if background_lookup_first:
        assert server._market_cache_lookup(key(),allow_previous_generation=False)==(None,False)
    result=server.api_market_spreads(tmp_path/'board', {'limit':['20']})
    assert result is payload
    assert result['quote']=='fresh'
    assert warms==[key()]
    assert server._MARKET_CACHE[key()][0]==100.0  # Serving never extends structure TTL.


@pytest.mark.parametrize('age,groups', [(901,[{'token':'BTC'}]),(20,[])])
def test_empty_or_structurally_expired_projection_is_not_a_fallback(monkeypatch, age, groups):
    payload={'mode':'materialized_live_query_projection','groups':groups}
    monkeypatch.setattr(server,'_MARKET_CACHE',{key():(100.0,payload)})
    monkeypatch.setattr(server,'_LIVE_QUERY_RESULT_TTL_SECONDS',10)
    monkeypatch.setattr(server,'_MARKET_CACHE_TTL_SECONDS',900)
    monkeypatch.setattr(server.time,'monotonic',lambda:100.0+age)
    assert server._market_cache_lookup(key(),allow_previous_generation=True)==(None,False)


def test_background_build_replaces_same_key_fallback_with_new_membership(monkeypatch, tmp_path):
    old={'mode':'materialized_live_query_projection','groups':[{'token':'BTC'}]}
    new={'mode':'materialized_live_query_projection','groups':[{'token':'ETH'}]}
    monkeypatch.setattr(server,'_MARKET_CACHE',{key():(100.0,old)})
    monkeypatch.setattr(server,'_MARKET_CACHE_INFLIGHT',{})
    monkeypatch.setattr(server,'_MARKET_CACHE_LAST_USED',{})
    monkeypatch.setattr(server,'_LIVE_QUERY_RESULT_TTL_SECONDS',10)
    monkeypatch.setattr(server,'_MARKET_CACHE_TTL_SECONDS',900)
    monkeypatch.setattr(server.time,'monotonic',lambda:120.0)
    monkeypatch.setattr(server,'_market_cache_key',lambda *a:key())
    monkeypatch.setattr(server,'_market_build_is_background',lambda:True)
    monkeypatch.setattr(server,'_exact_catalog_market_projection',lambda *a,**kw:None)
    monkeypatch.setattr(server.warm_query_projection.LIVE_UNIVERSE,'template',dict)
    monkeypatch.setattr(server.warm_query_projection,'project',lambda *a,**kw:new)
    monkeypatch.setattr(server,'_sync_telegram_client_universe',lambda p:p)
    assert server.api_market_spreads(tmp_path/'board',{'limit':['20']}) is new
    assert server._MARKET_CACHE[key()]==(120.0,new)
    assert key() not in server._MARKET_CACHE_INFLIGHT
