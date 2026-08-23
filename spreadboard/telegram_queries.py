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

from . import (
    api_spreads,
    catalog_pairs,
    chart_catalog,
    funding_radar,
    research_score,
    token_rankings,
)

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
COMMANDS = {
    "/spread": "spread",
    "/funding": "funding",
    "/transfer": "transfer",
    "/token": "spread",
    "/radar": "radar",
    "/depth": "depth",
    "/calc": "calc",
    # Board-wide, no token needed.
    "/top": "top",
    "/carry": "carry",
    "/deep": "deep",
    "/help": "help",
    "/status": "status",
}

#: Commands that answer about the whole board rather than one token.
BOARDWIDE = {"top", "carry", "deep", "help", "status"}

#: What can follow ``TOKEN/``. Single letters matter: this gets typed on a
#: phone in the middle of a conversation, and ``GUA/f`` is a third of the
#: keystrokes of ``/funding GUA``.
ASPECTS = {
    "": "spread", "s": "spread", "spread": "spread", "basis": "spread",
    "f": "funding", "funding": "funding", "carry": "funding",
    "d": "depth", "depth": "depth", "size": "depth", "liquidity": "depth",
    "t": "transfer", "transfer": "transfer", "rails": "transfer",
    "deposit": "transfer", "withdraw": "transfer",
    "c": "calc", "calc": "calc", "plan": "calc",
    "r": "radar", "radar": "radar",
    "?": "help", "h": "help", "help": "help",
}

#: ``TOKEN/aspect`` -- the token-first form. Anchored at the start of the
#: message on purpose. Mid-sentence matching would answer "what about GUA/f",
#: and with privacy mode off the bot sees every message in the group, so the
#: cost of a loose pattern is a bot that interrupts conversations.
SLASH_QUERY = re.compile(
    rf"^([^\W_][\w.-]{{0,{MAX_SYMBOL_LENGTH - 1}}})/([A-Za-z?]*)(?:\s+(.*))?$",
    re.UNICODE,
)

#: Refused before the pattern is even tried. A pasted link is the common one.
URL_MARKERS = (
    "://", "www.",
    ".com/", ".io/", ".ink/", ".org/", ".net/", ".xyz/", ".app/", ".dev/",
    ".co/", ".ai/", ".fi/", ".exchange/", ".finance/", ".t.me/", "t.me/",
)
#: The views a member can tap between under any token answer. Depth belongs
#: here because "can I actually get size in" is the question that decides
#: whether a wide spread is worth anything.
VIEW_LABELS = {
    "spread": "Spread",
    "funding": "Funding",
    "depth": "Depth",
    "transfer": "Deposits / Withdrawals",
}
# Bare intent words, accepted alongside a cashtag ("$SIREN funding").
KIND_WORDS = {"spread": "spread", "funding": "funding", "transfer": "transfer",
              "rails": "transfer", "deposit": "transfer", "withdraw": "transfer"}


@dataclass(frozen=True)
class Query:
    kind: str
    symbol: str
    #: Free text after the aspect, currently the capital for a sizing request.
    #: Part of the cooldown key: $1,000 and $50,000 are different questions.
    arg: str = ""


_LAST_ANSWERED: dict[tuple[int, str, str], float] = {}
_LOCK = threading.Lock()


def parse_query(text: str, *, bot_username: str = "") -> Query | None:
    """Extract a token lookup from a group message, or None if it is just chat."""
    raw = str(text or "").strip()
    if not raw:
        return None

    # A link contains a slash and a dot and would otherwise look exactly like
    # ``TOKEN/aspect``. Refuse the whole message rather than try to pick it out.
    lowered = raw.casefold()
    if not any(marker in lowered for marker in URL_MARKERS):
        slash = SLASH_QUERY.match(raw)
        if slash:
            symbol_text, aspect_text, argument = slash.groups()
            aspect = ASPECTS.get((aspect_text or "").casefold())
            # An unknown aspect is left alone. That single rule is what keeps
            # "and/or", "n/a" and "3/4" out: none of those trail an aspect we
            # know, and a date's "12/05" has digits where letters must be.
            # A bare "TOKEN/" has to be the entire message. Without this,
            # "w/ the short leg on Gate" is a lookup for token W -- the English
            # abbreviation for "with" is the exact shape of this syntax.
            bare_with_trailing = not aspect_text and bool(str(argument or "").strip())
            if aspect is not None and not bare_with_trailing:
                symbol = _normalise(symbol_text)
                if symbol:
                    return Query(
                        kind=aspect,
                        symbol=symbol,
                        arg=str(argument or "").strip(),
                    )

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

    # No slash at all: "esports funding". The "/" popup is what pastes
    # "@spreadarbitragesubscription_bot" into a supergroup message, and no
    # server setting can stop it, so the way to avoid the tag is to make the
    # slash unnecessary. Exactly two words, one of which must be a token the
    # board is actually carrying -- otherwise every sentence ending in "depth"
    # becomes a lookup.
    plain = raw.split()
    if len(plain) == 2 or (len(plain) == 3 and plain[1].casefold() in ASPECTS):
        first, second = plain[0].lstrip("$"), plain[1]
        for token_text, aspect_text in ((first, second), (second, first)):
            aspect = ASPECTS.get(aspect_text.casefold())
            if aspect is None or not aspect_text:
                continue
            if is_known_token(token_text):
                return Query(
                    kind=aspect,
                    symbol=_normalise(token_text),
                    arg=plain[2] if len(plain) == 3 else "",
                )

    head = words[0] if words else ""
    # "/top", "/help", "/status" and friends describe the board, not a token.
    if head in COMMANDS and COMMANDS[head] in BOARDWIDE:
        _, _, rest = raw.partition(" ")
        return Query(kind=COMMANDS[head], symbol="", arg=rest.strip())
    if head in COMMANDS:
        _, _, rest = raw.partition(" ")
        symbol = rest.strip().lstrip("$").split(" ")[0] if rest.strip() else ""
        # Strip a trailing intent word: "/spread SIREN funding"
        if symbol.casefold() in KIND_WORDS or symbol.casefold() in COMMANDS:
            symbol = ""
        if not symbol and COMMANDS[head] == "radar":
            return Query(kind="radar", symbol="")
        if not symbol:
            # A token-taking command with no token is not nothing: Telegram's
            # popup replaces the compose box, so "ESports" + pick "/funding"
            # arrives here as a bare "/funding". Hand it back with an empty
            # symbol so `resolve` can supply what the chat was discussing.
            return Query(kind=COMMANDS[head], symbol="")
        remainder = rest.strip()
        _, _, after = remainder.partition(" ")
        return Query(
            kind=COMMANDS[head], symbol=_normalise(symbol), arg=after.strip()
        )
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
    key = (int(chat_id), query.symbol, query.kind, query.arg)
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


def _money(value: Any) -> str:
    """Exact dollars. `_usd` abbreviates, which is right for a board column and
    wrong for a figure someone is about to size a position from: "$2K" hides
    the difference between $2,000 and $2,499."""
    if value is None:
        return "--"
    amount = float(value)
    if amount != 0 and abs(amount) < 100:
        return f"${amount:,.2f}"
    return f"${amount:,.0f}"


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _route(row: dict[str, Any]) -> str:
    """Route label, suffixed with ? when identity is unverified.

    Large spreads here are real, so they are shown -- but a member must be able
    to see at a glance which legs have not had their token identity confirmed.
    """
    mark = "?" if row.get("mirage_guarded") else ""
    return f"{row.get('long_venue') or '?'}>{row.get('short_venue') or '?'}{mark}"[:22]


def _current_basis(row: dict[str, Any]) -> Any:
    """A basis is current only while both legs meet the spread-age promise."""

    current = api_spreads.spread_quote_current(row)
    return (
        row.get("depth_weighted_spread_pct")
        if current and row.get("depth_weighted_spread_pct") is not None
        else row.get("executable_spread_pct")
        if current
        else None
    )


def _radar_age(minutes: Any) -> str:
    try:
        value = max(0.0, float(minutes))
    except (TypeError, ValueError):
        return "unknown"
    if value < 60:
        return f"{value:.0f}m"
    if value < 1_440:
        return f"{value / 60:.1f}h"
    return f"{value / 1_440:.1f}d"


def _radar_rows(symbol: str) -> list[dict[str, Any]]:
    rows = funding_radar.routes_for(symbol)
    rows.sort(key=_radar_score, reverse=True)
    return rows


def _radar_score(row: dict[str, Any]) -> float:
    """Compare unlike windows as average settled carry per day."""
    windows = row.get("radar_windows") if isinstance(row.get("radar_windows"), dict) else {}
    scores = []
    for label, days in (("1d", 1), ("7d", 7), ("30d", 30)):
        try:
            scores.append(float(windows[label]) / days)
        except (KeyError, TypeError, ValueError):
            continue
    return max(scores, default=float("-inf"))


def _radar_summary(rows: list[dict[str, Any]]) -> str:
    """Compact settled-history context that fits beneath a Telegram answer."""
    if not rows:
        return ""
    best = rows[0]
    windows = best.get("radar_windows") if isinstance(best.get("radar_windows"), dict) else {}
    outlook = research_score.assess_funding_outlook(best, windows=windows)
    expected = outlook.get("expected_24h_pct")
    outlook_line = ""
    if expected is not None:
        regime = str(outlook.get("regime") or "mixed").replace("_", " ")
        outlook_line = (
            f"\nBlended outlook: {_pct(expected, 3)} per 24h · {escape(regime)}"
            + (" · current/history conflict haircut applied." if outlook.get("regime_conflict") else ".")
        )
    return (
        f"<b>Funding radar</b> · {escape(_route(best))} · last live {_radar_age(best.get('radar_last_seen_age_min'))} ago\n"
        f"Settled: 24h {_pct(windows.get('1d'))} · 7d {_pct(windows.get('7d'))} · 30d {_pct(windows.get('30d'))}; "
        f"last basis {_pct(best.get('executable_spread_pct'))}.{outlook_line}"
    )


def _radar_leaders() -> list[dict[str, Any]]:
    """One retained leader per token, ranked on its best settled window."""
    best_by_token: dict[str, dict[str, Any]] = {}
    for row in funding_radar.routes_for():
        token = str(row.get("token") or "").upper()
        if not token:
            continue
        score = _radar_score(row)
        if score <= 0:
            continue
        current = best_by_token.get(token)
        current_score = float(current["_radar_score"]) if current else float("-inf")
        if score > current_score:
            best_by_token[token] = {**row, "_radar_score": score}
    return sorted(best_by_token.values(), key=lambda row: -float(row.get("_radar_score") or 0.0))


def _render_radar(*, public_url: str = "") -> str:
    leaders = _radar_leaders()
    if not leaders:
        return "<b>Funding radar</b> — history is warming. Try again after the next board refresh."
    lines = []
    for row in leaders[:MAX_ROWS]:
        windows = row.get("radar_windows") or {}
        lines.append(
            (
                str(row.get("token") or "")[:10],
                _pct(windows.get("1d"), 2),
                _pct(windows.get("7d"), 2),
                _pct(windows.get("30d"), 2),
            )
        )
    body = _table(("TOKEN", "24H", "7D", "30D"), (10, 8, 8, 8), lines)
    extra = f"\n<i>Showing top {MAX_ROWS} of {len(leaders)} retained tokens.</i>" if len(leaders) > MAX_ROWS else ""
    link = ""
    if public_url:
        link = f'\n\n<a href="{escape(public_url.rstrip("/"))}/funding?rank=1d">Open the full funding radar</a>'
    return (
        f"<b>Funding radar · last 30 days</b>\n<pre>{escape(body)}</pre>{extra}\n"
        "<i>Settled historical carry. A retained token may be cooled now; re-check the live route before acting.</i>"
        f"{link}"
    )


#: The one query the bot builds from. It is warmed by the service, so a
#: Telegram lookup is a dictionary read rather than a board build.
#:
#: Every lookup used to call load_spreads(q=SYMBOL), which is its own cache key
#: and was never warmed -- so a bare "$" took 36.2s and "$SIREN" 26.3s, and
#: Telegram's webhook gave up long before either returned. The member saw
#: nothing at all, which is exactly what "the $ is not working" looked like.
WARM_QUERY: dict[str, Any] = {}
_WARM_QUERY_UPDATED_AT = 0.0
FUNDING_QUERY: dict[str, Any] = {}
_FUNDING_QUERY_UPDATED_AT = 0.0
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
        # ``api_market_spreads`` deliberately returns this sentinel while a
        # different thread owns the expensive grouping slot. It is neither a
        # completed market generation nor evidence that the market is empty.
        # A background warm used to install it unconditionally and erase the
        # last complete Telegram snapshot; the website recovered on its next
        # request, while every bot command remained stuck behind the warming
        # gate. Preserve the atomic last-complete generation instead.
        source_health = payload.get("source_health")
        canonical = (
            source_health.get("canonical_api") or {}
            if isinstance(source_health, dict)
            else {}
        )
        if not isinstance(canonical, dict):
            canonical = {}
        try:
            canonical_rows = int(float(canonical.get("row_count") or 0))
        except (TypeError, ValueError):
            canonical_rows = 0
        incomplete_empty = not payload.get("groups") and canonical_rows > 0
        if payload.get("status") == "warming" or incomplete_empty:
            return WARM_QUERY
        catalog_pairs.clear_cache()
        # An empty generation is installed like any other: the board can
        # legitimately hold nothing when it is a completed, authoritative
        # answer. `payload_status().ready` is what keeps that empty snapshot
        # from being answered as if it were data.
        WARM_QUERY = payload
        _WARM_QUERY_UPDATED_AT = time.time()
    return payload


def replace_funding_payloads(payloads: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Install the exact live funding rows used by the website's three tabs.

    The ordinary member snapshot intentionally follows the spread board's Now
    filters. A token can therefore cool out of that list while remaining a
    current, book-verified funding pair on /funding. Keeping this second small
    immutable snapshot lets ``$TOKEN funding`` and a cooled ``$TOKEN`` lookup
    show the current rate/basis without weakening the spread-board filters or
    doing exchange work inside Telegram's webhook deadline.
    """
    global FUNDING_QUERY, _FUNDING_QUERY_UPDATED_AT
    groups: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[tuple[Any, ...]]] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            raise TypeError("telegram_funding_payload_must_be_a_mapping")
        if not isinstance(payload.get("groups"), list):
            raise TypeError("telegram_funding_payload_groups_must_be_a_list")
        # Funding is assembled from several lane views. Installing only the
        # lanes that happened to finish would silently remove valid routes and
        # make /funding disagree with the website. Keep the previous complete
        # multi-lane snapshot and let the regular warmer retry the whole set.
        if payload.get("status") == "warming":
            with _WARM_QUERY_LOCK:
                return FUNDING_QUERY
        for original in payload.get("groups") or []:
            if not isinstance(original, dict):
                continue
            token = _normalise(str(original.get("token") or ""))
            if not token:
                continue
            group = groups.setdefault(token, {**original, "token": token, "routes": []})
            route_keys = seen.setdefault(token, set())
            for route in original.get("routes") or []:
                if not isinstance(route, dict):
                    continue
                identity = (
                    route.get("route_key"), route.get("long_venue"),
                    route.get("long_market_symbol"), route.get("short_venue"),
                    route.get("short_market_symbol"),
                )
                if identity in route_keys:
                    continue
                route_keys.add(identity)
                group["routes"].append(route)
    installed = {"groups": list(groups.values())}
    with _WARM_QUERY_LOCK:
        FUNDING_QUERY = installed
        _FUNDING_QUERY_UPDATED_AT = time.time()
    return installed


def _warm_payload(_board_path: Path | str) -> dict[str, Any]:
    """Return the last complete Telegram snapshot without doing heavy work."""
    with _WARM_QUERY_LOCK:
        return WARM_QUERY


def client_visible_payload() -> dict[str, Any]:
    """The last complete all-token snapshot shared by website and Telegram.

    The payload is replaced atomically and treated as immutable.  Watchlist and
    Portfolio can therefore join exact client-visible routes without paying a
    new 20+ second board build or drifting from what ``$TOKEN`` returns.
    """
    with _WARM_QUERY_LOCK:
        return WARM_QUERY


def payload_status(*, now: float | None = None) -> dict[str, Any]:
    moment = time.time() if now is None else float(now)
    with _WARM_QUERY_LOCK:
        # A payload dict with zero groups is still truthy. Treating that as
        # ready let the gate through during the ~150s a deploy takes to warm,
        # and every token then answered "no parsed routes right now" -- which
        # a member reads as "not listed" rather than "not loaded yet".
        ready = bool(WARM_QUERY.get("groups"))
        updated_at = _WARM_QUERY_UPDATED_AT
        groups = WARM_QUERY.get("groups") or []
        token_count = len(groups)
        route_count = sum(len(group.get("routes") or []) for group in groups)
        funding_groups = FUNDING_QUERY.get("groups") or []
        funding_token_count = len(funding_groups)
        funding_route_count = sum(len(group.get("routes") or []) for group in funding_groups)
    return {
        "ready": ready,
        "age_seconds": max(0.0, moment - updated_at) if ready and updated_at else None,
        "token_count": token_count,
        "route_count": route_count,
        "funding_token_count": funding_token_count,
        "funding_route_count": funding_route_count,
    }


def reset_payload() -> None:
    """Test/support hook; production refreshes by replacing, never mutating."""
    global WARM_QUERY, _WARM_QUERY_UPDATED_AT, FUNDING_QUERY, _FUNDING_QUERY_UPDATED_AT
    with _WARM_QUERY_LOCK:
        WARM_QUERY = {}
        _WARM_QUERY_UPDATED_AT = 0.0
        FUNDING_QUERY = {}
        _FUNDING_QUERY_UPDATED_AT = 0.0


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
    # The canonical scanner keeps a bounded number of combinations per token;
    # the full warm catalogue does not. Enrich even a token already present in
    # the warm snapshot; otherwise the first scanner route short-circuited this
    # fallback and `$TOKEN` kept claiming one route while the website correctly
    # showed every complete warm pairing. This reads exact-symbol caches only --
    # no exchange call, no bot timeout, and no stale quote passed off as live.
    payload = catalog_pairs.with_routes(
        catalog_pairs.for_token(symbol, limit=None),
        [*rows, *token_rankings.dex_routes_for(token_rankings.load(), symbol)],
        limit=100,
    )
    return list(payload.get("routes") or [])


def _funding_rows_for(symbol: str) -> list[dict[str, Any]]:
    """Current funding-tab rows, already filtered and warmed by the service."""
    with _WARM_QUERY_LOCK:
        groups = FUNDING_QUERY.get("groups") or []
    rows: list[dict[str, Any]] = []
    for group in groups:
        if str(group.get("token") or "").upper() == symbol:
            rows.extend(route for route in group.get("routes") or [] if isinstance(route, dict))
    return rows


def _current_funding_monitor(
    symbol: str,
    rows: list[dict[str, Any]],
    radar_rows: list[dict[str, Any]],
    *,
    public_url: str = "",
) -> str:
    rows = sorted(
        rows,
        key=lambda row: -abs(float(row.get("funding_daily_pct") or row.get("funding_spread_pct") or 0)),
    )
    body = _table(
        ("ROUTE", "NET/DAY", "BASIS"), (22, 9, 8),
        [
            (
                _route(row),
                _pct(row.get("funding_daily_pct") or row.get("funding_spread_pct"), 3),
                _pct(_current_basis(row), 2),
            )
            for row in rows[:MAX_ROWS]
        ],
    )
    radar = f"\n\n{_radar_summary(radar_rows)}" if radar_rows else ""
    warning = (
        "\n<i>? means token identity is unresolved on that route. Research data, not advice.</i>"
        if any(row.get("mirage_guarded") for row in rows)
        else "\n<i>Re-check live books, rails and carry before acting. Research data, not advice.</i>"
    )
    link = ""
    if public_url:
        link = (
            f'\n\n<a href="{escape(public_url.rstrip("/"))}/funding?'
            f'q={escape(symbol)}">Open current and historical funding on SpreadBoard</a>'
        )
    return (
        f"<b>{escape(symbol)} · current funding monitor · {len(rows)} routes</b>\n"
        "This token cooled out of the spread Now ranking; these are the current funding-tab pairs, not a relaxed spread signal.\n"
        f"<pre>{escape(body)}</pre>{radar}{warning}{link}"
    )


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
    buttons = [
        {"text": label, "callback_data": f"v:{kind}:{query.symbol}"[:64]}
        for kind, label in others
    ]
    # Two per row: four buttons on one row truncate to initials on a phone.
    rows = (
        []
        if query.kind in {"radar", "top", "carry", "deep", "help", "status"}
        or not query.symbol
        else [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    )
    if public_url:
        destination = (
            "/funding?rank=1d"
            if query.kind == "radar"
            else f"/funding?q={query.symbol}&rank=1d"
            if query.kind == "funding"
            else f"/markets?q={query.symbol}&view=table"
        )
        rows.append([{
            "text": "Open on SpreadBoard",
            "url": f"{public_url.rstrip('/')}{destination}",
        }])
        if query.kind in {"spread", "funding"} and query.symbol:
            rows.append([{
                "text": "Personalized margin stress",
                "url": f"{public_url.rstrip('/')}/watchlist#margin-planner",
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
    if query.kind == "radar":
        return _render_radar(public_url=public_url)
    if query.kind in {"top", "carry", "deep"}:
        return _render_leaderboard(query.kind, public_url=public_url)
    if query.kind == "help":
        return _render_help(query.symbol, public_url=public_url)
    if query.kind == "status":
        return _render_status(public_url=public_url)
    # Normalise here too, not only in parse_query, so render() is safe for any
    # caller and the symbol can never carry markup into an HTML-parsed message.
    symbol = _normalise(query.symbol)
    rows = _rows_for(symbol, board_path)
    funding_rows = _funding_rows_for(symbol)
    if query.kind == "funding" and funding_rows:
        rows = funding_rows
    elif query.kind == "spread":
        rows = [row for row in rows if _current_basis(row) is not None]
    if query.kind == "depth":
        return _render_depth(symbol, rows, public_url=public_url)
    if query.kind == "calc":
        return _render_calc(symbol, rows, query.arg, public_url=public_url)
    radar_rows = _radar_rows(symbol) if query.kind in {"spread", "funding"} else []
    if not rows:
        if funding_rows and query.kind == "spread":
            return _current_funding_monitor(
                symbol, funding_rows, radar_rows, public_url=public_url
            )
        if radar_rows:
            summary = _radar_summary(radar_rows)
            link = ""
            if public_url:
                link = (
                    f'\n\n<a href="{escape(public_url.rstrip("/"))}/funding?'
                    f'q={escape(symbol)}&amp;rank=1d">Open the historical radar on SpreadBoard</a>'
                )
            return (
                f"<b>{escape(symbol)} · historical funding radar</b>\n"
                "No client-visible route passes the live Now filters. The retained rate and basis below are the last live observation, not a current entry quote.\n\n"
                f"{summary}\n"
                "<i>Kept for 30 days so a cooled leader stays on the watch radar. Re-check live books, identity, rails, and carry before acting.</i>"
                f"{link}"
            )
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
        current_basis = _current_basis(rows[0])
        basis_context = (
            f"\n<i>Current basis on the top funding pair: "
            f"{_pct(current_basis, 2)}.</i>"
            if current_basis is not None
            else "\n<i>Basis is refreshing; the funding quote remains current.</i>"
        )
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
            key=lambda r: -(float(_current_basis(r) or 0)),
        )
        body = _table(
            ("ROUTE", "EDGE", "DEPTH"), (22, 8, 7),
            [(_route(r), _pct(_current_basis(r)), _usd(r.get("depth_usd"))) for r in rows[:MAX_ROWS]],
        )
        title = f"{symbol} · spread · {len(rows)} routes"
        basis_context = ""

    if query.kind == "transfer":
        basis_context = ""

    total = len(venues) if query.kind == "transfer" else len(rows)
    extra = f"\n<i>Showing top {MAX_ROWS} of {total}.</i>" if total > MAX_ROWS else ""
    link = ""
    if public_url:
        destination = (
            f"/funding?q={escape(symbol)}"
            if query.kind == "funding"
            else f"/markets?q={escape(symbol)}&amp;view=table"
        )
        link = f'\n\n<a href="{escape(public_url.rstrip("/"))}{destination}">Open full detail on SpreadBoard</a>'
    radar = f"\n\n{_radar_summary(radar_rows)}" if radar_rows else ""
    return (
        f"<b>{escape(title)}</b>\n<pre>{escape(body)}</pre>"
        f"{extra}"
        f"{basis_context}"
        "\n<i>? = token identity unverified on that route. Research data, not advice.</i>"
        f"{radar}"
        f"{link}"
    )


# ---------------------------------------------------------------------------
# Board-wide answers and sizing
# ---------------------------------------------------------------------------


#: How people write money. "5k" and "$5,000" are the same request.
def parse_capital(text: str) -> float | None:
    """Read a capital figure the way a member would type it, or None."""
    raw = str(text or "").strip().lower().replace(",", "").replace("$", "").replace("_", "")
    if not raw:
        return None
    multiplier = 1.0
    if raw.endswith("k"):
        multiplier, raw = 1_000.0, raw[:-1]
    elif raw.endswith("m"):
        multiplier, raw = 1_000_000.0, raw[:-1]
    try:
        value = float(raw) * multiplier
    except ValueError:
        return None
    return value if value > 0 else None


def _all_routes() -> list[dict[str, Any]]:
    payload = client_visible_payload()
    return [
        route
        for group in payload.get("groups") or []
        for route in group.get("routes") or []
        if isinstance(route, dict)
    ]


def _proved_depth(row: dict[str, Any]) -> bool:
    """Whether this route walked a real ladder at the board's probe size."""
    return row.get("depth_weighted_spread_pct") is not None


def _spread_of(row: dict[str, Any]) -> float:
    for key in ("depth_weighted_spread_pct", "executable_spread_pct"):
        value = row.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return float("-inf")


def _link(public_url: str, path: str, label: str) -> str:
    if not public_url:
        return ""
    return f'\n\n<a href="{escape(public_url.rstrip("/"))}{path}">{escape(label)}</a>'


def _render_leaderboard(
    kind: str, *, public_url: str = "", limit: int = MAX_ROWS
) -> str:
    """The three "show me the board" answers, which differ only in ranking."""
    rows = _all_routes()
    if not rows:
        return (
            "The live snapshot is still warming. Try again in a minute."
        )
    if kind == "deep":
        rows = [row for row in rows if _proved_depth(row)]
        heading = f"Proven at {_probe_label()} — these walked a real ladder"
        ranker = _spread_of
        note = "Every row here filled the board's probe size. Re-check the book before sending."
    elif kind == "carry":
        rows = [row for row in rows if row.get("funding_daily_pct") is not None]
        heading = "Best paired carry right now"
        def ranker(row: dict[str, Any]) -> float:
            return abs(float(row.get("funding_daily_pct") or 0.0))

        note = "Carry is what the pair pays per day. It is not the entry edge."
    else:
        heading = "Widest spreads right now"
        ranker = _spread_of
        note = (
            f"A wide number is a lead, not a fill. Rows marked <b>unproven</b> "
            f"did not fill {_probe_label()} — check depth before sizing."
        )
    if not rows:
        return f"<b>{escape(heading)}</b>\nNothing qualifies right now."
    # One token, one row. The same asset routed through near-identical venue
    # pairs otherwise fills the whole answer -- live /top spent seven of eight
    # rows on two tokens -- and a leaderboard is for breadth.
    rows = sorted(rows, key=ranker, reverse=True)
    best_per_token: dict[str, dict[str, Any]] = {}
    for row in rows:
        token = str(row.get("token") or "?").upper()
        if token not in best_per_token:
            best_per_token[token] = row
    rows = list(best_per_token.values())[:limit]

    lines: list[tuple[str, ...]] = []
    for row in rows:
        token = str(row.get("token") or "?").upper()
        if kind == "carry":
            lines.append((token, _pct(row.get("funding_daily_pct"), 3), _pct(row.get("funding_apr_pct"), 0)))
        else:
            flag = "" if _proved_depth(row) else " *"
            lines.append((token + flag, _pct(_spread_of(row), 2), _route(row)))
    header = ("TOKEN", "PER DAY", "APR") if kind == "carry" else ("TOKEN", "SPREAD", "ROUTE")
    widths = (12, 9, 8) if kind == "carry" else (12, 9, 22)
    table = _table(header, widths, lines)
    star = "" if kind != "top" or all(_proved_depth(row) for row in rows) else "\n<i>* depth unproven at the probe size.</i>"
    destination = "/funding?rank=1d" if kind == "carry" else "/markets"
    return (
        f"<b>{escape(heading)}</b>\n{table}{star}\n\n<i>{note}</i>"
        f"{_link(public_url, destination, 'Open the full board')}"
    )


def _probe_label() -> str:
    from . import api_spreads

    return f"${api_spreads.LIVE_BOOK_TARGET_NOTIONAL_USD:,.0f}"


def _render_depth(symbol: str, rows: list[dict[str, Any]], *, public_url: str = "") -> str:
    """Can this actually be entered, and at what size."""
    probe = _probe_label()
    if not rows:
        return (
            f"<b>{escape(symbol)} · depth</b>\nNo client-visible route right now."
        )
    proved = [row for row in rows if _proved_depth(row)]
    if not proved:
        best = max(rows, key=_spread_of)
        return (
            f"<b>{escape(symbol)} · depth</b>\n"
            f"No route proved {probe}. The book was too thin to walk at that size, "
            "so the spread shown on the board is top-of-book only.\n\n"
            f"Widest top-of-book: <b>{_pct(best.get('executable_spread_pct'), 2)}</b> on {escape(_route(best))}\n\n"
            "<i>Treat it as a lead. Size down, or check the ladder yourself before sending.</i>"
            + _link(public_url, f"/markets?q={escape(symbol)}&view=table", "Open on SpreadBoard")
        )
    table = _table(
        ("ROUTE", "MATCHED", "TOP"), (22, 9, 8),
        [
            (_route(row), _pct(row.get("depth_weighted_spread_pct"), 2),
             _pct(row.get("executable_spread_pct"), 2))
            for row in sorted(proved, key=_spread_of, reverse=True)[:MAX_ROWS]
        ],
    )
    return (
        f"<b>{escape(symbol)} · depth proven at {probe}</b>\n{table}\n\n"
        f"<i>MATCHED walked the ladder to {probe}. TOP is best bid/ask only, so it is "
        "the more optimistic of the two.</i>"
        + _link(public_url, f"/markets?q={escape(symbol)}&view=table", "Open on SpreadBoard")
    )


def _render_calc(
    symbol: str, rows: list[dict[str, Any]], capital_text: str, *, public_url: str = ""
) -> str:
    """Turn a capital figure into per-leg size and what the carry pays.

    Both legs are funded, so capital splits in half. Quoting a per-day figure
    against the full capital -- rather than one leg's notional -- is the error
    that overstated every return on the portfolio page.
    """
    capital = parse_capital(capital_text)
    if capital is None:
        return (
            f"<b>{escape(symbol)} · sizing</b>\n"
            f"Tell me the capital and I will split it: <code>{escape(symbol)}/calc 5000</code>\n\n"
            "<i>Both legs get funded, so the capital you commit is about twice "
            "one leg's notional.</i>"
        )
    if not rows:
        return (
            f"<b>{escape(symbol)} · sizing</b>\nNo client-visible route right now."
        )
    best = max(rows, key=lambda row: abs(float(row.get("funding_daily_pct") or 0.0)))
    per_leg = capital / 2.0
    daily_pct = _number(best.get("funding_daily_pct"))
    lines = [
        f"Capital committed  <b>{_money(capital)}</b>",
        f"Per leg (1x)       <b>{_money(per_leg)}</b> long + <b>{_money(per_leg)}</b> short",
    ]
    if daily_pct is not None:
        per_day = per_leg * float(daily_pct) / 100.0
        lines.append(f"Carry              <b>{_money(per_day)}</b>/day at {_pct(daily_pct, 3)}")
        lines.append(f"                   {_money(per_day * 30)}/30d · {_pct(best.get('funding_apr_pct'), 1)} APR")
    entry = best.get("depth_weighted_spread_pct")
    if entry is not None:
        lines.append(f"Entry basis        {_pct(entry, 2)} matched at {_probe_label()}")
    body = "\n".join(lines)
    return (
        f"<b>{escape(symbol)} · sizing {_money(capital)}</b>\n"
        f"{escape(_route(best))}\n\n<pre>{body}</pre>\n"
        "<i>Carry is charged on notional, so it is quoted against one leg. "
        "Funding rates move; this is the current rate, not a promise.</i>"
        + _link(public_url, f"/markets?q={escape(symbol)}&view=table", "Open on SpreadBoard")
    )


def _render_help(symbol: str = "", *, public_url: str = "") -> str:
    """Everything the bot answers, written as things you would actually type."""
    # A fixed example reads as the only token that works, which is exactly the
    # conclusion the operator drew. Show something currently on the board.
    if symbol:
        example = escape(symbol.upper())
    else:
        leaders = sorted(
            (
                group
                for group in (client_visible_payload().get("groups") or [])
                if group.get("token")
            ),
            key=lambda group: -(float(group.get("best_edge_pct") or 0.0)),
        )
        example = escape(str(leaders[0]["token"]).upper()) if leaders else "ESPORTS"
    return (
        "<b>Ask me anything on the board — no need to tag me.</b>\n\n"
        f"<b>Token first</b> (fastest on a phone)\n"
        f"<code>{example}/</code>          spread and best route\n"
        f"<code>{example}/f</code>         funding — what the pair pays per day\n"
        f"<code>{example}/d</code>         depth — can you actually get size in\n"
        f"<code>{example}/t</code>         transfer rails — deposits and withdrawals\n"
        f"<code>{example}/c 5000</code>    sizing — splits capital across both legs\n"
        f"<code>{example}/?</code>         this help, for that token\n\n"
        "<b>Whole board</b>\n"
        "<code>/top</code>       widest spreads right now\n"
        "<code>/deep</code>      only routes that proved the probe size\n"
        "<code>/carry</code>     best paired carry per day\n"
        "<code>/radar</code>     historical funding leaders\n"
        "<code>/status</code>    how fresh the data is\n\n"
        f"<b>Or no slash at all</b>\n"
        f"<code>{example} funding</code>  ·  <code>{example} depth</code>  ·  "
        f"<code>{example} calc 5000</code>\n"
        f"Either word order. Typing a token on its own also sets the subject, so "
        f"a following <code>/funding</code> knows what you meant.\n\n"
        f"<code>${example}</code> still works, and so does <code>/spread {example}</code>.\n"
        f"Every token on the board answers these, not just this one.\n\n"
        "<i>Spread is the entry edge. Carry is what the pair pays while you hold. "
        f"Depth means the book actually filled {_probe_label()} — without it a wide "
        "number is only a lead.</i>"
        + _link(public_url, "/markets", "Open the board")
    )


def _render_status(*, public_url: str = "") -> str:
    snapshot = payload_status()
    age = snapshot.get("age_seconds")
    age_text = "unknown" if age is None else f"{float(age):.0f}s ago"
    state = "live" if snapshot.get("ready") else "warming"
    return (
        "<b>Data status</b>\n"
        f"<pre>Snapshot   {state}\n"
        f"Updated    {age_text}\n"
        f"Tokens     {snapshot.get('token_count')}\n"
        f"Routes     {snapshot.get('route_count')}\n"
        f"Funding    {snapshot.get('funding_token_count')} tokens</pre>\n"
        f"<i>Probe size {_probe_label()}. Numbers here are the same ones the website shows.</i>"
        + _link(public_url, "/status", "Open status")
    )


# ---------------------------------------------------------------------------
# Remembering what the chat is talking about
# ---------------------------------------------------------------------------

#: How long a chat's last token stays the implied subject. Long enough to type
#: a follow-up, short enough that tomorrow's "/funding" is not answered with
#: yesterday's token.
CONTEXT_TTL_SECONDS = 600.0

_CHAT_TOKEN: dict[int, tuple[str, float]] = {}
_CONTEXT_LOCK = threading.Lock()


def remember_token(chat_id: int, symbol: str, *, now: float | None = None) -> None:
    """Record the token a chat is currently discussing."""
    token = _normalise(symbol)
    if not token:
        return
    with _CONTEXT_LOCK:
        _CHAT_TOKEN[int(chat_id)] = (token, time.time() if now is None else float(now))


def recall_token(chat_id: int, *, now: float | None = None) -> str:
    """The token this chat last discussed, if it is still recent."""
    moment = time.time() if now is None else float(now)
    with _CONTEXT_LOCK:
        entry = _CHAT_TOKEN.get(int(chat_id))
    if not entry:
        return ""
    token, stamp = entry
    return token if moment - stamp <= CONTEXT_TTL_SECONDS else ""


def reset_context() -> None:
    with _CONTEXT_LOCK:
        _CHAT_TOKEN.clear()


_CATALOG_TOKENS: dict[str, Any] = {"key": None, "tokens": frozenset()}


def known_tokens() -> set[str]:
    """Every token the bot can actually answer about.

    Not just the snapshot. That is installed from the website's default view,
    which is capped at 500 groups, while the board carries over a thousand
    tokens -- and `_rows_for` reaches all of them through the pair catalogue.
    Gating on the snapshot alone made "funding CHZ" silent while "CHZ/d"
    answered, which is the kind of inconsistency nobody can be expected to
    predict.

    The catalogue half is cached against the catalogue's own identity, so this
    stays cheap enough for a webhook.
    """
    tokens = {
        str(group.get("token") or "").upper()
        for group in client_visible_payload().get("groups") or []
        if group.get("token")
    }
    try:
        catalog = chart_catalog.load()
    except Exception:  # noqa: BLE001 - a lookup must never break the webhook.
        return tokens
    key = f"{catalog.get('generated_at')}|{catalog.get('count')}"
    if _CATALOG_TOKENS["key"] != key:
        _CATALOG_TOKENS["tokens"] = frozenset(
            str(market.get("token") or "").upper()
            for market in catalog.get("markets") or []
            if market.get("token")
        )
        _CATALOG_TOKENS["key"] = key
    return tokens | set(_CATALOG_TOKENS["tokens"])


def is_known_token(symbol: str) -> bool:
    return _normalise(symbol) in known_tokens()


def note_message(chat_id: int, text: str, *, now: float | None = None) -> None:
    """Set the chat's subject when a message is exactly one listed token.

    Deliberately narrow. Scraping tokens out of ordinary sentences would mean
    "the funding on BTW looked good" silently redirects the next bare command,
    and the member would have no idea why they got the wrong asset.
    """
    raw = str(text or "").strip().lstrip("$")
    if not raw or " " in raw:
        return
    if is_known_token(raw):
        remember_token(chat_id, raw, now=now)


def resolve(
    query: Query | None, *, chat_id: int, now: float | None = None
) -> Query | None:
    """Fill in a token the member did not repeat.

    Telegram's command popup replaces the whole compose box, so a member who
    types "ESports" and then picks "/funding" actually sends the single word
    "/funding". The token is not missing because they forgot it; it is missing
    because the client threw it away.
    """
    if query is None:
        return None
    # "radar" is both: with a token it scopes to that token, without one it is
    # the retained-leaders board. Demanding a token for the bare form turned a
    # working command into a "which token?" prompt.
    if query.symbol or query.kind in BOARDWIDE or query.kind == "radar":
        return query
    remembered = recall_token(chat_id, now=now)
    if not remembered:
        return None
    return Query(kind=query.kind, symbol=remembered, arg=query.arg)


def needs_token_prompt(query: Query) -> str:
    """What to say when a command arrived with no token and no context."""
    label = VIEW_LABELS.get(query.kind, query.kind).lower()
    return (
        f"Which token? Send <code>ESPORTS/{query.kind[0]}</code> for {label}, "
        f"or just <code>ESPORTS {query.kind}</code> — no slash needed.\n\n"
        "Tap one below, or <code>/top</code> for what is widest right now."
    )
