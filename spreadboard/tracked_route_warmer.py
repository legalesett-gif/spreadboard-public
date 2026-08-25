"""Continuously preserve current chart evidence for subscriber-tracked routes."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from spreadboard import (
    accounts,
    api_spreads,
    historical_spreads,
    market_history,
    warm_query_projection,
)

RouteResolver = Callable[[str], dict[str, Any] | None]
QuoteScheduler = Callable[[dict[str, Any]], dict[str, Any]]
RouteKeyProvider = Callable[[], list[str]]
_LAST_STATUS: dict[str, Any] = {}


class Worker(threading.Thread):
    """Warm saved charts, route alerts and open-position charts off-request."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        accounts_path: Path | str,
        route_resolver: RouteResolver,
        quote_scheduler: QuoteScheduler,
        proxy_route_keys_provider: RouteKeyProvider | None = None,
        interval_seconds: float = 10.0,
        persist_batch: int = 64,
        exact_batch: int = 6,
        proxy_batch: int = 4,
        warm_history_proxies: bool = True,
    ) -> None:
        super().__init__(name="subscriber-route-warm", daemon=True)
        self.stop_event = stop_event
        self.accounts_path = Path(accounts_path)
        self.route_resolver = route_resolver
        self.quote_scheduler = quote_scheduler
        self.proxy_route_keys_provider = proxy_route_keys_provider
        self.interval_seconds = max(5.0, float(interval_seconds))
        self.persist_batch = max(1, int(persist_batch))
        self.exact_batch = max(1, int(exact_batch))
        self.proxy_batch = max(1, int(proxy_batch))
        self.warm_history_proxies = bool(warm_history_proxies)
        self._persist_cursor = 0
        self._exact_cursor = 0
        self._proxy_cursor = 0
        self._proxy_warmed_at: dict[str, float] = {}
        self.last_run: dict[str, Any] = _empty_status()

    def run(self) -> None:
        if self.stop_event.wait(5.0):
            return
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.last_run = self.check_once()
            except Exception as exc:  # noqa: BLE001 - core pages remain independent.
                self.last_run = {
                    **_empty_status(),
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                }
            global _LAST_STATUS
            _LAST_STATUS = dict(self.last_run)
            self.stop_event.wait(
                max(0.0, self.interval_seconds - (time.monotonic() - started))
            )

    def check_once(self) -> dict[str, Any]:
        keys = accounts.all_tracked_route_keys(db_path=self.accounts_path)
        proxy_priority_keys = (
            self.proxy_route_keys_provider()
            if self.proxy_route_keys_provider is not None
            else []
        )
        proxy_keys = list(dict.fromkeys([*keys, *proxy_priority_keys]))
        if not proxy_keys:
            return {**_empty_status(), "ready": True}
        current, universe = warm_query_projection.LIVE_UNIVERSE.target_rows(
            route_keys=proxy_keys
        )
        by_key = {
            str(row.get("route_key") or ""): row
            for row in current
            if row.get("route_key")
        }
        persist_keys = _rotated(keys, self._persist_cursor, self.persist_batch)
        if keys:
            self._persist_cursor = (self._persist_cursor + len(persist_keys)) % len(keys)
        recordable = [
            by_key[key]
            for key in persist_keys
            if key in by_key
            and api_spreads.spread_quote_current(by_key[key])
            and api_spreads.matched_probe_verified(by_key[key])
        ]
        inserted = (
            market_history.record_snapshot(
                {"api_discovered_rows": recordable},
                sample_source="subscriber_tracked_resident_books",
                prune=False,
            )
            if recordable
            else 0
        )

        scheduled = 0
        inspected = 0
        # Round-robin cold routes separately from the cheap resident recorder.
        # At most ``exact_batch`` provider samplers are queued per pass, so one
        # hundred subscribers cannot create an unbounded exchange-call burst.
        for key in _rotated(keys, self._exact_cursor, len(keys)):
            inspected += 1
            row = by_key.get(key)
            if row is not None and api_spreads.spread_quote_current(row) and (
                api_spreads.matched_probe_verified(row)
            ):
                continue
            row = row or self.route_resolver(key)
            if row is None:
                continue
            self.quote_scheduler(row)
            scheduled += 1
            if scheduled >= self.exact_batch:
                break
        if keys:
            self._exact_cursor = (self._exact_cursor + max(1, inspected)) % len(keys)

        proxy_started = (
            self._warm_proxies(proxy_keys, by_key)
            if self.warm_history_proxies
            else 0
        )
        return {
            "ready": bool(universe.get("ready")),
            "tracked_routes": len(keys),
            "priority_chart_routes": len(proxy_priority_keys),
            "resident_routes": len(by_key),
            "recorded_routes": inserted,
            "exact_refreshes_scheduled": scheduled,
            "history_proxies_started": proxy_started,
            "history_proxy_started": proxy_started > 0,
            "universe_age_seconds": universe.get("age_seconds"),
            "error": None,
        }

    def _warm_proxies(
        self, keys: list[str], by_key: dict[str, dict[str, Any]]
    ) -> int:
        minimum_age = max(
            300.0,
            float(os.environ.get("SPREADBOARD_TRACKED_PROXY_REFRESH_SECONDS", "900")),
        )
        now = time.monotonic()
        inspected = 0
        started = 0
        for key in _rotated(keys, self._proxy_cursor, len(keys)):
            inspected += 1
            if now - self._proxy_warmed_at.get(key, 0.0) < minimum_age:
                continue
            row = by_key.get(key) or self.route_resolver(key)
            if row is None:
                continue
            result = historical_spreads.load_or_fetch(
                row,
                hours=24.0,
                max_points=1,
                blocking=False,
            )
            if result.get("status") == "warming" and not result.get("started"):
                # The global backfill pool is occupied (or this route is
                # already in flight). Retry on the next pass instead of
                # falsely suppressing it for fifteen minutes.
                break
            self._proxy_warmed_at[key] = now
            started += 1
            if started >= self.proxy_batch:
                break
        self._proxy_cursor = (self._proxy_cursor + max(1, inspected)) % len(keys)
        return started


def status() -> dict[str, Any]:
    return dict(_LAST_STATUS or _empty_status())


def _rotated(values: list[str], cursor: int, limit: int) -> list[str]:
    if not values or limit <= 0:
        return []
    offset = cursor % len(values)
    rotated = values[offset:] + values[:offset]
    return rotated[: min(len(values), limit)]


def _empty_status() -> dict[str, Any]:
    return {
        "ready": False,
        "tracked_routes": 0,
        "priority_chart_routes": 0,
        "resident_routes": 0,
        "recorded_routes": 0,
        "exact_refreshes_scheduled": 0,
        "history_proxy_started": False,
        "history_proxies_started": 0,
        "universe_age_seconds": None,
        "error": None,
    }
