"""Historical segments, live metadata and expiry must not share one guessed clock."""
import pytest

from spreadboard import bulk_quotes, fast_quotes
from spreadboard import venue_funding_history as h

HOUR = 3_600_000
END = 500_000 * HOUR
NOW = END + HOUR // 2


def rows_for(before, after):
    change = END - 48 * HOUR
    times = list(range(END - 31 * 24 * HOUR, change, before * HOUR))
    times += list(range(change, END + 1, after * HOUR))
    return [{"timestamp": t, "fundingRate": .0001} for t in times]


@pytest.mark.parametrize("before,after", [(1, 4), (4, 1), (8, 1), (1, 8), (4, 2), (2, 4)])
def test_each_transition_uses_actual_events_and_latest_cadence(before, after):
    rows = rows_for(before, after)
    result = h.realised_window_details(rows, now_ms=NOW)
    detail = result["window_details"]["30d"]
    assert result["windows"]["30d"] == pytest.approx(sum(
        r["fundingRate"] for r in rows if NOW - 30 * 24 * HOUR < r["timestamp"] <= NOW) * 100)
    assert detail["inferred_interval_hours"] == after
    assert detail["cadence_segments"][-1]["interval_hours"] == after
    assert h._window_expiry_ms({"window_details": {"30d": detail}}, "30d") <= END + after * HOUR


@pytest.mark.parametrize("before,after", [(1, 4), (4, 1), (8, 1), (1, 8)])
@pytest.mark.parametrize("where", ["old_regime", "new_regime"])
def test_no_segment_can_hide_a_missing_payment(before, after, where):
    rows = rows_for(before, after)
    del rows[30 if where == "old_regime" else -4]
    assert h.realised_window_details(rows, now_ms=NOW)["windows"]["30d"] is None


def test_new_tail_with_only_one_payment_remains_unconfirmed():
    rows = rows_for(4, 4)
    rows.append({"timestamp": END + HOUR, "fundingRate": .0001})
    assert h.realised_window_details(rows, now_ms=END + HOUR + 100)["windows"]["30d"] is None


def test_legacy_percentile_mixed_total_requires_revalidation():
    detail = {"complete": True, "event_count": 198, "expected_event_count": 180,
              "inferred_interval_hours": 4, "max_gap_hours": 4}
    assert h._recorded_completeness_failure(detail) == "mixed_cadence_revalidation_required"


def test_last_complete_fallback_cannot_revive_invalid_mixed_total(monkeypatch):
    detail = {"complete": True, "event_count": 198, "expected_event_count": 180,
              "latest_event_at": END, "inferred_interval_hours": 4, "max_gap_hours": 4}
    monkeypatch.setattr(h, "_load_raw", lambda: {"legs": {"Gate|ONE": {"30d": 12}},
                        "leg_status": {"Gate|ONE": {"window_details": {"30d": detail}}}})
    route = {"long_market_type": "Spot", "short_market_type": "Futures",
             "short_venue": "Gate", "short_market_symbol": "ONE"}
    assert h.route_windows_last_complete(route) == {}


def test_slower_current_schedule_requires_exhaustive_history_before_extending_expiry():
    rows = [{"timestamp": END - i * HOUR, "fundingRate": .0001} for i in range(745)]
    r = h.realised_window_details(rows, now_ms=END + HOUR // 2)
    status = {"window_details": r["window_details"]}
    live = {"interval_hours": 4, "interval_assumed": False,
            "next_funding_ts_us": (END + 4 * HOUR) * 1000}
    # At one hour, the oldest 24h/7d/30d event also exits. Use a synthetic
    # later rolling boundary to isolate the next-settlement rule here.
    for detail in status["window_details"].values():
        detail.pop("earliest_event_at")
    now = END + HOUR + 100
    assert h._current_leg_windows(r["windows"], status, now_ms=now, live_leg=live)[0]["30d"] is None
    status["source_range"] = {"method": "aster_bounded_range_v1", "end_ms": now}
    assert h._current_leg_windows(r["windows"], status, now_ms=now, live_leg=live)[0]["30d"] is not None
    # Switching back to hourly expires the old series again immediately.
    live.update(interval_hours=1, next_funding_ts_us=(END + 2 * HOUR) * 1000)
    assert h._current_leg_windows(r["windows"], status, now_ms=now, live_leg=live)[0]["30d"] is None


@pytest.mark.parametrize("payload", [{"code": "error"}, ["malformed"], [{"symbol": "ONE", "fundingIntervalHours": "NaN"}]])
def test_malformed_published_schedule_never_falls_back_to_eight_hours(monkeypatch, payload):
    monkeypatch.setattr(fast_quotes, "_json_url", lambda _: payload)
    assert fast_quotes.FastQuoteRefresher._bulk_funding_interval_overrides("Aster") is None


def test_schedule_journal_tracks_both_directions_once_and_has_no_invented_effective_time():
    previous = {"legs": {"Aster|ONE": {"interval_hours": 1, "interval_assumed": False}},
                "leg_updated_at": {"Aster|ONE": 100}}
    rates = {"Aster|ONE": {"interval_hours": 4, "interval_assumed": False,
                            "interval_source": "provider_funding_info"}}
    changes = bulk_quotes._schedule_changes(previous, rates, {"Aster|ONE": 200})
    assert len(changes) == 1
    assert changes[0]["effective_at"] is None
    assert changes[0]["observed_at"] == 200
    previous = {"legs": rates, "schedule_changes": changes}
    assert bulk_quotes._schedule_changes(previous, rates, {}) == changes
    reverted = {"Aster|ONE": {"interval_hours": 1}}
    assert len(bulk_quotes._schedule_changes(previous, reverted, {})) == 2
    reverted["Aster|ONE"]["interval_assumed"] = True
    assert bulk_quotes._schedule_changes(previous, reverted, {}) == changes
