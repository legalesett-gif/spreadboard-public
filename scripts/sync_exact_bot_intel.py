#!/usr/bin/env python3
"""Sanitize one exact private Telegram bot conversation into SpreadBoard Intel.

The Telegram user session remains on the operator's Mac.  The server receives
only token, view/category, a five-minute time bucket and a fixed source label:
no names, chat/user/message IDs, usernames, email addresses or raw message
text.  No AI or paid API is used.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
from typing import Any, Iterable

from spreadarb.public_runtime import keychain
from spreadboard.telegram_queries import parse_query

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION_PATH = ROOT / "runtime" / "exact-bot-intel.session"
DEFAULT_OUTPUT_PATH = ROOT / "runtime" / "community" / "external_bot_events.jsonl"
DEFAULT_REMOTE_PATH = "/opt/spreadboard/runtime/community/external_bot_events.jsonl"
SERVICE_API_ID = "SPREADARB/cryptoinvest/telegram_api_id"
SERVICE_API_HASH = "SPREADARB/cryptoinvest/telegram_api_hash"
SERVICE_PHONE = "SPREADARB/cryptoinvest/telegram_phone"
PAIR_SYMBOL = re.compile(r"\b([A-Z0-9][A-Z0-9._-]{0,31})/(?:USDT|USD)(?::USDT)?\b", re.I)
TAGGED_SYMBOL = re.compile(r"\$([A-Z0-9][A-Z0-9._-]{0,31})\b", re.I)
HEADING_SYMBOL = re.compile(r"^\s*([A-Z0-9][A-Z0-9._-]{0,31})\s*(?:[·|:]|\s+-\s+)", re.I)
STOPWORDS = frozenset(
    {
        "APR", "BEST", "CEX", "DEX", "FUNDING", "FUTURES", "LONG", "NET",
        "PAIR", "PAIRS", "PERP", "ROUTE", "ROUTES", "SHORT", "SPOT", "USD", "USDT",
    }
)


def _clean_symbol(value: Any) -> str:
    symbol = "".join(
        character
        for character in str(value or "").upper()
        if character.isalnum() or character in "._-"
    )[:32]
    if not symbol or symbol in STOPWORDS or symbol.isdigit():
        return ""
    return symbol


def _kind(text: str, fallback: str = "spread") -> str:
    lowered = text.casefold()
    if "fund" in lowered:
        return "funding"
    if any(word in lowered for word in ("deposit", "withdraw", "transfer", "rail")):
        return "rails"
    return fallback if fallback in {"spread", "funding", "rails"} else "spread"


def _symbols_from_reply(text: str) -> set[str]:
    symbols = set(TAGGED_SYMBOL.findall(text)) | set(PAIR_SYMBOL.findall(text))
    for line in text.splitlines()[:4]:
        match = HEADING_SYMBOL.match(line)
        if match:
            symbols.add(match.group(1))
    return {clean for value in symbols if (clean := _clean_symbol(value))}


def _time_bucket_us(value: Any) -> int:
    if isinstance(value, datetime):
        stamp = value.astimezone(timezone.utc).timestamp()
    else:
        stamp = float(value or time.time())
    return int(stamp // 300 * 300 * 1_000_000)


def attention_event(symbol: str, kind: str, *, at: Any) -> dict[str, Any]:
    """A deliberately identity-free record accepted by ``spreadboard.intel``."""
    return {
        "schema": "spreadboard.exact_bot_attention.v1",
        "source": "SpreadArbitrage research bot",
        "source_role": "operator_research",
        "received_at_us": _time_bucket_us(at),
        "parsed": {
            "symbol": _clean_symbol(symbol),
            "kind": _kind("", kind).upper(),
            "event": "chat_signal",
            "first_line": f"${_clean_symbol(symbol)} {_kind('', kind)}",
            "topic_id": None,
        },
        "privacy": "token_view_and_five_minute_bucket_only",
    }


def sanitize_messages(messages: Iterable[Any], *, bot_username: str) -> list[dict[str, Any]]:
    """Turn newest/oldest Telegram message objects into deduplicated attention."""
    output: dict[tuple[int, str, str], dict[str, Any]] = {}
    last_query: tuple[str, str, float] | None = None
    ordered = sorted(
        messages,
        key=lambda item: getattr(item, "date", None) or datetime.min.replace(tzinfo=timezone.utc),
    )
    for message in ordered:
        text = str(getattr(message, "message", None) or getattr(message, "text", None) or "")
        when = getattr(message, "date", None) or datetime.now(tz=timezone.utc)
        timestamp = when.timestamp() if isinstance(when, datetime) else float(when)
        if bool(getattr(message, "out", False)):
            query = parse_query(text, bot_username=bot_username)
            if query is None:
                continue
            symbol, kind = _clean_symbol(query.symbol), query.kind
            last_query = (symbol, kind, timestamp)
            candidates = {(symbol, kind)} if symbol else set()
        else:
            candidates = {(symbol, _kind(text)) for symbol in _symbols_from_reply(text)}
            if last_query and timestamp - last_query[2] <= 15 * 60:
                candidates.add((last_query[0], last_query[1]))
        for symbol, kind in candidates:
            if not symbol:
                continue
            event = attention_event(symbol, kind, at=when)
            key = (event["received_at_us"], symbol, kind)
            output[key] = event
    return [output[key] for key in sorted(output)]


def _write_atomic(path: Path, events: list[dict[str, Any]]) -> bytes:
    body = ("\n".join(json.dumps(event, separators=(",", ":")) for event in events) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(body)
    temporary.chmod(0o600)
    temporary.replace(path)
    return body


def _sync_remote(body: bytes, *, host: str, key_path: Path, remote_path: str) -> None:
    remote_dir = shlex.quote(str(Path(remote_path).parent))
    target = shlex.quote(remote_path)
    temporary = shlex.quote(f"{remote_path}.tmp")
    command = (
        f"umask 077; mkdir -p {remote_dir}; "
        f"cat > {temporary}; mv {temporary} {target}"
    )
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-i", str(key_path), host, command],
        input=body,
        check=True,
        timeout=30,
    )


async def collect(args: argparse.Namespace) -> list[dict[str, Any]]:
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise SystemExit("Telethon is required; run with `uv run --with telethon`.") from exc
    api_id = keychain(SERVICE_API_ID) or os.environ.get("SPREADARB_CRYPTOINVEST_TELEGRAM_API_ID")
    api_hash = keychain(SERVICE_API_HASH) or os.environ.get("SPREADARB_CRYPTOINVEST_TELEGRAM_API_HASH")
    phone = keychain(SERVICE_PHONE) or os.environ.get("SPREADARB_CRYPTOINVEST_TELEGRAM_PHONE")
    if not api_id or not api_hash:
        raise SystemExit("Telegram API ID/hash are not configured in Keychain or environment.")
    client = TelegramClient(str(args.session_path), int(api_id), api_hash)
    await client.start(phone=phone)
    try:
        args.session_path.chmod(0o600)
    except OSError:
        pass
    try:
        entity = await client.get_entity(args.bot_username)
        cutoff = datetime.now(tz=timezone.utc).timestamp() - args.hours * 3600
        messages = []
        async for message in client.iter_messages(entity, limit=args.limit):
            when = getattr(message, "date", None)
            if when and when.timestamp() < cutoff:
                break
            messages.append(message)
        return sanitize_messages(messages, bot_username=args.bot_username)
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-username", default="SpreadArbitrageBot")
    parser.add_argument("--session-path", type=Path, default=DEFAULT_SESSION_PATH)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--hours", type=float, default=168.0)
    parser.add_argument("--limit", type=int, default=2_000)
    parser.add_argument("--ssh-host", default=os.environ.get("SPREADBOARD_INTEL_SSH_HOST", ""))
    parser.add_argument(
        "--ssh-key",
        type=Path,
        default=Path(os.environ["SPREADBOARD_INTEL_SSH_KEY"])
        if os.environ.get("SPREADBOARD_INTEL_SSH_KEY")
        else None,
    )
    parser.add_argument("--remote-path", default=DEFAULT_REMOTE_PATH)
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    events = asyncio.run(collect(args))
    body = _write_atomic(args.output_path, events)
    if not args.local_only:
        if not args.ssh_host or not str(args.ssh_key):
            raise SystemExit("--ssh-host and --ssh-key are required unless --local-only is used.")
        _sync_remote(body, host=args.ssh_host, key_path=args.ssh_key, remote_path=args.remote_path)
    print(json.dumps({"ok": True, "sanitized_events": len(events), "raw_text_stored": False}))


if __name__ == "__main__":
    main()
