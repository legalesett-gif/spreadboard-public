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

import re
import threading
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from . import api_spreads

MAX_ROWS = 8
COOLDOWN_SECONDS = 60.0
PUBLIC_URL_ENV = "SPREADBOARD_PUBLIC_URL"

# $TICKER, or an explicit command. Scanner symbols can be longer than ordinary
# tickers (for example 1000000BABYDOGE), so keep a bounded 32-character ceiling
# rather than truncating a live symbol. They can start with a digit (for example
# 1INCH). A purely numeric cashtag is accepted
# only when it is the whole message, so ordinary prose such as "I paid $4" is
# not mistaken for a token query while the real one-character token ``$4`` is
# still reachable.
MAX_SYMBOL_LENGTH = 32
CASHTAG = re.compile(
    rf"(?:^|\s)\$([\w.-]{{1,{MAX_SYMBOL_LENGTH}}})\b", re.UNICODE
)
COMMANDS = {"/spread": "spread", "/funding": "funding", "/transfer": "transfer", "/token": "spread"}
VIEW_LABELS = {"spread": "Spread", "funding": "Funding", "transfer": "Deposits / Withdrawals"}
# Bare intent words, accepted alongside a cashtag ("$SIREN funding").
KIND_WORDS = {"spread": "spread", "funding": "funding", "transfer": "transfer",
              "rails": "transfer", "deposit": "transfer", "withdraw": "transfer"}


@dataclass(frozen=True)
class Query:
    kind: str
    symbol: str


_LAST_ANSWERED: dict[tuple[int, str, str], float] = {}
_LOCK = threading.Lock()


def parse_query(text: str, *, bot_username: str = "") -> Query | None:
    """Extract a token lookup from a group message, or None if it is just chat."""
    raw = str(text or "").strip()
    if not raw:
        return None

    # A Telegram mention is itself an explicit trigger. Privacy-mode messages
    # such as ``@spreadarbitragesubscription_bot SIREN`` reach the bot, but used
    # to fall through because only cashtags and slash commands were parsed.
    mentioned = False
    username = str(bot_username or "").strip().lstrip("@")
    if username:
        mention = re.compile(rf"^@{re.escape(username)}\b", re.IGNORECASE)
        if mention.search(raw):
            mentioned = True
            raw = mention.sub("", raw, count=1).strip()

    # Members write these in any order: "/funding SIREN", "$SIREN /funding",
    # "$SIREN funding". Scan the whole message for an intent word rather than
    # trusting the first token, or "$Vanry /funding" silently returns a spread.
    words = [w.split("@", 1)[0].casefold() for w in raw.split()]
    kind = next((COMMANDS[w] for w in words if w in COMMANDS), None)
    if kind is None:
        kind = next((KIND_WORDS[w] for w in words if w in KIND_WORDS), None)

    match = CASHTAG.search(raw)
    if match:
        symbol = _normalise(match.group(1))
        if not symbol.isdigit() or raw == f"${symbol}":
            return Query(kind=kind or "spread", symbol=symbol)

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
    if mentioned:
        candidates = re.findall(
            rf"\b[\w.-]{{1,{MAX_SYMBOL_LENGTH}}}\b", raw, re.UNICODE
        )
        symbol = next(
            (
                value
                for value in candidates
                if value.casefold() not in KIND_WORDS
                and value.casefold() not in COMMANDS
            ),
            "",
        )
        if symbol:
            return Query(kind=kind or "spread", symbol=_normalise(symbol))
    return None


def _normalise(symbol: str) -> str:
    # Scanner symbols are not guaranteed to be ASCII (for example ``龙虾``).
    # Keep Unicode letters/numbers plus the three venue-safe separators, while
    # stripping markup, spaces and control characters before HTML escaping.
    return "".join(
        character
        for character in str(symbol).upper()
        if character.isalnum() or character in "._-"
    )[:MAX_SYMBOL_LENGTH]


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


#: The one query the bot builds from. It is warmed by the service, so a
#: Telegram lookup is a dictionary read rather than a board build.
#:
#: Every lookup used to call load_spreads(q=SYMBOL), which is its own cache key
#: and was never warmed -- so a bare "$" took 36.2s and "$SIREN" 26.3s, and
#: Telegram's webhook gave up long before either returned. The member saw
#: nothing at all, which is exactly what "the $ is not working" looked like.
WARM_QUERY: dict[str, Any] = {}
_WARM_QUERY_UPDATED_AT = 0.0
_WARM_QUERY_LOCK = threading.Lock()


def refresh_payload(board_path: Path | str) -> dict[str, Any]:
    """Build and atomically replace the bot's dedicated read snapshot.

    Telegram gives a webhook only a short response window. The website's
    request cache is deliberately invalidated after every quote publication,
    so it cannot also be the bot's availability boundary: a member happened to
    ask during invalidation and paid the whole multi-second board build.

    The service calls this after each successful board warm. Webhook threads
    only read the last complete payload and therefore never wait on grouping,
    exchange I/O, or a cache rebuild.
    """
    payload = api_spreads.load_spreads(
        board_path=board_path,
        include_stale=False,
        include_unverified=False,
        # This is the same gate used by /api/spreads and the member markets
        # page. The bot must neither lose a client-visible route nor surface a
        # transfer route the website has correctly removed from consideration.
        require_deliverable=True,
        limit=None,
    )
    return replace_payload(payload)


def replace_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Atomically install the exact all-token payload served to clients.

    Production normally calls this from ``server.api_market_spreads`` when its
    unfiltered 500-token view is served or rebuilt. Keeping the install step
    separate from the expensive build makes the website response and Telegram
    lookup share one immutable generation instead of independently producing
    equal-sized but different token sets during a quote refresh.
    """
    if not isinstance(payload, dict):
        raise TypeError("telegram_payload_must_be_a_mapping")
    if not isinstance(payload.get("groups"), list):
        raise TypeError("telegram_payload_groups_must_be_a_list")
    global WARM_QUERY, _WARM_QUERY_UPDATED_AT
    with _WARM_QUERY_LOCK:
        WARM_QUERY = payload
        _WARM_QUERY_UPDATED_AT = time.time()
    return payload


def _warm_payload(_board_path: Path | str) -> dict[str, Any]:
    """Return the last complete Telegram snapshot without doing heavy work."""
    with _WARM_QUERY_LOCK:
        return WARM_QUERY


def payload_status(*, now: float | None = None) -> dict[str, Any]:
    moment = time.time() if now is None else float(now)
    with _WARM_QUERY_LOCK:
        ready = bool(WARM_QUERY)
        updated_at = _WARM_QUERY_UPDATED_AT
        groups = WARM_QUERY.get("groups") or []
        token_count = len(groups)
        route_count = sum(len(group.get("routes") or []) for group in groups)
    return {
        "ready": ready,
        "age_seconds": max(0.0, moment - updated_at) if ready and updated_at else None,
        "token_count": token_count,
        "route_count": route_count,
    }


def reset_payload() -> None:
    """Test/support hook; production refreshes by replacing, never mutating."""
    global WARM_QUERY, _WARM_QUERY_UPDATED_AT
    with _WARM_QUERY_LOCK:
        WARM_QUERY = {}
        _WARM_QUERY_UPDATED_AT = 0.0


#: A bare "$" is a request for suggestions, not a token.
BARE_CASHTAG = re.compile(r"^\$([\w.-]{0,12})$", re.UNICODE)

#: How many tokens to offer. Three to a row, four rows, fits a phone.
SUGGESTION_LIMIT = 12


def suggestions(prefix: str, *, board_path: Path | str) -> list[str]:
    """Tokens to offer for a bare `$`, best first.

    Telegram gives a bot no autocomplete hook, so `$` on its own used to parse
    to nothing and the member got silence. Offering the tokens actually moving
    is the closest thing to the autocomplete they expected.
    """
    wanted = _normalise(prefix)
    payload = _warm_payload(board_path)
    scored: list[tuple[float, str]] = []
    for group in payload.get("groups") or []:
        token = str(group.get("token") or "").upper()
        if not token:
            continue
        if wanted and not token.startswith(wanted) and wanted not in token:
            continue
        edge = group.get("best_edge_pct")
        try:
            score = abs(float(edge))
        except (TypeError, ValueError):
            score = 0.0
        scored.append((score, token))
    # Prefixes first, then contains, each by size of edge.
    starts = sorted((s for s in scored if s[1].startswith(wanted)), key=lambda x: -x[0])
    rest = sorted((s for s in scored if not s[1].startswith(wanted)), key=lambda x: -x[0])
    ordered: list[str] = []
    for _score, token in starts + rest:
        if token not in ordered:
            ordered.append(token)
        if len(ordered) >= SUGGESTION_LIMIT:
            break
    return ordered


def suggestion_keyboard(symbols: list[str], *, public_url: str = "") -> dict[str, Any]:
    rows: list[list[dict[str, Any]]] = []
    row: list[dict[str, Any]] = []
    for symbol in symbols:
        row.append({"text": symbol, "callback_data": f"t:{symbol}"[:64]})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if public_url:
        rows.append([{"text": "Open the board", "url": f"{public_url.rstrip('/')}/markets"}])
    return {"inline_keyboard": rows}


def _rows_for(symbol: str, board_path: Path | str) -> list[dict[str, Any]]:
    """Routes for one token, from the same feed the website renders.

    The website serves api_spreads (api_discovery_latest.json); board.jsonl is a
    separate, currently-empty legacy source. Reading the wrong one makes the bot
    disagree with the site, which is worse than being silent.
    """
    # The same warm payload the suggestions use. Passing q=SYMBOL made every
    # lookup its own cache key, so each one paid a full 26s board build and
    # Telegram timed out. Filtering a warm payload costs nothing and, because
    # include_stale/include_unverified match the site's own defaults, the bot
    # still cannot be more permissive than the UI.
    payload = _warm_payload(board_path)
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


def keyboard(query: Query, *, public_url: str = "") -> dict[str, Any]:
    """Buttons to switch view without retyping the token.

    Telegram has no hook for autocompleting a bare "$", so the discoverable
    equivalent is to offer the other views right under the answer.
    """
    others = [(kind, label) for kind, label in VIEW_LABELS.items() if kind != query.kind]
    rows = [[
        {"text": label, "callback_data": f"v:{kind}:{query.symbol}"[:64]}
        for kind, label in others
    ]]
    if public_url:
        rows.append([{
            "text": "Open on SpreadBoard",
            "url": f"{public_url.rstrip('/')}/markets?q={query.symbol}&view=table",
        }])
    return {"inline_keyboard": rows}


def suggest(prefix: str, *, board_path: Path | str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Token suggestions for inline mode, ranked by best edge."""
    # Inline queries share Telegram's short webhook deadline. Rebuilding the
    # full board here took tens of seconds and made autocomplete another source
    # of silent failures. The resident service already maintains this atomic
    # client-visible snapshot; every Telegram entry point must use it.
    payload = _warm_payload(board_path)
    needle = _normalise(prefix)
    out = []
    for group in payload.get("groups") or []:
        token = str(group.get("token") or "").upper()
        if needle and not token.startswith(needle):
            continue
        out.append({
            "token": token,
            "best_edge_pct": group.get("best_edge_pct"),
            "route_count": group.get("route_count"),
            "venues": group.get("venues") or [],
        })
    out.sort(key=lambda r: -(float(r.get("best_edge_pct") or 0)))
    return out[:limit]


def render(query: Query, *, board_path: Path | str, public_url: str = "") -> str:
    """Build the HTML reply for a token lookup."""
    # Normalise here too, not only in parse_query, so render() is safe for any
    # caller and the symbol can never carry markup into an HTML-parsed message.
    symbol = _normalise(query.symbol)
    rows = _rows_for(symbol, board_path)
    if not rows:
        link = ""
        if public_url:
            link = (
                f'\n\n<a href="{escape(public_url.rstrip("/"))}/markets?'
                f'q={escape(symbol)}&amp;include_unverified=1&amp;view=table">'
                "Check audit-only routes on SpreadBoard</a>"
            )
        return (
            f"<b>{escape(symbol)}</b> — query recognised; no parsed routes right now.\n"
            "There are no current client-visible routes. The token may be "
            "unlisted, stale, or available only behind the board's explicit "
            f"unverified/audit filter.{link}"
        )

    if query.kind == "funding":
        rows = sorted(rows, key=lambda r: -abs(float(r.get("funding_daily_pct") or r.get("funding_spread_pct") or 0)))
        body = _table(
            ("ROUTE", "NET/DAY", "APR"), (22, 9, 8),
            [(_route(r), _pct(r.get("funding_daily_pct") or r.get("funding_spread_pct"), 3), _pct(r.get("funding_apr_pct"), 1)) for r in rows[:MAX_ROWS]],
        )
        title = f"{symbol} · funding · {len(rows)} routes"
    elif query.kind == "transfer":
        venues: dict[str, tuple[Any, Any]] = {}
        for r in rows:
            if r.get("long_venue"):
                venues.setdefault(r.get("long_venue"), (r.get("long_deposit_enabled"), r.get("long_withdraw_enabled")))
            if r.get("short_venue"):
                venues.setdefault(r.get("short_venue"), (r.get("short_deposit_enabled"), r.get("short_withdraw_enabled")))
        flag = {True: "open", False: "SHUT", None: "unknown"}
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
        link = f'\n\n<a href="{escape(public_url.rstrip("/"))}/markets?q={escape(symbol)}&amp;view=table">Open full detail on SpreadBoard</a>'
    return (
        f"<b>{escape(title)}</b>\n<pre>{escape(body)}</pre>"
        f"{extra}"
        "\n<i>? = token identity unverified on that route. Research data, not advice.</i>"
        f"{link}"
    )
