"""Independent coverage reconciliation and completed-cycle health gates."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spreadboard import api_spreads, chart_catalog, funding_navigation, live_book_cache

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
REFERENCE_PATH = RUNTIME_DIR / "uacryptoinvest_reference_latest.json"
STATUS_PATH = RUNTIME_DIR / "coverage_reconciliation_status.json"
BOOK_COVERAGE_PATH = RUNTIME_DIR / "book_coverage_health.json"
FUNDING_NAVIGATION_HEALTH_PATH = RUNTIME_DIR / "funding_navigation_health.json"
MAX_REFERENCE_ROWS = 200
MIN_EXACT_PAIR_RECALL_PCT = 95.0
MAX_RECALL_DROP_PP = 2.0
WARN_BOOK_COVERAGE_PCT = 90.0
CRITICAL_BOOK_COVERAGE_PCT = 85.0
SPREAD_DIFFERENCE_PP = 0.5

_VENUE_ALIASES = {
    "asterdex": "aster",
    "bingx": "bingx",
    "bitget": "bitget",
    "bitmart": "bitmart",
    "bybit": "bybit",
    "gate": "gate",
    "gateio": "gate",
    "hyperliquid": "hyperliquid",
    "kucoin": "kucoin",
    # UACryptoInvest labels this venue as Kucoin while SpreadBoard's adapter
    # historically used Kucoin Futures for the perpetual namespace.
    "kucoinfutures": "kucoin",
    "mexc": "mexc",
    "okx": "okx",
    "whitebit": "whitebit",
}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_json(path: Path | str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _slug(value: Any) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def normalize_venue(value: Any) -> str:
    slug = _slug(value)
    return _VENUE_ALIASES.get(slug, slug)


def normalize_market_type(value: Any) -> str:
    slug = _slug(value)
    if slug in {"future", "futures", "perpetual", "swap"}:
        return "Futures"
    if slug in {"spot"}:
        return "Spot"
    if slug in {"dex"}:
        return "Dex"
    return ""


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def validate_reference_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept only a small public-market reference envelope."""

    if str(payload.get("source") or "") != "uacryptoinvest.com":
        raise ValueError("invalid_reconciliation_source")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows or len(raw_rows) > MAX_REFERENCE_ROWS:
        raise ValueError("invalid_reconciliation_rows")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise TypeError("invalid_reconciliation_row")
        token = str(raw.get("token") or "").strip().upper()
        long_venue = normalize_venue(raw.get("long_venue"))
        short_venue = normalize_venue(raw.get("short_venue"))
        long_type = normalize_market_type(raw.get("long_market_type"))
        short_type = normalize_market_type(raw.get("short_market_type"))
        if (
            not token
            or len(token) > 64
            or not token.replace("-", "").replace("_", "").isalnum()
            or not long_venue
            or not short_venue
            or (long_type, short_type) not in {("Futures", "Futures"), ("Spot", "Futures")}
        ):
            raise ValueError("invalid_reconciliation_identity")
        identity = (token, long_venue, long_type, short_venue, short_type)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {
                "token": token,
                "long_venue": long_venue,
                "long_market_type": long_type,
                "short_venue": short_venue,
                "short_market_type": short_type,
                "reference_spread_pct": _number(raw.get("reference_spread_pct")),
                "source_rank": max(1, int(_number(raw.get("source_rank")) or len(rows) + 1)),
                "sample_bucket": (
                    str(raw.get("sample_bucket") or "tail")[:20]
                    if str(raw.get("sample_bucket") or "tail") in {"top", "tail"}
                    else "tail"
                ),
            }
        )
    if not rows:
        raise ValueError("empty_reconciliation_sample")
    observed_at = str(payload.get("observed_at") or "")[:64]
    return {
        "schema": "spreadboard.external_route_reference.v1",
        "source": "uacryptoinvest.com",
        "source_url": str(payload.get("source_url") or "https://uacryptoinvest.com")[:500],
        "observed_at": observed_at,
        "received_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "rows": rows,
    }


def _identity(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("token") or "").strip().upper(),
        normalize_venue(row.get("long_venue")),
        normalize_market_type(row.get("long_market_type")),
        normalize_venue(row.get("short_venue")),
        normalize_market_type(row.get("short_market_type")),
    )


def _usdt_symbol(value: Any) -> bool:
    symbol = str(value or "").upper()
    return "/USDT" in symbol or symbol.endswith("USDT") or "USDT:" in symbol


def _route_spread(row: dict[str, Any]) -> float | None:
    direct = _number(row.get("_reconciliation_spread_pct"))
    if direct is not None:
        return direct
    if api_spreads.matched_probe_verified(row):
        return _number(row.get("depth_weighted_spread_pct"))
    return _number(row.get("executable_spread_pct"))


def _catalog_index(payload: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (
            str(row.get("token") or "").strip().upper(),
            normalize_venue(row.get("venue")),
            normalize_market_type(row.get("market_type")),
        )
        for row in payload.get("markets") or []
        if isinstance(row, dict)
    }


def _route_books(route: dict[str, Any]) -> tuple[str, str]:
    return (
        live_book_cache.cache_key(
            str(route.get("long_venue") or ""),
            str(route.get("long_market_type") or ""),
            str(route.get("long_market_symbol") or ""),
        ),
        live_book_cache.cache_key(
            str(route.get("short_venue") or ""),
            str(route.get("short_market_type") or ""),
            str(route.get("short_market_symbol") or ""),
        ),
    )


def dex_monitor(routes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Separate OKX-DEX identity and matched-quote health from CEX recall."""

    dex_rows = [
        dict(row)
        for row in routes
        if str(row.get("route_kind") or "").upper()
        in {"DEX-FUTURES", "FUTURES-DEX"}
        and "okxdex56"
        in {
            normalize_venue(row.get("long_venue")),
            normalize_venue(row.get("short_venue")),
        }
    ]
    # The route index is the complete structural catalogue.  Current prices are
    # intentionally published as a compact, process-shared overlay every few
    # seconds so a 150 MB catalogue is not rewritten for every quote.  Reading
    # only the structural row here therefore made a healthy OKX DEX warmer look
    # stale until the next broad catalogue generation.  Apply the same live
    # overlay used by browser/API readers; timestamps remain the provider/book
    # timestamps and are rechecked below, so this cannot manufacture freshness.
    updates = api_spreads.live_route_updates_for(dex_rows, include_basis=True)
    for row in dex_rows:
        update = updates.get(str(row.get("route_key") or ""))
        if update is None or len(update) < 3 or update[0] is None:
            continue
        row["depth_weighted_spread_pct"] = update[0]
        row["quote_ts_us"] = update[2]
        if len(update) > 3:
            row["quote_basis"] = update[3]
    identity_rows = [
        row
        for row in dex_rows
        if str(row.get("dex_chain") or "").strip()
        and str(row.get("dex_contract") or "").strip()
    ]
    current_matched = [
        row
        for row in identity_rows
        if api_spreads.spread_quote_current(row)
        and api_spreads.matched_probe_verified(row)
    ]
    tokens = {str(row.get("token") or "").upper() for row in dex_rows if row.get("token")}
    failures: list[str] = []
    if not dex_rows:
        failures.append("okx_dex_structural_routes_empty")
    if dex_rows and not identity_rows:
        failures.append("okx_dex_identity_evidence_empty")
    if identity_rows and not current_matched:
        failures.append("okx_dex_current_matched_quotes_empty")
    return {
        "status": "ok" if not failures else "failed",
        "route_count": len(dex_rows),
        "token_count": len(tokens),
        "identity_route_count": len(identity_rows),
        "current_matched_route_count": len(current_matched),
        "target_notional_usd": api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD,
        "failures": failures,
    }


def reconcile(
    reference: dict[str, Any],
    *,
    routes: Iterable[dict[str, Any]],
    catalog: dict[str, Any] | None = None,
    fresh_books: dict[str, Any] | None = None,
    previous_status: dict[str, Any] | None = None,
    navigation_status: dict[str, Any] | None = None,
    book_coverage_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trace every external sample through catalogue, books and route output."""

    route_index: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for route in routes:
        if isinstance(route, dict):
            route_index[_identity(route)].append(route)
    catalog_pairs = _catalog_index(catalog or {})
    rows: list[dict[str, Any]] = []
    matched = current = mismatch_count = 0
    for sample in reference.get("rows") or []:
        identity = _identity(sample)
        token, long_venue, long_type, short_venue, short_type = identity
        candidates = [
            row
            for row in route_index.get(identity, [])
            if _usdt_symbol(row.get("long_market_symbol"))
            and _usdt_symbol(row.get("short_market_symbol"))
        ]
        long_catalogued = (token, long_venue, long_type) in catalog_pairs
        short_catalogued = (token, short_venue, short_type) in catalog_pairs
        if candidates:
            matched += 1
            candidate = max(
                candidates,
                key=lambda row: (
                    int(api_spreads.spread_quote_current(row)),
                    _route_spread(row) if _route_spread(row) is not None else -1e12,
                ),
            )
            long_key, short_key = _route_books(candidate)
            books = fresh_books or {}
            long_book = long_key in books
            short_book = short_key in books
            is_current = bool(
                long_book
                and short_book
                and api_spreads.spread_quote_current(candidate)
                and _route_spread(candidate) is not None
            )
            current += int(is_current)
            our_spread = _route_spread(candidate)
            reference_spread = _number(sample.get("reference_spread_pct"))
            delta = (
                our_spread - reference_spread
                if our_spread is not None and reference_spread is not None
                else None
            )
            investigate = bool(is_current and delta is not None and abs(delta) > SPREAD_DIFFERENCE_PP)
            mismatch_count += int(investigate)
            reason = "matched_current" if is_current else (
                "missing_both_fresh_books"
                if not long_book and not short_book
                else "missing_long_fresh_book"
                if not long_book
                else "missing_short_fresh_book"
                if not short_book
                else "route_quote_not_current"
            )
            rows.append(
                {
                    **sample,
                    "matched": True,
                    "current": is_current,
                    "reason_code": reason,
                    "route_key": str(candidate.get("route_key") or ""),
                    "long_market_symbol": str(candidate.get("long_market_symbol") or ""),
                    "short_market_symbol": str(candidate.get("short_market_symbol") or ""),
                    "spreadboard_spread_pct": our_spread,
                    "spread_difference_pp": delta,
                    "official_book_investigation_required": investigate,
                }
            )
            continue
        if not long_catalogued and not short_catalogued:
            reason = "missing_both_catalog_markets"
        elif not long_catalogued:
            reason = "missing_long_catalog_market"
        elif not short_catalogued:
            reason = "missing_short_catalog_market"
        else:
            reason = "route_not_generated"
        rows.append(
            {
                **sample,
                "matched": False,
                "current": False,
                "reason_code": reason,
                "route_key": None,
                "long_market_symbol": None,
                "short_market_symbol": None,
                "spreadboard_spread_pct": None,
                "spread_difference_pp": None,
                "official_book_investigation_required": False,
            }
        )

    total = len(rows)
    recall = matched / total * 100.0 if total else 0.0
    previous_recall = _number((previous_status or {}).get("exact_pair_recall_pct"))
    recall_drop = max(0.0, previous_recall - recall) if previous_recall is not None else 0.0
    unexplained = sum(1 for row in rows if not row.get("reason_code"))
    navigation = dict(navigation_status or {})
    book_health = dict(book_coverage_status or {})
    failures: list[str] = []
    if recall < MIN_EXACT_PAIR_RECALL_PCT:
        failures.append("exact_pair_recall_below_95_pct")
    if recall_drop > MAX_RECALL_DROP_PP:
        failures.append("exact_pair_recall_drop_above_2pp")
    if unexplained:
        failures.append("unexplained_absence")
    if not navigation.get("complete") or int(navigation.get("empty_view_count") or 0):
        failures.append("funding_navigation_incomplete_or_empty")
    if str(book_health.get("status") or "unknown") in {"warn", "critical"}:
        failures.append(f"book_coverage_{book_health.get('status')}")
    return {
        "schema": "spreadboard.coverage_reconciliation.v1",
        "source": reference.get("source"),
        "source_observed_at": reference.get("observed_at"),
        "checked_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "sample_count": total,
        "matched_pair_count": matched,
        "current_pair_count": current,
        "exact_pair_recall_pct": round(recall, 4),
        "previous_exact_pair_recall_pct": previous_recall,
        "recall_drop_pp": round(recall_drop, 4),
        "absence_count": total - matched,
        "unexplained_absence_count": unexplained,
        "spread_investigation_count": mismatch_count,
        "navigation": navigation,
        "book_coverage": book_health,
        "release_gate_passed": not failures,
        "failures": failures,
        "rows": rows,
    }


def run(
    reference_payload: dict[str, Any],
    *,
    routes: dict[str, dict[str, Any]],
    catalog: dict[str, Any] | None = None,
    reference_path: Path | str = REFERENCE_PATH,
    status_path: Path | str = STATUS_PATH,
    book_path: Path | str = BOOK_COVERAGE_PATH,
) -> dict[str, Any]:
    reference = validate_reference_payload(reference_payload)
    all_route_values = [row for row in routes.values() if isinstance(row, dict)]
    dex_status = dex_monitor(all_route_values)
    reference_identities = {_identity(row) for row in reference["rows"]}
    route_values = [
        dict(row)
        for row in routes.values()
        if isinstance(row, dict) and _identity(row) in reference_identities
    ]
    keys = [key for route in route_values for key in _route_books(route)]
    books = live_book_cache.load_live_books_by_keys(
        keys, max_age_seconds=api_spreads.LIVE_BOOK_MAX_AGE_SECONDS
    )
    updates = api_spreads.live_route_updates_for(route_values, include_basis=True)
    for route in route_values:
        update = updates.get(str(route.get("route_key") or ""))
        if update is None or len(update) < 3 or update[0] is None:
            continue
        route["_reconciliation_spread_pct"] = update[0]
        route["quote_ts_us"] = update[2]
        route["_reconciliation_quote_basis"] = update[3] if len(update) > 3 else None
    previous = load_json(status_path)
    status = reconcile(
        reference,
        routes=route_values,
        catalog=catalog if catalog is not None else chart_catalog.load(),
        fresh_books=books,
        previous_status=previous,
        navigation_status=funding_navigation.status(),
        book_coverage_status=load_json(book_path),
    )
    status["okx_dex_monitor"] = dex_status
    if dex_status["status"] != "ok":
        status["failures"] = list(status.get("failures") or []) + list(
            dex_status.get("failures") or []
        )
        status["release_gate_passed"] = False
    _atomic_json(Path(reference_path), reference)
    _atomic_json(Path(status_path), status)
    return status


def record_book_coverage(
    coverage: dict[str, Any],
    *,
    path: Path | str = BOOK_COVERAGE_PATH,
) -> dict[str, Any]:
    """Evaluate only completed route-index generations, never a partial pass."""

    target = Path(path)
    previous = load_json(target)
    value = _number(coverage.get("book_coverage_pct"))
    if value is None:
        return previous or {"status": "unknown", "completed_cycles": 0}
    consecutive = int(previous.get("consecutive_below_90") or 0)
    consecutive = consecutive + 1 if value < WARN_BOOK_COVERAGE_PCT else 0
    status = (
        "critical"
        if value < CRITICAL_BOOK_COVERAGE_PCT
        else "warn"
        if consecutive >= 2
        else "ok"
    )
    history = [
        item
        for item in previous.get("history") or []
        if isinstance(item, dict)
    ][-19:]
    history.append({"checked_at_unix": time.time(), "book_coverage_pct": value})
    result = {
        "schema": "spreadboard.book_coverage_health.v1",
        "status": status,
        "book_coverage_pct": round(value, 4),
        "catalog_market_count": int(coverage.get("catalog_market_count") or 0),
        "fresh_market_count": int(coverage.get("fresh_market_count") or 0),
        "missing_book_count": int(coverage.get("missing_book_count") or 0),
        "consecutive_below_90": consecutive,
        "completed_cycles": int(previous.get("completed_cycles") or 0) + 1,
        "updated_at_unix": time.time(),
        "history": history,
    }
    _atomic_json(target, result)
    return result


def record_funding_navigation_health(
    *,
    ok: bool,
    detail: str,
    path: Path | str = FUNDING_NAVIGATION_HEALTH_PATH,
) -> dict[str, Any]:
    """Publish collector outcome so the web process can notify owners."""

    result = {
        "schema": "spreadboard.funding_navigation_health.v1",
        "status": "ok" if ok else "failed",
        "detail": str(detail)[:500],
        "updated_at_unix": time.time(),
    }
    _atomic_json(Path(path), result)
    return result
