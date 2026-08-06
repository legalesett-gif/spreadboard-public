#!/usr/bin/env python3
"""Audit mechanically selected live SpreadBoard routes and chart history."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from datetime import datetime, timezone
import json
import math
import os
import time
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

LANES = ("FUTURES", "FUTURES-SPOT-PAIR", "SPOT", "DEX-FUTURES")
WINDOWS = {
    "1m": 1 / 60,
    "5m": 5 / 60,
    "30m": 0.5,
    "1h": 1.0,
}

_OPENER = build_opener(HTTPCookieProcessor(CookieJar()))


def _json_url(url: str, *, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "SpreadBoardAudit/1"})
    with _OPENER.open(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("response_is_not_an_object")
    return payload


def _authenticate(base_url: str, *, email: str, password: str, timeout: float) -> None:
    payload = json.dumps({"email": email, "password": password}).encode("utf-8")
    request = Request(
        _endpoint(base_url, "/api/login"),
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "SpreadBoardAudit/1",
        },
        method="POST",
    )
    with _OPENER.open(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("audit_login_failed")


def _endpoint(base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
    url = f"{base_url.rstrip('/')}{path}"
    return f"{url}?{urlencode(query)}" if query else url


def _select_routes(base_url: str, *, limit: int, timeout: float) -> list[dict[str, Any]]:
    lane_groups: dict[str, list[dict[str, Any]]] = {}
    for lane in LANES:
        payload = _json_url(
            _endpoint(
                base_url,
                "/api/spreads",
                {
                    "kind": lane,
                    "limit": 25,
                    "sort": "edge",
                    "direction": "desc",
                },
            ),
            timeout=timeout,
        )
        lane_groups[lane] = [
            group
            for group in payload.get("groups") or []
            if isinstance(group, dict) and isinstance(group.get("best_route"), dict)
        ]

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    index = 0
    while len(selected) < limit:
        added = False
        for lane in LANES:
            groups = lane_groups[lane]
            if index >= len(groups):
                continue
            route = dict(groups[index]["best_route"])
            route["audit_lane"] = lane
            route_key = str(route.get("route_key") or "")
            if route_key and route_key not in seen:
                selected.append(route)
                seen.add(route_key)
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        index += 1
    return selected


def _history(
    base_url: str,
    route_key: str,
    *,
    hours: float,
    live: bool,
    timeout: float,
) -> dict[str, Any]:
    return _json_url(
        _endpoint(
            base_url,
            f"/api/history/{quote(route_key, safe='')}",
            {
                "hours": hours,
                "live": int(live),
                "max_points": 25000,
            },
        ),
        timeout=timeout,
    )


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _formula_errors(row: dict[str, Any]) -> list[str]:
    checks = (
        (
            "open",
            "executable_spread_pct",
            "short_bid_price",
            "long_ask_price",
        ),
        (
            "out",
            "exit_spread_pct",
            "long_bid_price",
            "short_ask_price",
        ),
        (
            "matched",
            "depth_weighted_spread_pct",
            "short_bid_vwap_price",
            "long_ask_vwap_price",
        ),
    )
    errors: list[str] = []
    for label, result_key, numerator_key, denominator_key in checks:
        shown = _number(row.get(result_key))
        numerator = _number(row.get(numerator_key))
        denominator = _number(row.get(denominator_key))
        if shown is None or numerator is None or denominator is None or denominator <= 0:
            errors.append(f"{label}_inputs_missing")
            continue
        calculated = (numerator / denominator - 1.0) * 100.0
        if not math.isclose(shown, calculated, rel_tol=0, abs_tol=1e-7):
            errors.append(f"{label}_formula_mismatch:{shown:.9f}!={calculated:.9f}")
    return errors


def _funding_errors(row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for side in ("long", "short"):
        if str(row.get(f"{side}_market_type") or "") != "Futures":
            continue
        if _number(row.get(f"{side}_current_funding_pct")) is None:
            errors.append(f"{side}_current_funding_missing")
        interval = _number(row.get(f"{side}_funding_interval_hours"))
        if interval is None or interval <= 0:
            errors.append(f"{side}_funding_interval_missing")
    return errors


def _audit_route(
    base_url: str,
    route: dict[str, Any],
    *,
    first_last_ts: int,
    timeout: float,
    max_age_seconds: float,
) -> dict[str, Any]:
    route_key = str(route["route_key"])
    history = _history(base_url, route_key, hours=1, live=True, timeout=timeout)
    rows = [row for row in history.get("rows") or [] if isinstance(row, dict)]
    latest = rows[-1] if rows else {}
    latest_ts = int(_number(latest.get("quote_ts_us")) or 0)
    errors: list[str] = []
    sample_status = str((history.get("sample") or {}).get("status") or "")
    if not history.get("ok"):
        errors.append("history_not_ok")
    if not rows:
        errors.append("history_empty")
    if sample_status not in {"ok", "idle"}:
        errors.append(f"sampler_{sample_status or 'missing'}")
    if latest_ts <= first_last_ts:
        errors.append("timestamp_did_not_advance")
    age = _number((history.get("meta") or {}).get("age_seconds"))
    if age is None or age > max_age_seconds:
        errors.append(f"latest_sample_too_old:{age}")
    errors.extend(_formula_errors(latest))
    errors.extend(_funding_errors(latest))

    window_counts: dict[str, int] = {}
    now_us = int(time.time() * 1_000_000)
    for label, hours in WINDOWS.items():
        payload = _history(base_url, route_key, hours=hours, live=False, timeout=timeout)
        window_rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
        cutoff = now_us - int(hours * 3600 * 1_000_000)
        too_old = [
            row
            for row in window_rows
            if int(_number(row.get("quote_ts_us")) or 0) < cutoff - 2_000_000
        ]
        if too_old:
            errors.append(f"{label}_window_leak")
        window_counts[label] = len(window_rows)

    return {
        "token": route.get("token"),
        "lane": route.get("audit_lane"),
        "route_key": route_key,
        "sample_status": sample_status,
        "first_last_quote_ts_us": first_last_ts,
        "last_quote_ts_us": latest_ts,
        "age_seconds": age,
        "window_counts": window_counts,
        "errors": errors,
        "ok": not errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--max-age-seconds", type=float, default=60.0)
    parser.add_argument("--output")
    parser.add_argument("--email", default=os.environ.get("SPREADBOARD_AUDIT_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("SPREADBOARD_AUDIT_PASSWORD"))
    args = parser.parse_args()

    if args.email or args.password:
        if not args.email or not args.password:
            parser.error("both --email and --password are required for authenticated audits")
        _authenticate(args.base_url, email=args.email, password=args.password, timeout=args.timeout)

    routes = _select_routes(
        args.base_url,
        limit=max(1, args.limit),
        timeout=args.timeout,
    )
    first: dict[str, int] = {}
    for route in routes:
        payload = _history(
            args.base_url,
            str(route["route_key"]),
            hours=1,
            live=True,
            timeout=args.timeout,
        )
        first[str(route["route_key"])] = int(
            _number((payload.get("meta") or {}).get("last_quote_ts_us")) or 0
        )
    time.sleep(max(1.0, args.settle_seconds))

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                _audit_route,
                args.base_url,
                route,
                first_last_ts=first[str(route["route_key"])],
                timeout=args.timeout,
                max_age_seconds=args.max_age_seconds,
            ): route
            for route in routes
        }
        for future in as_completed(futures):
            route = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "token": route.get("token"),
                        "lane": route.get("audit_lane"),
                        "route_key": route.get("route_key"),
                        "ok": False,
                        "errors": [f"{type(exc).__name__}:{exc}"],
                    }
                )
    results.sort(key=lambda item: LANES.index(str(item.get("lane"))))
    report = {
        "schema": "spreadboard.live_chart_audit.v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "base_url": args.base_url,
        "selection": "round_robin_top_ranked_groups_across_primary_lanes",
        "requested_routes": args.limit,
        "selected_routes": len(routes),
        "passed_routes": sum(bool(item.get("ok")) for item in results),
        "failed_routes": sum(not bool(item.get("ok")) for item in results),
        "routes": results,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    return 0 if len(routes) >= args.limit and not report["failed_routes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
