"""Demo (paper) blacklist for the live entry policy.

Strategy (operator 2026-05-29): enter ANY route with a good spread (and the
other rules — identity, depth, funding, a REAL short method, $25/leg cap),
EXCEPT tokens that have demonstrated in paper that they don't work — i.e. a
paper position that has been **open for a long time AND is currently losing**
(diverged). This is the inverse of the old "proven-convergence only" gate.

This module computes that blacklist read-only from the runtime DB. A token is
blacklisted if it has any open (un-closed) paper position that is both:
  - older than ``min_open_hours`` ("open for a long time"), and
  - currently underwater (latest marked ``unrealized_pnl_usd`` below
    ``-min_loss_usd``) ("losing on demo").
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Any

DEFAULT_MIN_OPEN_HOURS = 72.0
DEFAULT_MIN_LOSS_USD = 0.0
# The wide-net API-discovery paper ledger opens routes at "spreads" up to 90%,
# which the real-money policy would never take. Only count an API-discovery
# position as blacklist *evidence* if it was entered at a spread the real policy
# could plausibly have taken — anything above this ceiling is data noise (stale /
# illiquid quotes), not proof the token doesn't work. Sane-entry positions that
# then diverge ARE evidence and do get blacklisted.
DEFAULT_MAX_DISCOVERY_ENTRY_SPREAD_PCT = 50.0


def _now_us() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000_000)


def blacklisted_tokens(
    db_path: Path,
    *,
    min_open_hours: float = DEFAULT_MIN_OPEN_HOURS,
    min_loss_usd: float = DEFAULT_MIN_LOSS_USD,
    now_us: int | None = None,
    max_discovery_entry_spread_pct: float = DEFAULT_MAX_DISCOVERY_ENTRY_SPREAD_PCT,
) -> dict[str, dict[str, Any]]:
    """Return {TOKEN: {reason, source, open_hours, unrealized_pnl_usd,
    entry_spread_pct, current_spread_pct}} for tokens with a long-open + losing
    demo position.

    Two paper ledgers are consulted (a token is blacklisted if EITHER flags it):
      1. ``paper_positions`` + ``paper_pnl_ticks`` — the real-strategy paper
         ledger (latest mark per open position).
      2. ``api_discovery_paper_positions`` — the wide-net discovery ledger that
         also holds the DEX and broad-CEX paper routes. Only positions entered at
         a spread ``<= max_discovery_entry_spread_pct`` count as evidence (the
         wide net opens 90%-"spread" routes the real policy would never take).

    Read-only and fail-open *per source*: a missing/broken table contributes
    nothing rather than aborting the whole blacklist (an empty blacklist is the
    permissive default — the spread/identity/funding/short-method gates still
    apply to every candidate). Returns {} only if the DB can't be opened at all.
    """

    now = now_us if now_us is not None else _now_us()
    cutoff = now - int(min_open_hours * 3600 * 1_000_000)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except Exception:
        return {}

    blacklist: dict[str, dict[str, Any]] = {}

    def _consider(token: Any, opened_us: Any, upnl: Any, entry_spread: Any, current_spread: Any, source: str) -> None:
        tok = str(token or "").upper()
        if not tok or upnl is None or opened_us is None:
            return
        if float(upnl) >= -float(min_loss_usd):
            return  # not losing enough
        open_hours = round((now - int(opened_us)) / 3_600_000_000, 1)
        entry = {
            "reason": "demo_open_long_and_losing",
            "source": source,
            "open_hours": open_hours,
            "unrealized_pnl_usd": round(float(upnl), 4),
            "entry_spread_pct": str(entry_spread),
            "current_spread_pct": str(current_spread),
        }
        # Keep the worst (most negative) record per token, across both ledgers.
        prev = blacklist.get(tok)
        if prev is None or float(upnl) < prev["unrealized_pnl_usd"]:
            blacklist[tok] = entry

    # Source 1: the real-strategy paper ledger (latest mark per open position).
    try:
        for r in conn.execute(
            """
            WITH latest AS (
                SELECT position_id, MAX(ts_us) AS mt FROM paper_pnl_ticks GROUP BY position_id
            )
            SELECT pp.token AS token,
                   pp.opened_us AS opened_us,
                   pp.entry_spread_pct AS entry_spread_pct,
                   t.unrealized_pnl_usd AS upnl,
                   t.spread_pct AS current_spread_pct
            FROM paper_positions pp
            JOIN latest l ON l.position_id = pp.id
            JOIN paper_pnl_ticks t ON t.position_id = pp.id AND t.ts_us = l.mt
            WHERE pp.closed_us IS NULL AND pp.opened_us < ?
            """,
            (cutoff,),
        ).fetchall():
            _consider(r["token"], r["opened_us"], r["upnl"], r["entry_spread_pct"], r["current_spread_pct"], "paper")
    except Exception:
        pass

    # Source 2: the wide-net API-discovery paper ledger (DEX + broad-CEX routes).
    # current_net_pnl_usd is the live mark for an open route; only sane-entry
    # routes count as evidence (see DEFAULT_MAX_DISCOVERY_ENTRY_SPREAD_PCT).
    try:
        for r in conn.execute(
            """
            SELECT token,
                   opened_us,
                   entry_spread_pct,
                   current_net_pnl_usd AS upnl,
                   current_spread_pct
            FROM api_discovery_paper_positions
            WHERE closed_us IS NULL AND opened_us < ? AND entry_spread_pct <= ?
            """,
            (cutoff, float(max_discovery_entry_spread_pct)),
        ).fetchall():
            _consider(r["token"], r["opened_us"], r["upnl"], r["entry_spread_pct"], r["current_spread_pct"], "api_discovery")
    except Exception:
        pass

    conn.close()
    return blacklist
