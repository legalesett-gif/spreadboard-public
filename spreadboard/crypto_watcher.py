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
from datetime import UTC, datetime
from typing import Any, Callable
import urllib.request
from urllib.error import HTTPError

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


#: Alchemy's free tier caps eth_getLogs at a ten-block range, and Arbitrum makes
#: roughly 14,400 blocks an hour -- so a range scan could never keep up with a
#: one-hour invoice window, and every scan failed outright with a 400. Their
#: asset-transfers endpoint answers the same question ("what reached this
#: address") with no range limit, so it is preferred when available and
#: eth_getLogs remains the fallback for any other RPC.
def _transfers_to_us(
    call: Callable[[str, list[Any]], Any],
    settings: Any,
    from_block: int,
    to_block: int,
) -> list[dict[str, Any]]:
    tokens = list(crypto_billing.TOKENS.keys())
    if "alchemy" in str(getattr(settings, "rpc_url", "")).casefold():
        try:
            payload = call(
                "alchemy_getAssetTransfers",
                [{
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "toAddress": settings.receiving_address,
                    "contractAddresses": tokens,
                    "category": ["erc20"],
                    "excludeZeroValue": True,
                    "maxCount": "0x3e8",
                }],
            ) or {}
            return [
                {
                    "address": (item.get("rawContract") or {}).get("address"),
                    "data": (item.get("rawContract") or {}).get("value"),
                    "transactionHash": item.get("hash"),
                    "logIndex": "0x0",
                    "blockNumber": item.get("blockNum"),
                    "topics": [
                        crypto_billing.TRANSFER_TOPIC,
                        _topic_address(str(item.get("from") or "0x" + "0" * 40)),
                        _topic_address(settings.receiving_address),
                    ],
                }
                for item in (payload.get("transfers") or [])
            ]
        except Exception:  # noqa: BLE001 - fall back rather than stall the cursor
            LOGGER.warning("asset-transfers unavailable; falling back to eth_getLogs")
    return call(
        "eth_getLogs",
        [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": tokens,
            "topics": [
                crypto_billing.TRANSFER_TOPIC,
                None,
                _topic_address(settings.receiving_address),
            ],
        }],
    ) or []


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

    logs = _transfers_to_us(call, settings, from_block, to_block)

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
        except Exception:
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
    LOGGER.info(
        "crypto watcher started: %s",
        ", ".join(f"{c.name} ({c.confirmations} conf)" for c in watchable_chains())
        or "no chain configured",
    )
    while not (stop and stop.is_set()):
        try:
            scan_all(db_path=db_path)
        except Exception:
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


# ---------------------------------------------------------------------------
# Multi-chain watching
# ---------------------------------------------------------------------------

# Public endpoints so a chain works the moment its address is configured. Both
# are overridable for a paid provider with better rate limits.
DEFAULT_RPC_URLS = {
    "bsc": "https://bsc-dataseed.binance.org",
    "tron": "https://api.trongrid.io",
}
TRON_FIRST_RUN_LOOKBACK_MS = 6 * 60 * 60 * 1000


def chain_rpc_url(chain: str) -> str:
    definition = crypto_billing.CHAINS.get(str(chain))
    if definition is None:
        return ""
    import os

    return (
        os.environ.get(definition.rpc_env, "").strip()
        or DEFAULT_RPC_URLS.get(str(chain), "")
    )


def watchable_chains() -> list[Any]:
    """Chains with both a valid receiving address and somewhere to read from.

    A chain missing either cannot detect a payment, and a chain that cannot
    detect a payment must never be offered: it would take the money and grant
    nothing.
    """
    return [
        definition
        for definition in crypto_billing.enabled_chains()
        if chain_rpc_url(definition.key)
    ]


def _chain_transfers_to_us(
    call: Callable[[str, list[Any]], Any],
    definition: Any,
    address: str,
    rpc_url: str,
    from_block: int,
    to_block: int,
) -> list[dict[str, Any]]:
    """Confirmed stablecoin transfers to us, by whichever route the node serves.

    Every free BSC endpoint refuses a filtered ``eth_getLogs`` outright -- not
    merely over a wide range, but at any range -- so a log scan is not a
    portable way to read an EVM chain. Alchemy's asset-transfers index is asked
    first wherever it is available, exactly as the Arbitrum scanner does, and
    ``eth_getLogs`` remains the fallback for nodes that do serve it.
    """
    tokens = list(definition.tokens.keys())
    if "alchemy" in str(rpc_url).casefold():
        try:
            payload = call(
                "alchemy_getAssetTransfers",
                [{
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "toAddress": address,
                    "contractAddresses": tokens,
                    "category": ["erc20"],
                    "excludeZeroValue": True,
                    "maxCount": "0x3e8",
                }],
            ) or {}
            return [
                {
                    "address": (item.get("rawContract") or {}).get("address"),
                    "data": (item.get("rawContract") or {}).get("value"),
                    "transactionHash": item.get("hash"),
                    "logIndex": "0x0",
                    "blockNumber": item.get("blockNum"),
                    "topics": [
                        crypto_billing.TRANSFER_TOPIC,
                        _topic_address(str(item.get("from") or "0x" + "0" * 40)),
                        _topic_address(address),
                    ],
                }
                for item in (payload.get("transfers") or [])
            ]
        except Exception:  # noqa: BLE001 - fall back rather than stall the cursor
            LOGGER.warning(
                "%s asset-transfers unavailable; falling back to eth_getLogs",
                definition.key,
            )
    return call(
        "eth_getLogs",
        [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": tokens,
            "topics": [crypto_billing.TRANSFER_TOPIC, None, _topic_address(address)],
        }],
    ) or []


def scan_evm(
    chain: str,
    *,
    db_path=accounts.DEFAULT_DB_PATH,
    rpc_call: Callable[[str, str, list[Any]], Any] | None = None,
) -> dict[str, Any]:
    """Scan one confirmed block range on any EVM chain."""
    definition = crypto_billing.CHAINS.get(str(chain))
    if definition is None or definition.kind != "evm":
        return {"ok": False, "reason": "not_an_evm_chain"}
    address = crypto_billing.chain_address(definition.key)
    rpc_url = chain_rpc_url(definition.key)
    if not address or not rpc_url:
        return {"ok": False, "reason": "not_configured"}

    transport = rpc_call or _default_rpc_call

    def call(method: str, params: list[Any]) -> Any:
        return transport(rpc_url, method, params)

    head = int(str(call("eth_blockNumber", [])), 16)
    safe_head = head - definition.confirmations
    if safe_head <= 0:
        return {"ok": True, "scanned": 0, "reason": "chain_too_short"}

    cursor = accounts.chain_cursor(definition.key, db_path=db_path)
    if cursor <= 0:
        cursor = max(0, safe_head - FIRST_RUN_LOOKBACK)
    if cursor >= safe_head:
        return {"ok": True, "scanned": 0, "results": [], "cursor": cursor}

    from_block = cursor + 1
    to_block = min(safe_head, from_block + MAX_BLOCK_SPAN - 1)
    logs = _chain_transfers_to_us(call, definition, address, rpc_url, from_block, to_block)

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
                chain=definition.key,
                db_path=db_path,
            )
            results.append(outcome)
            if outcome.get("resolution") in {"unmatched", "ambiguous"}:
                LOGGER.warning(
                    "%s payment needs review: %s %s",
                    definition.key, entry.get("transactionHash"), outcome.get("note"),
                )
        except Exception:
            LOGGER.exception("failed to apply %s log", definition.key)

    accounts.set_chain_cursor(definition.key, to_block, db_path=db_path)
    return {
        "ok": True, "chain": definition.key, "from_block": from_block,
        "to_block": to_block, "scanned": to_block - from_block + 1,
        "results": results, "cursor": to_block,
    }


TRON_THROTTLE_RETRIES = 3
TRON_THROTTLE_BACKOFF_SECONDS = 4.0


def _default_http_get(url: str) -> Any:
    """Read TronGrid, riding out a throttle rather than reporting a failure.

    Without an API key TronGrid rate-limits by IP, and a throttle is a "come
    back shortly", not a broken chain. Left unhandled, three transient 429s in
    a row would trip the health gate and withdraw Tron from checkout for
    everyone -- a self-inflicted outage. Retries stay bounded so a scan cannot
    hang the watcher loop.
    """
    import os

    key = os.environ.get("SPREADBOARD_CRYPTO_TRON_API_KEY", "").strip()
    last: Exception | None = None
    for attempt in range(TRON_THROTTLE_RETRIES):
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if key:
            request.add_header("TRON-PRO-API-KEY", key)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last = exc
            if exc.code != 429:
                raise
            LOGGER.warning(
                "tron throttled (429); backing off %.0fs",
                TRON_THROTTLE_BACKOFF_SECONDS * (attempt + 1),
            )
            time.sleep(TRON_THROTTLE_BACKOFF_SECONDS * (attempt + 1))
    raise last if last is not None else RuntimeError("tron_request_failed")


def scan_tron(
    *,
    db_path=accounts.DEFAULT_DB_PATH,
    http_get: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Poll TronGrid for confirmed TRC20 transfers to the receiving address.

    Tron has no log-scanning equivalent, so this reads the account's own
    confirmed TRC20 history and advances a millisecond timestamp cursor.
    ``only_confirmed`` is what keeps a reorg from crediting anyone.
    """
    definition = crypto_billing.CHAINS["tron"]
    address = crypto_billing.chain_address("tron")
    base = chain_rpc_url("tron")
    if not address or not base:
        return {"ok": False, "reason": "not_configured"}

    stored = accounts.chain_cursor("tron", db_path=db_path)
    cursor = stored
    if cursor <= 0:
        cursor = max(0, int(time.time() * 1000) - TRON_FIRST_RUN_LOOKBACK_MS)

    url = (
        f"{base.rstrip('/')}/v1/accounts/{address}/transactions/trc20"
        f"?only_confirmed=true&only_to=true&limit=50&order_by=block_timestamp,asc"
        f"&min_timestamp={cursor + 1}"
    )
    payload = (http_get or _default_http_get)(url) or {}
    entries = payload.get("data") or []

    results, newest = [], cursor
    for entry in entries:
        try:
            stamp = int(entry.get("block_timestamp") or 0)
            newest = max(newest, stamp)
            if str(entry.get("to") or "") != address:
                continue
            token_info = entry.get("token_info") or {}
            outcome = crypto_billing.record_transfer(
                token_address=str(token_info.get("address") or ""),
                raw_units=int(str(entry.get("value") or "0")),
                tx_hash=str(entry.get("transaction_id") or ""),
                log_index=0,
                from_address=str(entry.get("from") or ""),
                # Tron reports a timestamp rather than a height here; the
                # cursor is the timestamp, so store it for provenance.
                block_number=stamp,
                chain="tron",
                db_path=db_path,
            )
            results.append(outcome)
            if outcome.get("resolution") in {"unmatched", "ambiguous"}:
                LOGGER.warning(
                    "tron payment needs review: %s %s",
                    entry.get("transaction_id"), outcome.get("note"),
                )
        except Exception:
            LOGGER.exception("failed to apply tron transfer")

    # Persist even when the window was empty. Comparing only against the
    # in-memory cursor meant a chain with no traffic never wrote one, and so
    # re-read the same six-hour window on every single poll, forever.
    if newest > stored:
        accounts.set_chain_cursor("tron", newest, db_path=db_path)
    return {
        "ok": True, "chain": "tron", "scanned": len(entries),
        "results": results, "cursor": newest,
        "confirmations": definition.confirmations,
    }


SCAN_STALE_SECONDS = 15 * 60
ALERT_AFTER_FAILURES = 3


def chain_is_healthy(chain: str, *, db_path=accounts.DEFAULT_DB_PATH) -> bool:
    """Has this chain been read successfully recently enough to trust?

    Never scanned is not the same as scanning fine, so an unproven chain is
    unhealthy rather than assumed good.
    """
    health = accounts.chain_health(chain, db_path=db_path)
    stamp = health.get("last_ok_at")
    if not stamp:
        return False
    try:
        last = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (datetime.now(tz=UTC) - last).total_seconds() <= SCAN_STALE_SECONDS


def payable_chains(*, db_path=accounts.DEFAULT_DB_PATH) -> list[Any]:
    """Networks a customer may safely be offered.

    Configured and reachable is not enough. BSC was both while every scan was
    failing, so checkout kept taking payments nobody could see. A network is
    only offered while its watcher is demonstrably working.
    """
    return [
        definition
        for definition in watchable_chains()
        if chain_is_healthy(definition.key, db_path=db_path)
    ]


def _alert_operator(message: str, *, db_path=accounts.DEFAULT_DB_PATH) -> None:
    """Tell a human. A silent outage is the whole problem being solved here."""
    LOGGER.error("%s", message)
    try:
        from spreadboard import telegram_bot

        for candidate in accounts.telegram_membership_candidates(db_path=db_path):
            user = accounts.get_user_object(int(candidate["user_id"]), db_path=db_path)
            if user is not None and user.is_admin:
                telegram_bot.send_direct_message(
                    int(candidate["telegram_user_id"]), message
                )
    except Exception:  # noqa: BLE001 - alerting must never break the watcher
        LOGGER.exception("could not deliver chain health alert")


def _maybe_alert(definition: Any, *, db_path=accounts.DEFAULT_DB_PATH) -> None:
    health = accounts.chain_health(definition.key, db_path=db_path)
    failures = int(health.get("consecutive_failures") or 0)
    if failures < ALERT_AFTER_FAILURES or health.get("alerted_at"):
        return
    _alert_operator(
        f"{definition.name} payments are NOT being watched. "
        f"{failures} consecutive scan failures. Last error: "
        f"{health.get('last_error') or 'unknown'}. "
        "This network is withdrawn from checkout until it recovers.",
        db_path=db_path,
    )
    accounts.mark_chain_alerted(definition.key, db_path=db_path)


def scan_all(*, db_path=accounts.DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """One pass over every chain that can actually be watched."""
    summaries = []
    for definition in watchable_chains():
        try:
            if definition.kind == "tron":
                summary = scan_tron(db_path=db_path)
            elif definition.key == crypto_billing.DEFAULT_CHAIN:
                # Arbitrum keeps its original cursor row and code path so an
                # existing deployment does not rescan or skip on upgrade.
                summary = scan_once(db_path=db_path)
            else:
                summary = scan_evm(definition.key, db_path=db_path)
            summaries.append(summary)
            accounts.record_chain_scan(
                definition.key, ok=bool(summary.get("ok", True)), db_path=db_path
            )
        except Exception as exc:  # noqa: BLE001 - one dead chain must not stop the others
            LOGGER.exception("scan failed for %s", definition.key)
            summaries.append({"ok": False, "chain": definition.key})
            accounts.record_chain_scan(
                definition.key, ok=False,
                error=f"{type(exc).__name__}: {exc}", db_path=db_path,
            )
        _maybe_alert(definition, db_path=db_path)
    crypto_billing.expire_stale_invoices(db_path=db_path)
    return summaries
