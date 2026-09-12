"""A slow history provider cannot block fast venues or discard fetched events."""
import threading
import time

from spreadboard import venue_funding_history as history


def test_slow_venue_does_not_hold_fast_venue_behind_batch_barrier(monkeypatch):
    completed_fast = threading.Event()

    def fetch(venue, symbol, budget):
        if venue == "Slow":
            assert completed_fast.wait(2), "fast venue was trapped behind a batch barrier"
        elif symbol == "3":
            completed_fast.set()
        return {"status": "ok", "entries": []}

    monkeypatch.setattr(history, "_fetch_leg_outcome", fetch)
    monkeypatch.setattr(history, "FUNDING_HISTORY_PER_VENUE", 1)
    legs = [("Slow", "1"), ("Fast", "1"), ("Fast", "2"), ("Fast", "3")]
    results = list(history._fetch_outcomes_in_batches(
        legs, page_budget_for=lambda *args: 1,
        deadline=time.monotonic() + 10, workers=2,
    ))
    assert completed_fast.is_set()
    assert {(venue, symbol) for venue, symbol, _, _ in results} == set(legs)


def test_budget_stops_new_work_but_preserves_completed_request(monkeypatch):
    clock = [1.0]
    asked = []

    def fetch(venue, symbol, budget):
        asked.append(symbol)
        clock[0] = 20.0
        return {"status": "ok", "entries": []}

    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(history, "_fetch_leg_outcome", fetch)
    results = list(history._fetch_outcomes_in_batches(
        [("Gate", "1"), ("Gate", "2")], page_budget_for=lambda *args: 1,
        deadline=10.0, workers=1,
    ))
    assert asked == ["1"]
    assert len(results) == 1
    assert results[0][1] == "1"
