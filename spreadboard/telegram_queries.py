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

from . import api_spreads, funding_radar, research_score

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
}
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
        if not symbol and COMMANDS[head] == "radar":
            return Query(kind="radar", symbol="")
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
    groups: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[tuple[Any, ...]]] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            raise TypeError("telegram_funding_payload_must_be_a_mapping")
        if not isinstance(payload.get("groups"), list):
            raise TypeError("telegram_funding_payload_groups_must_be_a_list")
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
    global FUNDING_QUERY, _FUNDING_QUERY_UPDATED_AT
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
        ready = bool(WARM_QUERY)
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
    return rows


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
                _pct(row.get("executable_spread_pct"), 2),
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
    rows = [] if query.kind == "radar" else [[
        {"text": label, "callback_data": f"v:{kind}:{query.symbol}"[:64]}
        for kind, label in others
    ]]
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
    # Normalise here too, not only in parse_query, so render() is safe for any
    # caller and the symbol can never carry markup into an HTML-parsed message.
    symbol = _normalise(query.symbol)
    rows = _rows_for(symbol, board_path)
    funding_rows = _funding_rows_for(symbol)
    if query.kind == "funding" and funding_rows:
        rows = funding_rows
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
        basis_context = (
            f"\n<i>Current basis on the top funding pair: "
            f"{_pct(rows[0].get('executable_spread_pct'), 2)}.</i>"
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
            key=lambda r: -(float(r.get("executable_spread_pct") or 0)),
        )
        body = _table(
            ("ROUTE", "EDGE", "DEPTH"), (22, 8, 7),
            [(_route(r), _pct(r.get("executable_spread_pct")), _usd(r.get("depth_usd"))) for r in rows[:MAX_ROWS]],
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
