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

from spreadboard import api_spreads, materialized_views


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


def build(board_path: Path, output_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    initial = source_signature(board_path)
    rows, source_health = api_spreads.load_public_route_index()
    final = source_signature(board_path)
    if final != initial:
        raise RuntimeError("source_generation_changed_during_live_index_build")
    meta = materialized_views.Store(output_root).write_live_route_index(
        rows, source_signature=initial
    )
    return {
        "status": "ok",
        "routes": len(rows),
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
