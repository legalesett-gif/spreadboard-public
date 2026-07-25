"""Read-only Community Intel aggregation for SpreadBoard.

This module only reads local community and SpreadBoard artifacts. It does not
call private exchange APIs, executors, notification senders, or wallet code.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from spreadboard import board

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "runtime/community"
DEFAULT_EVENTS_PATH = DATA_DIR / "telegram_events.jsonl"
DEFAULT_BRIEF_DIR = DATA_DIR / "hourly_topic_briefs"
DEFAULT_PREFLIGHT_CANDIDATES_PATH = DATA_DIR / "preflight_candidates.jsonl"
DEFAULT_STRATEGY_QUEUE_PATH = DATA_DIR / "strategy_review_queue.jsonl"
DEFAULT_STRATEGY_PROMPTS_PATH = DATA_DIR / "strategy_prompts.jsonl"
DEFAULT_PRIVATE_PREFLIGHT_PATH = DATA_DIR / "strategy_private_preflight_results.jsonl"
DEFAULT_DIGEST_PATH = DATA_DIR / "strategy_digest.jsonl"
DEFAULT_SOURCE_FILES = {
    "telegram_events": DEFAULT_EVENTS_PATH,
    "preflight_candidates": DEFAULT_PREFLIGHT_CANDIDATES_PATH,
    "strategy_review_queue": DEFAULT_STRATEGY_QUEUE_PATH,
    "strategy_prompts": DEFAULT_STRATEGY_PROMPTS_PATH,
    "private_preflight": DEFAULT_PRIVATE_PREFLIGHT_PATH,
    "website_digest": DEFAULT_DIGEST_PATH,
}
DEFAULT_WINDOW_HOURS = 12.0
DEFAULT_LIMIT = 12
ALERT_EVENT_FRESH_MAX_AGE_MIN = 60.0
INTEL_ALLOWED_TOPIC_IDS = frozenset({None, 14})
MAX_TEXT = 220
SCOREBOARD_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_-]{1,23})\s+([+-]?\d+(?:\.\d+)?)\b")
SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|password|private[_-]?key|token)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\b(authorization\s*:\s*bearer)\s+([^\s,;]+)"),
)
CHART_SYMBOL_PATTERN = re.compile(r"(?i)[?&]charts=([A-Z0-9_-]{2,24})~")
TAGGED_SYMBOL_PATTERN = re.compile(r"[$#]([A-Z][A-Z0-9_-]{1,23})\b")
UPPER_SYMBOL_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_-]{2,15})\b")
SYMBOL_STOPWORDS = {
    "API",
    "APR",
    "DEX",
    "FUND",
    "FUNDING",
    "FUTURES",
    "LONG",
    "PNL",
    "SHORT",
    "SPOT",
    "USDC",
    "USDT",
}

QUESTION_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Pushover / alerts", ("pushover", "alert", "увед", "звонок", "уведом")),
    ("Funding farms", ("funding", "fund", "фанд", "фарм", "farm")),
    ("D/W and transfer rails", ("deposit", "withdraw", "transfer", "contract", "chain", "деп", "вывод", "сеть", "контракт")),
    ("Why not futures-futures", ("why not futures", "futures-futures", "фьюч фьюч", "почему не фьюч")),
    ("Missed spread", ("miss", "missed", "проеб", "спред", "spread gone", "вливал")),
    ("Convergence / exit", ("conver", "exit", "close", "закр", "выход", "уровня", "сход")),
    ("Liquidation / PnL", ("liq", "liquid", "pnl", "profit", "loss", "ликв", "приб", "убыт")),
)


def build_intel(
    *,
    board_path: Path | str = board.DEFAULT_BOARD_PATH,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    kind: str | None = None,
    symbol: str | None = None,
    topic: str | None = None,
    limit: int = DEFAULT_LIMIT,
    now: float | None = None,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    brief_dir: Path | str = DEFAULT_BRIEF_DIR,
    preflight_candidates_path: Path | str = DEFAULT_PREFLIGHT_CANDIDATES_PATH,
    strategy_queue_path: Path | str = DEFAULT_STRATEGY_QUEUE_PATH,
    strategy_prompts_path: Path | str = DEFAULT_STRATEGY_PROMPTS_PATH,
    private_preflight_path: Path | str = DEFAULT_PRIVATE_PREFLIGHT_PATH,
    digest_path: Path | str = DEFAULT_DIGEST_PATH,
) -> dict[str, Any]:
    """Build the Community Intel payload for the local dashboard."""

    now = time.time() if now is None else now
    limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    window_hours = max(0.25, min(float(window_hours or DEFAULT_WINDOW_HOURS), 168.0))
    rows = read_jsonl_tail(Path(events_path), max_rows=5_000)
    events = []
    for row in rows:
        event = _normal_event(row, now=now)
        if event is not None and _intel_topic_allowed(event):
            events.append(event)
    filtered_events = [
        event
        for event in events
        if _event_matches(event, window_hours=window_hours, kind=kind, symbol=symbol, topic=topic)
    ]
    board_snapshot = board.load_board(board_path, include_stale=True, max_age_min=None, limit=None, now=now)
    board_by_symbol = _board_by_symbol(board_snapshot.rows)
    latest_preflight = _latest_preflight_by_symbol(
        *(
            read_jsonl_tail(Path(path), max_rows=1500)
            for path in (strategy_queue_path, strategy_prompts_path, private_preflight_path)
        )
    )
    hot_symbols = _hot_symbols(filtered_events, board_by_symbol, latest_preflight, limit=limit)
    latest_brief = latest_topic_brief(Path(brief_dir), now=now)
    source_freshness = build_source_freshness(
        board_path=board_path,
        now=now,
        events_path=events_path,
        brief_dir=brief_dir,
        preflight_candidates_path=preflight_candidates_path,
        strategy_queue_path=strategy_queue_path,
        strategy_prompts_path=strategy_prompts_path,
        private_preflight_path=private_preflight_path,
        digest_path=digest_path,
    )
    route_reality = [
        _route_reality(symbol_row["symbol"], board_by_symbol, latest_preflight)
        for symbol_row in hot_symbols[:limit]
    ]
    action_queue = build_action_queue(hot_symbols, route_reality, limit=limit)
    change_digest = build_change_digest(
        filtered_events,
        hot_symbols=hot_symbols,
        source_freshness=source_freshness,
        limit=limit,
    )
    community_cards = _community_cards(filtered_events, limit=limit)
    question_patterns = _question_patterns(filtered_events, limit=limit)
    signal_lifecycle = build_signal_lifecycle(
        filtered_events,
        board_by_symbol=board_by_symbol,
        limit=limit,
    )
    community_insights = build_community_insights(
        community=community_cards,
        latest_brief=latest_brief,
        hot_symbols=hot_symbols,
        question_patterns=question_patterns,
        route_reality=route_reality,
        limit=limit,
    )
    payload = {
        "ok": True,
        "generated_at_us": int(now * 1_000_000),
        "mode": "read_only_local_intel",
        "filters": {
            "window_hours": window_hours,
            "kind": kind,
            "symbol": symbol,
            "topic": topic,
            "limit": limit,
        },
        "source_freshness": source_freshness,
        "hot_symbols": hot_symbols,
        "recent_events": _recent_events(filtered_events, limit=limit),
        "funding_watch": _funding_watch(filtered_events, board_by_symbol, limit=limit),
        "community": community_cards,
        "community_insights": community_insights,
        "question_patterns": question_patterns,
        "signal_lifecycle": signal_lifecycle,
        "action_queue": action_queue,
        "change_digest": change_digest,
        "route_reality": route_reality,
        "latest_brief": latest_brief,
        "profile_shell": build_profile_shell(hot_symbols, route_reality),
    }
    payload["alert_preview"] = build_alert_preview(payload)
    return payload


def build_change_digest(
    events: list[dict[str, Any]],
    *,
    hot_symbols: list[dict[str, Any]],
    source_freshness: dict[str, Any],
    limit: int = DEFAULT_LIMIT,
    recent_min: float = 60.0,
) -> dict[str, Any]:
    """Summarize what changed recently from local Telegram/source data."""

    limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    recent = [
        event
        for event in events
        if (event.get("age_min") is None or (_float_or_none(event.get("age_min")) or 0.0) <= recent_min)
    ]
    older_symbols = {
        str(event.get("symbol") or "")
        for event in events
        if event.get("symbol") and (_float_or_none(event.get("age_min")) or 0.0) > recent_min
    }
    recent_symbols = {str(event.get("symbol") or "") for event in recent if event.get("symbol")}
    hot_by_symbol = {str(item.get("symbol") or ""): item for item in hot_symbols if item.get("symbol")}
    new_symbols = sorted(symbol for symbol in recent_symbols if symbol and symbol not in older_symbols)
    counts = Counter(str(event.get("event") or "event") for event in recent)
    source_gaps = [
        {"source": name, "status": item.get("status"), "age_min": item.get("age_min")}
        for name, item in source_freshness.items()
        if isinstance(item, dict) and item.get("status") in {"stale", "missing", "error"}
    ]
    highlights = []
    for event in sorted(recent, key=lambda item: item.get("at_us") or 0, reverse=True):
        symbol = str(event.get("symbol") or "")
        hot = hot_by_symbol.get(symbol) or {}
        best = hot.get("best_board") if isinstance(hot.get("best_board"), dict) else {}
        highlights.append(
            {
                "symbol": symbol,
                "event": event.get("event"),
                "kind": event.get("kind"),
                "age_min": event.get("age_min"),
                "spread_pct": event.get("spread_pct") if event.get("spread_pct") is not None else event.get("open_spread_pct"),
                "funding_delta_pct": event.get("funding_delta_pct"),
                "route_line": best.get("route_line"),
                "href": best.get("pair_url") or (f"/token/{symbol}" if symbol else "/signals"),
                "text_excerpt": event.get("first_line") or event.get("text_excerpt"),
            }
        )
        if len(highlights) >= min(limit, 6):
            break
    return {
        "status": "fresh" if recent else "quiet",
        "window_min": recent_min,
        "recent_event_count": len(recent),
        "new_symbol_count": len(new_symbols),
        "new_symbols": new_symbols[:6],
        "counts": {
            "alerts": counts.get("alert", 0),
            "closes": counts.get("close", 0),
            "funding": counts.get("funding_alert", 0),
            "community": counts.get("community_signal", 0) + counts.get("result_signal", 0),
            "momentum": counts.get("momentum", 0),
        },
        "source_gap_count": len(source_gaps),
        "source_gaps": source_gaps[:4],
        "highlights": highlights,
    }


def build_source_freshness(
    *,
    board_path: Path | str = board.DEFAULT_BOARD_PATH,
    now: float | None = None,
    events_path: Path | str = DEFAULT_EVENTS_PATH,
    brief_dir: Path | str = DEFAULT_BRIEF_DIR,
    preflight_candidates_path: Path | str = DEFAULT_PREFLIGHT_CANDIDATES_PATH,
    strategy_queue_path: Path | str = DEFAULT_STRATEGY_QUEUE_PATH,
    strategy_prompts_path: Path | str = DEFAULT_STRATEGY_PROMPTS_PATH,
    private_preflight_path: Path | str = DEFAULT_PRIVATE_PREFLIGHT_PATH,
    digest_path: Path | str = DEFAULT_DIGEST_PATH,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    source_files = {
        "telegram_events": Path(events_path),
        "preflight_candidates": Path(preflight_candidates_path),
        "strategy_review_queue": Path(strategy_queue_path),
        "strategy_prompts": Path(strategy_prompts_path),
        "private_preflight": Path(private_preflight_path),
        "website_digest": Path(digest_path),
    }
    freshness = {name: _file_freshness(path, now=now) for name, path in source_files.items()}
    latest_event = _latest_timestamp(read_jsonl_tail(Path(events_path), max_rows=500))
    if latest_event:
        freshness["telegram_events"]["latest_at_us"] = latest_event
        freshness["telegram_events"]["age_min"] = _age_min(now, latest_event)
        freshness["telegram_events"]["status"] = _freshness_status(freshness["telegram_events"]["age_min"], stale_min=30)
    brief = latest_topic_brief(Path(brief_dir), now=now, body_chars=0)
    freshness["topic_brief"] = {
        "path": brief.get("path"),
        "exists": bool(brief.get("path")),
        "age_min": brief.get("age_min"),
        "status": brief.get("status"),
        "title": brief.get("title"),
    }
    board_snapshot = board.load_board(board_path, limit=0, now=now)
    freshness["board"] = {
        "path": board_snapshot.source_path,
        "exists": Path(board_snapshot.source_path).exists(),
        "age_min": board_snapshot.age_min,
        "fresh_count": board_snapshot.fresh_count,
        "stale_count": board_snapshot.stale_count,
        "status": "error" if board_snapshot.error else _freshness_status(board_snapshot.age_min, stale_min=120),
        "error": board_snapshot.error,
    }
    return freshness


def latest_topic_brief(
    brief_dir: Path = DEFAULT_BRIEF_DIR,
    *,
    now: float | None = None,
    body_chars: int = 1600,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    try:
        candidates = [
            path
            for path in brief_dir.glob("*.md")
            if path.name != "LATEST.md" and not path.name.startswith(".")
        ]
        latest = max(candidates, key=lambda path: path.stat().st_mtime)
    except (OSError, ValueError):
        return {"status": "missing", "path": None, "title": "No topic brief found", "age_min": None, "body": ""}
    age_min = max(0.0, (now - latest.stat().st_mtime) / 60.0)
    body = _read_text(latest, max_chars=body_chars) if body_chars else ""
    return {
        "status": _freshness_status(age_min, stale_min=6 * 60),
        "path": str(latest),
        "title": latest.name,
        "age_min": age_min,
        "body": body,
    }


def build_profile_shell(hot_symbols: list[dict[str, Any]], route_reality: list[dict[str, Any]]) -> dict[str, Any]:
    pinned = []
    seen: set[str] = set()
    for item in hot_symbols:
        symbol = str(item.get("symbol") or "")
        if symbol and symbol not in seen:
            pinned.append(symbol)
            seen.add(symbol)
        if len(pinned) >= 6:
            break
    return {
        "status": "profile_shell_only",
        "auth": "not_implemented",
        "local_only": True,
        "pnl_enabled": False,
        "watchlist": pinned,
        "sections": [
            {"key": "watchlist", "label": "Watchlist", "status": "ready", "count": len(pinned)},
            {"key": "pnl", "label": "PnL", "status": "planned", "count": 0},
            {"key": "alerts", "label": "Alert rules", "status": "preview_only", "count": 5},
            {
                "key": "route_notes",
                "label": "Route notes",
                "status": "local_read_only",
                "count": sum(1 for item in route_reality if item.get("routes")),
            },
        ],
    }


def build_alert_preview(intel_payload: dict[str, Any]) -> dict[str, Any]:
    hot = intel_payload.get("hot_symbols") or []
    source = intel_payload.get("source_freshness") or {}
    funding = intel_payload.get("funding_watch") or []
    community = (intel_payload.get("community") or {}).get("calls") or []
    generated_at_us = _int_or_none(intel_payload.get("generated_at_us"))
    spread_examples = []
    for item in hot:
        best_board = item.get("best_board") if isinstance(item.get("best_board"), dict) else {}
        spread = _float_or_none(best_board.get("open_spread_pct"))
        if spread is not None and abs(spread) >= 8.0:
            spread_examples.append(_alert_route_example(item, best_board))
    spread_hits, spread_review = _split_fresh_examples(spread_examples)
    funding_examples = [
        _alert_example_with_freshness(item)
        for item in funding
        if abs(_float_or_none(item.get("funding_apr_pct")) or 0.0) >= 50.0
        or abs(_float_or_none(item.get("funding_24h_pct")) or 0.0) >= 0.15
    ]
    funding_hits, funding_review = _split_fresh_examples(funding_examples)
    freshness_hits = [
        {"source": name, "status": info.get("status"), "age_min": info.get("age_min")}
        for name, info in source.items()
        if isinstance(info, dict) and info.get("status") in {"stale", "missing", "error"}
    ]
    route_change_examples = [
        _alert_hot_example(item, generated_at_us=generated_at_us)
        for item in hot
        if item.get("new_count") or item.get("close_count")
    ]
    route_change_hits, route_change_review = _split_fresh_examples(route_change_examples)
    community_examples = [_alert_example_with_freshness({**item, "source": "telegram"}) for item in community]
    community_hits, community_review = _split_fresh_examples(community_examples)
    cards = [
        _preview_card("spread_threshold", "Spread threshold", bool(spread_hits), spread_hits[:5], review_examples=spread_review[:5]),
        _preview_card("funding_threshold", "Funding threshold", bool(funding_hits), funding_hits[:5], review_examples=funding_review[:5]),
        _preview_card("source_freshness", "Source freshness", bool(freshness_hits), freshness_hits[:5]),
        _preview_card("route_change", "Route changed", bool(route_change_hits), route_change_hits[:5], review_examples=route_change_review[:5]),
        _preview_card("community_call", "Community call", bool(community_hits), community_hits[:5], review_examples=community_review[:5]),
    ]
    return {
        "mode": "preview_only_no_pushover_send",
        "would_trigger_count": sum(1 for card in cards if card["would_trigger"]),
        "cards": cards,
    }


def build_action_queue(
    hot_symbols: list[dict[str, Any]],
    route_reality: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Join Telegram heat, board rows, and preflight state into next-read rows."""

    limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    reality_by_symbol = {str(item.get("symbol") or ""): item for item in route_reality}
    rows = []
    for hot in hot_symbols[: limit * 2]:
        symbol = str(hot.get("symbol") or "")
        if not symbol:
            continue
        reality = reality_by_symbol.get(symbol) or {}
        best = hot.get("best_board") if isinstance(hot.get("best_board"), dict) else {}
        preflight = hot.get("latest_preflight") if isinstance(hot.get("latest_preflight"), dict) else {}
        blockers = _dedupe_text(
            [
                *(best.get("blockers") or []),
                *(reality.get("top_blockers") or []),
                *(preflight.get("blockers") or []),
            ]
        )
        spread = _float_or_none(best.get("open_spread_pct"))
        funding_apr = _float_or_none(best.get("funding_apr_pct"))
        has_route = bool(best or reality.get("routes"))
        status = _action_status(best, reality, preflight, blockers)
        href = best.get("pair_url") or f"/token/{symbol}"
        row = {
            "symbol": symbol,
            "status": status,
            "priority": round(_action_priority(hot, best, preflight, blockers), 3),
            "reason": _action_reason(hot, best, preflight, blockers),
            "event_count": hot.get("event_count") or 0,
            "route_status": reality.get("status") or ("matched_board" if has_route else "telegram_only"),
            "route_line": best.get("route_line") or "No matched board route",
            "kind": best.get("kind") or preflight.get("kind") or _first_key(hot.get("kinds")) or "?",
            "spread_pct": spread,
            "funding_apr_pct": funding_apr,
            "freshness": best.get("freshness") or ("not_enough_data" if not has_route else "unknown"),
            "next_action": _action_next(status, best, reality, preflight),
            "blockers": blockers[:4],
            "badges": _action_badges(hot, best, preflight, reality),
            "href": href,
            "token_href": f"/token/{symbol}",
            "board_href": f"/arbitrage?kind={best.get('kind') or preflight.get('kind') or 'FUTURES'}",
        }
        rows.append(row)
    rows.sort(key=lambda item: (item.get("priority") or 0.0, item.get("event_count") or 0), reverse=True)
    return rows[:limit]


def build_community_insights(
    *,
    community: dict[str, list[dict[str, Any]]],
    latest_brief: dict[str, Any],
    hot_symbols: list[dict[str, Any]],
    question_patterns: list[dict[str, Any]],
    route_reality: list[dict[str, Any]],
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return a scoreboard/results/calls summary for the Community page."""

    limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    calls = list((community or {}).get("calls") or [])[:limit]
    results = list((community or {}).get("results") or [])[:limit]
    route_by_symbol = {str(item.get("symbol") or ""): item for item in route_reality}
    call_symbols = Counter(str(item.get("symbol") or "") for item in calls if item.get("symbol"))
    result_symbols = Counter(str(item.get("symbol") or "") for item in results if item.get("symbol"))
    discussion = []
    for item in hot_symbols[:limit]:
        symbol = str(item.get("symbol") or "")
        reality = route_by_symbol.get(symbol) or {}
        discussion.append(
            {
                "symbol": symbol,
                "score": item.get("score"),
                "message_count": item.get("event_count"),
                "call_count": call_symbols.get(symbol, 0),
                "result_count": result_symbols.get(symbol, 0),
                "board_match": bool(item.get("best_board")),
                "route_status": reality.get("status") or ("matched_board" if item.get("best_board") else "telegram_only"),
                "next_action": _first_text((reality.get("next_actions") or [])[:1]) or "watch",
                "reason": _community_reason(item, reality),
            }
        )
    scoreboard = _scoreboard_from_brief(str(latest_brief.get("body") or ""), limit=limit)
    call_ledger = build_call_ledger(
        calls=calls,
        results=results,
        hot_symbols=hot_symbols,
        route_reality=route_reality,
        limit=limit,
    )
    return {
        "status": "ready",
        "source": "local_telegram_and_topic_brief",
        "scoreboard": {
            "status": latest_brief.get("status") or "missing",
            "source_title": latest_brief.get("title"),
            "top_positive": scoreboard["top_positive"],
            "net_negative": scoreboard["net_negative"],
        },
        "call_ledger": call_ledger,
        "calls": calls,
        "results": results,
        "discussion": discussion,
        "question_patterns": question_patterns[:limit],
        "brief_excerpt": _brief_excerpt_lines(latest_brief.get("body"), max_lines=8),
    }


def build_call_ledger(
    *,
    calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
    hot_symbols: list[dict[str, Any]],
    route_reality: list[dict[str, Any]],
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Join local community calls to outcomes, board reality, and next action."""

    limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    calls_by_symbol = _events_by_symbol(calls)
    results_by_symbol = _events_by_symbol(results)
    hot_by_symbol = {str(item.get("symbol") or ""): item for item in hot_symbols if item.get("symbol")}
    reality_by_symbol = {str(item.get("symbol") or ""): item for item in route_reality if item.get("symbol")}
    symbols = set(calls_by_symbol) | set(results_by_symbol)
    symbols.update(
        str(item.get("symbol") or "")
        for item in hot_symbols
        if (item.get("community_count") or item.get("close_count") or item.get("new_count"))
    )
    rows = []
    for symbol in sorted(item for item in symbols if item):
        call_rows = calls_by_symbol.get(symbol, [])
        result_rows = results_by_symbol.get(symbol, [])
        hot = hot_by_symbol.get(symbol) or {}
        reality = reality_by_symbol.get(symbol) or {}
        best = hot.get("best_board") if isinstance(hot.get("best_board"), dict) else {}
        status = _call_ledger_status(call_rows, result_rows, hot, best)
        latest_call = _latest_event_by_age(call_rows)
        latest_result = _latest_event_by_age(result_rows)
        next_action = _call_ledger_next_action(status, best, reality)
        href = best.get("pair_url") or f"/token/{symbol}"
        row = {
            "symbol": symbol,
            "status": status,
            "score": round(_float_or_none(hot.get("score")) or 0.0, 3),
            "call_count": len(call_rows) or hot.get("community_count") or 0,
            "result_count": len(result_rows),
            "alert_count": hot.get("alert_count") or 0,
            "close_count": hot.get("close_count") or 0,
            "route_status": reality.get("status") or ("matched_board" if best else "telegram_only"),
            "route_line": best.get("route_line") or "No matched board route",
            "spread_pct": best.get("open_spread_pct"),
            "funding_apr_pct": best.get("funding_apr_pct"),
            "freshness": best.get("freshness") or ("unknown" if best else "not_enough_data"),
            "latest_call_age_min": latest_call.get("age_min") if latest_call else None,
            "latest_result_age_min": latest_result.get("age_min") if latest_result else None,
            "latest_call": latest_call,
            "latest_result": latest_result,
            "next_action": next_action,
            "href": href,
            "signals_href": f"/signals?symbol={symbol}",
            "badges": _call_ledger_badges(status, hot, best, reality),
        }
        rows.append(row)
    rows.sort(key=_call_ledger_sort_key, reverse=True)
    return rows[:limit]


def build_signal_lifecycle(
    events: list[dict[str, Any]],
    *,
    board_by_symbol: dict[str, list[board.BoardRow]] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Pair local Telegram alerts to subsequent close/fade rows."""

    limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    board_by_symbol = board_by_symbol or {}
    alerts = [
        event
        for event in events
        if event.get("event") == "alert" and event.get("symbol") and event.get("at_us")
    ]
    closes = [
        event
        for event in events
        if event.get("event") == "close" and event.get("symbol") and event.get("at_us")
    ]
    closes_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    used_close_ids: set[tuple[str, str, int]] = set()
    for close in closes:
        key = (str(close.get("symbol") or ""), str(close.get("kind") or ""))
        closes_by_key[key].append(close)
    for rows in closes_by_key.values():
        rows.sort(key=lambda item: _int_or_none(item.get("at_us")) or 0)

    lifecycle_rows = []
    for alert in sorted(alerts, key=lambda item: _int_or_none(item.get("at_us")) or 0, reverse=True):
        symbol = str(alert.get("symbol") or "")
        kind = str(alert.get("kind") or "")
        alert_at = _int_or_none(alert.get("at_us")) or 0
        close = _first_close_after(closes_by_key.get((symbol, kind), []), alert_at)
        status = "open_or_unresolved"
        minutes_to_close = None
        if close:
            close_at = _int_or_none(close.get("at_us")) or alert_at
            minutes_to_close = max(0.0, (close_at - alert_at) / 60_000_000.0)
            status = "closed_or_faded"
            used_close_ids.add((symbol, kind, close_at))
        lifecycle_rows.append(
            _lifecycle_row(
                symbol=symbol,
                kind=kind,
                status=status,
                alert=alert,
                close=close,
                minutes_to_close=minutes_to_close,
                board_rows=board_by_symbol.get(symbol, []),
            )
        )

    for close in sorted(closes, key=lambda item: _int_or_none(item.get("at_us")) or 0, reverse=True):
        symbol = str(close.get("symbol") or "")
        kind = str(close.get("kind") or "")
        close_at = _int_or_none(close.get("at_us")) or 0
        if (symbol, kind, close_at) in used_close_ids:
            continue
        lifecycle_rows.append(
            _lifecycle_row(
                symbol=symbol,
                kind=kind,
                status="close_without_recent_alert",
                alert=None,
                close=close,
                minutes_to_close=None,
                board_rows=board_by_symbol.get(symbol, []),
            )
        )

    lifecycle_rows.sort(key=_lifecycle_sort_key, reverse=True)
    visible = lifecycle_rows[:limit]
    closed_minutes = [
        _float_or_none(row.get("minutes_to_close"))
        for row in lifecycle_rows
        if row.get("minutes_to_close") is not None
    ]
    closed_minutes = [value for value in closed_minutes if value is not None]
    latest = visible[0] if visible else {}
    return {
        "status": "ready" if visible else "quiet",
        "alert_count": len(alerts),
        "close_count": len(closes),
        "closed_count": sum(1 for row in lifecycle_rows if row.get("status") == "closed_or_faded"),
        "unresolved_count": sum(1 for row in lifecycle_rows if row.get("status") == "open_or_unresolved"),
        "median_close_min": _median_float(closed_minutes),
        "latest_status": latest.get("status") or "quiet",
        "rows": visible,
    }


def _first_close_after(closes: list[dict[str, Any]], alert_at: int) -> dict[str, Any] | None:
    for close in closes:
        close_at = _int_or_none(close.get("at_us")) or 0
        if close_at >= alert_at:
            return close
    return None


def _lifecycle_row(
    *,
    symbol: str,
    kind: str,
    status: str,
    alert: dict[str, Any] | None,
    close: dict[str, Any] | None,
    minutes_to_close: float | None,
    board_rows: list[board.BoardRow],
) -> dict[str, Any]:
    alert_spread = _float_or_none((alert or {}).get("spread_pct"))
    close_spread = _float_or_none((close or {}).get("spread_pct"))
    spread_move = close_spread - alert_spread if close_spread is not None and alert_spread is not None else None
    best_board = _compact_board_row(_best_board_row(board_rows))
    age_min = (alert or close or {}).get("age_min")
    if status == "closed_or_faded":
        takeaway = "closed or faded after the alert"
    elif status == "open_or_unresolved":
        takeaway = "no close row yet in the local window"
    else:
        takeaway = "close row seen without a matching recent alert"
    return {
        "symbol": symbol,
        "kind": kind,
        "status": status,
        "takeaway": takeaway,
        "alert": _compact_event(alert) if alert else None,
        "close": _compact_event(close) if close else None,
        "alert_spread_pct": alert_spread,
        "close_spread_pct": close_spread,
        "spread_move_pct": spread_move,
        "minutes_to_close": round(minutes_to_close, 3) if minutes_to_close is not None else None,
        "age_min": age_min,
        "board_freshness": (best_board or {}).get("freshness") or "not_enough_data",
        "route_line": (best_board or {}).get("route_line") or "No matched board route",
        "href": (best_board or {}).get("pair_url") or f"/token/{symbol}",
        "signals_href": f"/signals?symbol={symbol}",
    }


def _lifecycle_sort_key(item: dict[str, Any]) -> tuple[float, float]:
    status_weight = {"open_or_unresolved": 3.0, "closed_or_faded": 2.0, "close_without_recent_alert": 1.0}
    age = _float_or_none(item.get("age_min"))
    recency = -age if age is not None else -999999.0
    return (status_weight.get(str(item.get("status")), 0.0), recency)


def _median_float(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 3)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 3)


def _events_by_symbol(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        symbol = str(event.get("symbol") or "")
        if symbol:
            grouped[symbol].append(event)
    for rows in grouped.values():
        rows.sort(key=lambda item: _float_or_none(item.get("age_min")) or 999999)
    return dict(grouped)


def _latest_event_by_age(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return min(events, key=lambda item: _float_or_none(item.get("age_min")) or 999999, default=None)


def _call_ledger_status(
    calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
    hot: dict[str, Any],
    best: dict[str, Any],
) -> str:
    if results:
        return "result_reported"
    if hot.get("close_count"):
        return "closed_or_faded"
    if best and best.get("freshness") == "fresh":
        return "inspect_route"
    if best:
        return "stale_board_match"
    if calls or hot.get("community_count"):
        return "community_only"
    return "watch"


def _call_ledger_next_action(
    status: str,
    best: dict[str, Any],
    reality: dict[str, Any],
) -> str:
    explicit = best.get("next_action") or _first_text((reality.get("next_actions") or [])[:1])
    if explicit:
        return str(explicit)
    if status == "result_reported":
        return "compare reported result with current board route before trusting it"
    if status == "closed_or_faded":
        return "inspect close/fade timing before chasing the old spread"
    if status == "inspect_route":
        return "open pair and compare spread, funding, D/W, and blockers"
    if status == "stale_board_match":
        return "refresh source before treating the route as current"
    if status == "community_only":
        return "wait for board or preflight match; verify identity if a DEX route appears"
    return "watch for a fresh call, result, or board match"


def _call_ledger_badges(
    status: str,
    hot: dict[str, Any],
    best: dict[str, Any],
    reality: dict[str, Any],
) -> list[str]:
    badges = [status]
    if best.get("kind"):
        badges.append(str(best.get("kind")))
    if hot.get("close_count"):
        badges.append("close")
    if hot.get("funding_count") or abs(_float_or_none(best.get("funding_apr_pct")) or 0.0) >= 50.0:
        badges.append("funding")
    if reality.get("okx_dex_identity") == "exact_identity_present":
        badges.append("OKX DEX")
    return _dedupe_text(badges)[:5]


def _call_ledger_sort_key(item: dict[str, Any]) -> tuple[float, float, float]:
    status_weight = {
        "inspect_route": 5,
        "result_reported": 4,
        "closed_or_faded": 3,
        "community_only": 2,
        "stale_board_match": 1,
        "watch": 0,
    }.get(str(item.get("status") or ""), 0)
    score = _float_or_none(item.get("score")) or 0.0
    newest_age = min(
        _float_or_none(item.get("latest_call_age_min")) or 999999,
        _float_or_none(item.get("latest_result_age_min")) or 999999,
    )
    return (
        status_weight,
        score + abs(_float_or_none(item.get("spread_pct")) or 0.0),
        -newest_age,
    )


def read_jsonl_tail(path: Path, *, max_rows: int = 1000) -> list[dict[str, Any]]:
    if max_rows <= 0:
        return []
    rows: deque[dict[str, Any]] = deque(maxlen=max_rows)
    try:
        raw_lines = _tail_lines(path, max_rows=max_rows)
    except OSError:
        return []
    for line in raw_lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return list(rows)


def _tail_lines(path: Path, *, max_rows: int, chunk_size: int = 256 * 1024) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        chunks: list[bytes] = []
        newline_count = 0
        while position > 0 and newline_count <= max_rows:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
        data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_rows:]


def _normal_event(row: dict[str, Any], *, now: float) -> dict[str, Any] | None:
    parsed = row.get("parsed") if isinstance(row.get("parsed"), dict) else {}
    text = str(row.get("text") or parsed.get("first_line") or "")
    topic_id = parsed.get("topic_id") or row.get("topic_id")
    source_role = (
        "lead_analyst"
        if row.get("source_role") == "lead_analyst"
        else "community"
    )
    symbol = _clean_symbol(parsed.get("symbol")) or _infer_discussion_symbol(text)
    kind = str(parsed.get("kind") or "").upper() or None
    event = str(parsed.get("event") or "")
    if not event:
        if topic_id == 14:
            event = "community_signal"
        elif topic_id is None and (symbol or source_role == "lead_analyst"):
            event = "chat_signal"
        else:
            return None
    at_us = _event_at_us(row, parsed)
    return {
        "symbol": symbol,
        "kind": kind,
        "event": event,
        "priority": parsed.get("priority"),
        "spread_pct": _float_or_none(parsed.get("spread_pct")),
        "open_spread_pct": _float_or_none(parsed.get("open_spread_pct")),
        "funding_delta_pct": _float_or_none(parsed.get("funding_delta_pct")),
        "minutes_to_funding": _int_or_none(parsed.get("minutes_to_funding")),
        "seconds": _int_or_none(parsed.get("seconds")),
        "side": parsed.get("side"),
        "is_new": bool(parsed.get("is_new")),
        "is_recycle": bool(parsed.get("is_recycle")),
        "topic_id": topic_id,
        "topic_title": parsed.get("topic_title") or row.get("topic_title"),
        "message_id": row.get("message_id") or parsed.get("message_id"),
        "at_us": at_us,
        "age_min": _age_min(now, at_us),
        "first_line": _clip(parsed.get("first_line") or text),
        "text_excerpt": _clip(text),
        "exchanges": _string_list(parsed.get("exchanges")),
        "exchange_rows": _safe_exchange_rows(parsed.get("exchange_rows")),
        "chains": _string_list(parsed.get("chains")),
        "contracts": _string_list(parsed.get("contracts")),
        "green_green_rows": _int_or_none(parsed.get("green_green_rows")) or 0,
        "red_any_rows": _int_or_none(parsed.get("red_any_rows")) or 0,
        "max_volume_usd": _float_or_none(parsed.get("max_volume_usd")),
        "liquidity_usd": _float_or_none(parsed.get("liquidity_usd")),
        "mcap_usd": _float_or_none(parsed.get("mcap_usd")),
        "source_role": source_role,
        "question_categories": _categorize_question(text),
    }


def _event_matches(
    event: dict[str, Any],
    *,
    window_hours: float,
    kind: str | None,
    symbol: str | None,
    topic: str | None,
) -> bool:
    age_min = event.get("age_min")
    if age_min is not None and age_min > window_hours * 60.0:
        return False
    if kind and str(event.get("kind") or "").upper() != kind.upper():
        return False
    if symbol and str(event.get("symbol") or "").upper() != symbol.upper():
        return False
    if topic and str(event.get("topic_id") or "") != str(topic):
        return False
    return True


def _intel_topic_allowed(event: dict[str, Any]) -> bool:
    """Only surface the main chat and Community Calls forum topics."""

    topic_id = _int_or_none(event.get("topic_id"))
    return topic_id in INTEL_ALLOWED_TOPIC_IDS


def _hot_symbols(
    events: list[dict[str, Any]],
    board_by_symbol: dict[str, list[board.BoardRow]],
    latest_preflight: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        symbol = str(event.get("symbol") or "")
        if symbol:
            grouped[symbol].append(event)
    output = []
    for symbol, rows in grouped.items():
        event_counter = Counter(str(item.get("event") or "event") for item in rows)
        kind_counter = Counter(str(item.get("kind") or "?") for item in rows)
        contracts = sorted({contract for item in rows for contract in item.get("contracts", [])})
        chains = sorted({chain for item in rows for chain in item.get("chains", [])})
        exchanges = sorted({exchange for item in rows for exchange in item.get("exchanges", [])})[:10]
        board_rows = board_by_symbol.get(symbol, [])
        best_board = _best_board_row(board_rows)
        preflight = latest_preflight.get(symbol)
        score = 0.0
        for item in rows:
            score += _event_score(item)
        if contracts and chains:
            score += 8
        if board_rows:
            score += 6
        if preflight:
            score += 3
        max_liquidity = max((_float_or_none(item.get("liquidity_usd")) or 0.0 for item in rows), default=0.0)
        max_volume = max((_float_or_none(item.get("max_volume_usd")) or 0.0 for item in rows), default=0.0)
        score += min(8.0, math.log10(max(max_liquidity, 1.0)))
        score += min(6.0, math.log10(max(max_volume, 1.0)))
        output.append(
            {
                "symbol": symbol,
                "score": round(score, 3),
                "event_count": len(rows),
                "alert_count": event_counter.get("alert", 0),
                "close_count": event_counter.get("close", 0),
                "new_count": sum(1 for item in rows if item.get("is_new")),
                "momentum_count": event_counter.get("momentum", 0),
                "funding_count": event_counter.get("funding_alert", 0),
                "community_count": event_counter.get("community_signal", 0),
                "lead_analyst_count": sum(
                    1 for item in rows if item.get("source_role") == "lead_analyst"
                ),
                "kinds": dict(kind_counter.most_common(5)),
                "exchanges": exchanges,
                "chains": chains,
                "contract_count": len(contracts),
                "liquidity_usd": max_liquidity or None,
                "max_volume_usd": max_volume or None,
                "latest_event": max((item.get("at_us") or 0 for item in rows), default=0),
                "best_board": _compact_board_row(best_board),
                "latest_preflight": _compact_preflight(preflight),
            }
        )
    output.sort(key=lambda item: (item["score"], item["latest_event"]), reverse=True)
    return output[:limit]


def _recent_events(events: list[dict[str, Any]], *, limit: int) -> dict[str, list[dict[str, Any]]]:
    buckets = {
        "alerts": {"alert"},
        "closes": {"close"},
        "momentum": {"momentum"},
        "funding": {"funding_alert"},
        "chat": {"chat_signal"},
        "community": {"community_signal"},
    }
    output: dict[str, list[dict[str, Any]]] = {}
    recent = sorted(events, key=lambda item: item.get("at_us") or 0, reverse=True)
    for label, names in buckets.items():
        output[label] = [_compact_event(item) for item in recent if item.get("event") in names][:limit]
    return output


def _funding_watch(
    events: list[dict[str, Any]],
    board_by_symbol: dict[str, list[board.BoardRow]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") == "funding_alert":
            rows.append(
                {
                    "symbol": event.get("symbol"),
                    "kind": event.get("kind"),
                    "funding_delta_pct": event.get("funding_delta_pct"),
                    "minutes_to_funding": event.get("minutes_to_funding"),
                    "open_spread_pct": event.get("open_spread_pct"),
                    "age_min": event.get("age_min"),
                    "freshness": _alert_freshness(event),
                    "source": "telegram",
                }
            )
    for symbol, symbol_rows in board_by_symbol.items():
        best = max(symbol_rows, key=lambda row: abs(row.funding_apr_pct or 0.0), default=None)
        if best and best.funding_apr_pct is not None and abs(best.funding_apr_pct) >= 25:
            rows.append(
                {
                    "symbol": symbol,
                    "kind": best.kind,
                    "funding_apr_pct": best.funding_apr_pct,
                    "funding_spread_pct": best.funding_spread_pct,
                    "open_spread_pct": best.displayed_open_spread_pct,
                    "age_min": best.age_min,
                    "freshness": "fresh" if best.age_min is not None and best.age_min <= board.DEFAULT_FRESH_MAX_AGE_MIN else "stale",
                    "source": "board",
                }
            )
    rows.sort(
        key=lambda item: (
            abs(_float_or_none(item.get("funding_apr_pct")) or _float_or_none(item.get("funding_delta_pct")) or 0.0),
            -(_float_or_none(item.get("age_min")) or 0.0),
        ),
        reverse=True,
    )
    return rows[:limit]


def _community_cards(events: list[dict[str, Any]], *, limit: int) -> dict[str, list[dict[str, Any]]]:
    recent = sorted(events, key=lambda item: item.get("at_us") or 0, reverse=True)
    calls = [
        _compact_event(item)
        for item in recent
        if item.get("event") == "community_signal" or str(item.get("topic_id")) == "14"
    ][:limit]
    results = [
        _compact_event(item)
        for item in recent
        if item.get("event") == "result_signal" or str(item.get("topic_id")) == "20"
    ][:limit]
    return {"calls": calls, "results": results}


def _scoreboard_from_brief(text: str, *, limit: int) -> dict[str, list[dict[str, Any]]]:
    positive: list[dict[str, Any]] = []
    negative: list[dict[str, Any]] = []
    in_scoreboard = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.casefold()
        if "scoreboard" in lower or "reported pnl" in lower:
            in_scoreboard = True
            continue
        is_score_line = in_scoreboard or "top " in lower or "net-negative" in lower
        if not is_score_line:
            continue
        if in_scoreboard and stripped.startswith("**") and "scoreboard" not in lower:
            break
        for symbol, raw_value in SCOREBOARD_PATTERN.findall(stripped):
            value = _float_or_none(raw_value)
            if value is None:
                continue
            row = {
                "symbol": symbol,
                "reported_pnl": value,
                "sentiment": "negative" if value < 0 or "distrust" in lower else "positive",
            }
            if row["sentiment"] == "negative":
                negative.append(row)
            else:
                positive.append(row)
    positive.sort(key=lambda item: item.get("reported_pnl") or 0, reverse=True)
    negative.sort(key=lambda item: item.get("reported_pnl") or 0)
    return {"top_positive": positive[:limit], "net_negative": negative[:limit]}


def _community_reason(item: dict[str, Any], reality: dict[str, Any]) -> str:
    reasons = []
    if item.get("community_count"):
        reasons.append("community call")
    if item.get("funding_count"):
        reasons.append("funding")
    if item.get("contract_count"):
        reasons.append("identity")
    if item.get("best_board"):
        reasons.append("board match")
    if reality.get("top_blockers"):
        reasons.append("blockers")
    return ", ".join(reasons) or "recent discussion"


def _first_text(values: list[Any]) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _first_key(value: Any) -> str | None:
    if isinstance(value, dict) and value:
        return str(next(iter(value.keys())))
    return None


def _brief_excerpt_lines(text: Any, *, max_lines: int) -> list[str]:
    lines = []
    for line in str(text or "").splitlines():
        stripped = _clip(line, max_chars=180)
        if stripped:
            lines.append(stripped)
        if len(lines) >= max_lines:
            break
    return lines


def _question_patterns(events: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if "?" not in str(event.get("text_excerpt") or "") and not event.get("question_categories"):
            continue
        for category in event.get("question_categories") or ["Other questions"]:
            buckets[category].append(event)
    output = []
    for category, rows in buckets.items():
        rows.sort(key=lambda item: item.get("at_us") or 0, reverse=True)
        output.append(
            {
                "category": category,
                "count": len(rows),
                "examples": [_compact_event(item) for item in rows[:3]],
            }
        )
    output.sort(key=lambda item: item["count"], reverse=True)
    return output[:limit]


def _route_reality(
    symbol: str,
    board_by_symbol: dict[str, list[board.BoardRow]],
    latest_preflight: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    routes = [_compact_board_row(row) for row in board_by_symbol.get(symbol, [])[:5]]
    blockers = []
    next_actions = []
    for row in board_by_symbol.get(symbol, [])[:5]:
        blockers.extend(row.blockers or [])
        if row.next_action:
            next_actions.append(row.next_action)
    preflight = latest_preflight.get(symbol)
    if preflight:
        blockers.extend(_string_list(preflight.get("blockers")))
        if preflight.get("next_action"):
            next_actions.append(str(preflight.get("next_action")))
    return {
        "symbol": symbol,
        "status": "matched_board" if routes else "telegram_only",
        "routes": routes,
        "top_blockers": [item for item, _ in Counter(blockers).most_common(5)],
        "next_actions": [item for item, _ in Counter(next_actions).most_common(5)],
        "preflight": _compact_preflight(preflight),
        "volatility": "available_on_pair_page" if routes else "not_enough_data",
        "okx_dex_identity": _okx_identity_status(routes),
    }


def _action_status(
    best: dict[str, Any],
    reality: dict[str, Any],
    preflight: dict[str, Any],
    blockers: list[str],
) -> str:
    route_kind = str(best.get("kind") or preflight.get("kind") or "").upper()
    dex_like = "DEX" in route_kind or "DEX" in str(best.get("route_line") or "").upper()
    if dex_like and reality.get("okx_dex_identity") != "exact_identity_present":
        return "identity_needed"
    if blockers or preflight.get("status") in {"blocked", "setup_needed"}:
        return "setup_needed"
    if best and best.get("freshness") == "fresh":
        return "inspect_now"
    if best:
        return "stale_route"
    return "telegram_only"


def _action_priority(
    hot: dict[str, Any],
    best: dict[str, Any],
    preflight: dict[str, Any],
    blockers: list[str],
) -> float:
    score = _float_or_none(hot.get("score")) or 0.0
    if best:
        score += 12.0
    if best.get("freshness") == "fresh":
        score += 7.0
    if preflight:
        score += 4.0
    if blockers:
        score += 3.0
    if abs(_float_or_none(best.get("open_spread_pct")) or 0.0) >= 8.0:
        score += 6.0
    if abs(_float_or_none(best.get("funding_apr_pct")) or 0.0) >= 50.0:
        score += 5.0
    if hot.get("contract_count"):
        score += 3.0
    return score


def _action_reason(
    hot: dict[str, Any],
    best: dict[str, Any],
    preflight: dict[str, Any],
    blockers: list[str],
) -> str:
    parts = []
    event_count = int(hot.get("event_count") or 0)
    if event_count:
        parts.append(f"{event_count} Telegram events")
    if best:
        parts.append("matched board route")
    if abs(_float_or_none(best.get("open_spread_pct")) or 0.0) >= 8.0:
        parts.append("spread threshold")
    if abs(_float_or_none(best.get("funding_apr_pct")) or 0.0) >= 50.0:
        parts.append("funding carry")
    if hot.get("contract_count"):
        parts.append("contract identity seen")
    if preflight:
        parts.append("preflight context")
    if blockers:
        parts.append("blockers to resolve")
    return ", ".join(parts[:4]) or "recent community activity"


def _action_next(
    status: str,
    best: dict[str, Any],
    reality: dict[str, Any],
    preflight: dict[str, Any],
) -> str:
    explicit = (
        best.get("next_action")
        or preflight.get("next_action")
        or _first_text((reality.get("next_actions") or [])[:1])
    )
    if explicit:
        return str(explicit)
    if status == "identity_needed":
        return "verify exact chain and contract before treating as more than watch-only"
    if status == "setup_needed":
        return "open pair and inspect blockers"
    if status == "inspect_now":
        return "open pair detail and compare spread, funding, and D/W gates"
    if status == "stale_route":
        return "check source freshness before acting on the route"
    return "watch Telegram and wait for a matched board or preflight row"


def _action_badges(
    hot: dict[str, Any],
    best: dict[str, Any],
    preflight: dict[str, Any],
    reality: dict[str, Any],
) -> list[str]:
    badges = []
    if best:
        badges.append(str(best.get("kind") or "board"))
    if hot.get("funding_count") or abs(_float_or_none(best.get("funding_apr_pct")) or 0.0) >= 50.0:
        badges.append("funding")
    if hot.get("contract_count"):
        badges.append("identity")
    if preflight:
        badges.append("preflight")
    if reality.get("okx_dex_identity") == "exact_identity_present":
        badges.append("OKX DEX")
    if not badges:
        badges.append("telegram")
    return _dedupe_text(badges)[:5]


def _dedupe_text(values: list[Any]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _latest_preflight_by_symbol(*row_groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for rows in row_groups:
        for row in rows:
            symbol = _clean_symbol(row.get("symbol") or ((row.get("candidate") or {}).get("symbol") if isinstance(row.get("candidate"), dict) else None))
            if not symbol:
                continue
            at_us = _int_or_none(row.get("checked_at_us") or row.get("ingested_at_us") or row.get("recorded_at_us") or row.get("queued_at_us")) or 0
            current = latest.get(symbol)
            current_at = _int_or_none((current or {}).get("_at_us")) or 0
            if at_us >= current_at:
                item = dict(row)
                item["_at_us"] = at_us
                latest[symbol] = item
    return latest


def _board_by_symbol(rows: list[board.BoardRow]) -> dict[str, list[board.BoardRow]]:
    grouped: dict[str, list[board.BoardRow]] = defaultdict(list)
    for row in rows:
        grouped[row.symbol].append(row)
    for symbol_rows in grouped.values():
        symbol_rows.sort(key=lambda row: (row.age_min or 999999, abs(row.displayed_open_spread_pct or row.spread_pct)), reverse=False)
    return dict(grouped)


def _best_board_row(rows: list[board.BoardRow]) -> board.BoardRow | None:
    if not rows:
        return None
    return max(rows, key=lambda row: (abs(row.displayed_open_spread_pct if row.displayed_open_spread_pct is not None else row.spread_pct), abs(row.funding_apr_pct or 0.0)))


def _compact_board_row(row: board.BoardRow | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return {
        "symbol": row.symbol,
        "kind": row.kind,
        "route_key": row.route_key,
        "pair_url": f"/pair/{board.route_key_url(row.route_key)}",
        "route_line": f"{row.long_venue or '?'} {row.long_market_type or '?'} -> {row.short_venue or '?'} {row.short_market_type or '?'}",
        "open_spread_pct": row.displayed_open_spread_pct if row.displayed_open_spread_pct is not None else row.spread_pct,
        "executable_spread_pct": row.spread_pct,
        "funding_apr_pct": row.funding_apr_pct,
        "funding_spread_pct": row.funding_spread_pct,
        "age_min": row.age_min,
        "freshness": "fresh" if row.age_min is not None and row.age_min <= board.DEFAULT_FRESH_MAX_AGE_MIN else "stale",
        "depth_usd": row.depth_usd,
        "dw": {
            "long_deposit": row.long_deposit_enabled,
            "long_withdraw": row.long_withdraw_enabled,
            "short_deposit": row.short_deposit_enabled,
            "short_withdraw": row.short_withdraw_enabled,
        },
        "next_action": row.next_action,
        "blockers": row.blockers[:5],
    }


def _compact_preflight(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    route = row.get("route") if isinstance(row.get("route"), dict) else {}
    return {
        "status": row.get("status"),
        "kind": row.get("kind") or route.get("kind"),
        "strategy_intent": row.get("strategy_intent"),
        "next_action": row.get("next_action"),
        "public_edge_pct": _float_or_none(row.get("public_edge_pct") or row.get("computed_edge_pct")),
        "blockers": _string_list(row.get("blockers"))[:5],
        "age_marker_us": row.get("_at_us"),
    }


def _compact_event(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "kind": item.get("kind"),
        "event": item.get("event"),
        "spread_pct": item.get("spread_pct"),
        "open_spread_pct": item.get("open_spread_pct"),
        "funding_delta_pct": item.get("funding_delta_pct"),
        "minutes_to_funding": item.get("minutes_to_funding"),
        "seconds": item.get("seconds"),
        "side": item.get("side"),
        "is_new": item.get("is_new"),
        "topic_id": item.get("topic_id"),
        "message_id": item.get("message_id"),
        "age_min": item.get("age_min"),
        "first_line": item.get("first_line"),
        "text_excerpt": item.get("text_excerpt"),
        "exchanges": item.get("exchanges", [])[:8],
        "chains": item.get("chains", []),
        "contract_count": len(item.get("contracts") or []),
        "source_role": item.get("source_role"),
        "liquidity_usd": item.get("liquidity_usd"),
        "max_volume_usd": item.get("max_volume_usd"),
    }


def _categorize_question(text: str) -> list[str]:
    lowered = text.casefold()
    categories = []
    for label, needles in QUESTION_CATEGORIES:
        if any(needle in lowered for needle in needles):
            categories.append(label)
    return categories


def _event_score(item: dict[str, Any]) -> float:
    score = 1.0
    event = item.get("event")
    kind = str(item.get("kind") or "")
    if event == "alert":
        score += 5
    if event == "close":
        score += 1
    if event == "momentum":
        score += 2
    if event == "funding_alert":
        score += 5
    if event == "community_signal":
        score += 7
    if event == "chat_signal":
        score += 4
    if item.get("source_role") == "lead_analyst":
        score += 8
    if "DEX" in kind:
        score += 4
    if item.get("is_new"):
        score += 2
    if item.get("contracts") and item.get("chains"):
        score += 3
    score += min(abs(_float_or_none(item.get("spread_pct")) or 0.0) / 5.0, 6.0)
    return score


def _infer_discussion_symbol(text: str) -> str | None:
    for pattern in (CHART_SYMBOL_PATTERN, TAGGED_SYMBOL_PATTERN, UPPER_SYMBOL_PATTERN):
        for match in pattern.finditer(text):
            symbol = _clean_symbol(match.group(1))
            if symbol and symbol not in SYMBOL_STOPWORDS:
                return symbol
    return None


def _alert_route_example(hot: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
    return _alert_example_with_freshness(
        {
            "symbol": hot.get("symbol"),
            "kind": best.get("kind"),
            "route_line": best.get("route_line"),
            "open_spread_pct": best.get("open_spread_pct"),
            "funding_apr_pct": best.get("funding_apr_pct"),
            "age_min": best.get("age_min"),
            "freshness": best.get("freshness"),
            "href": best.get("pair_url"),
            "source": "board",
        }
    )


def _alert_hot_example(item: dict[str, Any], *, generated_at_us: int | None) -> dict[str, Any]:
    latest_event = _int_or_none(item.get("latest_event"))
    age_min = None
    if generated_at_us and latest_event:
        age_min = max(0.0, (generated_at_us - latest_event) / 60_000_000.0)
    best = item.get("best_board") if isinstance(item.get("best_board"), dict) else {}
    fresh_board = best.get("freshness") == "fresh"
    return _alert_example_with_freshness(
        {
            "symbol": item.get("symbol"),
            "kind": best.get("kind") or _first_key(item.get("kinds")),
            "route_line": best.get("route_line") if fresh_board else "Telegram route change",
            "open_spread_pct": best.get("open_spread_pct") if fresh_board else None,
            "funding_apr_pct": best.get("funding_apr_pct") if fresh_board else None,
            "age_min": age_min,
            "event_count": item.get("event_count"),
            "freshness": "fresh" if age_min is not None and age_min <= ALERT_EVENT_FRESH_MAX_AGE_MIN else "stale",
            "route_freshness": best.get("freshness") if best else "not_enough_data",
            "source": "telegram",
        }
    )


def _alert_example_with_freshness(item: dict[str, Any]) -> dict[str, Any]:
    output = dict(item)
    output["freshness"] = _alert_freshness(output)
    if output["freshness"] == "stale" and not output.get("review_note"):
        output["review_note"] = "stale context, not a now-trigger"
    return output


def _split_fresh_examples(examples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fresh = [item for item in examples if item.get("freshness") == "fresh"]
    review = [item for item in examples if item.get("freshness") != "fresh"]
    return fresh, review


def _alert_freshness(item: dict[str, Any]) -> str:
    explicit = str(item.get("freshness") or "").casefold()
    if explicit in {"fresh", "stale", "missing", "error"}:
        return explicit
    age = _float_or_none(item.get("age_min"))
    if age is None:
        return "stale"
    source = str(item.get("source") or "").casefold()
    threshold = board.DEFAULT_FRESH_MAX_AGE_MIN if source == "board" else ALERT_EVENT_FRESH_MAX_AGE_MIN
    return "fresh" if age <= threshold else "stale"


def _preview_card(
    key: str,
    title: str,
    trigger: bool | list[Any],
    examples: list[Any],
    *,
    review_examples: list[Any] | None = None,
) -> dict[str, Any]:
    review_examples = review_examples or []
    would_trigger = bool(trigger)
    status = "would_trigger" if would_trigger else "review_only" if review_examples else "quiet"
    visible_examples = examples if would_trigger or not review_examples else review_examples
    return {
        "key": key,
        "title": title,
        "status": status,
        "would_trigger": would_trigger,
        "examples": visible_examples,
        "review_examples": review_examples,
        "review_count": len(review_examples),
    }


def _okx_identity_status(routes: list[dict[str, Any] | None]) -> str:
    if not routes:
        return "not_enough_data"
    if any(route and "DEX" in str(route.get("kind") or "") for route in routes):
        return "requires_exact_chain_contract"
    return "not_applicable"


def _file_freshness(path: Path, *, now: float) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False, "status": "missing", "age_min": None}
    age_min = max(0.0, (now - stat.st_mtime) / 60.0)
    return {
        "path": str(path),
        "exists": True,
        "status": _freshness_status(age_min, stale_min=120),
        "age_min": age_min,
        "size_bytes": stat.st_size,
    }


def _latest_timestamp(rows: list[dict[str, Any]]) -> int | None:
    values = []
    for row in rows:
        parsed = row.get("parsed") if isinstance(row.get("parsed"), dict) else {}
        value = _event_at_us(row, parsed)
        if value:
            values.append(value)
    return max(values) if values else None


def _event_at_us(row: dict[str, Any], parsed: dict[str, Any]) -> int | None:
    for key in ("received_at_us", "ingested_at_us"):
        value = _int_or_none(row.get(key))
        if value:
            return value
    for key in ("message_date",):
        value = row.get(key) or parsed.get(key)
        parsed_us = _iso_to_us(value)
        if parsed_us:
            return parsed_us
    return None


def _iso_to_us(value: Any) -> int | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return int(time.mktime(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")) * 1_000_000)
    except (ValueError, TypeError):
        return None


def _freshness_status(age_min: float | None, *, stale_min: float) -> str:
    if age_min is None:
        return "missing"
    return "stale" if age_min > stale_min else "fresh"


def _age_min(now: float, at_us: int | None) -> float | None:
    if at_us is None:
        return None
    return max(0.0, (now - at_us / 1_000_000) / 60.0)


def _safe_exchange_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "exchange": item.get("exchange"),
                "price": _float_or_none(item.get("price")),
                "funding": _float_or_none(item.get("funding")),
                "funding_24h": _float_or_none(item.get("funding_24h")),
                "deposit_enabled": item.get("deposit_enabled"),
                "withdraw_enabled": item.get("withdraw_enabled"),
                "time": item.get("time"),
            }
        )
    return rows


def _read_text(path: Path, *, max_chars: int) -> str:
    try:
        return _sanitize_text(path.read_text(encoding="utf-8", errors="replace"))[:max_chars]
    except OSError:
        return ""


def _clip(value: Any, *, max_chars: int = MAX_TEXT) -> str:
    text = " ".join(_sanitize_text(str(value or "")).split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _sanitize_text(text: str) -> str:
    safe = text
    for pattern in SECRET_TEXT_PATTERNS:
        safe = pattern.sub(lambda match: f"{match.group(1)}=[redacted]", safe)
    return safe


def _clean_symbol(value: Any) -> str | None:
    if value is None:
        return None
    symbol = "".join(ch for ch in str(value).upper() if ch.isalnum() or ch in {"_", "-"}).strip("-_")
    return symbol[:24] or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
