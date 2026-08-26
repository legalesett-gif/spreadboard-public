#!/usr/bin/env python3
"""Publish all exact-ranked Funding navigation lanes as one generation."""

from __future__ import annotations

import json
import platform
import resource
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
    bulk_quotes,
    funding_catalog,
    funding_navigation,
    funding_radar,
    materialized_views,
    server,
    venue_funding_history,
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
        "complete_catalog": _signature(funding_catalog.DEFAULT_CACHE_PATH),
        "current_funding": _signature(bulk_quotes.FUNDING_CACHE_PATH),
        "exact_settlements": _signature(venue_funding_history.DEFAULT_CACHE_PATH),
        "retained_radar": _signature(funding_radar.DEFAULT_CACHE_PATH),
    }


def build(board_path: Path, output_root: Path) -> dict[str, Any]:
    started = time.monotonic()
    restored = funding_catalog.restore_persisted_cache()
    if not restored.get("ready"):
        raise RuntimeError("complete_funding_catalog_unavailable")
    initial_signature = source_signature(board_path)
    pages = funding_catalog.build_navigation_pages(
        limit=funding_navigation.MAX_TOKENS,
        preview_limit=server.FUNDING_PAIR_PREVIEW_LIMIT,
    )
    if len(pages) != len(funding_navigation.QUERIES):
        raise RuntimeError(f"incomplete_funding_navigation:{len(pages)}")

    store = materialized_views.Store(output_root)
    writer = materialized_views.GenerationWriter(
        store,
        required_queries=funding_navigation.QUERIES,
        source_signature=initial_signature,
    )
    try:
        writer.write_route_index({})
        for query in funding_navigation.QUERIES:
            kind = str((query.get("kind") or [""])[0])
            window = str((query.get("funding_window") or ["now"])[0])
            complete = pages[(kind, window)]
            shell = server._funding_catalog_seed_payload(
                query, offset=0, limit=funding_navigation.MAX_TOKENS
            )
            payload = server._merge_complete_funding_page(
                shell, complete, limit=funding_navigation.MAX_TOKENS
            )
            payload["funding_navigation"] = {
                "generation_kind": "exact_ranked_background_projection",
                "built_at_unix": time.time(),
                "request_owned_exchange_work": False,
            }
            writer.write_view(query, payload)
        final_signature = source_signature(board_path)
        manifest = writer.publish()
    except Exception:
        writer.abort()
        raise
    max_rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Linux reports KiB; macOS reports bytes.  Keep local and production
    # diagnostics comparable without changing the worker's memory behaviour.
    max_rss_mb = max_rss / (1024.0 * 1024.0) if platform.system() == "Darwin" else max_rss / 1024.0
    return {
        "status": "ok",
        "generation": manifest["generation"],
        "views": len(manifest["views"]),
        # Every source file and its decoded object is atomic. A newer funding
        # handoff during this bounded build does not make the loaded snapshot
        # internally inconsistent; aborting here could starve publication
        # forever because live rates advance every 30 seconds. The next
        # five-minute generation will incorporate the newer handoff.
        "source_advanced_during_build": final_signature != initial_signature,
        "seconds": round(time.monotonic() - started, 3),
        "max_rss_mb": round(max_rss_mb, 1),
    }


def main() -> int:
    board_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "spreadboard.jsonl"
    try:
        result = build(board_path, funding_navigation.DEFAULT_ROOT)
    except Exception as exc:  # noqa: BLE001 - retain last complete generation.
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
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
