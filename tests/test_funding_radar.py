"""Historical funding leaders remain discoverable without weakening Now."""

from __future__ import annotations

import inspect
from pathlib import Path

from scripts import run_spreadboard_service as service
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
    monkeypatch.setattr(
        funding_radar.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": 1.1, "7d": 2.4, "30d": 5.9},
    )

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


def test_blank_exact_history_never_becomes_a_sampled_realised_window(monkeypatch) -> None:
    monkeypatch.setattr(
        funding_radar.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": None, "7d": None, "30d": None},
    )

    assert funding_radar.window_value(_gua(), "7d") is None


def test_retained_route_uses_current_trailing_window_not_frozen_radar(monkeypatch) -> None:
    route = {
        **_gua(),
        "radar_historical": True,
        "radar_windows": {"1d": 9.0, "7d": 19.0, "30d": 29.0},
    }
    monkeypatch.setattr(
        funding_radar.venue_funding_history,
        "route_windows",
        lambda _route: {"1d": 1.2, "7d": 4.8, "30d": None},
    )

    assert funding_radar.window_value(route, "1d") == 1.2
    assert funding_radar.window_value(route, "7d") == 4.8
    assert funding_radar.window_value(route, "30d") is None


def _dex_route(
    token: str,
    key: str,
    *,
    value: float | None,
    short_venue: str = "Gate",
) -> dict:
    return {
        "token": token,
        "route_key": key,
        "route_kind": "DEX-FUTURES",
        "long_venue": "OKX DEX 56",
        "long_market_type": "DEX",
        "long_market_symbol": f"0x{token.casefold()}",
        "short_venue": short_venue,
        "short_market_type": "Futures",
        "short_market_symbol": f"{token}/USDT:USDT",
        "exact_7d": value,
    }


def test_dex_history_filters_then_ranks_all_routes_before_token_pagination(monkeypatch) -> None:
    top = _dex_route("TOP", "legacy-top", value=5.0)
    multi_a = _dex_route("MULTI", "multi-a", value=4.5, short_venue="Gate")
    multi_b = _dex_route("MULTI", "multi-b", value=4.2, short_venue="Aster")
    second = _dex_route("SECOND", "second", value=3.0)
    negative = _dex_route("NEG", "negative", value=-1.0)
    incomplete = _dex_route("BLANK", "blank", value=None)
    retained_duplicate = {**top, "route_key": "CUSTOM|top", "radar_historical": True}
    retained = {
        **_dex_route("RETAIN", "retained", value=4.0),
        "radar_historical": True,
    }
    route_calls: list[dict] = []

    def retained_routes(*_args, **kwargs):
        route_calls.append(kwargs)
        return [retained_duplicate, retained]

    monkeypatch.setattr(funding_radar, "routes_for", retained_routes)
    monkeypatch.setattr(
        funding_radar.venue_funding_history,
        "route_windows",
        lambda route: {"1d": None, "7d": route.get("exact_7d"), "30d": None},
    )
    current = [
        {
            "token": "source",
            "routes": [top, multi_a, multi_b, second, negative, incomplete],
        }
    ]

    page = server._historical_funding_page(
        current,
        route_kind="DEX-FUTURES",
        window="7d",
        offset=1,
        limit=1,
    )

    assert route_calls == [{"route_kind": "DEX-FUTURES"}]
    assert page["matching_token_count"] == 4
    assert page["matching_route_count"] == 5
    assert page["returned_token_count"] == 1
    assert page["returned_route_count"] == 2
    assert page["largest_value"] == 5.0
    assert [group["token"] for group in page["groups"]] == ["MULTI"]
    assert page["groups"][0]["route_count"] == 2
    assert page["groups"][0]["best_funding_window_aggregate_pct"] == 4.5


def test_radar_routes_dedupe_legacy_and_custom_keys_by_economic_legs(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "funding_radar.json"
    monkeypatch.setattr(
        funding_radar.venue_funding_history,
        "route_windows",
        lambda route: {"1d": route.get("exact_7d"), "7d": 2.0, "30d": 3.0},
    )
    legacy = _dex_route("SAME", "legacy", value=1.0)
    custom = {**legacy, "route_key": "CUSTOM|same"}

    funding_radar.refresh([legacy, custom], cache_path=path, now=1_000_000)
    retained = funding_radar.routes_for("SAME", cache_path=path, now=1_000_001)

    assert len(retained) == 1


def test_dex_historical_api_exports_the_same_globally_ranked_page_two(monkeypatch) -> None:
    loaded: list[dict] = []
    expanded: list[dict] = []
    page_two = _dex_route("PAGE2", "page-two", value=2.0)

    monkeypatch.setattr(
        server.api_spreads,
        "load_spreads",
        lambda **kwargs: loaded.append(kwargs)
        or {
            "ok": True,
            "groups": [{"token": "CURRENT", "routes": []}],
            "rows": [],
            "summary": {},
            "source_health": {"canonical_api": {"status": "fresh"}},
        },
    )

    def historical_page(groups, **kwargs):
        expanded.append({"groups": groups, **kwargs})
        return {
            "ok": True,
            "mode": "complete_dex_funding_radar_ranked_before_pagination",
            "window": "7d",
            "window_value_kind": "aggregate_exact_settlements",
            "window_duration_days": 7,
            "now_is_independent": True,
            "groups": [{"token": "PAGE2", "routes": [page_two], "route_count": 1}],
            "rows": [page_two],
            "matching_token_count": 41,
            "matching_route_count": 63,
            "returned_token_count": 1,
            "returned_route_count": 1,
            "offset": 20,
            "limit": 20,
            "largest_value": 7.5,
        }

    monkeypatch.setattr(server, "_historical_funding_page", historical_page)

    class UnrelatedBuildSlot:
        def acquire(self, **_kwargs):
            raise AssertionError("historical DEX pagination must not queue here")

        def release(self):
            raise AssertionError("an unacquired slot must not be released")

    monkeypatch.setattr(server, "_MARKET_BUILD_SLOTS", UnrelatedBuildSlot())

    payload = server.api_market_spreads(
        Path("board.json"),
        {
            "funding_only": ["1"],
            "kind": ["DEX-FUTURES"],
            "funding_window": ["7d"],
            "sort": ["funding"],
            "direction": ["desc"],
            "offset": ["20"],
            "limit": ["20"],
            "no_cache": ["1"],
        },
    )

    assert loaded[0]["offset"] == 0
    assert loaded[0]["limit"] == 500
    assert expanded[0]["offset"] == 20
    assert expanded[0]["limit"] == 20
    assert expanded[0]["window"] == "7d"
    assert payload["funding_catalog"]["window"] == "7d"
    assert payload["pagination"]["offset"] == 20
    assert payload["summary"]["matching_tokens"] == 41
    assert [group["token"] for group in payload["groups"]] == ["PAGE2"]


def test_broad_dex_history_lane_is_archived_but_not_added_to_telegram_queries() -> None:
    assert service.FUNDING_ARCHIVE_QUERIES == (
        {
            "funding_only": ["1"],
            "kind": ["DEX-FUTURES"],
            "sort": ["funding"],
            "direction": ["desc"],
            "limit": ["500"],
            "offset": ["0"],
        },
    )
    telegram_queries = [
        query for query in service.WARM_QUERIES if query.get("funding_only")
    ]
    assert service.FUNDING_ARCHIVE_QUERIES[0] not in telegram_queries
    assert "FUNDING_ARCHIVE_QUERIES" in inspect.getsource(service._refresh_funding_windows)


def test_web_startup_explicitly_owns_the_cold_funding_generation() -> None:
    source = inspect.getsource(service._warm_telegram_payload_at_startup)

    assert "funding_catalog.refresh_cache()" in source
    assert source.index("funding_catalog.refresh_cache()") < source.index(
        "_complete_telegram_funding_payloads"
    )
