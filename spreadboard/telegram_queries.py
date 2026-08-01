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

from . import board


MAX_ROWS = 8
COOLDOWN_SECONDS = 60.0
PUBLIC_URL_ENV = "SPREADBOARD_PUBLIC_URL"

# $TICKER, or an explicit command. Tickers are 2-12 chars to avoid matching
# dollar amounts and stray punctuation.
CASHTAG = re.compile(r"(?:^|\s)\$([A-Za-z][A-Za-z0-9._-]{1,11})\b")
COMMANDS = {"/spread": "spread", "/funding": "funding", "/transfer": "transfer", "/token": "spread"}


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

    head, _, rest = raw.partition(" ")
    command = head.split("@", 1)[0].casefold()
    if command in COMMANDS:
        symbol = rest.strip().lstrip("$").split(" ")[0]
        if not symbol:
            return None
        return Query(kind=COMMANDS[command], symbol=_normalise(symbol))

    match = CASHTAG.search(raw)
    if match:
        return Query(kind="spread", symbol=_normalise(match.group(1)))
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


def _route(row: Any) -> str:
    return f"{row.long_venue or '?'}>{row.short_venue or '?'}"[:22]


def _rows_for(symbol: str, board_path: Path | str) -> list[Any]:
    snapshot = board.load_board(
        board_path, q=symbol, include_stale=True, max_age_min=None, limit=None
    )
    rows = [r for r in snapshot.rows if str(r.symbol).upper() == symbol]
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
        rows = sorted(rows, key=lambda r: (r.funding_spread_pct is None, -(r.funding_spread_pct or 0)))
        body = _table(
            ("ROUTE", "NET/DAY", "APR"), (22, 9, 8),
            [(_route(r), _pct(r.funding_spread_pct, 3), _pct(r.funding_apr_pct, 1)) for r in rows[:MAX_ROWS]],
        )
        title = f"{symbol} · funding · {len(rows)} routes"
    elif query.kind == "transfer":
        venues: dict[str, tuple[Any, Any]] = {}
        for r in rows:
            if r.long_venue:
                venues.setdefault(r.long_venue, (r.long_deposit_enabled, r.long_withdraw_enabled))
            if r.short_venue:
                venues.setdefault(r.short_venue, (r.short_deposit_enabled, r.short_withdraw_enabled))
        flag = {True: "open", False: "SHUT", None: "?"}
        body = _table(
            ("VENUE", "DEPOSIT", "WITHDRAW"), (16, 8, 9),
            [(v[:16], flag.get(d, "?"), flag.get(w, "?")) for v, (d, w) in sorted(venues.items())][:MAX_ROWS],
        )
        title = f"{symbol} · transfer rails · {len(venues)} venues"
    else:
        rows = sorted(
            rows,
            key=lambda r: (r.displayed_headline_spread_pct is None, -(r.displayed_headline_spread_pct or 0)),
        )
        body = _table(
            ("ROUTE", "EDGE", "DEPTH"), (22, 8, 7),
            [(_route(r), _pct(r.displayed_headline_spread_pct), _usd(r.depth_usd)) for r in rows[:MAX_ROWS]],
        )
        title = f"{symbol} · spread · {len(rows)} routes"

    extra = f"\n<i>Showing top {MAX_ROWS} of {len(rows)}.</i>" if len(rows) > MAX_ROWS else ""
    link = ""
    if public_url:
        link = f'\n\n<a href="{escape(public_url.rstrip("/"))}/markets?q={escape(symbol)}">Open full detail on SpreadBoard</a>'
    return (
        f"<b>{escape(title)}</b>\n<pre>{escape(body)}</pre>"
        f"{extra}"
        "\n<i>Research data, not advice. Verify identity and rails before trading.</i>"
        f"{link}"
    )
