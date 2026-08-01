"""Alert when a shut transfer rail reopens while the spread is still wide.

A fat spread that persists is usually evidence of a closed rail, not of an
opportunity -- if the coin could move, arbitrage would have closed it in minutes.
SIREN sat near 100% between OKX DEX and Kucoin purely because Kucoin deposits
were shut. The moment such a rail reopens the edge is briefly real, and that
window is the product: members need to know inside minutes, not at the next scan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import threading

from spreadboard import api_spreads, public_rails

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
DEFAULT_STATE_PATH = RUNTIME_DIR / "rail_reopen_state.json"

# Below this the reopen is not worth waking anyone for: ordinary rails open and
# shut all day, and only a surviving edge makes it actionable.
DEFAULT_MIN_EDGE_PCT = float(os.environ.get("SPREADBOARD_RAIL_REOPEN_MIN_EDGE_PCT", "2.0"))

DIRECTIONS = ("deposit", "withdraw")


def detect_reopens(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rails that went explicitly False -> True.

    Unknown (None) on either side is not a transition. A rail we simply had no
    reading for last cycle has not "reopened"; treating it as one would fire on
    every venue whose API was briefly unavailable.
    """
    reopened: list[dict[str, Any]] = []
    for venue, tokens in sorted(current.items()):
        if not isinstance(tokens, dict):
            continue
        previous_tokens = previous.get(venue) if isinstance(previous.get(venue), dict) else {}
        for token, state in sorted(tokens.items()):
            if not isinstance(state, dict):
                continue
            was = previous_tokens.get(token) if isinstance(previous_tokens.get(token), dict) else {}
            for direction in DIRECTIONS:
                if was.get(direction) is False and state.get(direction) is True:
                    reopened.append({"venue": venue, "token": token, "direction": direction})
    return reopened


def _route_uses(row: dict[str, Any], venue: str, direction: str) -> bool:
    """Does this route depend on that specific rail?

    Withdrawing happens from the leg you buy; depositing happens into the leg you
    sell. A reopen only matters for the side that actually needs it.
    """
    side = "long" if direction == "withdraw" else "short"
    return str(row.get(f"{side}_venue") or "") == venue


def alertable_reopens(
    reopens: list[dict[str, Any]],
    market: dict[str, Any],
    *,
    min_edge_pct: float = DEFAULT_MIN_EDGE_PCT,
) -> list[dict[str, Any]]:
    """Keep the reopens that leave a deliverable route with a surviving edge."""
    rows_by_token: dict[str, list[dict[str, Any]]] = {}
    for row in market.get("rows") or []:
        if isinstance(row, dict) and row.get("token"):
            rows_by_token.setdefault(str(row["token"]).upper(), []).append(row)

    alerts: list[dict[str, Any]] = []
    for reopen in reopens:
        token = str(reopen["token"]).upper()
        best: dict[str, Any] | None = None
        best_edge = min_edge_pct
        for row in rows_by_token.get(token, []):
            if not _route_uses(row, reopen["venue"], reopen["direction"]):
                continue
            # It has to be takeable now -- a reopened withdrawal does not help if
            # the other side's deposits are still shut.
            if row.get("deliverable") is not True:
                continue
            edge = _float(row.get("executable_spread_pct"))
            if edge is None or edge < best_edge:
                continue
            best, best_edge = row, edge
        if best is not None:
            alerts.append({**reopen, "edge_pct": best_edge, "route": best})
    return alerts


def format_alert(alert: dict[str, Any]) -> str:
    route = alert["route"]
    verb = "Withdrawals" if alert["direction"] == "withdraw" else "Deposits"
    return (
        f"RAIL REOPENED: {alert['token']} {verb.lower()} are open again on {alert['venue']}.\n"
        f"{alert['edge_pct']:+.2f}% still on the board: "
        f"buy {route.get('long_venue') or '?'} -> sell {route.get('short_venue') or '?'}.\n"
        f"{verb} were shut, which is why the spread survived. It will not survive long."
    )


class RailReopenWatcher:
    """Poll the public rail cache and announce reopens that still pay."""

    def __init__(
        self,
        *,
        state_path: Path | str = DEFAULT_STATE_PATH,
        poll_seconds: float = 300.0,
        min_edge_pct: float = DEFAULT_MIN_EDGE_PCT,
        notify: Any = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.poll_seconds = max(60.0, float(poll_seconds))
        self.min_edge_pct = float(min_edge_pct)
        self.notify = notify
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="spreadboard-rail-reopen", daemon=True
        )

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        if not self.running:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.running:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        self._stop.wait(30.0)
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not end the watch.
                print(f"spreadboard-rail-reopen: {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(self.poll_seconds)

    def check_once(self) -> dict[str, Any]:
        current = public_rails.load_public_rails()
        if not current:
            return {"status": "no_rail_data", "reopened": 0, "alerted": 0}
        previous = self._load_state()
        # First run has no baseline. Record it and stay quiet rather than
        # announcing every open rail on the board as a fresh reopening.
        if not previous:
            self._save_state(current)
            return {"status": "baseline_recorded", "reopened": 0, "alerted": 0}
        reopens = detect_reopens(previous, current)
        alerts: list[dict[str, Any]] = []
        if reopens:
            market = api_spreads.load_spreads(limit=None, include_stale=True)
            alerts = alertable_reopens(reopens, market, min_edge_pct=self.min_edge_pct)
            for alert in alerts:
                self._announce(alert)
        self._save_state(current)
        return {
            "status": "ok",
            "reopened": len(reopens),
            "alerted": len(alerts),
            "tokens": sorted({str(alert["token"]) for alert in alerts}),
        }

    def _announce(self, alert: dict[str, Any]) -> None:
        message = format_alert(alert)
        if self.notify is not None:
            self.notify(message)
            return
        chat_id = os.environ.get("SPREADBOARD_TELEGRAM_GROUP_CHAT_ID", "").strip()
        if not chat_id:
            print(f"spreadboard-rail-reopen: {message}", flush=True)
            return
        from spreadboard import telegram_bot

        telegram_bot.send_group_message(chat_id, message)

    def _load_state(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        rails = payload.get("rails") if isinstance(payload, dict) else None
        return rails if isinstance(rails, dict) else {}

    def _save_state(self, rails: dict[str, dict[str, Any]]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"rails": rails}, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
