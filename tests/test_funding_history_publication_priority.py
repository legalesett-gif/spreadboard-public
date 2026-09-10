"""Continuous bulk publications must not starve exact settlement collection."""

import threading

import pytest

from scripts import run_spreadboard_service as service


@pytest.mark.parametrize("state", ["recent", "cold", "stale"])
def test_history_uses_recent_real_publisher_but_yields_to_cold_or_stale_index(
    monkeypatch, state,
):
    clock = [1000.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "_COLLECTOR_HEAVY_LOCK", threading.Lock())
    monkeypatch.setattr(service, "_BACKGROUND_ANALYTICS_LOCK", threading.Lock())
    monkeypatch.setattr(service, "_refresh_live_route_index", lambda **kwargs: True)
    monkeypatch.setattr(service, "_schedule_token_rankings", lambda: None)
    monkeypatch.setattr(service, "_log", lambda _: None)
    publisher = service.LiveRouteIndexPublisher(threading.Event(), min_interval_seconds=120)
    publisher.request()
    if state != "cold":
        assert publisher.check_once()["status"] == "published"
        publisher.request()
    clock[0] += 121 if state == "recent" else 301
    assert publisher.publication_due()

    history = service.MarketEvidenceLoop(threading.Event(), route_index_publisher=publisher)
    history.INTERVAL_SECONDS = 300
    ran = []

    def sweep():
        # History still owns the shared slot; the next index cannot overlap it.
        assert not service._COLLECTOR_HEAVY_LOCK.acquire(blocking=False)
        assert publisher.check_once()["status"] == "publication_slot_busy"
        ran.append(True)

    monkeypatch.setattr(history, "_run_isolated_sweep", sweep)
    history._sweep_once()
    assert ran == ([True] if state == "recent" else [])
    assert publisher.has_pending()
    # As soon as the history owner releases the slot, the pending index can run.
    assert publisher.check_once()["status"] == "published"
