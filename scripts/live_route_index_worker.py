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

from spreadboard import api_spreads, catalog_pairs, chart_catalog, materialized_views


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
    """Keep only prior CEX routes whose exact legs remain in the catalogue."""

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


def build(board_path: Path, output_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    initial = source_signature(board_path)
    store = materialized_views.Store(output_root)
    previous_meta = store.live_route_index_status()
    previous_rows: dict[str, dict[str, Any]] = {}
    if previous_meta.get("ready"):
        previous_rows = store.live_route_index(board_path=board_path) or {}
        if previous_meta.get("source_signature") != initial:
            # A changed discovery/catalogue generation may legitimately remove
            # markets. Carry forward only exact CEX legs that the new catalogue
            # still contains; DEX/provider rows must be rediscovered in the new
            # source itself. This preserves structural coverage without making
            # the index an append-only graveyard.
            previous_rows = _retained_structural_cex_rows(previous_rows)
    rows, source_health = api_spreads.load_public_route_index()
    final = source_signature(board_path)
    if final != initial:
        raise RuntimeError("source_generation_changed_during_live_index_build")
    current_route_count = len(rows)
    if previous_rows:
        # Bulk venues finish at different moments and the isolated build takes
        # tens of seconds. A thin timing slice must update current rows, never
        # erase thousands of structurally valid lookups from the same
        # discovery/catalogue generation. Foreground rendering independently
        # rechecks the real quote timestamp, so retaining the lookup cannot
        # turn an old price into a current opportunity. A changed structural
        # source signature starts clean and permits genuine removals.
        rows = _merge_by_economic_identity(previous_rows, rows)
    meta = store.write_live_route_index(
        rows, source_signature=initial
    )
    return {
        "status": "ok",
        "routes": len(rows),
        "current_routes": current_route_count,
        "retained_routes": max(0, len(rows) - current_route_count),
        "seconds": round(time.monotonic() - started, 3),
        "source_updated_at": source_health.get("updated_at"),
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
