#!/usr/bin/env python3
"""Sync exact portfolio accounting without putting secrets on the server.

The worker runs on the operator's Mac, reads API credentials from macOS
Keychain, pulls signed private funding cashflows, and uploads only sanitized
per-position totals and quantity-independent reference marks. It never creates
orders, transfers, withdrawals, approvals, signatures, or other venue
mutations.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
from typing import Any, Callable
from urllib.request import Request, urlopen

import ccxt

from spreadarb.public_runtime import keychain
from spreadboard import fair_price, portfolio_funding


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
DEXSCREENER_CHAIN_NAMES = {56: "bsc"}


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


def build_exchange(venue: str, credentials: dict[str, str] | None = None) -> Any:
    spec = next(
        (item for label, item in VENUES.items() if label.casefold() == str(venue).casefold()),
        None,
    )
    if spec is None:
        raise RuntimeError(f"unsupported_venue:{venue}")
    ccxt_id, service = spec
    api_key = str((credentials or {}).get("api_key") or "") or keychain(
        f"SPREADARB/{service}/api_key"
    )
    secret = str((credentials or {}).get("secret") or "") or keychain(
        f"SPREADARB/{service}/secret"
    )
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
    passphrase = str((credentials or {}).get("passphrase") or "") or keychain(
        f"SPREADARB/{service}/passphrase"
    )
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


def fetch_dex_reference_mark(
    position: dict[str, Any], side: str, leg: dict[str, Any]
) -> dict[str, Any]:
    """Read the deepest exact-contract pool price without quoting a swap.

    A DEX has no venue-issued mark price. The least misleading current
    accounting reference is the exact token contract's deepest pool price.
    It is quantity-independent and contains no route impact, gas or slippage.
    """

    chain_id = int(leg.get("dex_chain") or 0)
    chain = DEXSCREENER_CHAIN_NAMES.get(chain_id)
    if not chain:
        raise RuntimeError(f"unsupported_dex_chain:{chain_id}")
    contract = str(leg.get("dex_contract") or "").strip().lower()
    if not contract.startswith("0x") or len(contract) != 42:
        raise RuntimeError("missing_dex_contract")
    quantity = dec(position.get(f"{side}_quantity"))
    if quantity is None or quantity <= 0:
        raise RuntimeError("invalid_dex_quantity")
    request = Request(
        f"https://api.dexscreener.com/token-pairs/v1/{chain}/{contract}",
        headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"},
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed HTTPS API.
        payload = json.load(response)
    pairs = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict) or str(row.get("chainId") or "").casefold() != chain:
            continue
        base = row.get("baseToken") if isinstance(row.get("baseToken"), dict) else {}
        quote = row.get("quoteToken") if isinstance(row.get("quoteToken"), dict) else {}
        base_matches = str(base.get("address") or "").casefold() == contract
        quote_matches = str(quote.get("address") or "").casefold() == contract
        if not base_matches and not quote_matches:
            continue
        base_price_usd = dec(row.get("priceUsd"))
        base_in_quote = dec(row.get("priceNative"))
        price = (
            base_price_usd
            if base_matches
            else base_price_usd / base_in_quote
            if base_price_usd is not None
            and base_in_quote is not None
            and base_in_quote > 0
            else None
        )
        liquidity = dec(
            (row.get("liquidity") or {}).get("usd")
            if isinstance(row.get("liquidity"), dict)
            else None
        )
        if price is None or price <= 0 or liquidity is None or liquidity <= 0:
            continue
        pairs.append((liquidity, price, row))
    if not pairs:
        raise RuntimeError("exact_dex_pool_reference_unavailable")
    liquidity, price, pair = max(pairs, key=lambda item: item[0])
    return {
        "status": "ok",
        "source": "dexscreener_exact_contract_pool",
        "basis": "dex_pool_reference",
        "quote_currency": "USD",
        "quantity": str(quantity),
        "price_usd": str(price),
        "pool_liquidity_usd": str(liquidity),
        "pair_address": str(pair.get("pairAddress") or ""),
        "dex_id": str(pair.get("dexId") or ""),
        "quoted_at": utc_iso(),
        "read_only": True,
    }


def fetch_cex_reference_mark(
    exchange: Any,
    position: dict[str, Any],
    side: str,
    leg: dict[str, Any],
) -> dict[str, Any]:
    """Fetch a venue mark/fair price or exact-book midpoint."""

    symbol = str(position.get(f"{side}_symbol") or "")
    quantity = dec(position.get(f"{side}_quantity"))
    if not symbol or quantity is None or quantity <= 0:
        raise RuntimeError("invalid_cex_position")
    market = exchange.market(symbol)
    ticker = exchange.fetch_ticker(symbol) or {}
    price: Decimal | None = None
    basis = ""
    market_type = str(position.get(f"{side}_market_type") or "").casefold()
    if market_type == "futures":
        try:
            funding = exchange.fetch_funding_rate(symbol) or {}
        except Exception:  # noqa: BLE001 - ticker/book midpoint remains valid.
            funding = {}
        price = dec(funding.get("markPrice"))
        basis = "markPrice" if price is not None else ""
        if price is None:
            venue_fair, fair_basis = fair_price.fair_price_of(ticker)
            price = dec(venue_fair)
            basis = str(fair_basis or "")
    bid = dec(ticker.get("bid"))
    ask = dec(ticker.get("ask"))
    if price is None and bid is not None and ask is not None and 0 < bid <= ask:
        price = (bid + ask) / Decimal(2)
        basis = "bid_ask_midpoint"
    if price is None:
        book = exchange.fetch_order_book(symbol, limit=5) or {}
        best_bid = dec((book.get("bids") or [[None]])[0][0])
        best_ask = dec((book.get("asks") or [[None]])[0][0])
        if best_bid is not None and best_ask is not None and 0 < best_bid <= best_ask:
            price = (best_bid + best_ask) / Decimal(2)
            basis = "bid_ask_midpoint"
    if price is None or price <= 0:
        raise RuntimeError("cex_reference_price_unavailable")
    return {
        "status": "ok",
        "source": (
            "venue_mark_price"
            if basis not in {"bid_ask_midpoint", ""}
            else "local_book_midpoint"
        ),
        "basis": basis or "reference_price",
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
    # Current reference marks are deliberately last. A slow private-ledger
    # venue must not make a mark stale before this atomic snapshot is written.
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
                    "status": f"mark_error:{type(exc).__name__}",
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
            return fetch_dex_reference_mark(position, side, leg)
        if venue not in exchanges:
            exchanges[venue] = build_exchange(venue)
        return fetch_cex_reference_mark(exchanges[venue], position, side, leg)

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
