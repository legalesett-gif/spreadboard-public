import json
from urllib.parse import parse_qs, urlsplit

from spreadboard import venue_funding_history as history

HOUR = 3_600_000
END = 500_000 * HOUR
NOW = END + HOUR // 2
START = END - 31 * 24 * HOUR


def events():
    change = END - 2 * 24 * HOUR
    times = list(range(START, change, HOUR)) + list(range(change, END + 1, 4 * HOUR))
    return [{"timestamp": timestamp, "fundingRate": 0.0001} for timestamp in times]


def proof(rows):
    return {"method": "aster_bounded_range_v1", "start_ms": START,
            "end_ms": NOW, "event_count": len(rows)}


def test_exhaustive_mixed_schedule_keeps_actual_30_day_payments():
    rows = events()
    assert history.realised_window_details(rows, now_ms=NOW)["windows"]["30d"] is None
    result = history.realised_window_details(rows, now_ms=NOW, source_range=proof(rows))
    expected = sum(r["fundingRate"] for r in rows if NOW - 30 * 24 * HOUR < r["timestamp"] <= NOW) * 100
    assert result["windows"]["30d"] == expected
    detail = result["window_details"]["30d"]
    assert detail["completeness_basis"] == "exhaustive_provider_range_with_cadence_runs"
    assert detail["inferred_interval_hours"] == 4.0
    assert history._recorded_completeness_failure(detail) is None


def test_exhaustive_response_does_not_excuse_an_isolated_missing_payment():
    rows = events()
    del rows[100]
    result = history.realised_window_details(rows, now_ms=NOW, source_range=proof(rows))
    assert result["windows"]["30d"] is None


def test_scheduled_transition_bridge_is_not_a_missing_regular_payment():
    change = END - 2 * 24 * HOUR
    times = list(range(START, change - 2 * HOUR, HOUR)) + list(range(change, END + 1, 4 * HOUR))
    rows = [{"timestamp": t, "fundingRate": 0.0001} for t in times]
    result = history.realised_window_details(rows, now_ms=NOW, source_range=proof(rows))
    assert result["windows"]["30d"] is not None


def test_new_listing_does_not_get_a_fabricated_full_window():
    rows = [r for r in events() if r["timestamp"] > NOW - 29 * 24 * HOUR]
    assert history.realised_window_details(rows, now_ms=NOW, source_range=proof(rows))["windows"]["30d"] is None


def test_native_reader_uses_encoded_exact_symbol_and_bounded_complete_response(monkeypatch):
    asked = []
    monkeypatch.setattr(history.time, "time", lambda: NOW / 1000)
    def fetch(url):
        asked.append(parse_qs(urlsplit(url).query))
        return [{"symbol": "龙虾USDT", "fundingTime": r["timestamp"], "fundingRate": str(r["fundingRate"])}
                for r in events() if r["timestamp"] >= int(asked[-1]["startTime"][0])]
    monkeypatch.setattr(history, "_fetch_json", fetch)
    result = history._aster_history("龙虾/USDT:USDT", days=30, max_pages=1)
    assert result["status"] == "ok"
    assert asked[0]["symbol"] == ["龙虾USDT"]
    assert asked[0]["limit"] == ["1000"]
    assert result["source_range"]["event_count"] == len(events()) - 1
    monkeypatch.setattr(history, "_fetch_json", lambda url: [{}] * 1000)
    assert history._aster_history("龙虾/USDT:USDT", days=30, max_pages=1)["status"] == "api_error"


def test_build_persists_proven_mixed_window_and_retains_expiry_guard(tmp_path, monkeypatch):
    rows = events()
    monkeypatch.setattr(history.time, "time", lambda: NOW / 1000)
    monkeypatch.setattr(history, "leg_history_outcome", lambda *a, **kw: {
        "status": "ok", "entries": rows, "source_range": proof(rows), "pages": 1,
    })
    path = tmp_path / "funding.json"
    history.build([("Aster", "TEST/USDT:USDT")], cache_path=path, budget_seconds=10)
    payload = json.loads(path.read_text())
    key = "Aster|TEST/USDT:USDT"
    values, expiry = history._current_leg_windows(payload["legs"][key], payload["leg_status"][key], now_ms=NOW)
    assert values["30d"] is not None
    assert expiry > NOW
    values, _ = history._current_leg_windows(payload["legs"][key], payload["leg_status"][key], now_ms=END + 4 * HOUR)
    assert values["30d"] is None
