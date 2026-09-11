"""Settlement refresh must survive busy structural/ranking publishers."""
import threading

from scripts import run_spreadboard_service as service
from scripts import settlement_history_worker as worker


def test_history_child_runs_while_all_heavy_analytics_slots_are_owned(monkeypatch):
    calls = []
    monkeypatch.setattr(service, '_run_worker', lambda command, **kw: calls.append((command,kw)) or service.WorkerResult(0,'','',False))
    locks = [threading.Lock(), threading.Lock(), threading.Semaphore(1)]
    monkeypatch.setattr(service, '_COLLECTOR_HEAVY_LOCK', locks[0])
    monkeypatch.setattr(service, '_BACKGROUND_ANALYTICS_LOCK', locks[1])
    monkeypatch.setattr(service, '_HEAVY_CHILD_SLOT', locks[2])
    for lock in locks: lock.acquire()
    try:
        assert service.SettlementHistoryLoop(threading.Event()).check_once().returncode == 0
    finally:
        for lock in locks: lock.release()
    command, options = calls[0]
    assert command[-1].endswith('settlement_history_worker.py')
    assert not service._is_heavy_child(command)
    assert options['timeout'] == 600


def test_worker_returns_failure_instead_of_claiming_success(tmp_path, monkeypatch):
    monkeypatch.setattr(worker.venue_funding_history, 'DEFAULT_CACHE_PATH', tmp_path/'history.json')
    monkeypatch.setattr(worker.service, '_refresh_venue_funding_history', lambda: False)
    assert worker.main() == 1
    monkeypatch.setattr(worker.service, '_refresh_venue_funding_history', lambda: True)
    assert worker.main() == 0


def test_worker_does_not_overlap_an_existing_history_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(worker.venue_funding_history, 'DEFAULT_CACHE_PATH', tmp_path/'history.json')
    def overlap(*args): raise BlockingIOError()
    monkeypatch.setattr(worker.fcntl, 'flock', overlap)
    monkeypatch.setattr(worker.service, '_refresh_venue_funding_history', lambda: (_ for _ in ()).throw(AssertionError('overlap')))
    assert worker.main() == 0


def test_loop_retries_launch_failure_without_overlapping_or_busy_loop(monkeypatch):
    waits = []
    class Stop:
        def wait(self, seconds):
            waits.append(seconds)
            return len(waits) >= 3
        def is_set(self): return len(waits) >= 3
    loop = service.SettlementHistoryLoop(Stop())
    calls = []
    def run():
        calls.append(True)
        if len(calls) == 1: raise OSError('temporary launch failure')
        return service.WorkerResult(0, '', '', False)
    monkeypatch.setattr(loop, 'check_once', run)
    monkeypatch.setattr(service, '_log', lambda _: None)
    loop.run()
    assert len(calls) == 2
    assert waits[0] == 10
    assert all(5 <= value <= 300 for value in waits[1:])
