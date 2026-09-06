"""Collector publication must not wait for an unrelated discovery scan."""
from types import SimpleNamespace

import pytest

from scripts import run_spreadboard_service as service


def test_collector_bootstrap_starts_and_joins_independent_catalog_publisher(monkeypatch, tmp_path):
    started=[]
    joined=[]
    refresh=[]
    components={}
    arguments={}
    event=SimpleNamespace(wait=lambda _: True, set=lambda: None)

    class Component:
        def __init__(self, name, **kwargs):
            components[name]=self
            arguments[name]=kwargs
            self.name=name
            self.stop_event=event
            self.first_sweep_done=object()

        def request(self, **kwargs): pass
        def start(self):
            started.append(self.name)
            if self.name=='FundingCatalogPublisher':
                service._refresh_complete_funding_catalog(force=False)
        def join(self, **kwargs): joined.append(self.name)
        def stop(self): pass

    for name in ('RefreshLoop','LiveRouteIndexPublisher','BulkQuoteLoop','BulkFundingLoop',
                 'MarketEvidenceLoop','ChartHistoryWarmLoop','FundingCatalogPublisher','MemoryWatchdog'):
        monkeypatch.setattr(service,name,lambda *args,_name=name,**kwargs:Component(_name, **kwargs),raising=False)
    monkeypatch.setattr(service,'SNAPSHOT_PATH',tmp_path/'absent.json')
    monkeypatch.setattr(service.market_history,'initialize',lambda:None)
    monkeypatch.setattr(service,'_seed_public_caches',lambda:None)
    monkeypatch.setattr(service,'_seed_funding_history_demand',lambda:None)
    monkeypatch.setattr(service.signal,'signal',lambda *args:None)
    monkeypatch.setattr(service,'_log',lambda _:None)
    monkeypatch.setattr(service,'_refresh_complete_funding_catalog',lambda *,force:refresh.append(force))
    assert service._run_collector_service()==0
    assert started.count('FundingCatalogPublisher')==1
    assert joined.count('FundingCatalogPublisher')==1
    assert refresh==[False]
    assert arguments['FundingCatalogPublisher']['refresh_loop'] is components['RefreshLoop']
    assert {'BulkQuoteLoop','BulkFundingLoop','LiveRouteIndexPublisher'} <= set(started)


@pytest.mark.parametrize('failures', [False, True])
def test_publisher_retries_without_blocking_quote_collection_or_forcing_build(monkeypatch, failures):
    calls=[]
    publications=[]

    class Event:
        count=0
        def is_set(self): return self.count>=3
        def wait(self,seconds):
            assert seconds>=30
            self.count+=1
            return self.is_set()

    def refresh(*,force):
        calls.append(force)
        if len(calls)==2 and failures: raise RuntimeError('simulated worker failure')
        return len(calls)==3

    monkeypatch.setattr(service,'_refresh_complete_funding_catalog',refresh)
    monkeypatch.setattr(service,'_refresh_funding_navigation',lambda **kwargs:publications.append(kwargs))
    monkeypatch.setattr(service,'_log',lambda _:None)
    service.FundingCatalogPublisher(Event()).run()
    assert calls==[False,False,False]
    assert publications==[{'force':True}]


def test_due_catalogue_uses_the_shared_heavy_worker_slot(monkeypatch):
    import threading
    acquired=[]
    executed=[]

    class BusySlot:
        def acquire(self,timeout):
            acquired.append(timeout)
            return False
        def release(self): raise AssertionError('unowned slot')

    monkeypatch.setattr(service,'_HEAVY_CHILD_SLOT',BusySlot())
    monkeypatch.setattr(service,'_FUNDING_CATALOG_BUILD_LOCK',threading.Lock())
    monkeypatch.setattr(service,'_FUNDING_CATALOG_RETRY_AFTER',0)
    monkeypatch.setattr(service,'_service_role',lambda:'collector')
    monkeypatch.setattr(service.funding_catalog,'persisted_status',lambda:{'ready':True,'age_seconds':99999})
    monkeypatch.setattr(service,'_log',lambda _:None)
    monkeypatch.setattr(service,'_run_worker_unslotted',lambda command,**kwargs:executed.append(command) or service.WorkerResult(1,'','blocked',False))
    assert not service._refresh_complete_funding_catalog(force=False)
    assert acquired==[service.HEAVY_CHILD_SLOT_WAIT_SECONDS]
    assert executed==[]
