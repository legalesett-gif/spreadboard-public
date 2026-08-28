#!/usr/bin/env python3
"""Fail a deployment when persisted production coverage gates are degraded."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from spreadboard import coverage_reconciliation, funding_navigation


def evaluate() -> dict[str, object]:
    reconciliation = coverage_reconciliation.load_json(
        coverage_reconciliation.STATUS_PATH
    )
    books = coverage_reconciliation.load_json(
        coverage_reconciliation.BOOK_COVERAGE_PATH
    )
    navigation = funding_navigation.status()
    failures: list[str] = []
    if not reconciliation:
        failures.append("coverage_reconciliation_not_run")
    elif not reconciliation.get("release_gate_passed"):
        failures.extend(str(item) for item in reconciliation.get("failures") or [])
    if not books:
        failures.append("book_coverage_not_run")
    elif str(books.get("status") or "unknown") in {"warn", "critical"}:
        failures.append(f"book_coverage_{books.get('status')}")
    if not navigation.get("complete"):
        failures.append("funding_navigation_incomplete_or_empty")
    return {
        "ok": not failures,
        "failures": sorted(set(failures)),
        "exact_pair_recall_pct": reconciliation.get("exact_pair_recall_pct"),
        "book_coverage_pct": books.get("book_coverage_pct"),
        "funding_navigation": {
            "complete": navigation.get("complete"),
            "view_count": navigation.get("view_count"),
            "empty_view_count": navigation.get("empty_view_count"),
            "navigation_route_count": navigation.get("navigation_route_count"),
        },
    }


def main() -> int:
    result = evaluate()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
