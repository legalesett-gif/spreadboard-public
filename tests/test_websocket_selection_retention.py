"""Selection retains leg keys, not the parsed universe between batches."""
import asyncio
import gc
import weakref

import pytest

from scripts import websocket_book_worker as worker
from spreadboard import api_spreads


class Payload:
    pass


@pytest.mark.parametrize('fails', [False, True])
def test_selection_releases_query_objects_but_preserves_legs(monkeypatch, tmp_path, fails):
    references = []
    selected = {('Gate', 'Spot', 'BTC/USDT'), ('Bybit', 'Futures', 'BTC/USDT:USDT')}

    def select(path, *, limit, accounts_path):
        assert path == tmp_path / 'snapshot'
        assert limit == worker.MAX_SUBSCRIPTIONS
        assert accounts_path == worker.ACCOUNTS_PATH
        # Model both caches retaining a query object. Nothing else owns it.
        payload = Payload()
        references.append(weakref.ref(payload))
        api_spreads._ROW_CACHE['retention-test'] = (0, [payload], {})
        api_spreads._RESULT_CACHE['retention-test'] = (0, 0, {'row': payload})
        if fails:
            raise ValueError('selection failed')
        return selected.copy()

    monkeypatch.setattr(worker, '_desired_legs', select)
    monkeypatch.setattr(worker, 'SNAPSHOT_PATH', tmp_path / 'snapshot')
    instance = worker.BookWorker.__new__(worker.BookWorker)
    instance._desired = set()
    instance._desired_signature = None
    instance._desired_computed_at = -worker.DESIRED_REFRESH_SECONDS
    if fails:
        with pytest.raises(ValueError, match='selection failed'):
            asyncio.run(instance._desired_legs_cached())
        assert instance._desired == set()
        assert instance._desired_signature is None
    else:
        assert asyncio.run(instance._desired_legs_cached()) == selected
        assert instance._desired == selected
        # No new selection inside the refresh floor, and no leg lost in cleanup.
        assert asyncio.run(instance._desired_legs_cached()) == selected
        assert len(references) == 1
    assert not api_spreads._ROW_CACHE
    assert not api_spreads._RESULT_CACHE
    # A failed asyncio task can leave an unreachable traceback cycle until GC.
    gc.collect()
    assert references[0]() is None
