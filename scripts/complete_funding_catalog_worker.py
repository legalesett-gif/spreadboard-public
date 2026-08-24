#!/usr/bin/env python3
"""Build and atomically persist the complete funding-pair catalogue."""

from __future__ import annotations

import json
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from spreadboard import funding_catalog


def build() -> dict[str, object]:
    started = time.monotonic()
    payloads = funding_catalog.refresh_cache()
    state = funding_catalog.status()
    path = Path(str(state.get("path") or funding_catalog.DEFAULT_CACHE_PATH))
    return {
        "status": "ok" if payloads and state.get("ready") else "failed",
        "tokens": len(payloads),
        "bytes": path.stat().st_size if path.exists() else 0,
        "seconds": round(time.monotonic() - started, 3),
        "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "persist_error": state.get("persist_error"),
    }


def main() -> int:
    try:
        summary = build()
    except Exception as exc:  # noqa: BLE001 - parent retains the prior generation.
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
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
