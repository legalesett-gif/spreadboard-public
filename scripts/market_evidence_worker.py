#!/usr/bin/env python3
"""Publish slow funding/radar evidence in a process whose memory is reclaimed."""

from __future__ import annotations

import json
import os

from scripts import run_spreadboard_service as service


def main() -> int:
    service._refresh_funding_windows()
    print(
        json.dumps(
            {
                "status": "ok",
                "artifact": "market_evidence",
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    # The point of this worker is to return every parsed cache arena to Linux.
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
