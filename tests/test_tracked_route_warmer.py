from __future__ import annotations

import threading
import time
from pathlib import Path

from spreadboard import accounts, tracked_route_warmer, warm_query_projection


def _route(key: str, token: str, *, age_seconds: float = 0.0) -> dict[str, object]:
    stamp = int((time.time() - age_seconds) * 1_000_000)
    return {
        "route_key": key,
        "token": token,
        "route_kind": "FUTURES",
        "long_venue": "Gate",
        "long_market_type": "Futures",
        "long_market_symbol": f"{token}/USDT:USDT",
        "short_venue": "Mexc",
        "short_market_type": "Futures",
        "short_market_symbol": f"{token}/USDT:USDT",
        "long_price": 1.0,
        "short_price": 1.01,
        "long_bid": 0.999,
        "long_ask": 1.0,
        "short_bid": 1.01,
        "short_ask": 1.011,
        "executable_spread_pct": 1.0,
        "displayed_open_spread_pct": 1.0,
        "depth_weighted_spread_pct": 1.0,
        "depth_usd": 500.0,
        "matched_size_notional_usd": 500.0,
        "depth_unverified": False,
        "funding_daily_pct": 0.1,
        "quote_ts_us": stamp,
        "deliverable": True,
    }


def test_account_route_priority_contains_alerts_and_saved_charts(tmp_path: Path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="tracked@example.test",
        display_name="Tracked",
        password="long-enough-tracked-password",
        db_path=db_path,
    )
    accounts.add_saved_chart(
        user["id"], {"route_key": "SAVED", "label": "Saved"}, db_path=db_path
    )
    accounts.add_market_alert_rule(
        user["id"],
        {
            "route_key": "ALERT",
            "symbol": "GUA",
            "type": "token_spread",
            "threshold": 1,
        },
        db_path=db_path,
    )
    accounts.add_market_alert_rule(
        user["id"],
        {"symbol": "GUA", "type": "price", "threshold": 1},
        db_path=db_path,
    )

    assert accounts.all_tracked_route_keys(db_path=db_path) == ["ALERT", "SAVED"]


def test_worker_records_resident_route_and_schedules_only_cold_route(
    tmp_path: Path, monkeypatch
) -> None:
    current = _route("CURRENT", "GUA")
    stale = _route("STALE", "BTW", age_seconds=600)
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({"CURRENT": current, "STALE": stale})
    monkeypatch.setattr(
        warm_query_projection.api_spreads,
        "live_route_updates_for",
        lambda *_args, **_kwargs: {
            "CURRENT": (1.2, 0.1, int(time.time() * 1_000_000), "matched_vwap")
        },
    )
    universe.refresh()
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)
    monkeypatch.setattr(
        tracked_route_warmer.accounts,
        "all_tracked_route_keys",
        lambda **_kwargs: ["CURRENT", "STALE"],
    )
    recorded = []
    monkeypatch.setattr(
        tracked_route_warmer.market_history,
        "record_snapshot",
        lambda payload, **_kwargs: recorded.extend(payload["api_discovered_rows"]) or 1,
    )
    proxies = []
    monkeypatch.setattr(
        tracked_route_warmer.historical_spreads,
        "load_or_fetch",
        lambda row, **_kwargs: proxies.append(row["route_key"])
        or {"status": "warming", "started": True},
    )
    scheduled = []
    worker = tracked_route_warmer.Worker(
        threading.Event(),
        accounts_path=tmp_path / "accounts.sqlite3",
        route_resolver=lambda key: stale if key == "STALE" else current,
        quote_scheduler=lambda row: scheduled.append(row["route_key"]) or {"status": "warming"},
        persist_batch=10,
        exact_batch=2,
    )

    status = worker.check_once()

    assert [row["route_key"] for row in recorded] == ["CURRENT"]
    assert scheduled == ["STALE"]
    assert proxies
    assert status["tracked_routes"] == 2
    assert status["resident_routes"] == 2


def test_priority_funding_chart_is_warmed_without_becoming_an_exact_subscriber_route(
    tmp_path: Path, monkeypatch
) -> None:
    priority = _route("PRIORITY", "GUA")
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({"PRIORITY": priority})
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)
    monkeypatch.setattr(
        tracked_route_warmer.accounts,
        "all_tracked_route_keys",
        lambda **_kwargs: [],
    )
    proxies: list[str] = []
    scheduled: list[str] = []
    monkeypatch.setattr(
        tracked_route_warmer.historical_spreads,
        "load_or_fetch",
        lambda row, **_kwargs: proxies.append(str(row["route_key"]))
        or {"status": "warming", "started": True},
    )
    worker = tracked_route_warmer.Worker(
        threading.Event(),
        accounts_path=tmp_path / "accounts.sqlite3",
        route_resolver=lambda _key: priority,
        quote_scheduler=lambda row: scheduled.append(str(row["route_key"]))
        or {"status": "warming", "started": True},
        proxy_route_keys_provider=lambda: ["PRIORITY"],
    )

    status = worker.check_once()

    assert proxies == ["PRIORITY"]
    assert scheduled == []
    assert status["tracked_routes"] == 0
    assert status["priority_chart_routes"] == 1


def test_proxy_batch_starts_multiple_visible_routes(tmp_path: Path, monkeypatch) -> None:
    routes = {f"R{index}": _route(f"R{index}", "GUA") for index in range(6)}
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install(routes)
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)
    monkeypatch.setattr(
        tracked_route_warmer.accounts,
        "all_tracked_route_keys",
        lambda **_kwargs: [],
    )
    warmed: list[str] = []
    monkeypatch.setattr(
        tracked_route_warmer.historical_spreads,
        "load_or_fetch",
        lambda row, **_kwargs: warmed.append(str(row["route_key"]))
        or {"status": "warming", "started": True},
    )
    worker = tracked_route_warmer.Worker(
        threading.Event(),
        accounts_path=tmp_path / "accounts.sqlite3",
        route_resolver=lambda key: routes[key],
        quote_scheduler=lambda _row: {"status": "unused"},
        proxy_route_keys_provider=lambda: list(routes),
        proxy_batch=4,
    )

    status = worker.check_once()

    assert warmed == ["R0", "R1", "R2", "R3"]
    assert status["history_proxies_started"] == 4


def test_saturated_proxy_pool_is_retried_next_pass(tmp_path: Path, monkeypatch) -> None:
    route = _route("WAIT", "GUA")
    universe = warm_query_projection.LiveRouteUniverse()
    universe.install({"WAIT": route})
    monkeypatch.setattr(warm_query_projection, "LIVE_UNIVERSE", universe)
    monkeypatch.setattr(
        tracked_route_warmer.accounts,
        "all_tracked_route_keys",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        tracked_route_warmer.historical_spreads,
        "load_or_fetch",
        lambda *_args, **_kwargs: {"status": "warming", "started": False},
    )
    worker = tracked_route_warmer.Worker(
        threading.Event(),
        accounts_path=tmp_path / "accounts.sqlite3",
        route_resolver=lambda _key: route,
        quote_scheduler=lambda _row: {"status": "unused"},
        proxy_route_keys_provider=lambda: ["WAIT"],
    )

    first = worker.check_once()
    second = worker.check_once()

    assert first["history_proxies_started"] == 0
    assert second["history_proxies_started"] == 0
    assert worker._proxy_warmed_at == {}
