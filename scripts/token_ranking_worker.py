#!/usr/bin/env python3
"""Build the compact token-ranking artifact in a process that exits."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from spreadboard import board, token_rankings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--board-path",
        type=Path,
        default=Path(os.environ.get("SPREADBOARD_BOARD_PATH", str(board.DEFAULT_BOARD_PATH))),
    )
    parser.add_argument("--output-path", type=Path, default=token_rankings.DEFAULT_PATH)
    args = parser.parse_args()
    payload = token_rankings.build(board_path=args.board_path, output_path=args.output_path)
    print(
        json.dumps(
            {
                "status": "ok",
                "tokens": payload.get("token_count", 0),
                "live": payload.get("live_token_count", 0),
                "cooled": payload.get("cooled_token_count", 0),
                "generated_at": payload.get("generated_at"),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
