"""A fresh daily window cannot starve an unchecked longer archive."""
import json

from spreadboard import venue_funding_history as history


def test_background_checks_deep_archive_after_expired_before_current(tmp_path, monkeypatch):
    now = 1_800_000_000_000
    hour = 3_600_000
    labels = ("CURRENT", "CHECKED_SHORT", "UNCHECKED", "EXPIRED")
    legs = [("Gate", label + "/USDT:USDT") for label in labels]
    statuses, windows = {}, {}
    for label, (venue, symbol) in zip(labels, legs):
        key = venue + "|" + symbol
        latest = now - (5 if label == "EXPIRED" else 1 if label == "UNCHECKED" else 3) * hour
        values = {"1d": 0.1, "7d": 0.2 if label == "CURRENT" else None,
                  "30d": 0.3 if label == "CURRENT" else None}
        windows[key] = values
        statuses[key] = {"status": "ok", "window_details": {
            period: {"complete": True, "latest_event_at": latest,
                     "earliest_event_at": latest - int(period[:-1]) * 24 * hour + 4 * hour,
                     "inferred_interval_hours": 4}
            for period, value in values.items() if value is not None}}
        if label == "CHECKED_SHORT":
            statuses[key]["deep_history_checked_at"] = "already checked"
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"schema": history.SCHEMA, "leg_status": statuses, "legs": windows}))
    monkeypatch.setattr(history.time, "time", lambda: now / 1000)
    seen = []
    def fetch(items, *, page_budget_for, **_kwargs):
        seen.extend((item, page_budget_for(*item)) for item in items)
        return iter(())
    monkeypatch.setattr(history, "_fetch_outcomes_in_batches", fetch)
    history.build(legs, cache_path=path, budget_seconds=10)
    assert [item for item, _ in seen][:2] == [legs[3], legs[2]]
    assert dict(seen)[legs[2]] == history.PRIORITY_HISTORY_PAGES
    assert {item for item, _ in seen} == set(legs)
