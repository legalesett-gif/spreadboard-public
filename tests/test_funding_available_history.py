from datetime import UTC, datetime

import pytest

from spreadboard import funding_available_history as available
from spreadboard import settlement_store
from spreadboard import venue_funding_history as history

HOUR = 3_600_000
NOW = 1_789_252_200_000  # Fixed instant, half an hour after an hourly settlement.
NOW = NOW // HOUR * HOUR + HOUR // 2


def route(long_type="spot"):
    return {"long_venue": "Mexc", "long_market_type": long_type,
            "long_market_symbol": "ANSEM/USDT:USDT",
            "short_venue": "Hyperliquid", "short_market_type": "futures",
            "short_market_symbol": "PARA-ANSEM/USDC:USDC"}


def setup(monkeypatch, tmp_path, legs_data):
    path = tmp_path / "funding.sqlite3"
    legs, statuses = {}, {}
    for key, entries in legs_data.items():
        venue, symbol = key.split("|", 1)
        settlement_store.merge(path, venue, symbol, entries, NOW)
        details = history.realised_window_details(entries, now_ms=NOW)
        legs[key] = details["windows"]
        statuses[key] = {**details, "status": "ok",
                         "updated_at": datetime.fromtimestamp(NOW / 1000, UTC).isoformat()}
    monkeypatch.setattr(history, "_load_raw", lambda **kw: {"legs": legs, "leg_status": statuses})
    monkeypatch.setattr(available.bulk_quotes, "load_funding", dict)
    return path, legs


def events(count, rate=.0001):
    return [{"timestamp": NOW // HOUR * HOUR - i * HOUR, "fundingRate": rate}
            for i in reversed(range(count))]


def test_young_contract_shows_settled_sum_not_projected_30d(monkeypatch, tmp_path):
    rows = events(439)
    path, legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": rows})
    result = available.route_windows(route(), legs=legs, now_ms=NOW, store_path=path)
    assert history.route_windows(route(), legs=legs)["30d"] is None
    assert set(result) == {"30d"}
    assert result["30d"]["net"] == pytest.approx(4.39)
    assert result["30d"]["duration_days"] == pytest.approx(438.5 / 24)
    assert result["30d"]["listing_date_verified"] is False
    assert result["30d"]["event_counts"] == {"short": 439}


def test_full_30d_stays_strict(monkeypatch, tmp_path):
    path, legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": events(750)})
    assert available.route_windows(route(), legs=legs, now_ms=NOW, store_path=path) == {}
    assert history.route_windows(route(), legs=legs)["30d"] == pytest.approx(7.2)


def test_both_legs_sum_same_shorter_period(monkeypatch, tmp_path):
    path, legs = setup(monkeypatch, tmp_path, {
        "Hyperliquid|PARA-ANSEM/USDC:USDC": events(439, .0002),
        "Mexc|ANSEM/USDT:USDT": events(750, .0001),
    })
    result = available.route_windows(route("futures"), legs=legs, now_ms=NOW, store_path=path)["30d"]
    assert result["net"] == pytest.approx(4.39)  # 439 * (.0002 - .0001) * 100
    assert result["event_counts"] == {"long": 439, "short": 439}


def test_missing_interior_payment_is_not_short_history(monkeypatch, tmp_path):
    rows = events(439)
    del rows[200]
    path, legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": rows})
    assert available.route_windows(route(), legs=legs, now_ms=NOW, store_path=path) == {}


def test_freshness_expires_at_next_settlement(monkeypatch, tmp_path):
    path, legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": events(439)})
    assert available.route_windows(route(), legs=legs, now_ms=NOW + HOUR, store_path=path) == {}


def test_sub_day_history_is_labelled_for_all_longer_windows(monkeypatch, tmp_path):
    path, legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": events(8)})
    result = available.route_windows(route(), legs=legs, now_ms=NOW, store_path=path)
    assert set(result) == {"1d", "7d", "30d"}
    assert all(value["net"] == pytest.approx(.08) for value in result.values())


def test_missing_store_is_unavailable_not_zero(monkeypatch, tmp_path):
    _path, legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": events(439)})
    missing = tmp_path / "missing.sqlite3"
    assert available.route_windows(route(), legs=legs, now_ms=NOW, store_path=missing) == {}
    assert not missing.exists()


def test_retired_or_unindexed_leg_cannot_return_from_old_ledger(monkeypatch, tmp_path):
    path, _legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": events(439)})
    assert available.route_windows(route(), legs={}, now_ms=NOW, store_path=path) == {}


def test_ranking_navigation_and_html_include_short_history(monkeypatch, tmp_path):
    from spreadboard import funding_catalog, funding_radar, server

    path, legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": events(439)})
    monkeypatch.setattr(history, "load", lambda: legs)
    native_reader = available.route_windows
    monkeypatch.setattr(available, "route_windows", lambda row, **kw: native_reader(
        row, legs=legs, now_ms=NOW, store_path=path,
    ))
    young = {**route(), "token": "ANSEM", "route_key": "ansem", "route_kind": "SPOT-FUTURES"}
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(funding_catalog, "_complete_payloads", lambda: {"ANSEM": {"routes": [young]}})
    monkeypatch.setattr(funding_catalog, "_overlaid_routes", lambda rows, **kw: rows)
    monkeypatch.setattr(funding_radar, "routes_for", lambda *a, **kw: [])

    page = funding_catalog.page(route_kind="FUTURES-SPOT-PAIR", window="30d")
    assert page["groups"][0]["token"] == "ANSEM"
    row = page["groups"][0]["routes"][0]
    assert row["settled_funding_windows"]["30d"] is None
    assert row["settled_funding_available"]["30d"]["net"] == pytest.approx(4.39)
    assert page["largest_value"] == pytest.approx(4.39)

    monkeypatch.setattr(available.bulk_quotes, "load_funding", lambda: {
        "Hyperliquid|PARA-ANSEM/USDC:USDC": {"rate_pct": .01, "interval_hours": 1, "age_seconds": 1},
    })
    nav = funding_catalog.build_navigation_pages()[("FUTURES-SPOT-PAIR", "30d")]
    saved = nav["groups"][0]["routes"][0]
    assert nav["largest_value"] == pytest.approx(4.39)
    assert saved["funding_navigation_windows"]["30d"] is None
    assert funding_radar.window_value(saved, "30d") is None
    assert funding_radar.display_window_value(saved, "30d") == pytest.approx(4.39)
    monkeypatch.setattr(history, "route_history_status", lambda row: {})
    monkeypatch.setattr(history, "route_windows_last_complete", lambda row: {})
    html = server.render_funding_windows(saved, "ansem")
    assert "+4.39%" in html and "18.3d available" in html
    assert "not a full 30d return" in html
    assert server.funding_rank_value(saved, "30d") == pytest.approx(4.39)
    assert "18.3d available" in server.funding_rank_basis(saved, "30d")
    assert server.funding_metric_label(saved, "30d") == "Settled up to 30d"


def test_negative_short_period_keeps_its_sign(monkeypatch, tmp_path):
    path, legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": events(439, -.0001)})
    result = available.route_windows(route(), legs=legs, now_ms=NOW, store_path=path)
    assert result["30d"]["net"] == pytest.approx(-4.39)


def test_live_schedule_shortening_expires_partial_before_old_interval(monkeypatch, tmp_path):
    rows = [{"timestamp": NOW // HOUR * HOUR - i * 4 * HOUR, "fundingRate": .0001}
            for i in reversed(range(100))]
    path, legs = setup(monkeypatch, tmp_path, {"Hyperliquid|PARA-ANSEM/USDC:USDC": rows})
    monkeypatch.setattr(available.bulk_quotes, "load_funding", lambda: {
        "Hyperliquid|PARA-ANSEM/USDC:USDC": {
            "rate_pct": .01, "interval_hours": 1,
            "next_funding_ts_us": (NOW // HOUR * HOUR + 2 * HOUR) * 1000,
        },
    })
    assert available.route_windows(route(), legs=legs, now_ms=NOW + HOUR, store_path=path) == {}
