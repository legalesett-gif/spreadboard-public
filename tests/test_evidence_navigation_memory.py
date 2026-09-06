"""History publication excludes websocket memory through funding navigation."""
import threading

import pytest

from scripts import run_spreadboard_service as service


@pytest.mark.parametrize('outcome', ['success', 'worker_failure', 'worker_raise', 'navigation_raise'])
def test_evidence_navigation_keeps_websocket_paused(monkeypatch, outcome):
    refresh = service.RefreshLoop(300)
    history = service.MarketEvidenceLoop(threading.Event(), refresh_loop=refresh)
    events = []
    state = {'paused': False}
    monkeypatch.setattr(service, '_route_publication_due', lambda *a, **kw: False)

    def pause():
        assert service._COLLECTOR_HEAVY_LOCK.locked()
        state['paused'] = True
        events.append('pause')

    def resume():
        assert service._COLLECTOR_HEAVY_LOCK.locked()
        state['paused'] = False
        events.append('resume')

    def worker(*args, **kwargs):
        assert state['paused']
        events.append('evidence')
        if outcome == 'worker_raise':
            raise RuntimeError('worker failed')
        return service.WorkerResult(1 if outcome == 'worker_failure' else 0, '', '', False)

    def navigation(*, force):
        assert force
        assert state['paused'], 'funding navigation overlaps websocket heap'
        assert service._COLLECTOR_HEAVY_LOCK.locked()
        events.append('navigation')
        if outcome == 'navigation_raise':
            raise RuntimeError('navigation failed')
        return True

    monkeypatch.setattr(refresh, 'pause_websocket_worker', pause)
    monkeypatch.setattr(refresh, 'resume_websocket_worker', resume)
    monkeypatch.setattr(service, '_run_worker', worker)
    monkeypatch.setattr(service, '_refresh_funding_navigation', navigation)
    if outcome.endswith('_raise'):
        with pytest.raises(RuntimeError, match='failed'):
            history._sweep_once()
    else:
        history._sweep_once()
    assert events == ['pause', 'evidence', *(['navigation'] if outcome in {'success', 'navigation_raise'} else []), 'resume']
    assert not state['paused']
    assert not service._COLLECTOR_HEAVY_LOCK.locked()
    assert not service._BACKGROUND_ANALYTICS_LOCK.locked()
