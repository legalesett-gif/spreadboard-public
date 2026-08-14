#!/usr/bin/env python3
"""Build the compact token-ranking artifact in a process that exits."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from spreadboard import (  # noqa: E402
    accounts,
    api_spreads,
    board,
    market_history,
    research_calibration,
    token_rankings,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--board-path",
        type=Path,
        default=Path(os.environ.get("SPREADBOARD_BOARD_PATH", str(board.DEFAULT_BOARD_PATH))),
    )
    parser.add_argument("--output-path", type=Path, default=token_rankings.DEFAULT_PATH)
    args = parser.parse_args()
    market = api_spreads.load_spreads(
        board_path=args.board_path,
        include_stale=False,
        include_unverified=False,
        require_deliverable=True,
        sort_by="edge",
        direction="desc",
        limit=None,
    )
    payload = token_rankings.build(
        board_path=args.board_path,
        output_path=args.output_path,
        market_payload=market,
    )
    routes = [
        route
        for group in market.get("groups") or []
        if isinstance(group, dict)
        for route in group.get("routes") or []
        if isinstance(route, dict)
    ]
    try:
        account_evidence = accounts.anonymized_research_evidence()
        account_evidence_status = "available"
    except (OSError, ValueError, sqlite3.Error) as exc:
        account_evidence = {}
        account_evidence_status = f"unavailable:{type(exc).__name__}"
    followup_keys = research_calibration.shadow_followup_route_keys(routes)
    followup_history = market_history.record_research_routes_hourly(
        routes,
        route_keys=followup_keys,
    )
    calibration_capture = research_calibration.capture_routes(
        routes, account_evidence=account_evidence
    )
    calibration_labels = research_calibration.label_matured()
    print(
        json.dumps(
            {
                "status": "ok",
                "tokens": payload.get("token_count", 0),
                "live": payload.get("live_token_count", 0),
                "cooled": payload.get("cooled_token_count", 0),
                "generated_at": payload.get("generated_at"),
                "calibration_captured": calibration_capture["inserted"],
                "calibration_cost_evidenced": calibration_capture["cost_evidenced"],
                "calibration_transfer_evidenced": calibration_capture["transfer_evidenced"],
                "account_evidence_status": account_evidence_status,
                "research_history_wanted": followup_history["wanted"],
                "research_history_fresh": followup_history["fresh_candidates"],
                "research_history_inserted": followup_history["inserted"],
                "calibration_attempted": calibration_labels["attempted"],
                "calibration_labeled": calibration_labels["labeled"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
