"""Historical funding leaders remain discoverable without weakening Now."""

from __future__ import annotations

from spreadboard import funding_radar, server


def _gua() -> dict:
    return {
        "token": "GUA",
        "token_name": "GUA",
        "route_key": "GUA|Mexc|Spot|Aster|Futures",
        "route_kind": "SPOT-FUTURES",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_market_symbol": "GUA/USDT",
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
        "funding_daily_pct": 1.08,
        "funding_projected_24h_pct": 1.08,
        "funding_apr_pct": 394.2,
        "executable_spread_pct": -0.12,
    }


def test_a_cooled_leader_is_retained_with_its_settled_windows(tmp_path, monkeypatch) -> None:
    path = tmp_path / "funding_radar.json"
    monkeypatch.setattr(
        funding_radar.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": 1.1, "7d": 2.4, "30d": 5.9},
    )
    monkeypatch.setattr(funding_radar.market_history, "load_funding_windows", lambda: {})

    funding_radar.refresh([_gua()], cache_path=path, now=1_000_000)
    funding_radar.refresh([], cache_path=path, now=1_000_000 + 1_800)
    retained = funding_radar.routes_for("GUA", cache_path=path, now=1_000_000 + 1_800)

    assert len(retained) == 1
    assert retained[0]["radar_historical"] is True
    assert retained[0]["radar_last_seen_age_min"] == 30
    assert retained[0]["radar_windows"] == {"1d": 1.1, "7d": 2.4, "30d": 5.9}


def test_a_radar_record_expires_after_thirty_days(tmp_path, monkeypatch) -> None:
    path = tmp_path / "funding_radar.json"
    monkeypatch.setattr(
        funding_radar.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": 1.0, "7d": 2.0, "30d": 3.0},
    )
    monkeypatch.setattr(funding_radar.market_history, "load_funding_windows", lambda: {})
    funding_radar.refresh([_gua()], cache_path=path, now=1_000_000)

    funding_radar.refresh([], cache_path=path, now=1_000_000 + 31 * 86_400)

    assert funding_radar.routes_for("GUA", cache_path=path) == []


def test_historical_page_union_adds_gua_but_marks_it_non_live(monkeypatch) -> None:
    route = {
        **_gua(),
        "radar_historical": True,
        "radar_windows": {"1d": 1.1, "7d": 2.4, "30d": 5.9},
        "radar_last_seen_age_min": 30,
    }
    monkeypatch.setattr(funding_radar, "routes_for", lambda *_args, **_kwargs: [route])

    groups = server._historical_funding_groups(
        [], route_kind="FUTURES-SPOT-PAIR", window="7d", limit=25
    )

    assert [group["token"] for group in groups] == ["GUA"]
    assert groups[0]["best_funding_window_pct"] == 2.4
    html = server.render_funding_token_group(groups[0])
    assert "Cooled now" in html
    assert "Last basis" in html
    assert "Live now" not in html


def test_radar_kind_mapping_keeps_spot_futures_in_the_combined_lane() -> None:
    assert funding_radar.kind_matches("SPOT-FUTURES", "FUTURES-SPOT-PAIR")
    assert funding_radar.kind_matches("FUTURES-SPOT", "FUTURES-SPOT-PAIR")
    assert not funding_radar.kind_matches("FUTURES", "FUTURES-SPOT-PAIR")
