"""Watch Arbitrum One for confirmed stablecoin transfers to the receiving address.

Strictly read-only: this polls ``eth_getLogs`` and never signs or sends. It only
considers blocks buried under ``SPREADBOARD_CRYPTO_CONFIRMATIONS`` confirmations,
so a reorg cannot retroactively un-fund an activated member, and it persists its
cursor so a restart neither misses transfers nor double-counts them.

``rpc_call`` is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable
import urllib.request

from . import accounts, crypto_billing

LOGGER = logging.getLogger("spreadboard.crypto_watcher")

# Arbitrum produces blocks very fast; keep ranges modest so public RPCs do not
# reject the query outright.
MAX_BLOCK_SPAN = 2_000
FIRST_RUN_LOOKBACK = 10_000


def _default_rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        rpc_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode())
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"rpc_error: {body['error']}")
    return body.get("result") if isinstance(body, dict) else None


def _topic_address(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def _address_from_topic(topic: str) -> str:
    return "0x" + str(topic).removeprefix("0x")[-40:].lower()


def get_cursor(*, db_path=accounts.DEFAULT_DB_PATH) -> int:
    connection = accounts._connect(db_path)
    try:
        row = connection.execute(
            "SELECT last_scanned_block FROM crypto_watcher_state WHERE id = 1"
        ).fetchone()
        return int(row["last_scanned_block"]) if row else 0
    finally:
        connection.close()


def set_cursor(block_number: int, *, db_path=accounts.DEFAULT_DB_PATH) -> None:
    connection = accounts._connect(db_path)
    try:
        connection.execute(
            "INSERT INTO crypto_watcher_state (id, last_scanned_block, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_scanned_block = excluded.last_scanned_block, "
            "updated_at = excluded.updated_at",
            (int(block_number), accounts._utc_iso()),
        )
        connection.commit()
    finally:
        connection.close()


def scan_once(
    *,
    db_path=accounts.DEFAULT_DB_PATH,
    rpc_call: Callable[[str, str, list[Any]], Any] | None = None,
) -> dict[str, Any]:
    """Scan one confirmed block range. Returns a summary of what was applied."""
    settings = crypto_billing.config()
    if not settings.configured:
        return {"ok": False, "reason": "not_configured"}

    transport = rpc_call or _default_rpc_call

    def call(method: str, params: list[Any]) -> Any:
        return transport(settings.rpc_url, method, params)

    head = int(str(call("eth_blockNumber", [])), 16)
    safe_head = head - settings.confirmations
    if safe_head <= 0:
        return {"ok": True, "scanned": 0, "reason": "chain_too_short"}

    cursor = get_cursor(db_path=db_path)
    if cursor <= 0:
        # First run: do not replay the entire chain, and do not credit history
        # that predates the integration.
        cursor = max(0, safe_head - FIRST_RUN_LOOKBACK)
    if cursor >= safe_head:
        return {"ok": True, "scanned": 0, "results": [], "cursor": cursor}

    from_block = cursor + 1
    to_block = min(safe_head, from_block + MAX_BLOCK_SPAN - 1)

    logs = call(
        "eth_getLogs",
        [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": list(crypto_billing.TOKENS.keys()),
            "topics": [
                crypto_billing.TRANSFER_TOPIC,
                None,
                _topic_address(settings.receiving_address),
            ],
        }],
    ) or []

    results = []
    for entry in logs:
        try:
            topics = entry.get("topics") or []
            if len(topics) < 3:
                continue
            outcome = crypto_billing.record_transfer(
                token_address=str(entry.get("address")),
                raw_units=int(str(entry.get("data") or "0x0"), 16),
                tx_hash=str(entry.get("transactionHash")),
                log_index=int(str(entry.get("logIndex") or "0x0"), 16),
                from_address=_address_from_topic(topics[1]),
                block_number=int(str(entry.get("blockNumber") or "0x0"), 16),
                db_path=db_path,
            )
            results.append(outcome)
            if outcome.get("resolution") in {"unmatched", "ambiguous"}:
                LOGGER.warning(
                    "crypto payment needs review: %s %s",
                    entry.get("transactionHash"),
                    outcome.get("note"),
                )
        except Exception:  # noqa: BLE001 - one bad log must not stall the cursor
            LOGGER.exception("failed to apply transfer log %s", entry.get("transactionHash"))

    set_cursor(to_block, db_path=db_path)
    crypto_billing.expire_stale_invoices(db_path=db_path)
    return {
        "ok": True,
        "from_block": from_block,
        "to_block": to_block,
        "scanned": to_block - from_block + 1,
        "results": results,
        "cursor": to_block,
    }


def run_forever(*, db_path=accounts.DEFAULT_DB_PATH, stop: threading.Event | None = None) -> None:
    """Resident worker loop. Never raises; degrades to retrying on RPC failure."""
    settings = crypto_billing.config()
    if not settings.configured:
        LOGGER.info("crypto watcher idle: receiving address or RPC URL not configured")
        return
    LOGGER.info("crypto watcher started (%s confirmations)", settings.confirmations)
    while not (stop and stop.is_set()):
        try:
            scan_once(db_path=db_path)
        except Exception:  # noqa: BLE001 - a dead RPC must never kill the app
            LOGGER.exception("crypto watcher scan failed; will retry")
        if stop:
            stop.wait(settings.poll_seconds)
        else:
            time.sleep(settings.poll_seconds)


def start_background(*, db_path=accounts.DEFAULT_DB_PATH) -> threading.Event | None:
    settings = crypto_billing.config()
    if not settings.configured:
        return None
    stop = threading.Event()
    thread = threading.Thread(
        target=run_forever, kwargs={"db_path": db_path, "stop": stop},
        name="crypto-watcher", daemon=True,
    )
    thread.start()
    return stop
