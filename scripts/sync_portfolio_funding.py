#!/usr/bin/env python3
"""Sync exact portfolio accounting without putting secrets on the server.

The worker runs on the operator's Mac, reads API credentials from macOS
Keychain, pulls signed private funding cashflows, and uploads only sanitized
per-position totals and full-size DEX sell marks. It never creates orders,
transfers, withdrawals, approvals, signatures, or other venue mutations.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import ccxt

from spreadarb.public_runtime import keychain
from spreadboard import portfolio_funding


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_PATH = ROOT / "runtime" / "portfolio_funding.json"
DEFAULT_REMOTE_PATH = "/opt/spreadboard/runtime/portfolio_funding.json"
DEFAULT_SSH_KEY = Path.home() / ".ssh" / "spreadboard_digitalocean"
VENUES = {
    "Aster": ("aster", "aster"),
    "Gate": ("gate", "gate"),
    "Mexc": ("mexc", "mexc"),
    "Binance": ("binance", "binance"),
    "Bingx": ("bingx", "bingx"),
    "Bitget": ("bitget", "bitget"),
    "Bybit": ("bybit", "bybit"),
    "Kucoin": ("kucoinfutures", "kucoin"),
    "OKX": ("okx", "okx"),
}
USDT_BY_CHAIN = {
    56: {
        "contract": "0x55d398326f99059ff775485246999027b3197955",
        "decimals": 18,
    },
}
RPC_BY_CHAIN = {56: "https://bsc-dataseed.binance.org/"}


def dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def utc_iso(timestamp_ms: int | None = None) -> str:
    moment = (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        if timestamp_ms is not None
        else datetime.now(tz=timezone.utc)
    )
    return moment.isoformat().replace("+00:00", "Z")


def timestamp_ms(value: Any) -> int:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def build_exchange(venue: str) -> Any:
    spec = VENUES.get(venue)
    if spec is None:
        raise RuntimeError(f"unsupported_venue:{venue}")
    ccxt_id, service = spec
    api_key = keychain(f"SPREADARB/{service}/api_key")
    secret = keychain(f"SPREADARB/{service}/secret")
    if not api_key or not secret:
        raise RuntimeError(f"missing_credentials:{venue}")
    options: dict[str, Any] = {"defaultType": "swap"}
    if ccxt_id in {"binance", "mexc"}:
        options["adjustForTimeDifference"] = True
    params: dict[str, Any] = {
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "timeout": 20_000,
        "options": options,
    }
    passphrase = keychain(f"SPREADARB/{service}/passphrase")
    if passphrase:
        params["password"] = passphrase
    if ccxt_id == "aster":
        params["privateKey"] = secret
        params["walletAddress"] = api_key
        params["options"]["signerAddress"] = api_key
        params.pop("apiKey", None)
        params.pop("secret", None)
    exchange = getattr(ccxt, ccxt_id)(params)
    if options.get("adjustForTimeDifference") and hasattr(exchange, "load_time_difference"):
        exchange.load_time_difference()
    exchange.load_markets()
    return exchange


def fetch_private_funding(exchange: Any, symbol: str, since_ms: int) -> list[dict[str, Any]]:
    """Fetch a bounded, deduplicated signed account-funding ledger."""

    cursor = int(since_ms)
    events: dict[tuple[int, str, str], dict[str, Any]] = {}
    for _ in range(20):
        rows = exchange.fetch_funding_history(symbol, since=cursor, limit=1000) or []
        latest = cursor
        for row in rows:
            stamp = int(row.get("timestamp") or 0)
            amount = dec(row.get("amount"))
            if stamp <= 0 or amount is None:
                continue
            code = str(row.get("code") or "USDT")
            key = (stamp, str(amount), code)
            events[key] = {"timestamp": stamp, "amount": str(amount), "code": code}
            latest = max(latest, stamp)
        if not rows or len(rows) < 1000 or latest <= cursor:
            break
        cursor = latest + 1
    return [events[key] for key in sorted(events)]


def token_decimals(chain_id: int, contract: str) -> int:
    """Read ERC-20 decimals without a wallet or signed request."""

    rpc = RPC_BY_CHAIN.get(chain_id)
    if not rpc:
        raise RuntimeError(f"unsupported_dex_chain:{chain_id}")
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": contract, "data": "0x313ce567"}, "latest"],
        }
    ).encode()
    request = Request(rpc, data=body, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed HTTPS RPC.
        payload = json.load(response)
    value = int(str(payload.get("result") or "0x0"), 16)
    if not 0 <= value <= 36:
        raise RuntimeError("invalid_token_decimals")
    return value


def fetch_dex_exit_mark(position: dict[str, Any], side: str, leg: dict[str, Any]) -> dict[str, Any]:
    """Quote an exact saved DEX long quantity into USDT without execution."""

    if side != "long":
        raise RuntimeError("dex_short_mark_not_supported")
    chain_id = int(leg.get("dex_chain") or 0)
    destination = USDT_BY_CHAIN.get(chain_id)
    if not destination:
        raise RuntimeError(f"unsupported_dex_chain:{chain_id}")
    contract = str(leg.get("dex_contract") or "").strip().lower()
    if not contract.startswith("0x") or len(contract) != 42:
        raise RuntimeError("missing_dex_contract")
    quantity = dec(position.get(f"{side}_quantity"))
    if quantity is None or quantity <= 0:
        raise RuntimeError("invalid_dex_quantity")
    source_decimals = token_decimals(chain_id, contract)
    source_amount = int(quantity * (Decimal(10) ** source_decimals))
    query = urlencode(
        {
            "srcToken": contract,
            "destToken": destination["contract"],
            "amount": str(source_amount),
            "srcDecimals": str(source_decimals),
            "destDecimals": str(destination["decimals"]),
            "side": "SELL",
            "network": str(chain_id),
            "version": "6.2",
        }
    )
    request = Request(
        f"https://api.paraswap.io/prices/?{query}",
        headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS API.
        route = (json.load(response) or {}).get("priceRoute") or {}
    destination_amount = dec(route.get("destAmount"))
    if destination_amount is None or destination_amount <= 0:
        raise RuntimeError("invalid_dex_quote")
    proceeds = destination_amount / (Decimal(10) ** int(destination["decimals"]))
    return {
        "status": "ok",
        "source": "paraswap_exact_sell_quote",
        "quote_currency": "USDT",
        "quantity": str(quantity),
        "price_usd": str(proceeds / quantity),
        "proceeds_usdt": str(proceeds),
        "gas_cost_usd": str(route.get("gasCostUSD") or ""),
        "quoted_at": utc_iso(),
        "read_only": True,
    }


def quantity_vwap(
    levels: list[list[float]], quantity: Decimal, *, contract_size: Decimal
) -> Decimal | None:
    """VWAP an exact base-asset quantity; derivative book sizes are contracts."""

    if quantity <= 0 or contract_size <= 0:
        return None
    remaining = quantity
    value = Decimal(0)
    filled = Decimal(0)
    for raw in levels:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        price = dec(raw[0])
        contracts = dec(raw[1])
        if price is None or contracts is None or price <= 0 or contracts <= 0:
            continue
        available = contracts * contract_size
        take = min(remaining, available)
        value += take * price
        filled += take
        remaining -= take
        if remaining <= max(Decimal("1e-12"), quantity * Decimal("1e-12")):
            break
    if remaining > max(Decimal("1e-9"), quantity * Decimal("1e-9")) or filled <= 0:
        return None
    return value / filled


def fetch_cex_exit_mark(
    exchange: Any,
    position: dict[str, Any],
    side: str,
    leg: dict[str, Any],
) -> dict[str, Any]:
    """Fetch a full-position CEX exit VWAP; this cannot mutate the account."""

    symbol = str(position.get(f"{side}_symbol") or "")
    quantity = dec(position.get(f"{side}_quantity"))
    if not symbol or quantity is None or quantity <= 0:
        raise RuntimeError("invalid_cex_position")
    market = exchange.market(symbol)
    contract_size = (
        dec(leg.get("contract_size"))
        or dec(market.get("contractSize"))
        or Decimal(1)
    )
    book = exchange.fetch_order_book(symbol, limit=100)
    levels = book.get("bids") if side == "long" else book.get("asks")
    price = quantity_vwap(levels or [], quantity, contract_size=contract_size)
    if price is None:
        raise RuntimeError("insufficient_cex_depth")
    return {
        "status": "ok",
        "source": "local_full_position_book_vwap",
        "quote_currency": str(market.get("quote") or "USDT"),
        "quantity": str(quantity),
        "price_usd": str(price),
        "quoted_at": utc_iso(),
        "read_only": True,
    }


def build_snapshot(
    positions: list[dict[str, Any]],
    funding_fetcher: Callable[[str, str, int], list[dict[str, Any]]],
    *,
    generated_at: str | None = None,
    mark_fetcher: Callable[[dict[str, Any], str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Allocate exact ledger rows to non-overlapping saved position windows."""

    started = generated_at or utc_iso()
    legs_by_market: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = defaultdict(list)
    result: dict[str, dict[str, Any]] = {}
    for position in positions:
        key = f"{int(position['user_id'])}:{int(position['id'])}"
        result[key] = {
            "user_id": int(position["user_id"]),
            "position_id": int(position["id"]),
            "position_fingerprint": portfolio_funding.position_fingerprint(position),
            "status": "ok",
            "source": "private_exchange_ledger",
            "amount_usd": "0",
            "event_count": 0,
            "latest_event_at": None,
            "synced_at": started,
            "legs": [],
            "marks": {},
        }
        for side in ("long", "short"):
            if str(position.get(f"{side}_market_type") or "").casefold() != "futures":
                continue
            venue = str(position.get(f"{side}_venue") or "")
            symbol = str(position.get(f"{side}_symbol") or "")
            if not venue or not symbol:
                result[key]["status"] = "missing_futures_symbol"
                continue
            legs_by_market[(venue, symbol)].append((position, side))

    for (venue, symbol), legs in legs_by_market.items():
        earliest = min(timestamp_ms(position["opened_at"]) for position, _ in legs)
        try:
            events = funding_fetcher(venue, symbol, earliest)
        except Exception as exc:  # noqa: BLE001 - one venue must not poison the rest.
            for position, side in legs:
                item = result[f"{int(position['user_id'])}:{int(position['id'])}"]
                item["status"] = f"sync_error:{type(exc).__name__}"
                item["legs"].append(
                    {"side": side, "venue": venue, "symbol": symbol, "status": "error"}
                )
            continue
        ambiguous = _overlapping_positions(legs)
        for position, side in legs:
            key = f"{int(position['user_id'])}:{int(position['id'])}"
            item = result[key]
            if int(position["id"]) in ambiguous:
                item["status"] = "ambiguous_overlapping_position"
                item["legs"].append(
                    {"side": side, "venue": venue, "symbol": symbol, "status": "ambiguous"}
                )
                continue
            opened = timestamp_ms(position["opened_at"])
            closed = timestamp_ms(position["closed_at"]) if position.get("closed_at") else None
            selected = [
                event
                for event in events
                if int(event["timestamp"]) >= opened
                and (closed is None or int(event["timestamp"]) <= closed)
            ]
            amount = sum((dec(event.get("amount")) or Decimal("0")) for event in selected)
            item["amount_usd"] = str((dec(item["amount_usd"]) or Decimal("0")) + amount)
            item["event_count"] += len(selected)
            latest = max((int(event["timestamp"]) for event in selected), default=None)
            if latest is not None:
                previous = item.get("latest_event_at")
                previous_ms = timestamp_ms(previous) if previous else 0
                item["latest_event_at"] = utc_iso(max(previous_ms, latest))
            item["legs"].append(
                {
                    "side": side,
                    "venue": venue,
                    "symbol": symbol,
                    "status": "ok",
                    "amount_usd": str(amount),
                    "event_count": len(selected),
                }
            )
    # Full-position exit quotes are deliberately last. A slow private-ledger
    # venue must not make a quote stale before this atomic snapshot is written.
    for position in positions:
        if str(position.get("status") or "").casefold() != "open":
            continue
        key = f"{int(position['user_id'])}:{int(position['id'])}"
        resolved_legs = (
            position.get("_resolved_legs")
            if isinstance(position.get("_resolved_legs"), dict)
            else {}
        )
        for side in ("long", "short"):
            leg = resolved_legs.get(side) if isinstance(resolved_legs.get(side), dict) else {}
            try:
                if mark_fetcher is None:
                    raise RuntimeError("position_mark_not_configured")
                result[key]["marks"][side] = mark_fetcher(position, side, leg)
            except Exception as exc:  # noqa: BLE001 - funding remains usable.
                result[key]["marks"][side] = {
                    "status": f"quote_error:{type(exc).__name__}",
                    "source": "unavailable",
                    "quantity": str(position.get(f"{side}_quantity") or ""),
                    "quoted_at": utc_iso(),
                }
    completed = generated_at or utc_iso()
    for item in result.values():
        item["synced_at"] = completed
    return {
        "schema": portfolio_funding.SCHEMA,
        "started_at": started,
        "generated_at": completed,
        "read_only": True,
        "positions": result,
    }


def _overlapping_positions(
    legs: list[tuple[dict[str, Any], str]],
) -> set[int]:
    ambiguous: set[int] = set()
    for index, (left, _) in enumerate(legs):
        left_start = timestamp_ms(left["opened_at"])
        left_end = timestamp_ms(left["closed_at"]) if left.get("closed_at") else 2**63 - 1
        for right, _ in legs[index + 1 :]:
            if int(left["id"]) == int(right["id"]):
                ambiguous.add(int(left["id"]))
                continue
            right_start = timestamp_ms(right["opened_at"])
            right_end = timestamp_ms(right["closed_at"]) if right.get("closed_at") else 2**63 - 1
            if max(left_start, right_start) <= min(left_end, right_end):
                ambiguous.update((int(left["id"]), int(right["id"])))
    return ambiguous


def remote_positions(
    *, host: str, ssh_key: Path, user_id: int, container: str
) -> list[dict[str, Any]]:
    code = f"""
import json, sqlite3
from spreadboard import chart_catalog, position_markets
con=sqlite3.connect('file:/app/runtime/spreadboard_accounts.sqlite3?mode=ro', uri=True)
con.row_factory=sqlite3.Row
con.execute('PRAGMA query_only=ON')
rows=con.execute('''SELECT id,user_id,token,status,long_venue,long_market_type,long_symbol,long_quantity,long_entry_price,short_venue,short_market_type,short_symbol,short_quantity,short_entry_price,opened_at,closed_at FROM positions WHERE user_id = ? ORDER BY opened_at''', ({int(user_id)},)).fetchall()
catalogue=chart_catalog.load()
payload=[]
for row in rows:
    item=dict(row)
    resolved=position_markets.resolve_position_route(item, [], catalogue=catalogue)
    item['_resolved_legs']={{side: resolved.get(f'{{side}}_leg') for side in ('long','short')}}
    payload.append(item)
print(json.dumps(payload))
"""
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-i",
            str(ssh_key),
            host,
            "docker",
            "exec",
            "-i",
            container,
            "python",
            "-",
        ],
        input=code,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    return [row for row in payload if isinstance(row, dict)]


def write_atomic(path: Path, payload: dict[str, Any]) -> bytes:
    body = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(body)
    temporary.chmod(0o600)
    temporary.replace(path)
    return body


def sync_remote(body: bytes, *, host: str, ssh_key: Path, remote_path: str) -> None:
    remote_dir = shlex.quote(str(Path(remote_path).parent))
    target = shlex.quote(remote_path)
    temporary = shlex.quote(f"{remote_path}.tmp")
    command = (
        f"umask 077; mkdir -p {remote_dir}; cat > {temporary}; "
        f"mv {temporary} {target}; "
        f"owner=$(docker exec app-app-1 id -u); "
        f"group=$(docker exec app-app-1 id -g); "
        f"chown $owner:$group {target}; chmod 0600 {target}"
    )
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-i", str(ssh_key), host, command],
        input=body,
        check=True,
        timeout=30,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--ssh-host", default="root@178.128.126.204")
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    parser.add_argument("--container", default="app-app-1")
    parser.add_argument("--local-output", type=Path, default=DEFAULT_LOCAL_PATH)
    parser.add_argument("--remote-path", default=DEFAULT_REMOTE_PATH)
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    ssh_key = args.ssh_key.expanduser().resolve()
    if not ssh_key.is_file():
        raise SystemExit("SSH key is missing.")
    positions = remote_positions(
        host=args.ssh_host,
        ssh_key=ssh_key,
        user_id=args.user_id,
        container=args.container,
    )
    exchanges: dict[str, Any] = {}

    def fetcher(venue: str, symbol: str, since_ms: int) -> list[dict[str, Any]]:
        if venue not in exchanges:
            exchanges[venue] = build_exchange(venue)
        return fetch_private_funding(exchanges[venue], symbol, since_ms)

    def marker(
        position: dict[str, Any], side: str, leg: dict[str, Any]
    ) -> dict[str, Any]:
        market_type = str(position.get(f"{side}_market_type") or "").casefold()
        venue = str(position.get(f"{side}_venue") or "")
        if market_type == "dex" or " dex " in f" {venue.casefold()} ":
            return fetch_dex_exit_mark(position, side, leg)
        if venue not in exchanges:
            exchanges[venue] = build_exchange(venue)
        return fetch_cex_exit_mark(exchanges[venue], position, side, leg)

    snapshot = build_snapshot(positions, fetcher, mark_fetcher=marker)
    body = write_atomic(args.local_output.expanduser().resolve(), snapshot)
    if not args.local_only:
        sync_remote(body, host=args.ssh_host, ssh_key=ssh_key, remote_path=args.remote_path)
    exact = sum(item.get("status") == "ok" for item in snapshot["positions"].values())
    print(
        json.dumps(
            {
                "ok": True,
                "read_only": True,
                "positions": len(snapshot["positions"]),
                "exact": exact,
                "errors": len(snapshot["positions"]) - exact,
            }
        )
    )


if __name__ == "__main__":
    main()
