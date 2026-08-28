"""Read an optional local community verification JSONL board.

SpreadBoard is deliberately a read-only view over daemon-produced files. It
does not import live executors, place orders, or call private exchange APIs.
"""

from __future__ import annotations

import json
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BOARD_PATH = ROOT / "runtime/community_verification.jsonl"
DEFAULT_WEBSITE_STORAGE_STATE_PATH = ROOT / "runtime/community_session.json"
DEFAULT_PREMIUM_STORAGE_STATE_PATH = DEFAULT_WEBSITE_STORAGE_STATE_PATH
DEFAULT_MAX_LINES = 5000
DEFAULT_FRESH_MAX_AGE_MIN = 120.0


@dataclass(frozen=True)
class RouteKind:
    kind: str
    label: str
    tab: str
    market_types: tuple[str, str]
    requires_session: bool = False


ROUTE_KINDS: tuple[RouteKind, ...] = (
    RouteKind("FUTURES", "Futures-Futures", "Futures", ("Futures", "Futures")),
    RouteKind("SPOT-FUTURES", "Spot-Futures", "Spot-Futures", ("Spot", "Futures")),
    RouteKind("FUTURES-SPOT", "Futures-Spot", "Futures-Spot", ("Futures", "Spot"), True),
    RouteKind("SPOT", "Spot-Spot", "Spot", ("Spot", "Spot")),
    RouteKind("DEX-FUTURES", "Futures-DEX", "Futures-Dex", ("Futures", "Dex"), True),
    RouteKind("DEX-SPOT", "Spot-DEX", "Spot-Dex", ("Spot", "Dex"), True),
)
ROUTE_KIND_BY_KIND = {item.kind: item for item in ROUTE_KINDS}
ALLOWED_KINDS = set(ROUTE_KIND_BY_KIND)
# Keep the complete taxonomy readable for retained history and old signed chart
# links, while exposing only the three current research families in navigation
# and health contracts.
PUBLIC_ROUTE_KINDS: tuple[RouteKind, ...] = tuple(
    item for item in ROUTE_KINDS if item.kind not in {"SPOT", "DEX-SPOT"}
)


@dataclass(frozen=True)
class BoardRow:
    symbol: str
    kind: str
    route_key: str
    route_label: str
    route_direction: str
    spread_pct: float
    depth_weighted_spread_pct: float | None
    displayed_open_spread_pct: float | None
    displayed_headline_spread_pct: float | None
    funding_spread_pct: float | None
    funding_apr_pct: float | None
    long_venue: str | None
    long_market_type: str | None
    long_price: float | None
    long_mark: float | None
    long_depth_usd: float | None
    long_funding_pct: float | None
    long_funding_24h_pct: float | None
    long_deposit_enabled: bool | None
    long_withdraw_enabled: bool | None
    short_venue: str | None
    short_market_type: str | None
    short_price: float | None
    short_mark: float | None
    short_depth_usd: float | None
    short_funding_pct: float | None
    short_funding_24h_pct: float | None
    short_deposit_enabled: bool | None
    short_withdraw_enabled: bool | None
    depth_usd: float
    source_tab: str | None
    source_url: str | None
    chart_url: str | None
    strategy_verdict: str | None
    next_action: str | None
    blockers: list[str]
    daily_pct: float | None
    seven_day_pct: float | None
    thirty_day_pct: float | None
    observed_at_us: int | None
    ingested_at_us: int | None
    age_min: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BoardSnapshot:
    rows: list[BoardRow]
    stale_rows: list[BoardRow]
    age_min: float | None
    newest_ingested_at_us: int | None
    source_path: str
    max_age_min: float | None
    fresh_count: int
    stale_count: int
    kind_counts: dict[str, int]
    stale_kind_counts: dict[str, int]
    total_count: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.to_dict() for row in self.rows],
            "stale_rows": [row.to_dict() for row in self.stale_rows],
            "age_min": self.age_min,
            "newest_ingested_at_us": self.newest_ingested_at_us,
            "source_path": self.source_path,
            "max_age_min": self.max_age_min,
            "fresh_count": self.fresh_count,
            "stale_count": self.stale_count,
            "kind_counts": self.kind_counts,
            "stale_kind_counts": self.stale_kind_counts,
            "total_count": self.total_count,
            "error": self.error,
        }


def load_board(
    path: Path | str = DEFAULT_BOARD_PATH,
    *,
    kind: str | None = None,
    q: str | None = None,
    exchange: str | None = None,
    min_open_spread_pct: float | None = None,
    max_age_min: float | None = DEFAULT_FRESH_MAX_AGE_MIN,
    include_stale: bool = False,
    limit: int | None = None,
    max_lines: int = DEFAULT_MAX_LINES,
    now: float | None = None,
) -> BoardSnapshot:
    """Return latest-per-route rows, split into fresh and stale buckets."""

    source = Path(path)
    current_time = time.time() if now is None else now
    try:
        lines = deque(_iter_lines(source), maxlen=max_lines)
    except OSError as exc:
        return BoardSnapshot(
            rows=[],
            stale_rows=[],
            age_min=None,
            newest_ingested_at_us=None,
            source_path=str(source),
            max_age_min=max_age_min,
            fresh_count=0,
            stale_count=0,
            kind_counts={},
            stale_kind_counts={},
            total_count=0,
            error=str(exc),
        )

    latest: dict[str, BoardRow] = {}
    newest_ingested_at_us: int | None = None

    for line in lines:
        row, ingested_at_us = _row_from_line(line, now=current_time)
        if ingested_at_us is not None:
            newest_ingested_at_us = max(newest_ingested_at_us or ingested_at_us, ingested_at_us)
        if row is None:
            continue
        previous = latest.get(row.route_key)
        if previous is None or _row_sort_time(row) >= _row_sort_time(previous):
            latest[row.route_key] = row

    filtered = _apply_filters(
        latest.values(),
        kind=kind,
        q=q,
        exchange=exchange,
        min_open_spread_pct=min_open_spread_pct,
    )
    filtered = [row for row in filtered if not _is_tiny_depth_mirage(row)]
    filtered.sort(key=_row_rank, reverse=True)

    fresh_rows: list[BoardRow] = []
    stale_rows: list[BoardRow] = []
    for row in filtered:
        if _is_stale(row, max_age_min):
            stale_rows.append(row)
        else:
            fresh_rows.append(row)

    visible_rows = filtered if include_stale else fresh_rows
    if limit is not None:
        visible_rows = visible_rows[: max(0, limit)]
        stale_rows = stale_rows[: max(0, limit)]

    return BoardSnapshot(
        rows=visible_rows,
        stale_rows=stale_rows,
        age_min=_age_min(current_time, newest_ingested_at_us),
        newest_ingested_at_us=newest_ingested_at_us,
        source_path=str(source),
        max_age_min=max_age_min,
        fresh_count=len(fresh_rows),
        stale_count=len(stale_rows),
        kind_counts=dict(sorted(Counter(row.kind for row in fresh_rows).items())),
        stale_kind_counts=dict(sorted(Counter(row.kind for row in stale_rows).items())),
        total_count=len(filtered),
        error=None,
    )


def find_route(
    route_key: str,
    path: Path | str = DEFAULT_BOARD_PATH,
    *,
    now: float | None = None,
) -> BoardRow | None:
    snapshot = load_board(path, include_stale=True, max_age_min=None, limit=None, now=now)
    return next((row for row in snapshot.rows if row.route_key == route_key), None)


def load_history(
    path: Path | str = DEFAULT_BOARD_PATH,
    *,
    route_key: str | None = None,
    symbol: str | None = None,
    kind: str | None = None,
    max_points: int | None = 240,
    max_lines: int = DEFAULT_MAX_LINES,
    now: float | None = None,
) -> list[BoardRow]:
    """Return normalized historical rows from the local board JSONL source."""

    source = Path(path)
    current_time = time.time() if now is None else now
    try:
        lines = deque(_iter_lines(source), maxlen=max_lines)
    except OSError:
        return []
    target_route = str(route_key or "")
    target_symbol = str(symbol or "").upper().strip()
    target_kind = str(kind or "").upper().strip()
    rows: list[BoardRow] = []
    for line in lines:
        row, _ = _row_from_line(line, now=current_time)
        if row is None or _is_tiny_depth_mirage(row):
            continue
        if target_route and row.route_key != target_route:
            continue
        if target_symbol and row.symbol != target_symbol:
            continue
        if target_kind and row.kind != target_kind:
            continue
        rows.append(row)
    rows.sort(key=_row_sort_time)
    if max_points is not None and max_points >= 0:
        rows = rows[-max_points:]
    return rows


def build_source_health(
    path: Path | str = DEFAULT_BOARD_PATH,
    *,
    config: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    config = config or {}
    website_session_path = Path(
        str(
            config.get("website_storage_state_path")
            or config.get("premium_storage_state_path")
            or DEFAULT_WEBSITE_STORAGE_STATE_PATH
        )
    ).expanduser()
    website_session_exists = website_session_path.exists()
    snapshot = load_board(path, include_stale=True, max_age_min=None, now=now)
    fresh_snapshot = load_board(path, include_stale=False, now=now)
    by_kind = _rows_by_kind(snapshot.rows)
    fresh_by_kind = _rows_by_kind(fresh_snapshot.rows)
    tabs = []
    for meta in PUBLIC_ROUTE_KINDS:
        rows = by_kind.get(meta.kind, [])
        fresh_rows = fresh_by_kind.get(meta.kind, [])
        newest_age = min(
            (row.age_min for row in rows if row.age_min is not None),
            default=None,
        )
        status = "ok" if fresh_rows else "empty"
        detail = "fresh rows available" if fresh_rows else "no rows in local source"
        if rows and not fresh_rows:
            status = "stale"
            detail = "only stale rows in local source"
        if meta.requires_session and not rows:
            status = "unavailable"
            detail = (
                "session source exists but this tab has no captured rows"
                if website_session_exists
                else "no local source rows captured for this tab"
            )
        tabs.append(
            {
                "kind": meta.kind,
                "label": meta.label,
                "tab": meta.tab,
                "source_requires_session": meta.requires_session,
                "status": status,
                "detail": detail,
                "row_count": len(rows),
                "fresh_row_count": len(fresh_rows),
                "newest_age_min": newest_age,
            }
        )
    return {
        "ok": snapshot.error is None,
        "source_path": snapshot.source_path,
        "source_error": snapshot.error,
        "board_age_min": snapshot.age_min,
        "fresh_count": fresh_snapshot.fresh_count,
        "stale_count": fresh_snapshot.stale_count,
        "website_session": {
            "configured": website_session_exists,
            "path": str(website_session_path),
            "note": "storage-state file only; no password is stored in SpreadBoard",
        },
        "premium_session": {
            "configured": website_session_exists,
            "path": str(website_session_path),
            "note": "legacy alias for website_session",
        },
        "tabs": tabs,
    }


def route_kind_options() -> list[dict[str, Any]]:
    return [asdict(item) for item in PUBLIC_ROUTE_KINDS]


def kind_label(kind: str | None) -> str:
    return ROUTE_KIND_BY_KIND.get(str(kind or "").upper(), RouteKind("", str(kind or ""), "", ("", ""))).label


def _row_from_line(line: str, *, now: float) -> tuple[BoardRow | None, int | None]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    ingested_at_us = _int_or_none(event.get("ingested_at_us"))
    result = event.get("result") or {}
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None, ingested_at_us
    row_kind = str(result.get("kind") or "").upper()
    if row_kind not in ALLOWED_KINDS:
        return None, ingested_at_us
    symbol = str(result.get("symbol") or "").strip().upper()
    spread = _float_or_none(result.get("api_executable_spread_pct"), result.get("public_edge_pct"))
    if not symbol or spread is None:
        return None, ingested_at_us

    quote_data = result.get("quote") if isinstance(result.get("quote"), dict) else {}
    route = _route_dict(result)
    digest = result.get("raw_strategy_digest") if isinstance(result.get("raw_strategy_digest"), dict) else {}
    strategy = result.get("strategy") if isinstance(result.get("strategy"), dict) else {}
    transfer_rails = result.get("transfer_rails") if isinstance(result.get("transfer_rails"), dict) else {}
    source_tab = _str_or_none(result.get("source_tab") or event.get("source_tab"))
    long_market_type = _str_or_none(
        quote_data.get("long_market_type") or route.get("long_market_type")
    )
    short_market_type = _str_or_none(
        quote_data.get("short_market_type") or route.get("short_market_type")
    )
    long_venue = _str_or_none(quote_data.get("long_venue") or route.get("long_venue"))
    short_venue = _str_or_none(quote_data.get("short_venue") or route.get("short_venue"))
    route_key = _route_key(
        symbol=symbol,
        kind=row_kind,
        long_venue=long_venue,
        long_market_type=long_market_type,
        short_venue=short_venue,
        short_market_type=short_market_type,
        source_tab=source_tab,
    )
    long_deposit, long_withdraw, short_deposit, short_withdraw = _rail_statuses(
        digest=digest,
        transfer_rails=transfer_rails,
        long_venue=long_venue,
        short_venue=short_venue,
    )
    source_url = _str_or_none(event.get("source_url") or result.get("source_url"))
    return (
        BoardRow(
            symbol=symbol,
            kind=row_kind,
            route_key=route_key,
            route_label=kind_label(row_kind),
            route_direction=_route_direction(long_market_type, short_market_type),
            spread_pct=spread,
            depth_weighted_spread_pct=_float_or_none(
                result.get("api_depth_weighted_spread_pct"),
                quote_data.get("depth_weighted_spread_pct"),
            ),
            displayed_open_spread_pct=_float_or_none(
                result.get("displayed_open_spread_pct"),
                result.get("open_spread_pct"),
                digest.get("open_spread_pct"),
            ),
            displayed_headline_spread_pct=_float_or_none(
                result.get("displayed_headline_spread_pct"),
                result.get("displayed_open_spread_pct"),
                digest.get("spread_pct"),
            ),
            funding_spread_pct=_float_or_none(
                result.get("funding_spread_pct"),
                digest.get("funding_spread_pct"),
            ),
            funding_apr_pct=_float_or_none(
                result.get("funding_spread_apr_pct"),
                digest.get("funding_spread_apr_pct"),
            ),
            long_venue=long_venue,
            long_market_type=long_market_type,
            long_price=_float_or_none(
                quote_data.get("long_ask_vwap"),
                quote_data.get("long_ask"),
                quote_data.get("long_mark"),
            ),
            long_mark=_float_or_none(quote_data.get("long_mark")),
            long_depth_usd=_float_or_none(quote_data.get("long_top_depth_usd")),
            long_funding_pct=_fraction_to_pct(_float_or_none(quote_data.get("long_funding"))),
            long_funding_24h_pct=None,
            long_deposit_enabled=long_deposit,
            long_withdraw_enabled=long_withdraw,
            short_venue=short_venue,
            short_market_type=short_market_type,
            short_price=_float_or_none(
                quote_data.get("short_bid_vwap"),
                quote_data.get("short_bid"),
                quote_data.get("short_mark"),
            ),
            short_mark=_float_or_none(quote_data.get("short_mark")),
            short_depth_usd=_float_or_none(quote_data.get("short_top_depth_usd")),
            short_funding_pct=_fraction_to_pct(_float_or_none(quote_data.get("short_funding"))),
            short_funding_24h_pct=None,
            short_deposit_enabled=short_deposit,
            short_withdraw_enabled=short_withdraw,
            depth_usd=_depth_usd(quote_data),
            source_tab=source_tab,
            source_url=source_url,
            chart_url=_chart_url(source_url, symbol, long_venue, long_market_type, short_venue, short_market_type),
            strategy_verdict=_str_or_none(strategy.get("verdict")),
            next_action=_str_or_none(digest.get("next_action")),
            blockers=_string_list(strategy.get("blockers")) or _string_list(digest.get("blockers")),
            daily_pct=None,
            seven_day_pct=None,
            thirty_day_pct=None,
            observed_at_us=_int_or_none(event.get("observed_at_us") or result.get("observed_at_us")),
            ingested_at_us=ingested_at_us,
            age_min=_age_min(now, ingested_at_us),
        ),
        ingested_at_us,
    )


def _route_dict(result: dict[str, Any]) -> dict[str, Any]:
    route = result.get("route")
    if isinstance(route, dict):
        return route
    digest = result.get("raw_strategy_digest")
    if isinstance(digest, dict) and isinstance(digest.get("route"), dict):
        return digest["route"]
    return {}


def _rail_statuses(
    *,
    digest: dict[str, Any],
    transfer_rails: dict[str, Any],
    long_venue: str | None,
    short_venue: str | None,
) -> tuple[bool | None, bool | None, bool | None, bool | None]:
    rows = digest.get("exchange_rows") if isinstance(digest.get("exchange_rows"), list) else []
    long_row = _exchange_row_for_venue(rows, long_venue)
    short_row = _exchange_row_for_venue(rows, short_venue)
    long_deposit = _bool_or_none(long_row.get("deposit_enabled") if long_row else None)
    long_withdraw = _bool_or_none(long_row.get("withdraw_enabled") if long_row else None)
    short_deposit = _bool_or_none(short_row.get("deposit_enabled") if short_row else None)
    short_withdraw = _bool_or_none(short_row.get("withdraw_enabled") if short_row else None)
    if long_withdraw is None:
        long_withdraw = _bool_or_none(transfer_rails.get("source_withdraw_enabled"))
    if short_deposit is None:
        short_deposit = _bool_or_none(transfer_rails.get("destination_deposit_enabled"))
    return long_deposit, long_withdraw, short_deposit, short_withdraw


def _exchange_row_for_venue(rows: list[Any], venue: str | None) -> dict[str, Any] | None:
    target = str(venue or "").casefold()
    return next(
        (
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("exchange") or "").casefold() == target
        ),
        None,
    )


def _apply_filters(
    rows: Any,
    *,
    kind: str | None,
    q: str | None,
    exchange: str | None,
    min_open_spread_pct: float | None,
) -> list[BoardRow]:
    kind_filter = str(kind or "").upper().strip()
    q_filter = str(q or "").upper().strip()
    exchange_filter = str(exchange or "").casefold().strip()
    output = []
    for row in rows:
        if kind_filter and row.kind != kind_filter:
            continue
        if q_filter and q_filter not in row.symbol:
            continue
        if exchange_filter and exchange_filter not in " ".join(
            part.casefold()
            for part in (row.long_venue or "", row.short_venue or "")
        ):
            continue
        if min_open_spread_pct is not None and _spread_for_filter(row) < min_open_spread_pct:
            continue
        output.append(row)
    return output


def _is_tiny_depth_mirage(row: BoardRow) -> bool:
    return row.spread_pct > 40.0 and row.depth_usd < 500.0


def _is_stale(row: BoardRow, max_age_min: float | None) -> bool:
    return (
        max_age_min is not None
        and row.age_min is not None
        and row.age_min > float(max_age_min)
    )


def _row_rank(row: BoardRow) -> tuple[float, float, float]:
    return (
        _spread_for_filter(row),
        abs(row.funding_apr_pct or 0.0),
        row.depth_usd,
    )


def _row_sort_time(row: BoardRow) -> int:
    return int(row.ingested_at_us or row.observed_at_us or 0)


def _spread_for_filter(row: BoardRow) -> float:
    return abs(row.displayed_open_spread_pct if row.displayed_open_spread_pct is not None else row.spread_pct)


def _route_key(
    *,
    symbol: str,
    kind: str,
    long_venue: str | None,
    long_market_type: str | None,
    short_venue: str | None,
    short_market_type: str | None,
    source_tab: str | None,
) -> str:
    return "|".join(
        [
            symbol,
            kind,
            long_venue or "?",
            long_market_type or "?",
            short_venue or "?",
            short_market_type or "?",
            source_tab or "?",
        ]
    )


def route_key_url(route_key: str) -> str:
    return quote(route_key, safe="")


def _route_direction(long_market_type: str | None, short_market_type: str | None) -> str:
    left = long_market_type or "?"
    right = short_market_type or "?"
    return f"{left} -> {right}"


def _depth_usd(quote_data: dict[str, Any]) -> float:
    depths = [
        _float_or_none(quote_data.get("long_top_depth_usd")),
        _float_or_none(quote_data.get("short_top_depth_usd")),
    ]
    present = [item for item in depths if item is not None]
    return min(present) if present else 0.0


def _chart_url(
    source_url: str | None,
    symbol: str,
    long_venue: str | None,
    long_market_type: str | None,
    short_venue: str | None,
    short_market_type: str | None,
) -> str | None:
    if not all([symbol, long_venue, long_market_type, short_venue, short_market_type]):
        return None
    chart_id = "~".join(
        [symbol, long_venue or "", long_market_type or "", short_venue or "", short_market_type or ""]
    )
    return f"/charts?charts={quote(chart_id, safe='~')}"


def _rows_by_kind(rows: list[BoardRow]) -> dict[str, list[BoardRow]]:
    output: dict[str, list[BoardRow]] = {}
    for row in rows:
        output.setdefault(row.kind, []).append(row)
    return output


def _iter_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as handle:
        return handle.read().splitlines()


def _age_min(now: float, ingested_at_us: int | None) -> float | None:
    if ingested_at_us is None:
        return None
    return max(0.0, (now - ingested_at_us / 1_000_000) / 60.0)


def _fraction_to_pct(value: float | None) -> float | None:
    return None if value is None else value * 100.0


def _float_or_none(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:
            return number
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return []
