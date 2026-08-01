"""Token lookups answered inside the subscriber Telegram group.

Members type ``$SIREN`` (or ``/spread SIREN``) and the bot answers with the
same numbers the website shows, so the two surfaces can never disagree.

Design notes:

* The cashtag is the trigger rather than a bare ticker. With privacy mode off
  the bot receives every message in the group, and plain words like "GUA" or
  "SKY" occur in ordinary conversation -- answering those would make the group
  unusable.
* Replies are rate limited per chat, token and kind, so ten people asking about
  the same token produce one answer rather than ten.
* Access is enforced at the group door (join approval plus expiry removal), so
  anyone inside the registered community is a paying member. This module still
  refuses to answer in any chat that is not the registered community.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
import re
import threading
import time
from typing import Any

from . import api_spreads


MAX_ROWS = 8
COOLDOWN_SECONDS = 60.0
PUBLIC_URL_ENV = "SPREADBOARD_PUBLIC_URL"

# $TICKER, or an explicit command. Tickers are 2-12 chars to avoid matching
# dollar amounts and stray punctuation.
CASHTAG = re.compile(r"(?:^|\s)\$([A-Za-z][A-Za-z0-9._-]{1,11})\b")
COMMANDS = {"/spread": "spread", "/funding": "funding", "/transfer": "transfer", "/token": "spread"}
# Bare intent words, accepted alongside a cashtag ("$SIREN funding").
KIND_WORDS = {"spread": "spread", "funding": "funding", "transfer": "transfer",
              "rails": "transfer", "deposit": "transfer", "withdraw": "transfer"}


@dataclass(frozen=True)
class Query:
    kind: str
    symbol: str


_LAST_ANSWERED: dict[tuple[int, str, str], float] = {}
_LOCK = threading.Lock()


def parse_query(text: str) -> Query | None:
    """Extract a token lookup from a group message, or None if it is just chat."""
    raw = str(text or "").strip()
    if not raw:
        return None

    # Members write these in any order: "/funding SIREN", "$SIREN /funding",
    # "$SIREN funding". Scan the whole message for an intent word rather than
    # trusting the first token, or "$Vanry /funding" silently returns a spread.
    words = [w.split("@", 1)[0].casefold() for w in raw.split()]
    kind = next((COMMANDS[w] for w in words if w in COMMANDS), None)
    if kind is None:
        kind = next((KIND_WORDS[w] for w in words if w in KIND_WORDS), None)

    match = CASHTAG.search(raw)
    if match:
        return Query(kind=kind or "spread", symbol=_normalise(match.group(1)))

    head = words[0] if words else ""
    if head in COMMANDS:
        _, _, rest = raw.partition(" ")
        symbol = rest.strip().lstrip("$").split(" ")[0] if rest.strip() else ""
        # Strip a trailing intent word: "/spread SIREN funding"
        if symbol.casefold() in KIND_WORDS or symbol.casefold() in COMMANDS:
            symbol = ""
        if not symbol:
            return None
        return Query(kind=COMMANDS[head], symbol=_normalise(symbol))
    return None


def _normalise(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9._-]", "", str(symbol).upper())[:12]


def allow(chat_id: int, query: Query, *, now: float | None = None) -> bool:
    """Rate limit identical questions so a busy group stays readable."""
    moment = time.time() if now is None else now
    key = (int(chat_id), query.symbol, query.kind)
    with _LOCK:
        last = _LAST_ANSWERED.get(key, 0.0)
        if moment - last < COOLDOWN_SECONDS:
            return False
        _LAST_ANSWERED[key] = moment
        return True


def reset_cooldowns() -> None:
    with _LOCK:
        _LAST_ANSWERED.clear()


def _pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return "--"
    return f"{float(value):+.{digits}f}%"


def _usd(value: Any) -> str:
    if value is None:
        return "--"
    amount = float(value)
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:.0f}"


def _route(row: dict[str, Any]) -> str:
    """Route label, suffixed with ? when identity is unverified.

    Large spreads here are real, so they are shown -- but a member must be able
    to see at a glance which legs have not had their token identity confirmed.
    """
    mark = "?" if row.get("mirage_guarded") else ""
    return f"{row.get('long_venue') or '?'}>{row.get('short_venue') or '?'}{mark}"[:22]


def _rows_for(symbol: str, board_path: Path | str) -> list[dict[str, Any]]:
    """Routes for one token, from the same feed the website renders.

    The website serves api_spreads (api_discovery_latest.json); board.jsonl is a
    separate, currently-empty legacy source. Reading the wrong one makes the bot
    disagree with the site, which is worse than being silent.
    """
    # Use the website's own defaults. Forcing include_stale/max_age_min=None
    # surfaces stale quotes with absurd edges (+95%, -778% APR) that the site
    # deliberately withholds -- the bot must not be more permissive than the UI.
    payload = api_spreads.load_spreads(q=symbol)
    rows: list[dict[str, Any]] = []
    for group in payload.get("groups") or []:
        if str(group.get("token") or "").upper() != symbol:
            continue
        rows.extend(group.get("routes") or [])
    return rows


def _table(header: tuple[str, ...], widths: tuple[int, ...], lines: list[tuple[str, ...]]) -> str:
    out = [" ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip()]
    for line in lines:
        out.append(" ".join(str(c).ljust(w) for c, w in zip(line, widths)).rstrip())
    return "\n".join(out)


def render(query: Query, *, board_path: Path | str, public_url: str = "") -> str:
    """Build the HTML reply for a token lookup."""
    # Normalise here too, not only in parse_query, so render() is safe for any
    # caller and the symbol can never carry markup into an HTML-parsed message.
    symbol = _normalise(query.symbol)
    rows = _rows_for(symbol, board_path)
    if not rows:
        return (
            f"<b>{escape(symbol)}</b> — no parsed routes right now.\n"
            "It may be unlisted, stale, or filtered out of the current scan."
        )

    if query.kind == "funding":
        rows = sorted(rows, key=lambda r: -abs(float(r.get("funding_daily_pct") or r.get("funding_spread_pct") or 0)))
        body = _table(
            ("ROUTE", "NET/DAY", "APR"), (22, 9, 8),
            [(_route(r), _pct(r.get("funding_daily_pct") or r.get("funding_spread_pct"), 3), _pct(r.get("funding_apr_pct"), 1)) for r in rows[:MAX_ROWS]],
        )
        title = f"{symbol} · funding · {len(rows)} routes"
    elif query.kind == "transfer":
        venues: dict[str, tuple[Any, Any]] = {}  # noqa: F841 - used in the count below
        for r in rows:
            if r.get("long_venue"):
                venues.setdefault(r.get("long_venue"), (r.get("long_deposit_enabled"), r.get("long_withdraw_enabled")))
            if r.get("short_venue"):
                venues.setdefault(r.get("short_venue"), (r.get("short_deposit_enabled"), r.get("short_withdraw_enabled")))
        flag = {True: "open", False: "SHUT", None: "?"}
        body = _table(
            ("VENUE", "DEPOSIT", "WITHDRAW"), (16, 8, 9),
            [(v[:16], flag.get(d, "?"), flag.get(w, "?")) for v, (d, w) in sorted(venues.items())][:MAX_ROWS],
        )
        title = f"{symbol} · transfer rails · {len(venues)} venues"
    else:
        rows = sorted(
            rows,
            key=lambda r: -(float(r.get("executable_spread_pct") or 0)),
        )
        body = _table(
            ("ROUTE", "EDGE", "DEPTH"), (22, 8, 7),
            [(_route(r), _pct(r.get("executable_spread_pct")), _usd(r.get("depth_usd"))) for r in rows[:MAX_ROWS]],
        )
        title = f"{symbol} · spread · {len(rows)} routes"

    total = len(venues) if query.kind == "transfer" else len(rows)
    extra = f"\n<i>Showing top {MAX_ROWS} of {total}.</i>" if total > MAX_ROWS else ""
    link = ""
    if public_url:
        link = f'\n\n<a href="{escape(public_url.rstrip("/"))}/markets?q={escape(symbol)}">Open full detail on SpreadBoard</a>'
    return (
        f"<b>{escape(title)}</b>\n<pre>{escape(body)}</pre>"
        f"{extra}"
        "\n<i>? = token identity unverified on that route. Research data, not advice.</i>"
        f"{link}"
    )
