"""Independent funding publishers share websocket exclusion with history."""
from types import SimpleNamespace

import pytest

from scripts import run_spreadboard_service as service


@pytest.mark.parametrize('caller', ['catalogue', 'scheduled'])
@pytest.mark.parametrize('raises', [False, True])
def test_navigation_callers_exclude_websocket_and_restore(monkeypatch, caller, raises):
    refresh = service.RefreshLoop(300)
    events = []
    state = {'paused': False}

    def pause():
        assert service._COLLECTOR_HEAVY_LOCK.locked()
        state['paused'] = True
        events.append('pause')

    def resume():
        assert service._COLLECTOR_HEAVY_LOCK.locked()
        state['paused'] = False
        events.append('resume')

    def navigation(*, force):
        assert state['paused'], 'navigation overlaps websocket'
        assert service._COLLECTOR_HEAVY_LOCK.locked()
        assert force == (caller == 'catalogue')
        events.append('navigation')
        if raises:
            raise RuntimeError('navigation failed')
        return True

    monkeypatch.setattr(refresh, 'pause_websocket_worker', pause)
    monkeypatch.setattr(refresh, 'resume_websocket_worker', resume)
    monkeypatch.setattr(service, '_refresh_funding_navigation', navigation)
    monkeypatch.setattr(service, '_log', lambda message: None)
    if caller == 'catalogue':
        class Event:
            stopped = False
            def is_set(self): return self.stopped
            def wait(self, seconds): self.stopped = True

        def catalogue(*, force):
            assert not force
            assert not state['paused']
            assert not service._COLLECTOR_HEAVY_LOCK.locked()
            return True

        monkeypatch.setattr(service, '_refresh_complete_funding_catalog', catalogue)
        service.FundingCatalogPublisher(Event(), refresh_loop=refresh).run()
    else:
        targets = []
        monkeypatch.setattr(service, '_route_publication_due', lambda *args, **kwargs: False)
        monkeypatch.setattr(service.threading, 'Thread', lambda *, target, **kw: SimpleNamespace(start=lambda: targets.append(target)))
        publisher = SimpleNamespace(refresh_loop=refresh, has_published=lambda: True)
        service._schedule_funding_navigation(publisher)
        assert len(targets) == 1
        if raises:
            with pytest.raises(RuntimeError, match='navigation failed'):
                targets[0]()
        else:
            targets[0]()
        assert not service._FUNDING_NAVIGATION_SCHEDULE_LOCK.locked()
    assert events == ['pause', 'navigation', 'resume']
    assert not state['paused']
    assert not service._COLLECTOR_HEAVY_LOCK.locked()
