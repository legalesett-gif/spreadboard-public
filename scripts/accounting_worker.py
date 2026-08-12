#!/usr/bin/env python3
"""Always-on, read-only subscriber funding and reference-mark worker.

The service has no HTTP listener and no order endpoints.  It receives the
accounting private key as a read-only file, decrypts only opted-in credential
rows in memory, reads venue ledgers/marks, and atomically publishes a sanitized
portfolio snapshot.  It never logs credentials or ciphertext.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any

from spreadboard import (
    accounts,
    chart_catalog,
    credential_crypto,
    exchange_credentials,
    position_markets,
)
from scripts import sync_portfolio_funding as sync


DATA_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "/app/runtime"))
DB_PATH = DATA_DIR / "spreadboard_accounts.sqlite3"
OUTPUT_PATH = DATA_DIR / "portfolio_funding.json"
STATUS_PATH = DATA_DIR / "accounting_worker_status.json"
LOCK_PATH = DATA_DIR / "accounting_worker.lock"


def _positions(user_id: int) -> list[dict[str, Any]]:
    catalogue = chart_catalog.load()
    result = []
    for position in accounts.list_positions(user_id, db_path=DB_PATH):
        resolved = position_markets.resolve_position_route(position, [], catalogue=catalogue)
        position["_resolved_legs"] = {
            side: resolved.get(f"{side}_leg") for side in ("long", "short")
        }
        result.append(position)
    return result


def run_once() -> dict[str, Any]:
    connections = accounts.encrypted_exchange_connections(db_path=DB_PATH)
    by_user: dict[int, dict[str, dict[str, str]]] = {}
    connection_rows: dict[tuple[int, str], dict[str, Any]] = {}
    decrypt_errors = 0
    for row in connections:
        user_id = int(row["user_id"])
        venue = exchange_credentials.normalize_venue(str(row["venue"]))
        connection_rows[(user_id, venue)] = row
        try:
            credentials = credential_crypto.decrypt(
                str(row["credential_encrypted"]),
                context=credential_crypto.context(user_id, venue),
            )
            credentials = exchange_credentials.clean_payload(
                venue,
                {
                    **credentials,
                    "read_only_confirmed": True,
                    "sensitive_signer_confirmed": True,
                },
            )
            by_user.setdefault(user_id, {})[venue] = {
                str(key): str(value) for key, value in credentials.items()
            }
        except Exception as exc:  # noqa: BLE001 - isolate one subscriber connection.
            decrypt_errors += 1
            accounts.record_exchange_connection_sync(
                user_id,
                venue,
                status="decrypt_error",
                error=type(exc).__name__,
                db_path=DB_PATH,
            )

    generated_at = sync.utc_iso()
    merged: dict[str, Any] = {}
    position_count = 0
    exact = 0
    for user_id, credential_map in by_user.items():
        positions = _positions(user_id)
        position_count += len(positions)
        exchanges: dict[str, Any] = {}
        used: set[str] = set()

        def exchange_for(venue_label: str) -> Any:
            slug = exchange_credentials.normalize_venue(venue_label)
            credentials = credential_map.get(slug)
            if credentials is None:
                raise RuntimeError("subscriber_connection_missing")
            if slug not in exchanges:
                exchanges[slug] = sync.build_exchange(venue_label, credentials)
            used.add(slug)
            return exchanges[slug]

        def fetcher(venue: str, symbol: str, since_ms: int) -> list[dict[str, Any]]:
            return sync.fetch_private_funding(exchange_for(venue), symbol, since_ms)

        def marker(
            position: dict[str, Any], side: str, leg: dict[str, Any]
        ) -> dict[str, Any]:
            market_type = str(position.get(f"{side}_market_type") or "").casefold()
            venue = str(position.get(f"{side}_venue") or "")
            if market_type == "dex" or " dex " in f" {venue.casefold()} ":
                return sync.fetch_dex_reference_mark(position, side, leg)
            return sync.fetch_cex_reference_mark(exchange_for(venue), position, side, leg)

        snapshot = sync.build_snapshot(
            positions,
            fetcher,
            generated_at=generated_at,
            mark_fetcher=marker,
        )
        merged.update(snapshot["positions"])
        exact += sum(item.get("status") == "ok" for item in snapshot["positions"].values())
        for venue in credential_map:
            status = "ok" if venue in used else "connected_not_used"
            accounts.record_exchange_connection_sync(
                user_id, venue, status=status, db_path=DB_PATH
            )

    snapshot = {
        "schema": "spreadboard.portfolio_funding.v2",
        "started_at": generated_at,
        "generated_at": sync.utc_iso(),
        "read_only": True,
        "worker": "server_encrypted_opt_in",
        "positions": merged,
    }
    sync.write_atomic(OUTPUT_PATH, snapshot)
    status = {
        "ok": decrypt_errors == 0,
        "read_only": True,
        "generated_at": snapshot["generated_at"],
        "connections": len(connections),
        "users": len(by_user),
        "positions": position_count,
        "exact": exact,
        "errors": position_count - exact + decrypt_errors,
    }
    sync.write_atomic(STATUS_PATH, status)
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                result = run_once()
                print(json.dumps(result, sort_keys=True), flush=True)
            except Exception as exc:  # noqa: BLE001 - the supervisor keeps retrying.
                safe = {
                    "ok": False,
                    "read_only": True,
                    "generated_at": sync.utc_iso(),
                    "error": type(exc).__name__,
                }
                sync.write_atomic(STATUS_PATH, safe)
                print(json.dumps(safe, sort_keys=True), flush=True)
            if args.once:
                break
            time.sleep(max(60, int(args.interval)))


if __name__ == "__main__":
    main()
