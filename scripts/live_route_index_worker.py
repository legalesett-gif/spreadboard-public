#!/usr/bin/env python3
"""Publish the current complete route index without rendering navigation views."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from spreadboard import (
    api_spreads,
    catalog_pairs,
    chart_catalog,
    coverage_reconciliation,
    materialized_views,
)


def _signature(path: Path | str) -> list[int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_size]


def source_signature(board_path: Path) -> dict[str, Any]:
    return {
        "board_path": str(board_path.resolve()),
        "board": _signature(board_path),
        "discovery": _signature(api_spreads.DEFAULT_API_DISCOVERY_PATH),
        "chart_catalog": _signature(
            api_spreads.RUNTIME_DIR / "chart_market_catalog.json"
        ),
        "metadata": _signature(api_spreads.token_metadata.DEFAULT_CACHE_PATH),
        "rails": _signature(api_spreads.public_rails.DEFAULT_CACHE_PATH),
    }


def _retained_structural_cex_rows(
    rows: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Keep prior CEX pair identities while both exact legs remain listed.

    This artifact is a structural lookup index, not a current-price claim.
    Expiring identities by their old quote timestamp made a complete index
    collapse whenever a venue sweep and the isolated build crossed the
    90-second presentation boundary. Foreground projections always replace
    the stored economics from current books and independently enforce the
    strict quote-age gate, so keeping a still-listed pair cannot make it live.
    Genuine removals are driven by the exact chart-market catalogue instead.
    """

    markets = {
        (
            str(item.get("venue") or ""),
            str(item.get("market_type") or ""),
            str(item.get("symbol") or ""),
        )
        for item in (chart_catalog.load().get("markets") or [])
        if isinstance(item, dict)
        and str(item.get("market_type") or "") in {"Spot", "Futures"}
    }
    retained: dict[str, dict[str, Any]] = {}
    for key, row in rows.items():
        if str(row.get("route_kind") or "").startswith("DEX-"):
            continue
        identities = {
            (
                str(row.get(f"{side}_venue") or ""),
                str(row.get(f"{side}_market_type") or ""),
                str(row.get(f"{side}_market_symbol") or ""),
            )
            for side in ("long", "short")
        }
        if len(identities) == 2 and identities.issubset(markets):
            retained[key] = row
    return retained


def _retained_structural_dex_rows(
    rows: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Bridge transient OKX provider gaps inside one structural generation.

    DEX quote freshness is still enforced from the exact provider timestamp.
    Retaining the chain/contract route identity merely lets the next fast
    quote revive it without waiting for the broad discovery generation.
    A changed discovery/catalogue signature starts from the newly discovered
    DEX set and therefore removes contracts which really left the universe.
    """

    return {
        key: row
        for key, row in rows.items()
        if str(row.get("route_kind") or "").startswith("DEX-")
    }


def _current_dex_rows(
    previous_rows: dict[str, dict[str, Any]],
    *,
    metadata: dict[str, dict[str, Any]],
    now: float,
) -> list[dict[str, Any]]:
    """Rebuild current provider routes without parsing the broad discovery file."""

    rails = api_spreads.public_rails.load_public_rails()
    books = api_spreads._live_books()
    previous_seeds = [
        dict(row)
        for row in previous_rows.values()
        if str(row.get("route_kind") or "").startswith("DEX-")
        and api_spreads.spread_quote_current(row, now=now)
    ]
    delta_rows = api_spreads._apply_fast_quote_delta(
        [],
        api_spreads._fast_quote_delta_path(
            api_spreads.DEFAULT_API_DISCOVERY_PATH
        ),
        now=now,
        metadata=metadata,
        rails=rails,
    )
    delta_seeds: list[dict[str, Any]] = []
    if delta_rows:
        delta_rows = api_spreads.apply_live_books(delta_rows, books, now=now)
        delta_seeds = [api_spreads._public_row(row) for row in delta_rows]
    # The fast delta owns current provider economics. Prior rows contribute
    # only routes the bounded delta did not select in this cycle.
    seeds = [*delta_seeds, *previous_seeds]
    seeds = [
        row
        for row in catalog_pairs.with_routes({"routes": []}, seeds, limit=None).get(
            "routes", []
        )
        if isinstance(row, dict)
        and str(row.get("route_kind") or "").startswith("DEX-")
        and api_spreads.spread_quote_current(row, now=now)
    ]
    expanded = catalog_pairs.dex_futures_routes(
        seeds,
        books=books,
        include_history=False,
    )
    merged = catalog_pairs.with_routes(
        {"routes": expanded},
        seeds,
        limit=None,
    )
    return [
        row
        for row in merged.get("routes") or []
        if isinstance(row, dict)
        and not api_spreads.quote_basis_mismatch(row)
        and api_spreads.spread_evidence_state(row, now=now)
        in {"verified", "research"}
    ]


def _current_generation_rows(
    previous_rows: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Build live structural pairs from one complete resident-book cut.

    The complete quote pass and this isolated build can together exceed the
    90-second presentation boundary.  The index is therefore allowed to use
    the catalogue's bounded 180-second structural window; every web/Telegram
    projection still overlays the latest books and enforces the unchanged
    90-second current-quote gate. Reverse pair directions are stored beside an
    admitted direction so a genuine basis flip appears on the next live
    overlay rather than waiting for another whole-catalogue build.
    """

    now = time.time()
    metadata = api_spreads.token_metadata.load_token_metadata()
    cex_rows, catalogue_meta = api_spreads._complete_current_catalogue_rows(
        [],
        metadata=metadata,
        max_age_seconds=catalog_pairs.MAX_BOOK_AGE_SECONDS,
    )
    previous_by_identity = {
        catalog_pairs.route_identity(row): row
        for row in previous_rows.values()
        if not str(row.get("route_kind") or "").startswith("DEX-")
    }
    for row in cex_rows:
        prior = previous_by_identity.get(catalog_pairs.route_identity(row))
        if prior is not None:
            catalog_pairs._merge_conservative_route_evidence(row, prior)

    dex_rows = _current_dex_rows(previous_rows, metadata=metadata, now=now)
    merged = catalog_pairs.with_routes(
        {"routes": cex_rows},
        dex_rows,
        limit=None,
    )
    rows = {
        str(row.get("route_key") or ""): row
        for row in merged.get("routes") or []
        if isinstance(row, dict) and str(row.get("route_key") or "")
    }
    return rows, {
        "status": "complete_live_structural_catalogue_generation",
        "updated_at": None,
        "complete_catalogue": catalogue_meta,
        "current_cex_routes": len(cex_rows),
        "current_dex_routes": len(dex_rows),
    }


def _merge_by_economic_identity(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Let a current leg pair replace any retained serialization of it.

    OKX provider rows and catalogue fan-out rows intentionally use different
    route keys. Retaining by key alone kept the old scanner serialization next
    to the fresh chain/contract-identical route, so a direct lookup could still
    find the stale twin. Structural continuity is about economic legs, not the
    spelling of their key.
    """

    selected: dict[tuple[Any, ...], tuple[int, int, str, dict[str, Any]]] = {}
    for priority, rows in enumerate((previous, current)):
        for key, row in rows.items():
            identity = catalog_pairs.route_identity(row)
            try:
                quote_ts_us = int(row.get("quote_ts_us") or 0)
            except (TypeError, ValueError):
                quote_ts_us = 0
            existing = selected.get(identity)
            candidate = (priority, quote_ts_us, key, row)
            if existing is None or candidate[:2] >= existing[:2]:
                selected[identity] = candidate
    return {key: row for _priority, _stamp, key, row in selected.values()}


def _public_product_rows(
    rows: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Drop retired pair permutations before they consume shared memory.

    Spot instruments themselves remain in the chart catalogue and book store.
    This boundary only controls which cross-market permutations are serialized
    into the large resident route index.
    """

    return {
        key: row
        for key, row in rows.items()
        if str(row.get("route_kind") or "").upper()
        not in api_spreads.RETIRED_ROUTE_KINDS
    }


def build(board_path: Path, output_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    initial = source_signature(board_path)
    store = materialized_views.Store(output_root)
    previous_meta = store.live_route_index_status()
    previous_rows: dict[str, dict[str, Any]] = {}
    if previous_meta.get("ready"):
        previous_rows = store.live_route_index(board_path=board_path) or {}
    same_structural_generation = bool(
        previous_rows and previous_meta.get("source_signature") == initial
    )
    if same_structural_generation:
        rows, source_health = _current_generation_rows(previous_rows)
        build_mode = "complete_live_structural_catalogue"
    else:
        rows, source_health = api_spreads.load_public_route_index()
        build_mode = "full_discovery_catalogue"
    final = source_signature(board_path)
    if final != initial:
        raise RuntimeError("source_generation_changed_during_live_index_build")
    # Retired permutations should not contribute to current/retained metrics or
    # spend time in the continuity merge. The spot instruments remain in the
    # catalogue; only their standalone pair products leave the route index.
    rows = _public_product_rows(rows)
    current_route_count = len(rows)
    if previous_rows:
        # Bulk venues finish at different moments and the isolated build takes
        # tens of seconds. A thin timing slice must update current rows, never
        # erase thousands of structurally valid lookups from the same
        # discovery/catalogue generation. Foreground rendering independently
        # rechecks the real quote timestamp, so retaining the lookup cannot
        # turn an old price into a current opportunity. Catalogue membership,
        # rather than quote age, permits genuine CEX removals. DEX identities
        # bridge only an unchanged structural generation.
        retained = _public_product_rows(
            _retained_structural_cex_rows(previous_rows)
        )
        if same_structural_generation:
            retained.update(
                _public_product_rows(_retained_structural_dex_rows(previous_rows))
            )
        rows = _merge_by_economic_identity(retained, rows)
    coverage = source_health.get("complete_catalogue") or source_health
    coverage = {
        key: coverage.get(key)
        for key in (
            "catalog_market_count",
            "fresh_market_count",
            "missing_book_count",
            "book_coverage_pct",
            "catalogue_route_count",
            "merged_route_count",
            "catalogue_token_count",
            "catalogue_kind_counts",
            "book_max_age_seconds",
        )
        if isinstance(coverage, dict) and coverage.get(key) is not None
    }
    meta = store.write_live_route_index(
        rows,
        source_signature=initial,
        coverage=coverage,
    )
    book_health = coverage_reconciliation.record_book_coverage(coverage)
    return {
        "status": "ok",
        "routes": len(rows),
        "current_routes": current_route_count,
        "retained_routes": max(0, len(rows) - current_route_count),
        "build_mode": build_mode,
        "seconds": round(time.monotonic() - started, 3),
        "source_updated_at": source_health.get("updated_at"),
        "coverage": coverage,
        "book_coverage_health": book_health,
        "artifact": meta.get("file"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-path", type=Path, required=True)
    parser.add_argument(
        "--output-root", type=Path, default=materialized_views.DEFAULT_ROOT
    )
    args = parser.parse_args()
    try:
        summary = build(args.board_path, args.output_root)
    except Exception as exc:  # noqa: BLE001 - parent retains the last complete index.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": type(exc).__name__,
                    "detail": str(exc)[:300],
                }
            ),
            flush=True,
        )
        return 1
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
