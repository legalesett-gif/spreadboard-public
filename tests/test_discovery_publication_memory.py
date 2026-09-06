"""Post-discovery publication must exclude the optional websocket heap."""
import pytest

from scripts import run_spreadboard_service as service


@pytest.mark.parametrize('outcome', ['success', 'enrich_missing', 'publish_missing', 'index_error'])
def test_discovery_publication_pauses_and_restores_websocket_worker(tmp_path, monkeypatch, outcome):
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text('{}')
    monkeypatch.setattr(service, 'SNAPSHOT_PATH', snapshot)
    monkeypatch.setattr(service, 'REFRESH_SNAPSHOT_PATH', tmp_path / 'staging.json')
    monkeypatch.setenv('SPREADBOARD_DISABLE_LOCAL_CACHE_WARM', '1')
    loop = service.RefreshLoop(300)
    paused = []
    state = {'paused': False}

    def pause():
        assert service._COLLECTOR_HEAVY_LOCK.locked()
        state['paused'] = True
        paused.append('pause')

    def resume():
        assert service._COLLECTOR_HEAVY_LOCK.locked()
        state['paused'] = False
        paused.append('resume')

    monkeypatch.setattr(loop, 'pause_websocket_worker', pause)
    monkeypatch.setattr(loop, 'resume_websocket_worker', resume)
    monkeypatch.setattr(loop, '_refresh_verified_identity_registry', lambda **kw: None)

    def discovery(*args, **kwargs):
        assert not state['paused']
        assert not service._COLLECTOR_HEAVY_LOCK.locked()
        return service.WorkerResult(0, '', '', False)

    def finalize(stage):
        assert state['paused'], 'finalization overlaps websocket catalogue'
        if outcome == f'{stage}_missing':
            return None
        return {'routes': 123, 'funding': 10, 'refresh_status': 'ready'}

    def index(**kwargs):
        assert state['paused'], 'index build overlaps websocket catalogue'
        if outcome == 'index_error':
            raise RuntimeError('index failed')
        return True

    def heavy(**kwargs):
        assert state['paused']

    monkeypatch.setattr(service, '_run_worker', discovery)
    monkeypatch.setattr(service, '_finalize_snapshot', finalize)
    monkeypatch.setattr(service, '_refresh_live_route_index', index)
    monkeypatch.setattr(service, '_publish_shared_market_generation', lambda *args: None)
    for name in ('_refresh_complete_funding_catalog', '_refresh_funding_navigation',
                 '_refresh_enrichment_subprocess', '_refresh_materialized_views'):
        monkeypatch.setattr(service, name, heavy)
    monkeypatch.setattr(service, '_return_freed_memory', lambda: None)
    if outcome == 'index_error':
        with pytest.raises(RuntimeError, match='index failed'):
            loop.refresh_once()
    else:
        loop.refresh_once()
    assert paused == ['pause', 'resume']
    assert not state['paused']
    assert not service._COLLECTOR_HEAVY_LOCK.locked()
