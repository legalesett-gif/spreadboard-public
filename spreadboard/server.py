"""Local stdlib HTTP server for SpreadBoard."""

from __future__ import annotations

import html
import hashlib
import hmac
import json
import os
from http.cookies import SimpleCookie
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
import urllib.request

import click

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spreadboard import (  # noqa: E402
    accounts,
    alerts,
    api_spreads,
    billing,
    board,
    chart_catalog,
    crypto_billing,
    crypto_watcher,
    fair_price,
    historical_spreads,
    intel,
    live,
    live_book_cache,
    market_history,
    venue_funding_history,
    portfolio,
    telegram_bot,
)

#: Same story as the board cache: intel takes ~24s to build and a 20s life meant
#: it was rebuilt on almost every visit. It is derived from the same snapshot,
#: so it can live as long.
_INTEL_CACHE_TTL_SECONDS = max(
    20.0, float(os.environ.get("SPREADBOARD_INTEL_CACHE_SECONDS", "900"))
)
_INTEL_CACHE_LOCK = threading.Lock()
_INTEL_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
#: How long a grouped board payload stays served. This is the cache the pages
#: actually read, and at 20s it expired seconds after the warmer filled it --
#: which is why warming every view did not make the pages fast. It must outlive
#: the warm cycle. Prices do not go stale with it: the payload is structure, and
#: the stream re-prices what is on screen every three seconds.
_MARKET_CACHE_TTL_SECONDS = max(
    20.0, float(os.environ.get("SPREADBOARD_MARKET_CACHE_SECONDS", "900"))
)
#: Each payload is fully materialised and large, so the count is bounded to keep
#: a 4GB box off its limit -- an earlier unbounded version left it with 156MB.
_MARKET_CACHE_MAX_ENTRIES = max(4, int(os.environ.get("SPREADBOARD_MARKET_CACHE_ENTRIES", "14")))
_MARKET_CACHE_LOCK = threading.Lock()
_MARKET_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
#: The same payloads indexed by query alone. The full cache key includes the
#: snapshot's file signature, and the funding sweep rewrites that snapshot every
#: couple of minutes, so every view is invalidated together far more often than
#: the discovery scan runs. Serving the previous payload while the new one
#: builds is what keeps the board from going cold each time -- prices are not
#: stale with it, because the stream re-prices what is on screen every three
#: seconds; only the grouping is a little behind.
_MARKET_STALE_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_MARKET_STALE_MAX_SECONDS = max(
    60.0, float(os.environ.get("SPREADBOARD_MARKET_STALE_SECONDS", "1800"))
)
_MARKET_CACHE_INFLIGHT: dict[tuple[Any, ...], threading.Event] = {}
_CHART_SAMPLE_LOCK = threading.Lock()
_CHART_SAMPLE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CHART_SAMPLE_INFLIGHT: dict[str, threading.Event] = {}
_CHART_SAMPLE_SLOTS = threading.BoundedSemaphore(
    max(1, int(os.environ.get("SPREADBOARD_CHART_SAMPLE_CONCURRENCY", "2")))
)
_PUBLIC_INTEL_FEED_URL = os.environ.get(
    "SPREADBOARD_PUBLIC_INTEL_URL",
    "https://gist.githubusercontent.com/legalesett-gif/"
    "b348e50f10b0ad7de8b71fd619ea7151/raw/spreadboard-community-feed.json",
)
_PUBLIC_INTEL_FEED_CACHE: tuple[float, dict[str, Any]] | None = None
TERMS_VERSION = "2026-07-31"

DISPLAY_LABELS = {
    "available_on_pair_page": "Available on pair page",
    "blocked": "Blocked",
    "check_carry_and_basis": "Check carry and basis",
    "close_without_recent_alert": "Close without recent alert",
    "closed_or_faded": "Closed or faded",
    "converging": "Converging",
    "empty": "Empty",
    "error": "Error",
    "exact_chain_contract_required": "Exact chain and contract required",
    "fresh": "Fresh",
    "inspect_blockers": "Inspect blockers",
    "inspect_pair": "Open pair",
    "matched_board": "Matched board",
    "market_symbol_unresolved": "Venue symbol unresolved",
    "missing": "Missing",
    "no_local_signal": "No local signal",
    "not_applicable": "Not applicable",
    "not_enough_data": "Not enough data",
    "open_or_unresolved": "Open or unresolved",
    "private_collateral_open_order_position_checks_required": "Public data only",
    "profile_shell_only": "Profile shell only",
    "queue_public_preflight": "Open live route",
    "range_bound": "Range-bound",
    "refresh_or_explain_source": "Refresh or explain source",
    "requires_exact_chain_contract": "Exact chain and contract needed",
    "research_from_telegram": "Research from Telegram",
    "setup_needed": "Limited data",
    "normalized_funding_time_required": "Funding schedule unavailable",
    "basis_and_exit_monitor_required": "History is collecting",
    "source_unavailable": "Source unavailable",
    "spot_sell_inventory_required": "Spot or DEX inventory required",
    "stale": "Stale",
    "stale_route": "Stale route",
    "telegram_only": "Telegram only",
    "unavailable": "Unavailable",
    "unknown": "Unknown",
    "watch": "Watch",
    "watch_discussion_and_route_match": "Watch discussion",
    "watch_only": "Watch only",
    "verify_identity": "Verify identity",
    "widening": "Widening",
}

PLAYBOOK_DEFS = [
    {
        "category": "Pushover / alerts",
        "title": "Alert rules and notification hygiene",
        "why": "Use alerts for state changes: new route, spread or funding threshold, stale source, route change, or community call.",
        "answer": "This pass is preview-only. Treat the alert cards as a dry run of what would trigger, then wire Pushover later behind an explicit send-enabled change.",
        "checks": [
            "Open the alert planner and confirm which cards would trigger now.",
            "Check source freshness before trusting an alert idea.",
            "Prefer state-change alerts over repeated reminders.",
        ],
        "links": [("Alert planner", "/alerts"), ("Charts", "/charts")],
    },
    {
        "category": "Funding farms",
        "title": "Funding farm triage",
        "why": "Funding can dominate the visible spread, especially for DEX long plus perp short basis farms.",
        "answer": "Check current funding, 24h funding when available, open spread, convergence target, and whether the row is fresh before discussing entry or exit.",
        "checks": [
            "Open the funding page for APR outliers and pings.",
            "Open the pair cockpit to compare open spread with executable spread.",
            "Use route timeline to see whether the spread is converging or widening.",
        ],
        "links": [("Funding watch", "/funding"), ("Charts", "/charts")],
    },
    {
        "category": "D/W and transfer rails",
        "title": "Deposit, withdraw, chain, and contract checks",
        "why": "Spot and DEX routes can look profitable while being impossible because transfer rails, chain, or contract identity are unresolved.",
        "answer": "Treat DEX and transfer rows as research-only until exact chain, contract, venue symbols, and D/W direction are known.",
        "checks": [
            "Open the pair cockpit and inspect D/W and DEX gates.",
            "Use the route age and exact API rail fields to confirm current availability.",
            "Do not infer contract identity from ticker alone.",
        ],
        "links": [("Arbitrage", "/"), ("Triage", "/triage")],
    },
    {
        "category": "Why not futures-futures",
        "title": "Why a futures-futures row is not automatically enterable",
        "why": "Futures routes still need fresh books, venue symbols, funding context, collateral, open-position checks, and executor coverage.",
        "answer": "Use the pair cockpit as a proof surface, not a trade button. A green public quote is not enough to prove private readiness.",
        "checks": [
            "Compare open spread with executable VWAP.",
            "Check venue symbol resolution and funding.",
            "Look for route-health blockers and next action.",
        ],
        "links": [("Futures board", "/arbitrage?kind=FUTURES"), ("Triage", "/triage")],
    },
    {
        "category": "Missed spread",
        "title": "Missed spread postmortem",
        "why": "Fast rows decay. The useful question is whether the source was stale, liquidity vanished, or the route was never executable.",
        "answer": "Check row age, route timeline, executable spread, visible depth, and whether community/Telegram saw the same token earlier.",
        "checks": [
            "Open Charts to inspect the local history.",
            "Open Charts to check the captured API history.",
            "Open Signals to see whether the token had recent alerts or closes.",
        ],
        "links": [("Charts", "/charts"), ("Signals", "/signals")],
    },
    {
        "category": "Convergence / exit",
        "title": "Convergence and exit discussion",
        "why": "A route is converging only when the live long-vs-short basis moves toward the target, not because the thesis feels right.",
        "answer": "Use the pair timeline and current spread. Separate observed spread movement from funding carry and from full-convergence profit.",
        "checks": [
            "Open the pair route timeline.",
            "Compare current open spread with previous samples.",
            "Check funding separately from spread convergence.",
        ],
        "links": [("Charts", "/charts"), ("Funding", "/funding")],
    },
    {
        "category": "Liquidation / PnL",
        "title": "PnL and liquidation framing",
        "why": "Community users often mix mark-to-market PnL, funding, convergence value, leverage, and liquidation risk.",
        "answer": "Keep PnL profile-ready but not live in this pass. For now, show route context and leave real personal PnL to a later authenticated profile layer.",
        "checks": [
            "Use Watchlist for browser-local pins only.",
            "Do not claim personal PnL from public board rows.",
            "Keep future PnL features separate from public community data.",
        ],
        "links": [("Watchlist", "/watchlist"), ("Learn", "/learn")],
    },
]


class SpreadBoardServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        board_path: Path,
        config: dict[str, Any],
        accounts_path: Path | str = accounts.DEFAULT_DB_PATH,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.board_path = board_path
        self.config = config
        self.accounts_path = Path(accounts_path)
        accounts.initialize(self.accounts_path)
        self.alert_watcher: alerts.AlertWatcher | None = None
        self.position_alert_worker: Any = None


class SpreadBoardHandler(BaseHTTPRequestHandler):
    server: SpreadBoardServer
    _login_attempts: dict[str, list[float]] = {}
    _login_attempts_lock = threading.Lock()

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authorize(parsed.path, head_only=True):
            return
        public_paths = {
            "/",
            "/login",
            "/pricing",
            "/terms",
            "/privacy",
            "/refunds",
            "/guide",
            "/subscription",
            "/register",
            "/account",
            "/markets",
            "/intel",
            "/triage",
            "/arbitrage",
            "/charts",
            "/signals",
            "/funding",
            "/community",
            "/playbook",
            "/learn",
            "/profile",
            "/alerts",
            "/watchlist",
            "/favicon.ico",
        }
        if parsed.path in public_paths or parsed.path.startswith(("/pair/", "/token/", "/api/", "/assets/")):
            self._send_empty(HTTPStatus.OK)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorize(parsed.path):
            return
        accounts.set_current_user(getattr(self, "current_user", None))
        try:
            if parsed.path == "/login":
                self._send_html(render_login_page(query))
            elif parsed.path == "/register":
                self._send_html(render_register_page())
            elif parsed.path == "/pricing":
                self._send_html(render_pricing_page())
            elif parsed.path == "/fair":
                self._send_html(render_fair_price_page())
            elif parsed.path == "/free":
                self._send_html(render_free_page(self.server.board_path))
            elif parsed.path == "/terms":
                self._send_html(render_legal_page("terms"))
            elif parsed.path == "/privacy":
                self._send_html(render_legal_page("privacy"))
            elif parsed.path == "/guide":
                self._send_html(render_guide_page())
            elif parsed.path == "/refunds":
                self._send_html(render_legal_page("refunds"))
            elif parsed.path == "/subscription":
                self._send_html(render_subscription_page())
            elif parsed.path == "/account":
                self._send_html(render_account_page(self.server.board_path, self.server.accounts_path))
            elif parsed.path.startswith("/api/billing/crypto/invoice/"):
                user = self._required_user()
                try:
                    invoice_id = int(parsed.path.rsplit("/", 1)[-1])
                except ValueError:
                    raise ValueError("invalid_invoice_id") from None
                invoice = crypto_billing.get_invoice(invoice_id, db_path=self.server.accounts_path)
                # Never let one member poll another member's invoice.
                if invoice is None or (invoice["user_id"] != user.id and not user.is_admin):
                    raise ValueError("invoice_not_found")
                self._send_json({"ok": True, "invoice": invoice})
            elif parsed.path == "/api/billing/crypto/pending":
                user = self._required_user()
                if not user.is_admin:
                    raise PermissionError("admin_required")
                self._send_json({
                    "ok": True,
                    "payments": crypto_billing.pending_payments(db_path=self.server.accounts_path),
                })
            elif parsed.path == "/api/session":
                user = getattr(self, "current_user", None)
                self._send_json(
                    {
                        "ok": user is not None,
                        "user": user.public_dict() if user else None,
                        "csrf_token": user.csrf_token if user else None,
                        "billing": billing.status(),
                        "crypto_billing": crypto_billing.status(),
                        "telegram": telegram_bot.status(),
                    },
                    status=HTTPStatus.OK if user else HTTPStatus.UNAUTHORIZED,
                )
            elif parsed.path == "/api/portfolio":
                self._send_json(api_portfolio(self._required_user(), self.server.board_path, self.server.accounts_path))
            elif parsed.path == "/api/position-suggestions":
                self._send_json(api_position_suggestions(self.server.board_path, query))
            elif parsed.path == "/api/notification-preferences":
                user = self._required_user()
                self._send_json({"ok": True, "preferences": accounts.notification_preferences(user.id, db_path=self.server.accounts_path)})
            elif parsed.path == "/api/market-alert-rules":
                user = self._required_user()
                self._send_json({"ok": True, "rules": accounts.list_market_alert_rules(user.id, db_path=self.server.accounts_path)})
            elif parsed.path == "/api/saved-charts":
                user = self._required_user()
                self._send_json({
                    "ok": True,
                    "charts": accounts.list_saved_charts(user.id, db_path=self.server.accounts_path),
                })
            elif parsed.path == "/api/account-users":
                user = self._required_user()
                if not user.is_admin:
                    self._send_json({"ok": False, "error": "admin_required"}, status=HTTPStatus.FORBIDDEN)
                else:
                    self._send_json({"ok": True, "users": accounts.list_users(db_path=self.server.accounts_path)})
            elif parsed.path == "/":
                self._send_html(render_markets_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/markets":
                self._send_html(render_markets_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/intel":
                self._send_html(render_intel_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/triage":
                self._send_html(render_triage_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/arbitrage":
                self._send_html(render_markets_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/charts":
                self._send_html(render_charts_page(
                    self.server.board_path,
                    self.server.config,
                    query,
                    user=getattr(self, "current_user", None),
                    accounts_path=self.server.accounts_path,
                ))
            elif parsed.path == "/signals":
                self._send_html(render_signals_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/funding":
                self._send_html(render_funding_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/community":
                self._send_html(render_community_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/playbook":
                self._send_html(render_playbook_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/learn":
                self._send_html(render_learn_page())
            elif parsed.path == "/profile":
                self._send_html(render_account_page(self.server.board_path, self.server.accounts_path))
            elif parsed.path == "/alerts":
                self._send_html(render_alerts_page(self.server.board_path, self.server.config, query))
            elif parsed.path == "/watchlist":
                self._send_html(render_watchlist_page(self.server.board_path, self.server.config, query))
            elif parsed.path.startswith("/pair/"):
                route_key = unquote(parsed.path.removeprefix("/pair/"))
                self._send_html(render_pair_page(route_key, self.server.board_path, self.server.config))
            elif parsed.path.startswith("/token/"):
                symbol = _clean_symbol(parsed.path.removeprefix("/token/"))
                self._send_html(render_token_page(symbol, self.server.board_path))
            elif parsed.path == "/api/board":
                self._send_json(api_board(self.server.board_path, query))
            elif parsed.path == "/api/spreads":
                self._send_json(api_market_spreads(self.server.board_path, query))
            elif parsed.path == "/api/chart-catalog":
                catalog = chart_catalog.load()
                token = _clean_symbol(_query_first(query, "token") or "")
                if token:
                    selected = [item for item in catalog.get("markets") or [] if item.get("token") == token]
                    self._send_json({"ok": bool(selected), "token": token, "count": len(selected), "markets": selected, "generated_at": catalog.get("generated_at")})
                else:
                    self._send_json({key: value for key, value in catalog.items() if key != "markets"} | {"tokens": sorted({item.get("token") for item in catalog.get("markets") or [] if item.get("token")})})
            elif parsed.path == "/api/alert-context":
                self._send_json(api_alert_context(self.server.board_path, query))
            elif parsed.path == "/api/intel":
                self._send_json(api_intel(self.server.board_path, query))
            elif parsed.path == "/api/triage":
                self._send_json(api_triage(self.server.board_path, query))
            elif parsed.path == "/api/alert-preview":
                self._send_json(api_alert_preview(self.server.board_path, query))
            elif parsed.path == "/api/profile-shell":
                self._send_json(api_profile_shell(self.server.board_path, query))
            elif parsed.path == "/api/watchlist-suggestions":
                self._send_json(api_watchlist_suggestions(self.server.board_path, query))
            elif parsed.path == "/api/signals":
                self._send_json(api_signals(self.server.board_path, query))
            elif parsed.path == "/api/funding-watch":
                self._send_json(api_funding_watch(self.server.board_path, query))
            elif parsed.path == "/api/community":
                self._send_json(api_community(self.server.board_path, query))
            elif parsed.path == "/api/playbook":
                self._send_json(api_playbook(self.server.board_path, query))
            elif parsed.path == "/api/source-health":
                self._send_json(api_source_health(self.server.board_path, self.server.config))
            elif parsed.path.startswith("/api/pair/"):
                route_key = unquote(parsed.path.removeprefix("/api/pair/"))
                self._send_json(api_pair(route_key, self.server.board_path, self.server.config))
            elif parsed.path.startswith("/api/history/"):
                route_key = unquote(parsed.path.removeprefix("/api/history/"))
                self._send_json(api_history(route_key, self.server.board_path, query))
            elif parsed.path == "/api/stream/board":
                self._send_board_stream(query)
            elif parsed.path == "/api/stream/free":
                # Pinned, not taken from the request: this one is public, and a
                # visitor must not be able to widen it into the whole board.
                self._send_board_stream(dict(FREE_BOARD_QUERY))
            elif parsed.path.startswith("/api/stream/"):
                route_key = unquote(parsed.path.removeprefix("/api/stream/"))
                self._send_chart_stream(route_key, query)
            elif parsed.path.startswith("/api/token/"):
                symbol = _clean_symbol(parsed.path.removeprefix("/api/token/"))
                self._send_json(api_token(symbol, self.server.board_path, include_live=not _query_bool(query, "local")))
            elif parsed.path == "/api/health":
                self._send_json(api_health(
                    self.server.board_path,
                    self.server.config,
                    self.server.alert_watcher,
                    self.server.position_alert_worker,
                ))
            elif parsed.path == "/assets/lightweight-charts.js":
                self._send_asset(
                    Path(__file__).with_name("static")
                    / "lightweight-charts.standalone.production.js",
                    "text/javascript; charset=utf-8",
                )
            elif parsed.path == "/favicon.ico":
                self._send_empty(HTTPStatus.NO_CONTENT)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            try:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                return
        finally:
            accounts.set_current_user(None)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authorize(parsed.path):
            return
        accounts.set_current_user(getattr(self, "current_user", None))
        try:
            if parsed.path == "/api/billing/webhook":
                raw = self._read_raw_body()
                event = billing.verify_webhook(raw, self.headers.get("Stripe-Signature", ""))
                result = accounts.apply_billing_event(
                    event,
                    payload_sha256=hashlib.sha256(raw).hexdigest(),
                    db_path=self.server.accounts_path,
                )
                self._send_json(result)
                return
            if parsed.path == "/api/telegram/webhook":
                telegram_bot.verify_webhook(self.headers.get("X-Telegram-Bot-Api-Secret-Token", ""))
                response = telegram_bot.handle_update(
                    telegram_bot.parse_update(self._read_raw_body()),
                    board_path=self.server.board_path,
                    db_path=self.server.accounts_path,
                )
                self._send_json(response or {"ok": True})
                return
            if parsed.path == "/api/register":
                self._handle_register()
                return
            if parsed.path == "/api/login":
                self._handle_login()
                return
            if parsed.path == "/api/logout":
                self._require_csrf()
                token = self._session_token()
                if token:
                    accounts.logout(token, self.server.accounts_path)
                self._send_json({"ok": True}, clear_session=True)
                return
            self._require_csrf()
            payload = self._read_payload()
            user = self._required_user()
            if parsed.path == "/api/billing/checkout":
                if not payload.get("terms_accepted") or not payload.get("immediate_access_consent"):
                    raise ValueError("subscription_consent_required")
                accounts.record_subscription_consent(
                    user.id,
                    terms_version=TERMS_VERSION,
                    immediate_access=True,
                    ip_address=self.client_address[0] if self.client_address else "",
                    user_agent=self.headers.get("User-Agent", ""),
                    db_path=self.server.accounts_path,
                )
                self._send_json({"ok": True, "url": billing.create_checkout_session(user)})
            elif parsed.path == "/api/billing/portal":
                self._send_json({"ok": True, "url": billing.create_portal_session(user)})
            elif parsed.path == "/api/billing/crypto/invoice":
                if not payload.get("terms_accepted") or not payload.get("immediate_access_consent"):
                    raise ValueError("subscription_consent_required")
                accounts.record_subscription_consent(
                    user.id,
                    terms_version=TERMS_VERSION,
                    immediate_access=True,
                    ip_address=self.client_address[0] if self.client_address else "",
                    user_agent=self.headers.get("User-Agent", ""),
                    db_path=self.server.accounts_path,
                )
                try:
                    period_days = int(payload.get("period_days") or 0)
                except (TypeError, ValueError):
                    raise ValueError("invalid_period") from None
                invoice = crypto_billing.create_invoice(
                    user.id, period_days, db_path=self.server.accounts_path
                )
                self._send_json({"ok": True, "invoice": invoice}, status=HTTPStatus.CREATED)
            elif parsed.path == "/api/billing/crypto/settle":
                if not user.is_admin:
                    raise PermissionError("admin_required")
                try:
                    invoice_id = int(payload.get("invoice_id") or 0)
                except (TypeError, ValueError):
                    raise ValueError("invalid_invoice_id") from None
                self._send_json({
                    "ok": True,
                    "result": crypto_billing.settle_manually(
                        invoice_id, db_path=self.server.accounts_path
                    ),
                })
            elif parsed.path == "/api/positions":
                position = accounts.create_position(user.id, payload, db_path=self.server.accounts_path)
                self._send_json({"ok": True, "position": position}, status=HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/positions/") and parsed.path.endswith("/close"):
                position_id = int(parsed.path.split("/")[3])
                position = accounts.close_position(user.id, position_id, payload, db_path=self.server.accounts_path)
                self._send_json({"ok": True, "position": position})
            elif parsed.path.startswith("/api/positions/") and parsed.path.endswith("/funding"):
                position_id = int(parsed.path.split("/")[3])
                event = accounts.add_funding_cashflow(user.id, position_id, payload, db_path=self.server.accounts_path)
                self._send_json({"ok": True, "funding_cashflow": event}, status=HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/positions/") and parsed.path.endswith("/alerts"):
                position_id = int(parsed.path.split("/")[3])
                rule = accounts.add_alert_rule(user.id, position_id, payload, db_path=self.server.accounts_path)
                self._send_json({"ok": True, "alert_rule": rule}, status=HTTPStatus.CREATED)
            elif parsed.path == "/api/account-settings":
                updated = accounts.update_account_settings(
                    user.id,
                    display_name=str(payload.get("display_name") or user.display_name),
                    monthly_capital_usd=_float_or_none(payload.get("monthly_capital_usd")),
                    db_path=self.server.accounts_path,
                )
                self._send_json({"ok": True, "user": updated})
            elif parsed.path == "/api/telegram/link":
                token = accounts.create_telegram_link_token(user.id, db_path=self.server.accounts_path)
                self._send_json({"ok": True, "url": telegram_bot.link_url(token), "expires_in_seconds": 600})
            elif parsed.path == "/api/telegram/unlink":
                self._send_json({"ok": True, "unlinked": accounts.unlink_telegram_chat(user.id, db_path=self.server.accounts_path)})
            elif parsed.path == "/api/notification-preferences":
                preferences = accounts.save_notification_preferences(
                    user.id, payload, db_path=self.server.accounts_path
                )
                self._send_json({"ok": True, "preferences": preferences})
            elif parsed.path == "/api/market-alert-rules":
                try:
                    rule = accounts.add_market_alert_rule(
                        user.id, payload, db_path=self.server.accounts_path
                    )
                except (ValueError, TypeError) as exc:
                    # A bad threshold or a missing token is the member's typo,
                    # not a server fault, and the form shows what comes back.
                    self._send_json(
                        {"ok": False, "error": str(exc)[:200]},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                else:
                    self._send_json({"ok": True, "rule": rule}, status=HTTPStatus.CREATED)
            elif parsed.path == "/api/saved-charts":
                try:
                    chart = accounts.add_saved_chart(
                        user.id, payload, db_path=self.server.accounts_path
                    )
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                else:
                    self._send_json({"ok": True, "chart": chart}, status=HTTPStatus.CREATED)
            elif parsed.path == "/api/saved-charts/delete":
                removed = accounts.delete_saved_chart(
                    user.id,
                    str(payload.get("route_key") or ""),
                    db_path=self.server.accounts_path,
                )
                self._send_json({"ok": removed})
            elif parsed.path.startswith("/api/market-alert-rules/"):
                # This server speaks GET and POST only, so a member's edits and
                # deletes ride on POST sub-paths rather than PATCH/DELETE.
                tail = parsed.path.removeprefix("/api/market-alert-rules/")
                rule_id, _, action = tail.partition("/")
                if not rule_id.isdigit():
                    self._send_json({"ok": False, "error": "unknown_rule"}, status=HTTPStatus.NOT_FOUND)
                elif action == "delete":
                    removed = accounts.delete_market_alert_rule(
                        user.id, int(rule_id), db_path=self.server.accounts_path
                    )
                    self._send_json(
                        {"ok": removed},
                        status=HTTPStatus.OK if removed else HTTPStatus.NOT_FOUND,
                    )
                else:
                    rule = accounts.update_market_alert_rule(
                        user.id, int(rule_id), payload, db_path=self.server.accounts_path
                    )
                    self._send_json(
                        {"ok": rule is not None, "rule": rule},
                        status=HTTPStatus.OK if rule else HTTPStatus.NOT_FOUND,
                    )
            elif parsed.path == "/api/notifications/read":
                count = accounts.mark_notifications_read(user.id, db_path=self.server.accounts_path)
                self._send_json({"ok": True, "updated": count})
            elif parsed.path == "/api/account-users":
                if not user.is_admin:
                    self._send_json({"ok": False, "error": "admin_required"}, status=HTTPStatus.FORBIDDEN)
                else:
                    created = accounts.create_user(
                        email=str(payload.get("email") or ""),
                        display_name=str(payload.get("display_name") or ""),
                        password=str(payload.get("password") or ""),
                        subscription_status=str(payload.get("subscription_status") or "trialing"),
                        subscription_days=int(payload.get("subscription_days") or 30),
                        db_path=self.server.accounts_path,
                    )
                    self._send_json({"ok": True, "user": created}, status=HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/account-users/") and parsed.path.endswith("/subscription"):
                if not user.is_admin:
                    self._send_json({"ok": False, "error": "admin_required"}, status=HTTPStatus.FORBIDDEN)
                else:
                    target_id = int(parsed.path.split("/")[3])
                    updated = accounts.update_subscription(
                        target_id,
                        status=str(payload.get("status") or "inactive"),
                        expires_at=str(payload.get("expires_at") or "") or None,
                        db_path=self.server.accounts_path,
                    )
                    self._send_json({"ok": True, "user": updated})
            elif parsed.path == "/api/alert-test":
                self._send_json(alerts.send_user_test_alert(user.id, accounts_path=self.server.accounts_path))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except (ValueError, billing.BillingError, telegram_bot.TelegramBotError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        finally:
            accounts.set_current_user(None)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        sys.stderr.write("spreadboard: " + format % args + "\n")

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(
        self,
        data: object,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        session_token: str | None = None,
        clear_session: bool = False,
    ) -> None:
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if session_token:
            self.send_header(
                "Set-Cookie",
                f"{accounts.SESSION_COOKIE}={session_token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={accounts.SESSION_DAYS * 86400}",
            )
        elif clear_session:
            self.send_header(
                "Set-Cookie",
                f"{accounts.SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0",
            )
        self._send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorize(self, path: str, *, head_only: bool = False) -> bool:
        self.current_user = None
        if not accounts.auth_required():
            return True
        public = path in {"/login", "/register", "/pricing", "/guide", "/terms", "/privacy", "/refunds", "/free", "/api/stream/free", "/api/login", "/api/register", "/api/health", "/api/billing/webhook", "/api/telegram/webhook", "/favicon.ico"} or path.startswith("/assets/")
        token = self._session_token()
        user = accounts.user_for_session(token, self.server.accounts_path) if token else None
        self.current_user = user
        if public:
            return True
        if user is None:
            if path.startswith("/api/"):
                self._send_json({"ok": False, "error": "authentication_required"}, status=HTTPStatus.UNAUTHORIZED)
            elif head_only:
                self._send_empty(HTTPStatus.UNAUTHORIZED)
            elif path == "/":
                # The front door shows the product rather than a login form.
                self._redirect("/free")
            else:
                self._redirect("/login?" + urlencode({"next": self.path[:500]}))
            return False
        subscription_paths = {"/subscription", "/account", "/profile", "/api/session", "/api/portfolio", "/api/logout", "/api/account-settings", "/api/notifications/read", "/api/billing/checkout", "/api/billing/portal", "/api/billing/crypto/invoice", "/api/telegram/link", "/api/telegram/unlink"}
        # Paying members-to-be have no active subscription yet, so the checkout
        # and invoice-polling routes must stay reachable or nobody can ever buy.
        allowed_without_subscription = (
            path in subscription_paths
            or path.startswith("/api/billing/crypto/invoice/")
        )
        if not user.subscription_active and not allowed_without_subscription and not (user.is_admin and path.startswith("/api/account-users")):
            if path.startswith("/api/"):
                self._send_json({"ok": False, "error": "subscription_required"}, status=HTTPStatus.PAYMENT_REQUIRED)
            elif head_only:
                self._send_empty(HTTPStatus.PAYMENT_REQUIRED)
            else:
                self._redirect("/subscription")
            return False
        return True

    def _handle_login(self) -> None:
        payload = self._read_payload()
        key = self.client_address[0] if self.client_address else "unknown"
        now = time.monotonic()
        with self._login_attempts_lock:
            recent = [item for item in self._login_attempts.get(key, []) if now - item < 900]
            if len(recent) >= 10:
                self._send_json({"ok": False, "error": "too_many_login_attempts"}, status=HTTPStatus.TOO_MANY_REQUESTS)
                return
            self._login_attempts[key] = recent
        try:
            user, token = accounts.login(
                str(payload.get("email") or ""),
                str(payload.get("password") or ""),
                user_agent=self.headers.get("User-Agent", ""),
                ip_address=key,
                db_path=self.server.accounts_path,
            )
        except ValueError:
            with self._login_attempts_lock:
                self._login_attempts.setdefault(key, []).append(now)
            time.sleep(0.25)
            self._send_json({"ok": False, "error": "invalid_credentials"}, status=HTTPStatus.UNAUTHORIZED)
            return
        with self._login_attempts_lock:
            self._login_attempts.pop(key, None)
        self._send_json(
            {"ok": True, "user": user.public_dict(), "csrf_token": user.csrf_token},
            session_token=token,
        )

    def _handle_register(self) -> None:
        payload = self._read_payload()
        key = "register:" + (self.client_address[0] if self.client_address else "unknown")
        now = time.monotonic()
        with self._login_attempts_lock:
            recent = [item for item in self._login_attempts.get(key, []) if now - item < 3600]
            if len(recent) >= 10:
                self._send_json({"ok": False, "error": "too_many_registration_attempts"}, status=HTTPStatus.TOO_MANY_REQUESTS)
                return
            self._login_attempts[key] = recent + [now]
        accounts.create_user(
            email=str(payload.get("email") or ""),
            display_name=str(payload.get("display_name") or ""),
            password=str(payload.get("password") or ""),
            subscription_status="inactive",
            subscription_days=1,
            db_path=self.server.accounts_path,
        )
        user, token = accounts.login(
            str(payload.get("email") or ""), str(payload.get("password") or ""),
            user_agent=self.headers.get("User-Agent", ""), ip_address=key,
            db_path=self.server.accounts_path,
        )
        self._send_json(
            {"ok": True, "user": user.public_dict(), "csrf_token": user.csrf_token, "next": "/subscription"},
            status=HTTPStatus.CREATED, session_token=token,
        )

    def _required_user(self) -> accounts.User:
        user = getattr(self, "current_user", None)
        if user is None:
            raise ValueError("authentication_required")
        return user

    def _require_csrf(self) -> None:
        user = self._required_user()
        supplied = self.headers.get("X-CSRF-Token", "")
        if not supplied or not user.csrf_token or not hmac.compare_digest(supplied, user.csrf_token):
            raise ValueError("invalid_csrf_token")

    def _session_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(accounts.SESSION_COOKIE)
        return morsel.value if morsel is not None else ""

    def _read_payload(self) -> dict[str, Any]:
        raw = self._read_raw_body() or b"{}"
        if "application/json" in self.headers.get("Content-Type", ""):
            value = json.loads(raw.decode("utf-8") or "{}")
            return value if isinstance(value, dict) else {}
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {key: values[0] if values else "" for key, values in parsed.items()}

    def _read_raw_body(self) -> bytes:
        try:
            declared = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("invalid_content_length") from exc
        if declared < 0 or declared > 1_000_000:
            raise ValueError("request_body_too_large")
        return self.rfile.read(declared) if declared else b""

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_chart_stream(self, route_key: str, query: dict[str, list[str]]) -> None:
        interval = max(
            1.0,
            min(
                10.0,
                float(os.environ.get("SPREADBOARD_CHART_STREAM_SECONDS", "2")),
            ),
        )
        hours = max(1 / 60, min(_query_float(query, "hours", 1) or 1, 24 * 30))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._send_security_headers()
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            for _ in range(300):
                payload = _chart_stream_payload(route_key, self.server.board_path, hours)
                event = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
                self.wfile.write(b"event: quote\n")
                self.wfile.write(b"data: " + event + b"\n\n")
                self.wfile.flush()
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_board_stream(self, query: dict[str, list[str]]) -> None:
        """Push price changes to an open board instead of waiting for a reload.

        The data went live once routes were priced from the streaming books, but
        a member still only saw it by refreshing. On a spread that lasts minutes
        that is the difference between a trade and a screenshot.
        """
        # A spread that ticks 20.0 -> 20.1 -> 19.9 has to arrive as it happens,
        # not on a three-second beat. The books are flushed every 0.25s and the
        # re-pricing is shared between streams, so this can sit near that.
        interval = max(
            0.2, min(15.0, float(os.environ.get("SPREADBOARD_BOARD_STREAM_SECONDS", "0.5")))
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._send_security_headers()
        self.end_headers()
        previous: dict[str, tuple[Any, Any]] = {}
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            for _ in range(20_000):
                rows = _shared_stream_rows(self.server.board_path, query)
                changed = {
                    key: value for key, value in rows.items() if previous.get(key) != value
                }
                previous = rows
                if changed:
                    payload = {
                        "updated_at": datetime.now(tz=timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "routes": [
                            {"route_key": key, "spread_pct": value[0], "funding_pct": value[1]}
                            for key, value in changed.items()
                        ],
                    }
                    event = json.dumps(payload, separators=(",", ":"), default=str).encode()
                    self.wfile.write(b"event: board\n")
                    self.wfile.write(b"data: " + event + b"\n\n")
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_asset(self, path: Path, content_type: str) -> None:
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self._send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_security_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'",
        )
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")


def api_board(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    query = query or {}
    canonical_query = {key: list(values) for key, values in query.items()}
    canonical_query.setdefault("limit", ["500"])
    canonical_query.setdefault("sort", ["edge"])
    canonical_query.setdefault("direction", ["desc"])
    if "min_spread_pct" not in canonical_query and "min_open_spread_pct" in canonical_query:
        canonical_query["min_spread_pct"] = list(canonical_query["min_open_spread_pct"])

    market = api_market_spreads(board_path, canonical_query)
    max_age_min = _query_float(query, "max_age_min", board.DEFAULT_FRESH_MAX_AGE_MIN)
    rows = [
        _canonical_pair_row(row)
        for row in market.get("rows") or []
        if max_age_min is None
        or (_float_or_none(row.get("age_min")) is not None and float(row["age_min"]) <= max_age_min)
    ]
    kind_counts: dict[str, int] = {}
    for row in rows:
        kind = str(row.get("kind") or row.get("route_kind") or "UNKNOWN")
        kind_counts[kind] = kind_counts.get(kind, 0) + 1

    canonical_health = (market.get("source_health") or {}).get("canonical_api") or {}
    return {
        **market,
        "mode": "canonical_public_api_board",
        "age_min": canonical_health.get("age_min"),
        "max_age_min": max_age_min,
        "fresh_count": len(rows),
        "stale_count": 0,
        "total_count": len(rows),
        "kind_counts": kind_counts,
        "rows": rows,
        "stale_rows": [],
        "route_kinds": board.route_kind_options(),
        "source": "canonical_public_exchange_apis",
    }


def _legacy_board_snapshot(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Keep the retired renderer testable without exposing archived rows through the API."""

    query = query or {}
    snapshot = board.load_board(
        board_path,
        kind=_query_first(query, "kind"),
        q=_query_first(query, "q"),
        exchange=_query_first(query, "exchange"),
        min_open_spread_pct=_query_float(query, "min_open_spread_pct"),
        max_age_min=_query_float(query, "max_age_min", board.DEFAULT_FRESH_MAX_AGE_MIN),
        include_stale=_query_bool(query, "include_stale"),
    )
    data = snapshot.to_dict()
    data["rows"] = [_decorate_board_row(row) for row in snapshot.rows]
    data["stale_rows"] = [_decorate_board_row(row) for row in snapshot.stale_rows]
    data["route_kinds"] = board.route_kind_options()
    return data


def api_market_spreads(
    board_path: Path,
    query: dict[str, list[str]] | None = None,
    *,
    allow_stale: bool = True,
) -> dict[str, Any]:
    query = query or {}
    limit = max(20, min(500, int(_query_float(query, "limit", api_spreads.DEFAULT_LIMIT) or api_spreads.DEFAULT_LIMIT)))
    offset = max(0, int(_query_float(query, "offset", 0) or 0))
    cache_key = None if _query_bool(query, "no_cache") else _market_cache_key(board_path, query)
    if cache_key is not None:
        cached = _market_cache_get(cache_key)
        if cached is not None:
            return cached
        with _MARKET_CACHE_LOCK:
            inflight = _MARKET_CACHE_INFLIGHT.get(cache_key)
            if inflight is None:
                inflight = threading.Event()
                _MARKET_CACHE_INFLIGHT[cache_key] = inflight
                owns_refresh = True
            else:
                owns_refresh = False
        if not owns_refresh:
            # Someone else is already building this view. A waiter must never
            # build its own copy -- that is what turned one slow build into
            # forty concurrent ones, threads 14 -> 40 and the process
            # 0.5GB -> 4.2GB until the kernel killed the container.
            #
            # But it must not WAIT for that build either when it already has a
            # perfectly good previous copy. Checking stale only after the wait
            # expired put a 25s pause in front of every page served while the
            # warmer held the build: /free measured 26s three times running with
            # the answer sitting in the stale cache the whole time.
            stale = _market_cache_stale_get(cache_key) if allow_stale else None
            if stale is not None:
                return stale
            # Nothing to serve yet, so waiting is the only option.
            deadline = time.monotonic() + _MARKET_BUILD_WAIT_SECONDS
            while not inflight.wait(timeout=1.0) and time.monotonic() < deadline:
                cached = _market_cache_get(cache_key)
                if cached is not None:
                    return cached
            cached = _market_cache_get(cache_key)
            if cached is not None:
                return cached
            stale = _market_cache_stale_get(cache_key) if allow_stale else None
            if stale is not None:
                return stale
            # The owner is still building. Say so rather than start a second
            # copy of the same work.
            return _market_warming_payload()
        # The snapshot moved under us. Serve what we built for this same view a
        # moment ago and let the refresh finish behind the request, rather than
        # holding a page open for a full rebuild.
        stale = _market_cache_stale_get(cache_key) if allow_stale else None
        if stale is not None:
            threading.Thread(
                target=_rebuild_market_cache,
                args=(board_path, dict(query), cache_key),
                daemon=True,
            ).start()
            return stale

    try:
        min_funding_24h = _query_float(query, "min_abs_funding_24h_pct")
        min_funding_apr = _query_float(query, "min_abs_funding_apr_pct")
        data = api_spreads.load_spreads(
            board_path=board_path,
            q=_query_first(query, "q"),
            exchange=_query_first(query, "exchange"),
            kind=_query_first(query, "kind"),
            source=_query_first(query, "source"),
            min_spread_pct=_query_float(query, "min_spread_pct"),
            min_abs_funding_24h_pct=min_funding_24h,
            min_abs_funding_apr_pct=min_funding_apr,
            funding_only=_query_bool(query, "funding_only"),
            include_stale=_market_include_stale(query),
            include_unverified=_query_bool(query, "include_unverified"),
            # The board is a list of trades to consider; a route whose rail is
            # shut is not one. The reopen watcher reads load_spreads directly
            # and still sees them.
            require_deliverable=True,
            sort_by=_query_first(query, "sort") or "edge",
            direction=_query_first(query, "direction") or "desc",
            offset=offset,
            limit=limit,
        )
    except Exception:
        if cache_key is not None:
            _market_cache_finish(cache_key, None)
        raise
    if cache_key is not None:
        _market_cache_finish(cache_key, data)
    return data


def _rebuild_market_cache(
    board_path: Path, query: dict[str, list[str]], cache_key: tuple[Any, ...]
) -> None:
    """Refresh a view behind the request that was served the previous payload."""
    # The request that served the stale payload registered the in-flight marker
    # and then returned, so nobody is holding it. Release it here or the rebuild
    # blocks on its own gate for the full wait before doing anything.
    with _MARKET_CACHE_LOCK:
        waiting = _MARKET_CACHE_INFLIGHT.pop(cache_key, None)
    if waiting is not None:
        waiting.set()
    try:
        # allow_stale=False so this actually rebuilds and stores, instead of
        # being handed back the very payload it was started to replace.
        api_market_spreads(board_path, query, allow_stale=False)
    except Exception:  # noqa: BLE001 - the stale payload is already serving.
        pass


def api_alert_context(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Return only the local market rows needed to evaluate browser-local rules."""

    query = query or {}
    route_keys = {value for value in query.get("route_key", []) if value}
    symbols = {_clean_symbol(value) for value in query.get("symbol", []) if _clean_symbol(value)}
    if not route_keys and not symbols:
        return {
            "ok": True,
            "mode": "read_only_local_alert_context",
            "rows": [],
            "requested_route_count": 0,
            "matched_route_count": 0,
        }
    snapshot = api_market_spreads(
        board_path,
        {
            "limit": ["500"],
            "include_stale": ["1"],
            "sort": ["edge"],
            "direction": ["desc"],
        },
    )
    rows = [
        row
        for row in snapshot.get("rows") or []
        if row.get("route_key") in route_keys or _clean_symbol(row.get("token") or "") in symbols
    ]
    return {
        "ok": bool(snapshot.get("ok")),
        "mode": "read_only_local_alert_context",
        "rows": rows,
        "requested_route_count": len(route_keys),
        "matched_route_count": len({row.get("route_key") for row in rows if row.get("route_key") in route_keys}),
        "source_health": {
            name: {
                key: value
                for key, value in (state or {}).items()
                if key in {"status", "age_min", "row_count", "fresh_count", "stale_count", "error"}
            }
            for name, state in (snapshot.get("source_health") or {}).items()
        },
    }


def _market_cache_key(board_path: Path, query: dict[str, list[str]]) -> tuple[Any, ...]:
    normalized_query = tuple(
        sorted(
            (str(key), tuple(str(value) for value in values))
            for key, values in query.items()
            if key != "no_cache"
        )
    )
    return (
        str(board_path.resolve()),
        _file_signature(board_path),
        _file_signature(api_spreads.DEFAULT_API_DISCOVERY_PATH),
        _file_signature(api_spreads.token_metadata.DEFAULT_CACHE_PATH),
        _file_signature(api_spreads.public_rails.DEFAULT_CACHE_PATH),
        normalized_query,
    )


def _file_signature(path: Path | str) -> tuple[int, int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _market_cache_get(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    now = time.monotonic()
    with _MARKET_CACHE_LOCK:
        cached = _MARKET_CACHE.get(cache_key)
        if cached and now - cached[0] <= _MARKET_CACHE_TTL_SECONDS:
            return cached[1]
        if cached:
            _MARKET_CACHE.pop(cache_key, None)
    return None


def _market_stale_key(cache_key: tuple[Any, ...]) -> tuple[Any, ...]:
    """The cache key without the snapshot signatures -- query and board only."""
    return (cache_key[0], cache_key[-1])


def _market_cache_stale_get(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    """The last payload built for this query, whatever snapshot produced it."""
    now = time.monotonic()
    with _MARKET_CACHE_LOCK:
        cached = _MARKET_STALE_CACHE.get(_market_stale_key(cache_key))
        if cached and now - cached[0] <= _MARKET_STALE_MAX_SECONDS:
            return cached[1]
    return None


def _market_cache_finish(cache_key: tuple[Any, ...], data: dict[str, Any] | None) -> None:
    with _MARKET_CACHE_LOCK:
        if data is not None:
            _MARKET_CACHE[cache_key] = (time.monotonic(), data)
            _MARKET_STALE_CACHE[_market_stale_key(cache_key)] = (time.monotonic(), data)
            if len(_MARKET_STALE_CACHE) > _MARKET_CACHE_MAX_ENTRIES:
                oldest = min(
                    _MARKET_STALE_CACHE, key=lambda key: _MARKET_STALE_CACHE[key][0]
                )
                _MARKET_STALE_CACHE.pop(oldest, None)
            if len(_MARKET_CACHE) > _MARKET_CACHE_MAX_ENTRIES:
                oldest = min(_MARKET_CACHE, key=lambda key: _MARKET_CACHE[key][0])
                _MARKET_CACHE.pop(oldest, None)
        inflight = _MARKET_CACHE_INFLIGHT.pop(cache_key, None)
        if inflight is not None:
            inflight.set()


#: How long a request waits for someone else's build before giving up. Longer
#: than a cold build so waiters do not pile on, short enough that nobody holds a
#: page open indefinitely.
_MARKET_BUILD_WAIT_SECONDS = max(
    5.0, float(os.environ.get("SPREADBOARD_MARKET_BUILD_WAIT_SECONDS", "25"))
)


def _market_warming_payload() -> dict[str, Any]:
    """What a view looks like while someone else is building it.

    `ok: False` is what every page already checks to decide between the board
    and its reconnecting state, so this needs no special handling anywhere.
    """
    return {
        "ok": False,
        "status": "warming",
        "summary": {},
        "groups": [],
        "rows": [],
        "top_edges": [],
        "top_funding": [],
        "exchange_options": [],
        "source_health": {"canonical_api": {"status": "warming"}},
        "pagination": {},
    }


def _market_include_stale(query: dict[str, list[str]]) -> bool:
    del query
    return False


#: One health build at a time. This calls load_spreads directly, so it is not
#: covered by the board's single-flight -- and the container probes it every
#: thirty seconds while Caddy and any waiting browser probe it too. Concurrent
#: cold builds here were part of the same stampede.
_HEALTH_BUILD_LOCK = threading.Lock()


_HEALTH_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_HEALTH_CACHE_TTL_SECONDS = max(
    5.0, float(os.environ.get("SPREADBOARD_HEALTH_CACHE_SECONDS", "30"))
)


def _health_from_snapshot_file(board_path: Path) -> dict[str, Any]:
    """A readiness answer that costs a stat, for when nothing is built yet."""
    del board_path
    try:
        stat = Path(api_spreads.DEFAULT_API_DISCOVERY_PATH).stat()
        age_min = max(0.0, (time.time() - stat.st_mtime) / 60.0)
        present = stat.st_size > 0
    except OSError:
        age_min, present = None, False
    return {
        "ok": present,
        "mode": "canonical_public_api_health",
        "canonical_api": {
            "status": "warming" if present else "unavailable",
            "age_min": age_min,
        },
        "websocket_books": _live_book_status(),
        "market": {},
    }


def api_source_health(board_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    del config
    # A readiness probe must never build the board. This ran load_spreads at
    # limit=0 -- its own cache key, 14s cold -- against a 12s healthcheck
    # timeout, so the container reported unhealthy while it was serving pages in
    # two seconds, and every probe joined the stampede that OOM-killed it.
    now = time.monotonic()
    cached = _HEALTH_CACHE.get("payload")
    if cached is not None and now - float(_HEALTH_CACHE["at"]) <= _HEALTH_CACHE_TTL_SECONDS:
        return cached
    if not _HEALTH_BUILD_LOCK.acquire(blocking=False):
        return cached if cached is not None else _health_from_snapshot_file(board_path)
    try:
        market = api_spreads.load_spreads(board_path=board_path, limit=0, include_stale=False)
    finally:
        _HEALTH_BUILD_LOCK.release()
    payload = {
        "ok": market.get("ok"),
        "mode": "canonical_public_api_health",
        "canonical_api": (market.get("source_health") or {}).get("canonical_api") or {},
        "websocket_books": _live_book_status(),
        "market": {
            "asset_count": (market.get("summary") or {}).get("total_tokens"),
            "route_count": (market.get("summary") or {}).get("total_rows"),
            "funding_pair_count": (market.get("summary") or {}).get("funding_rows"),
        },
    }
    _HEALTH_CACHE["payload"] = payload
    _HEALTH_CACHE["at"] = time.monotonic()
    return payload


def _live_book_status() -> dict[str, Any]:
    if not live_book_cache.DEFAULT_PATH.exists():
        return {"status": "empty", "books": 0, "age_seconds": None}
    store = live_book_cache.LiveBookStore()
    try:
        return store.status()
    finally:
        store.close()


def api_intel(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    query = query or {}
    params = _intel_params(query)
    if not (_query_bool(query, "refresh") or _query_bool(query, "no_cache")):
        key = _intel_cache_key(board_path, params)
        now = time.monotonic()
        with _INTEL_CACHE_LOCK:
            cached = _INTEL_CACHE.get(key)
            if cached and now - cached[0] <= _INTEL_CACHE_TTL_SECONDS:
                return cached[1]
    else:
        key = None
        now = time.monotonic()
    data = intel.build_intel(board_path=board_path, **params)
    data["source_freshness"] = _sanitized_source_freshness(data.get("source_freshness"))
    if _public_mode():
        data = _public_intel_payload(data, board_path)
    if key is not None:
        with _INTEL_CACHE_LOCK:
            _INTEL_CACHE[key] = (now, data)
    return data


def _sanitized_source_freshness(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(name): {
            key: item
            for key, item in state.items()
            if key not in {"path", "size_bytes"}
        }
        for name, state in value.items()
        if isinstance(state, dict)
    }


def _public_mode() -> bool:
    return os.environ.get("SPREADBOARD_PUBLIC_MODE", "").strip().casefold() in {"1", "true", "yes", "on"}


def _public_intel_payload(data: dict[str, Any], board_path: Path) -> dict[str, Any]:
    """Join anonymous community facts to the current canonical market board."""

    feed = _load_public_intel_feed()
    if not feed:
        result = dict(data)
        for key in (
            "action_queue",
            "community",
            "community_insights",
            "hot_symbols",
            "question_patterns",
            "recent_events",
            "route_reality",
        ):
            result[key] = [] if isinstance(result.get(key), list) else {}
        result["latest_brief"] = {
            "status": "unavailable",
            "title": "Anonymous feed unavailable",
            "body": "The market board remains live while the community bridge reconnects.",
        }
        result["source_freshness"] = {
            "canonical_api": api_source_health(board_path, {}).get("canonical_api") or {},
            "telegram": {"status": "unavailable", "detail": "Anonymous bridge reconnecting."},
        }
        result["mode"] = "public_anonymous_intel"
        return result

    market = api_spreads.load_spreads(board_path=board_path, limit=None, include_stale=False)
    groups = {
        str(group.get("token") or ""): group
        for group in market.get("groups") or []
    }
    feed_age = max(
        0.0,
        (time.time() * 1_000_000 - (_float_or_none(feed.get("generated_at_us")) or 0.0))
        / 60_000_000.0,
    )
    requested_limit = max(
        1,
        min(int(_float_or_none((data.get("filters") or {}).get("limit")) or 12), 50),
    )
    hot = []
    reality = []
    for item in (feed.get("hot_symbols") or [])[:requested_limit]:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        symbol = str(item.get("symbol"))
        group = groups.get(symbol)
        best = _public_intel_best_route(group)
        hot.append({**item, "best_board": best})
        reality.append(_public_intel_route_reality(symbol, group))
    recent = {}
    for bucket, rows in (feed.get("recent_events") or {}).items():
        recent[bucket] = [
            {**row, "age_min": (_float_or_none(row.get("age_min")) or 0.0) + feed_age}
            for row in rows[:requested_limit]
            if isinstance(row, dict)
        ]
    action_queue = []
    for item in hot[:8]:
        best = item.get("best_board") or {}
        action_queue.append(
            {
                "symbol": item.get("symbol"),
                "status": "inspect_pair" if best else "watch",
                "href": best.get("pair_url") or f"/token/{item.get('symbol')}",
                "board_href": "/markets",
                "route_line": best.get("route_line") or "Community signal; no live route match",
                "reason": (
                    "Lead analyst signal joined to live markets"
                    if item.get("lead_analyst_count")
                    else "Anonymous community activity joined to live markets"
                ),
                "spread_pct": best.get("open_spread_pct"),
                "funding_24h_pct": best.get("funding_24h_pct"),
                "freshness": best.get("freshness") or "community_only",
                "next_action": "inspect_pair" if best else "watch",
                "badges": ["lead analyst"] if item.get("lead_analyst_count") else ["community"],
                "blockers": [],
            }
        )
    result = dict(data)
    result.update(
        {
            "mode": "public_anonymous_intel",
            "hot_symbols": hot,
            "recent_events": recent,
            "route_reality": reality,
            "action_queue": action_queue,
            "question_patterns": [],
            "community": {},
            "community_insights": {},
            "latest_brief": {
                "status": "fresh" if feed_age <= 10 else "stale",
                "title": "Anonymous community feed",
                "age_min": feed_age,
                "body": (
                    "Structured symbols, routes, spreads, funding and venue context only. "
                    "Message text and identities are never published."
                ),
            },
            "profile_shell": intel.build_profile_shell(hot, reality),
            "source_freshness": {
                "canonical_api": (market.get("source_health") or {}).get("canonical_api") or {},
                "telegram": {
                    "status": "fresh" if feed_age <= 10 else "stale",
                    "age_min": feed_age,
                    "detail": "Anonymous structured feed; no messages or identities.",
                },
            },
        }
    )
    return result


def _load_public_intel_feed() -> dict[str, Any] | None:
    global _PUBLIC_INTEL_FEED_CACHE
    now = time.monotonic()
    if _PUBLIC_INTEL_FEED_CACHE and now - _PUBLIC_INTEL_FEED_CACHE[0] <= 30:
        return _PUBLIC_INTEL_FEED_CACHE[1]
    try:
        request = urllib.request.Request(
            _PUBLIC_INTEL_FEED_URL,
            headers={"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            feed = json.load(response)
    except Exception:  # noqa: BLE001
        return _PUBLIC_INTEL_FEED_CACHE[1] if _PUBLIC_INTEL_FEED_CACHE else None
    if not isinstance(feed, dict) or not (feed.get("privacy") or {}).get("anonymous"):
        return None
    _PUBLIC_INTEL_FEED_CACHE = (now, feed)
    return feed


def _public_intel_best_route(group: dict[str, Any] | None) -> dict[str, Any] | None:
    if not group:
        return None
    row = group.get("best_route") or {}
    route_key = str(row.get("route_key") or "")
    return {
        "kind": row.get("route_kind"),
        "route_line": (
            f"{row.get('long_venue') or '?'} {row.get('long_market_type') or '?'} "
            f"→ {row.get('short_venue') or '?'} {row.get('short_market_type') or '?'}"
        ),
        "pair_url": f"/pair/{board.route_key_url(route_key)}",
        "open_spread_pct": row.get("executable_spread_pct"),
        "funding_24h_pct": row.get("funding_24h_pct"),
        "freshness": row.get("freshness") or "fresh",
    }


def _public_intel_route_reality(
    symbol: str,
    group: dict[str, Any] | None,
) -> dict[str, Any]:
    routes = []
    for row in (group or {}).get("routes") or []:
        route_key = str(row.get("route_key") or "")
        routes.append(
            {
                "kind": row.get("route_kind"),
                "pair_url": f"/pair/{board.route_key_url(route_key)}",
                "open_spread_pct": row.get("executable_spread_pct"),
                "funding_24h_pct": row.get("funding_24h_pct"),
                "freshness": row.get("freshness") or "fresh",
            }
        )
    return {
        "symbol": symbol,
        "status": "live_match" if routes else "community_only",
        "routes": routes[:3],
        "top_blockers": [],
        "next_actions": ["inspect pair"] if routes else ["watch for route"],
        "volatility": "available on pair page" if routes else "not enough data",
        "okx_dex_identity": (
            "route available"
            if any("DEX" in str(row.get("kind") or "") for row in routes)
            else "not applicable"
        ),
    }


def _intel_params(query: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "window_hours": _query_float(query, "window_hours", intel.DEFAULT_WINDOW_HOURS) or intel.DEFAULT_WINDOW_HOURS,
        "kind": _query_first(query, "kind"),
        "symbol": _clean_symbol(_query_first(query, "symbol") or _query_first(query, "q") or ""),
        "topic": _query_first(query, "topic"),
        "limit": int(_query_float(query, "limit", intel.DEFAULT_LIMIT) or intel.DEFAULT_LIMIT),
    }


def _intel_cache_key(board_path: Path, params: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(board_path.resolve()),
        round(float(params.get("window_hours") or intel.DEFAULT_WINDOW_HOURS), 4),
        str(params.get("kind") or ""),
        str(params.get("symbol") or ""),
        str(params.get("topic") or ""),
        int(params.get("limit") or intel.DEFAULT_LIMIT),
    )


def api_triage(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    data = api_intel(board_path, query)
    buckets = build_triage_buckets(data)
    return {
        "ok": data.get("ok"),
        "mode": "read_only_local_triage",
        "filters": data.get("filters"),
        "source_freshness": data.get("source_freshness") or {},
        "summary": {
            "look_now": len(buckets["look_now"]),
            "setup_needed": len(buckets["setup_needed"]),
            "funding_carry": len(buckets["funding_carry"]),
            "dex_identity": len(buckets["dex_identity"]),
            "community_spike": len(buckets["community_spike"]),
            "stale_routes": len(buckets["stale_routes"]),
            "source_gaps": len(buckets["source_gaps"]),
        },
        "buckets": buckets,
    }


def api_alert_preview(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return api_intel(board_path, query).get("alert_preview") or {}


def api_profile_shell(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return api_intel(board_path, query).get("profile_shell") or {}


def api_watchlist_suggestions(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    data = api_intel(board_path, query)
    return {
        "ok": data.get("ok"),
        "mode": "local_only_watchlist_shell",
        "filters": data.get("filters"),
        "source_freshness": data.get("source_freshness") or {},
        "profile_shell": data.get("profile_shell") or {},
        "hot_symbols": data.get("hot_symbols") or [],
        "route_reality": data.get("route_reality") or [],
        "alert_preview": data.get("alert_preview") or {},
    }


def api_position_suggestions(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    query = query or {}
    requested = _clean_symbol(_query_first(query, "q") or _query_first(query, "token") or "")
    limit = max(1, min(50, int(_query_float(query, "limit", 20) or 20)))
    catalogue = chart_catalog.load()
    catalog_markets = [item for item in catalogue.get("markets") or [] if isinstance(item, dict)]
    market = api_spreads.load_spreads(
        board_path=board_path,
        q=requested or None,
        include_stale=False,
        include_unverified=False,
        limit=None,
        sort_by="edge",
        direction="desc",
    )
    live_rows = [item for item in market.get("rows") or [] if isinstance(item, dict)]
    tokens = sorted(
        {
            str(item.get("token") or "").upper()
            for item in [*catalog_markets, *live_rows]
            if item.get("token") and (not requested or str(item.get("token") or "").upper().startswith(requested))
        }
    )[:limit]
    exact_rows = [
        row
        for row in live_rows
        if requested and str(row.get("token") or "").upper() == requested
    ]
    routes = []
    for row in exact_rows[:limit]:
        routes.append(
            {
                "token": row.get("token"),
                "route_key": row.get("route_key"),
                "route_kind": row.get("route_kind"),
                "long_venue": row.get("long_venue"),
                "long_market_type": row.get("long_market_type"),
                "long_symbol": row.get("long_market_symbol"),
                "long_entry_price": row.get("long_ask") or row.get("long_price"),
                "short_venue": row.get("short_venue"),
                "short_market_type": row.get("short_market_type"),
                "short_symbol": row.get("short_market_symbol"),
                "short_entry_price": row.get("short_bid") or row.get("short_price"),
                "entry_spread_pct": row.get("depth_weighted_spread_pct")
                if row.get("depth_weighted_spread_pct") is not None
                else row.get("executable_spread_pct"),
                "funding_24h_pct": row.get("funding_24h_pct"),
                "age_min": row.get("age_min"),
            }
        )
    legs = [
        {
            "token": item.get("token"),
            "venue": item.get("venue"),
            "market_type": item.get("market_type"),
            "symbol": item.get("symbol"),
        }
        for item in catalog_markets
        if requested and str(item.get("token") or "").upper() == requested
    ][:200]
    return {
        "ok": bool(tokens or routes or legs),
        "query": requested,
        "tokens": tokens,
        "routes": routes,
        "legs": legs,
        "catalog_generated_at": catalogue.get("generated_at"),
    }


def api_signals(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    data = api_intel(board_path, query)
    return {
        "ok": data.get("ok"),
        "mode": data.get("mode"),
        "filters": data.get("filters"),
        "source_freshness": data.get("source_freshness"),
        "hot_symbols": data.get("hot_symbols"),
        "recent_events": data.get("recent_events"),
        "community": data.get("community"),
        "question_patterns": data.get("question_patterns"),
    }


def api_funding_watch(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    data = api_intel(board_path, query)
    return {
        "ok": data.get("ok"),
        "mode": data.get("mode"),
        "filters": data.get("filters"),
        "source_freshness": data.get("source_freshness"),
        "funding_watch": data.get("funding_watch"),
        "alert_preview": data.get("alert_preview"),
        "recent_funding": (data.get("recent_events") or {}).get("funding") or [],
    }


def api_community(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    data = api_intel(board_path, query)
    insights = data.get("community_insights") or {}
    return {
        "ok": data.get("ok"),
        "mode": data.get("mode"),
        "filters": data.get("filters"),
        "source_freshness": data.get("source_freshness"),
        "community_insights": insights,
        "latest_brief": {
            "status": (data.get("latest_brief") or {}).get("status"),
            "title": (data.get("latest_brief") or {}).get("title"),
            "age_min": (data.get("latest_brief") or {}).get("age_min"),
        },
        "question_patterns": data.get("question_patterns") or [],
    }


def api_playbook(board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    data = api_intel(board_path, query)
    patterns = {
        str(item.get("category") or ""): item
        for item in data.get("question_patterns") or []
        if isinstance(item, dict)
    }
    source = data.get("source_freshness") or {}
    latest_brief = data.get("latest_brief") or {}
    cards = []
    for definition in PLAYBOOK_DEFS:
        pattern = patterns.get(definition["category"]) or {}
        examples = pattern.get("examples") or []
        cards.append(
            {
                **definition,
                "count": int(pattern.get("count") or 0),
                "status": "active" if pattern.get("count") else "ready",
                "examples": examples[:3],
            }
        )
    cards.sort(key=lambda item: (item["count"], item["category"] in {"Funding farms", "D/W and transfer rails"}), reverse=True)
    cards.sort(key=lambda item: 0 if item["count"] else 1)
    telegram = source.get("telegram_events") or {}
    brief = source.get("topic_brief") or {}
    return {
        "ok": data.get("ok"),
        "mode": "read_only_community_playbook",
        "filters": data.get("filters"),
        "source_freshness": source,
        "latest_brief": {
            "status": latest_brief.get("status"),
            "title": latest_brief.get("title"),
            "age_min": latest_brief.get("age_min"),
        },
        "source_note": {
            "telegram_status": telegram.get("status"),
            "telegram_age_min": telegram.get("age_min"),
            "brief_status": brief.get("status") or latest_brief.get("status"),
            "brief_age_min": brief.get("age_min") or latest_brief.get("age_min"),
            "message": playbook_source_message(telegram, brief or latest_brief),
        },
        "cards": cards,
        "quick_links": [
            {"label": "Intel", "href": "/"},
            {"label": "Triage", "href": "/triage"},
            {"label": "Signals", "href": "/signals"},
            {"label": "Alerts", "href": "/alerts"},
        ],
        "read_only_guards": [
            "No Pushover send path",
            "No live orders",
            "No swaps or approvals",
            "No transfers or withdrawals",
            "No private balance reads",
        ],
    }


def playbook_source_message(telegram: dict[str, Any], brief: dict[str, Any]) -> str:
    telegram_status = str(telegram.get("status") or "missing")
    brief_status = str(brief.get("status") or "missing")
    if telegram_status == "fresh" and brief_status == "fresh":
        return "Community inputs are fresh. Counts reflect the selected local window."
    if telegram_status == "fresh":
        return "Telegram events are fresh, but the topic brief is not fresh. Use live clusters over the brief."
    if brief_status == "fresh":
        return "Topic brief is fresh, but the raw Telegram event stream is stale or unavailable."
    return "Community inputs are stale or quiet. Playbook cards remain useful as operator checklists, not current crowd proof."


def build_triage_buckets(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    hot_by_symbol = {
        str(item.get("symbol") or ""): item
        for item in data.get("hot_symbols") or []
        if isinstance(item, dict) and item.get("symbol")
    }
    reality_rows = [
        _triage_route_item(item, hot_by_symbol.get(str(item.get("symbol") or "")))
        for item in data.get("route_reality") or []
        if isinstance(item, dict)
    ]
    reality_rows.sort(key=lambda item: _triage_sort_key(item), reverse=True)
    with_routes = [item for item in reality_rows if item.get("route_count")]
    fresh_routes = [item for item in with_routes if _triage_is_fresh_route(item)]
    stale_routes = [item for item in with_routes if not _triage_is_fresh_route(item)]
    setup_needed = [
        item
        for item in reality_rows
        if item.get("blockers") or str(item.get("status") or "") in {"setup_needed", "watch_only", "telegram_only"}
    ]
    funding = [_triage_funding_item(item) for item in data.get("funding_watch") or [] if isinstance(item, dict)]
    funding.sort(key=lambda item: abs(_float_or_none(item.get("funding_apr_pct")) or _float_or_none(item.get("funding_delta_pct")) or 0.0), reverse=True)
    dex_identity = [
        item
        for item in reality_rows
        if str(item.get("okx_dex_identity") or "").casefold()
        in {"requires_exact_chain_contract", "exact_chain_contract_required", "unknown", "not_applicable"}
        or any("chain" in str(blocker).casefold() or "contract" in str(blocker).casefold() for blocker in item.get("blockers") or [])
    ]
    community = [
        _triage_community_item(item)
        for item in data.get("hot_symbols") or []
        if isinstance(item, dict)
        and ((item.get("community_count") or 0) or (item.get("event_count") or 0) >= 2 or item.get("new_count"))
    ]
    community.sort(key=lambda item: (_float_or_none(item.get("score")) or 0.0, item.get("event_count") or 0), reverse=True)
    source_gaps = [
        _triage_source_item(name, item)
        for name, item in (data.get("source_freshness") or {}).items()
        if isinstance(item, dict) and item.get("status") in {"stale", "missing", "error"}
    ]
    source_gaps.sort(key=lambda item: _float_or_none(item.get("age_min")) or -1, reverse=True)
    return {
        "look_now": fresh_routes[:8],
        "setup_needed": setup_needed[:8],
        "funding_carry": funding[:8],
        "dex_identity": dex_identity[:8],
        "community_spike": community[:8],
        "stale_routes": stale_routes[:8],
        "source_gaps": source_gaps[:8],
    }


def _triage_route_item(item: dict[str, Any], hot: dict[str, Any] | None = None) -> dict[str, Any]:
    routes = item.get("routes") if isinstance(item.get("routes"), list) else []
    top = routes[0] if routes else {}
    hot = hot or {}
    best = hot.get("best_board") if isinstance(hot.get("best_board"), dict) else {}
    return {
        "symbol": item.get("symbol") or hot.get("symbol"),
        "status": item.get("status") or ("matched_board" if routes else "telegram_only"),
        "score": hot.get("score"),
        "event_count": hot.get("event_count") or 0,
        "route_count": len(routes),
        "route_url": top.get("pair_url") or best.get("pair_url"),
        "route_line": top.get("route_line") or best.get("route_line") or "No matched board route",
        "kind": top.get("kind") or best.get("kind"),
        "freshness": top.get("freshness") or best.get("freshness"),
        "open_spread_pct": top.get("open_spread_pct") if top.get("open_spread_pct") is not None else best.get("open_spread_pct"),
        "funding_apr_pct": top.get("funding_apr_pct") if top.get("funding_apr_pct") is not None else best.get("funding_apr_pct"),
        "age_min": top.get("age_min") if top.get("age_min") is not None else best.get("age_min"),
        "next_actions": list(item.get("next_actions") or [])[:3],
        "blockers": list(item.get("top_blockers") or [])[:4],
        "okx_dex_identity": item.get("okx_dex_identity"),
        "volatility": item.get("volatility"),
        "decision": _triage_decision(item, routes),
    }


def _triage_decision(item: dict[str, Any], routes: list[dict[str, Any]]) -> str:
    if not routes:
        return "research_from_telegram"
    blockers = item.get("top_blockers") or []
    if any("exact_chain_contract" in str(blocker) or "contract" in str(blocker).casefold() for blocker in blockers):
        return "verify_identity"
    if blockers:
        return "inspect_blockers"
    return "inspect_pair"


def _triage_is_fresh_route(item: dict[str, Any]) -> bool:
    freshness = str(item.get("freshness") or "").casefold()
    if freshness in {"fresh", "ok"}:
        return True
    age = _float_or_none(item.get("age_min"))
    return age is not None and age <= board.DEFAULT_FRESH_MAX_AGE_MIN


def _triage_sort_key(item: dict[str, Any]) -> tuple[float, float, float]:
    spread = abs(_float_or_none(item.get("open_spread_pct")) or 0.0)
    funding = abs(_float_or_none(item.get("funding_apr_pct")) or 0.0) / 20.0
    score = (_float_or_none(item.get("score")) or 0.0) / 10.0
    freshness_bonus = 2.0 if _triage_is_fresh_route(item) else 0.0
    return (spread + funding + score + freshness_bonus, item.get("route_count") or 0, -(item.get("age_min") or 999999))


def _triage_funding_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "kind": item.get("kind"),
        "source": item.get("source"),
        "funding_apr_pct": item.get("funding_apr_pct"),
        "funding_delta_pct": item.get("funding_delta_pct"),
        "open_spread_pct": item.get("open_spread_pct"),
        "minutes_to_funding": item.get("minutes_to_funding"),
        "age_min": item.get("age_min"),
        "decision": "check_carry_and_basis",
    }


def _triage_community_item(item: dict[str, Any]) -> dict[str, Any]:
    best = item.get("best_board") if isinstance(item.get("best_board"), dict) else {}
    return {
        "symbol": item.get("symbol"),
        "score": item.get("score"),
        "event_count": item.get("event_count"),
        "alert_count": item.get("alert_count"),
        "close_count": item.get("close_count"),
        "new_count": item.get("new_count"),
        "funding_count": item.get("funding_count"),
        "community_count": item.get("community_count"),
        "route_url": best.get("pair_url"),
        "route_line": best.get("route_line") or "Telegram/community only",
        "open_spread_pct": best.get("open_spread_pct"),
        "decision": "watch_discussion_and_route_match",
    }


def _triage_source_item(name: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": name,
        "status": item.get("status"),
        "age_min": item.get("age_min"),
        "path": item.get("path"),
        "title": item.get("title"),
        "decision": "refresh_or_explain_source",
    }


def api_pair(route_key: str, board_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    canonical_row = _find_canonical_route(route_key, board_path)
    if canonical_row is not None:
        row_data = _canonical_pair_row(canonical_row)
    else:
        row = board.find_route(route_key, board_path)
        if row is None:
            return {"ok": False, "error": "route_not_found", "route_key": route_key}
        row_data = _decorate_board_row(row)
    return {"ok": True, **live.get_route_detail(row_data, config=config)}


def _canonical_pair_row(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data.update(
        {
            "symbol": row.get("token"),
            "kind": row.get("route_kind"),
            "kind_label": route_kind_display(row.get("route_kind")),
            "kind_class": f"kind-{str(row.get('route_kind') or '').lower().replace('-', '_')}",
            "spread_pct": row.get("executable_spread_pct"),
            "displayed_open_spread_pct": row.get("displayed_open_spread_pct")
            if row.get("displayed_open_spread_pct") is not None
            else row.get("executable_spread_pct"),
            "displayed_headline_spread_pct": row.get("displayed_open_spread_pct")
            if row.get("displayed_open_spread_pct") is not None
            else row.get("executable_spread_pct"),
            "route_line": (
                f"Buy on {row.get('long_venue') or '?'} {row.get('long_market_type') or '?'}, "
                f"sell on {row.get('short_venue') or '?'} {row.get('short_market_type') or '?'}"
            ),
            "pair_url": f"/pair/{board.route_key_url(str(row.get('route_key') or ''))}",
            "chart_url": f"/charts?route_key={board.route_key_url(str(row.get('route_key') or ''))}",
            "strategy_verdict": "current_api_data",
            "next_action": "monitor_route",
            "blockers": list(row.get("conditions") or []),
            "canonical_api": True,
        }
    )
    return data


def api_history(route_key: str, board_path: Path, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
    query = query or {}
    points = int(_query_float(query, "max_points", 240) or 240)
    hours = max(1 / 60, min(_query_float(query, "hours", 24) or 24, 24 * 30))
    bucket_seconds = max(0, min(int(_query_float(query, "bucket_seconds", 0) or 0), 3600))
    since_us = int((time.time() - hours * 3600) * 1_000_000)
    current = _find_canonical_route(route_key, board_path)
    sample = (
        _refresh_chart_route(current)
        if current is not None and _query_bool(query, "live")
        else {"status": "idle"}
    )
    public_rows = market_history.load_history(
        route_key=route_key,
        max_points=points,
        since_us=since_us,
        bucket_seconds=bucket_seconds or None,
    )
    proxy = historical_spreads.load_or_fetch(current, hours=hours, max_points=points) if current is not None else {"status": "not_applicable", "rows": []}
    proxy_rows = proxy.get("rows") or []
    if proxy_rows:
        public_rows = _merge_history_rows(
            proxy_rows,
            public_rows,
            since_us=since_us,
            max_points=points,
            bucket_seconds=bucket_seconds,
        )
    if public_rows:
        meta = _history_meta(public_rows)
        meta.update(_history_coverage_meta(public_rows, hours, proxy))
        return {
            "ok": True,
            "mode": "canonical_public_api_history",
            "route_key": route_key,
            "count": len(public_rows),
            "sample": sample,
            "meta": meta,
            "rows": public_rows,
        }
    if current is not None:
        current_point = _current_history_point(current)
        current_ts = _float_or_none(current_point.get("quote_ts_us")) or 0
        rows = [current_point] if current_ts >= since_us else []
        return {
            "ok": True,
            "mode": "canonical_public_api_current_snapshot",
            "route_key": route_key,
            "count": len(rows),
            "collecting": True,
            "sample": sample,
            "meta": _history_meta(rows),
            "rows": rows,
        }
    rows = board.load_history(board_path, route_key=route_key, max_points=points)
    return {
        "ok": bool(rows),
        "route_key": route_key,
        "count": len(rows),
        "sample": sample,
        "meta": _history_meta(rows),
        "rows": [_decorate_history_row(row) for row in rows],
    }


def _merge_history_rows(
    proxy_rows: list[dict[str, Any]],
    exact_rows: list[dict[str, Any]],
    *,
    since_us: int,
    max_points: int,
    bucket_seconds: int = 0,
) -> list[dict[str, Any]]:
    """Merge proxy and exact samples without sacrificing full-window coverage."""
    bucket_us = max(0, int(bucket_seconds)) * 1_000_000
    merged: dict[int, dict[str, Any]] = {}

    def add(rows: list[dict[str, Any]], *, exact: bool) -> None:
        for row in rows:
            timestamp = int(row.get("quote_ts_us") or 0)
            if timestamp < since_us:
                continue
            key = timestamp // bucket_us if bucket_us else timestamp
            current = merged.get(key)
            current_exact = bool(current and current.get("sample_source") != "historical_ohlcv_close_proxy")
            if current is None or exact or (not current_exact and timestamp >= int(current.get("quote_ts_us") or 0)):
                merged[key] = row

    add(proxy_rows, exact=False)
    add(exact_rows, exact=True)
    rows = sorted(merged.values(), key=lambda item: int(item.get("quote_ts_us") or 0))
    return historical_spreads.evenly_sample(rows, max_points=max_points)


def _chart_stream_payload(route_key: str, board_path: Path, hours: float) -> dict[str, Any]:
    history = api_history(
        route_key,
        board_path,
        {
            "live": ["1"],
            "hours": [str(hours)],
            "max_points": ["1"],
            "no_cache": ["1"],
        },
    )
    rows = history.get("rows") or []
    return {
        "ok": bool(history.get("ok")),
        "route_key": route_key,
        "row": rows[-1] if rows else None,
        "sample": history.get("sample") or {},
        "meta": history.get("meta") or {},
    }


#: route_key -> row for the current snapshot. Finding one route used to build
#: the entire twelve-thousand-row board and then scan it, which cost 14.6s on
#: every chart opened by route -- most of the thirty seconds a member waited.
_ROUTE_INDEX: dict[str, Any] = {"signature": None, "rows": {}}
_ROUTE_INDEX_LOCK = threading.Lock()


def _route_index(board_path: Path) -> dict[str, dict[str, Any]]:
    """Every route on the board keyed by route_key, rebuilt when it changes."""
    signature = (
        str(board_path),
        _file_signature(board_path),
        _file_signature(api_spreads.DEFAULT_API_DISCOVERY_PATH),
    )
    with _ROUTE_INDEX_LOCK:
        if _ROUTE_INDEX["signature"] == signature:
            return _ROUTE_INDEX["rows"]
    market = api_spreads.load_spreads(
        board_path=board_path,
        include_stale=True,
        # A route can be quarantined by the fast refresh after the user opens
        # it. Keep the exact row available to the chart sampler so it can make
        # one fresh public quote attempt and report the real result.
        include_unverified=True,
        limit=None,
    )
    index = {
        str(row.get("route_key") or ""): row
        for row in market.get("rows") or []
        if row.get("route_key")
    }
    with _ROUTE_INDEX_LOCK:
        _ROUTE_INDEX["signature"] = signature
        _ROUTE_INDEX["rows"] = index
    return index


def _find_canonical_route(route_key: str, board_path: Path) -> dict[str, Any] | None:
    custom = chart_catalog.route_from_key(route_key)
    if custom is not None:
        return custom
    return _route_index(board_path).get(route_key)


def _refresh_chart_route(row: dict[str, Any]) -> dict[str, Any]:
    route_key = str(row.get("route_key") or "")
    configured_interval = float(os.environ.get("SPREADBOARD_CHART_SAMPLE_SECONDS", "2"))
    min_interval = (
        max(1.0, configured_interval)
        if _native_chart_route(row)
        else max(4.0, configured_interval)
    )
    now = time.monotonic()
    with _CHART_SAMPLE_LOCK:
        cached = _CHART_SAMPLE_CACHE.get(route_key)
        if cached and now - cached[0] < min_interval:
            return {**cached[1], "cached": True}
        inflight = _CHART_SAMPLE_INFLIGHT.get(route_key)
        if inflight is None:
            inflight = threading.Event()
            _CHART_SAMPLE_INFLIGHT[route_key] = inflight
            leader = True
        else:
            leader = False
    if not leader:
        inflight.wait(timeout=25.0)
        with _CHART_SAMPLE_LOCK:
            cached = _CHART_SAMPLE_CACHE.get(route_key)
        return {**(cached[1] if cached else {"status": "timeout"}), "cached": True}

    result: dict[str, Any]
    if not _CHART_SAMPLE_SLOTS.acquire(timeout=1.5):
        result = {"status": "busy", "error": "chart_sampler_capacity"}
        with _CHART_SAMPLE_LOCK:
            _CHART_SAMPLE_CACHE[route_key] = (time.monotonic(), result)
            event = _CHART_SAMPLE_INFLIGHT.pop(route_key, None)
            if event is not None:
                event.set()
        return result
    started = time.monotonic()
    try:
        if _native_chart_route(row):
            from spreadboard.fast_quotes import FastQuoteRefresher

            refresher = FastQuoteRefresher()
            try:
                worker = refresher.quote_route(row, target_notional_usd=50.0)
            finally:
                refresher.close()
            worker_exit_code = 0 if worker.get("status") == "ok" else 1
        else:
            command = [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "scripts/route_quote_worker.py"),
            ]
            completed = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                input=json.dumps(row, separators=(",", ":"), default=str),
                capture_output=True,
                text=True,
                timeout=float(os.environ.get("SPREADBOARD_CHART_SAMPLE_TIMEOUT_SECONDS", "22")),
                check=False,
            )
            worker = json.loads((completed.stdout or "").strip().splitlines()[-1])
            worker_exit_code = completed.returncode
        quoted_row = worker.get("row") if isinstance(worker, dict) else None
        if worker_exit_code == 0 and isinstance(quoted_row, dict):
            inserted = market_history.record_route(
                quoted_row,
                sample_source="live_chart_exact_route",
            )
            result = {
                "status": "ok",
                "inserted": inserted,
                "row": quoted_row,
                "quote_ts_us": quoted_row.get("quote_ts_us"),
                "target_notional_usd": worker.get("target_notional_usd") or 50.0,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        else:
            result = {
                "status": "unavailable",
                "error": str((worker or {}).get("error") or "route_quote_failed")[:120],
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
    except subprocess.TimeoutExpired:
        result = {"status": "timeout", "error": "route_quote_timeout"}
    except (IndexError, json.JSONDecodeError, OSError) as exc:
        result = {
            "status": "unavailable",
            "error": f"{type(exc).__name__}:chart_sampler_failed"[:120],
        }
    finally:
        _CHART_SAMPLE_SLOTS.release()
    with _CHART_SAMPLE_LOCK:
        _CHART_SAMPLE_CACHE[route_key] = (time.monotonic(), result)
        event = _CHART_SAMPLE_INFLIGHT.pop(route_key, None)
        if event is not None:
            event.set()
    return result


def _native_chart_route(row: dict[str, Any]) -> bool:
    from spreadboard.fast_quotes import supports_native_order_book

    return all(
        supports_native_order_book(
            str(row.get(f"{side}_venue") or ""),
            str(row.get(f"{side}_market_type") or ""),
        )
        for side in ("long", "short")
    )


def _history_meta(rows: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps = sorted(
        int(value)
        for row in rows
        if (value := _float_or_none(row.get("quote_ts_us"))) is not None
    )
    intervals = [
        (later - earlier) / 1_000_000.0
        for earlier, later in zip(timestamps, timestamps[1:])
        if later > earlier
    ]
    cadence = sorted(intervals)[len(intervals) // 2] if intervals else None
    latest = timestamps[-1] if timestamps else None
    return {
        "first_quote_ts_us": timestamps[0] if timestamps else None,
        "last_quote_ts_us": latest,
        "age_seconds": max(0.0, time.time() - latest / 1_000_000.0) if latest else None,
        "median_interval_seconds": cadence,
        "gap_threshold_seconds": max(90.0, cadence * 3.0) if cadence else 90.0,
        "target_notional_usd": next(
            (
                _float_or_none(row.get("target_notional_usd"))
                for row in reversed(rows)
                if _float_or_none(row.get("target_notional_usd")) is not None
            ),
            50.0,
        ),
    }


def _history_coverage_meta(
    rows: list[dict[str, Any]],
    requested_hours: float,
    proxy: dict[str, Any],
) -> dict[str, Any]:
    timestamps = sorted(int(item.get("quote_ts_us") or 0) for item in rows if item.get("quote_ts_us"))
    coverage = (timestamps[-1] - timestamps[0]) / 1_000_000 if len(timestamps) > 1 else 0.0
    requested = requested_hours * 3600
    sources = {str(item.get("sample_source") or "unknown") for item in rows}
    return {
        "requested_window_seconds": requested,
        "actual_coverage_seconds": coverage,
        "coverage_pct": min(100.0, coverage / requested * 100.0) if requested > 0 else 0.0,
        "history_complete": coverage >= requested * 0.95,
        "sample_sources": sorted(sources),
        "historical_proxy": proxy.get("status") == "ok",
        "historical_proxy_timeframe": proxy.get("timeframe"),
        "exact_point_count": sum(1 for item in rows if item.get("sample_source") != "historical_ohlcv_close_proxy"),
        "proxy_point_count": sum(1 for item in rows if item.get("sample_source") == "historical_ohlcv_close_proxy"),
    }


def _current_history_point(row: dict[str, Any]) -> dict[str, Any]:
    long_bid = _float_or_none(row.get("long_bid"))
    short_ask = _float_or_none(row.get("short_ask"))
    return {
        "route_key": row.get("route_key"),
        "quote_ts_us": row.get("quote_ts_us"),
        "token": row.get("token"),
        "route_kind": row.get("route_kind"),
        "long_venue": row.get("long_venue"),
        "long_market_type": row.get("long_market_type"),
        "short_venue": row.get("short_venue"),
        "short_market_type": row.get("short_market_type"),
        "executable_spread_pct": row.get("executable_spread_pct"),
        "depth_weighted_spread_pct": row.get("depth_weighted_spread_pct"),
        "funding_apr_pct": row.get("funding_apr_pct"),
        "funding_daily_pct": row.get("funding_daily_pct"),
        "long_price": row.get("long_price"),
        "short_price": row.get("short_price"),
        "long_bid_price": row.get("long_bid"),
        "long_ask_price": row.get("long_ask"),
        "short_bid_price": row.get("short_bid"),
        "short_ask_price": row.get("short_ask"),
        "exit_spread_pct": (
            (long_bid - short_ask) / short_ask * 100.0
            if long_bid is not None and short_ask is not None and short_ask > 0
            else None
        ),
        "sample_source": "current_board_snapshot",
        "target_notional_usd": 50.0,
    }


def api_token(symbol: str, board_path: Path, *, include_live: bool = True) -> dict[str, Any]:
    token_data = live.get_token_data(symbol) if include_live else local_token_data(symbol)
    token_data["public_enrichment"] = {
        "status": "ready" if include_live else "deferred",
        "mode": "public_scan" if include_live else "lazy_public_scan",
        "url": f"/api/token/{symbol}",
    }
    token_data["board_rows"] = _find_board_symbol(symbol, board_path)
    token_data["community_pulse"] = token_community_pulse(symbol, board_path)
    return token_data


def local_token_data(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "generated_at": int(time.time()),
        "cache_ttl_seconds": live.CACHE_TTL_SECONDS,
        "exchange_rows": [],
        "dex": None,
        "best_spreads": [],
        "convergence_hint": "Public exchange scan loads after the local page so slow venues cannot block Telegram and board context.",
    }


def token_community_pulse(symbol: str, board_path: Path) -> dict[str, Any]:
    payload = api_intel(
        board_path,
        {
            "symbol": [symbol],
            "limit": ["8"],
            "window_hours": ["12"],
        },
    )
    recent = payload.get("recent_events") or {}
    events = pair_signal_events(recent)
    hot = (payload.get("hot_symbols") or [{}])[0] if payload.get("hot_symbols") else {}
    reality = (payload.get("route_reality") or [{}])[0] if payload.get("route_reality") else {}
    queue = (payload.get("action_queue") or [{}])[0] if payload.get("action_queue") else {}
    source = payload.get("source_freshness") or {}
    telegram = source.get("telegram_events") if isinstance(source.get("telegram_events"), dict) else {}
    board_source = source.get("board") if isinstance(source.get("board"), dict) else {}
    return {
        "symbol": symbol,
        "score": hot.get("score"),
        "event_count": hot.get("event_count") or len(events),
        "alert_count": hot.get("alert_count") or len(recent.get("alerts") or []),
        "close_count": hot.get("close_count") or len(recent.get("closes") or []),
        "funding_count": hot.get("funding_count") or len(recent.get("funding") or []),
        "status": queue.get("status") or reality.get("status") or "no_local_signal",
        "next_action": queue.get("next_action") or (reality.get("next_actions") or ["watch"])[0],
        "route_status": reality.get("status") or "telegram_only",
        "identity": reality.get("okx_dex_identity") or "unknown",
        "blockers": queue.get("blockers") or reality.get("top_blockers") or [],
        "signal_lifecycle": payload.get("signal_lifecycle") or {},
        "events": events[:8],
        "telegram_status": telegram.get("status") or "missing",
        "telegram_age_min": telegram.get("age_min"),
        "board_status": board_source.get("status") or "missing",
        "board_age_min": board_source.get("age_min"),
        "signals_url": "/signals?" + urlencode({"symbol": symbol}),
        "triage_url": "/triage?" + urlencode({"symbol": symbol}),
    }


def api_health(
    board_path: Path,
    config: dict[str, Any],
    watcher: alerts.AlertWatcher | None,
    position_alert_worker: Any = None,
) -> dict[str, Any]:
    del watcher
    source_health = api_source_health(board_path, config)
    canonical = source_health.get("canonical_api") or {}
    return {
        "ok": bool(source_health.get("ok")),
        "service": "spreadboard",
        "data_source": "public_exchange_apis",
        "market_age_min": canonical.get("age_min"),
        "market_updated_at": canonical.get("updated_at"),
        "market_row_count": canonical.get("row_count"),
        "source_health": source_health,
        "position_alerts": {
            "running": bool(position_alert_worker and position_alert_worker.running),
            "poll_seconds": getattr(position_alert_worker, "poll_seconds", None),
        },
        "billing": billing.status(),
        "crypto_billing": crypto_billing.status(),
        "telegram_bot": telegram_bot.status(),
    }


def render_board_stream_script(
    query: dict[str, list[str]], *, endpoint: str = "/api/stream/board"
) -> str:
    """Subscribe the open page to price changes and patch them in place.

    Without this the numbers are live but a member only sees them by reloading,
    which is no use on a spread that lasts minutes.
    """
    # Every parameter that decides which routes the page shows has to reach the
    # stream, or it subscribes to a different set and none of the keys match.
    # Forwarding only kind/limit/sort left the funding page subscribed to the
    # spread board: events arrived for routes that were not on screen, so the
    # funding lanes never moved.
    params = []
    # `farm` and `rank` decide presentation, not which routes are returned, and
    # sending them would fragment the stream's cache key for an identical set.
    for name in ("kind", "limit", "sort", "direction", "funding_only",
                 "q", "exchange", "min_spread_pct", "min_abs_funding_24h_pct"):
        value = _query_first(query, name)
        if value:
            params.append(f"{name}={quote(str(value))}")
    suffix = ("?" + "&".join(params)) if params else ""
    return """
    <script>
    (function(){
      if (!window.EventSource) return;
      const source = new EventSource("__ENDPOINT____SUFFIX__");
      const pct = (value, digits) => {
        if (value === null || value === undefined || value === "") return null;
        const number = Number(value);
        if (!Number.isFinite(number)) return null;
        return (number >= 0 ? "+" : "") + number.toFixed(digits) + "%";
      };
      const flash = (node) => {
        if (!node) return;
        node.classList.remove("live-tick");
        void node.offsetWidth;
        node.classList.add("live-tick");
      };
      source.addEventListener("board", (event) => {
        let payload;
        try { payload = JSON.parse(event.data); } catch (error) { return; }
        for (const route of payload.routes || []) {
          // A route appears twice: once on the collapsed group a member reads
          // and once on its row inside the expansion. querySelector patched
          // only the first, so the number on screen never moved.
          const key = window.CSS && CSS.escape ? CSS.escape(route.route_key) : route.route_key;
          const rows = document.querySelectorAll('[data-route-key="' + key + '"]');
          const text = pct(route.spread_pct, 2);
          const carry = pct(route.funding_pct, 3);
          for (const row of rows) {
            for (const spread of row.querySelectorAll("[data-live-spread]")) {
              if (text && spread.textContent.trim() !== text) {
                spread.textContent = text;
                flash(spread);
              }
            }
            for (const funding of row.querySelectorAll("[data-live-funding]")) {
              if (carry && funding.textContent.trim() !== carry) {
                funding.textContent = carry;
                flash(funding);
              }
            }
          }
        }
        const stamp = document.querySelector("[data-live-stamp]");
        if (stamp) stamp.textContent = "live";
      });
    })();
    </script>""".replace("__SUFFIX__", suffix).replace("__ENDPOINT__", endpoint)


#: One computation of live prices, shared by every open stream. Re-pricing per
#: connection would multiply the cost by the number of readers, which is what
#: kept the tick at three seconds; sharing it makes the cadence independent of
#: how many people are watching.
_LIVE_TICK: dict[tuple[Any, ...], tuple[float, dict[str, tuple[Any, Any]]]] = {}
_LIVE_TICK_LOCK = threading.Lock()
#: The book writer flushes every 0.25s, so there is nothing to gain below that.
LIVE_TICK_SECONDS = max(
    0.2, float(os.environ.get("SPREADBOARD_LIVE_TICK_SECONDS", "0.5"))
)


def _shared_stream_rows(
    board_path: Path, query: dict[str, list[str]]
) -> dict[str, tuple[Any, Any]]:
    """Current prices for a lane, computed once however many streams want them."""
    key = (
        str(board_path),
        tuple(sorted((k, tuple(v)) for k, v in query.items())),
    )
    now = time.monotonic()
    with _LIVE_TICK_LOCK:
        cached = _LIVE_TICK.get(key)
        if cached is not None and now - cached[0] < LIVE_TICK_SECONDS:
            return cached[1]
    rows = _board_stream_rows(board_path, query)
    with _LIVE_TICK_LOCK:
        _LIVE_TICK[key] = (time.monotonic(), rows)
        if len(_LIVE_TICK) > 32:
            oldest = min(_LIVE_TICK, key=lambda item: _LIVE_TICK[item][0])
            _LIVE_TICK.pop(oldest, None)
    return rows


def _board_stream_rows(
    board_path: Path, query: dict[str, list[str]]
) -> dict[str, tuple[Any, Any]]:
    """Current spread and funding per route, for the lane the member is viewing.

    The grouped board is cached because building it is expensive, so prices in it
    are only as fresh as that cache. This re-prices the routes that are streaming
    directly from the live books on every tick, which is the whole point of the
    push: the page renders from cache and the feed corrects it within seconds.
    """
    # Re-price under the query the page was rendered with, not a normalised one.
    # Overriding limit/sort here produced a second cache key, so the stream and
    # the page each paid their own ~20s board build every time the cache turned
    # over -- on two cores that is what pushed warm page loads back to seconds.
    payload = api_market_spreads(board_path, query)
    routes = [
        route
        for group in payload.get("groups") or []
        for route in group.get("routes") or []
        if isinstance(route, dict) and route.get("route_key")
    ]
    live = api_spreads.live_prices_for(routes)
    rows: dict[str, tuple[Any, Any]] = {}
    for route in routes:
        key = str(route["route_key"])
        spread, funding = live.get(key, (None, None))
        rows[key] = (
            spread if spread is not None else route.get("executable_spread_pct"),
            funding if funding is not None else route.get("funding_daily_pct"),
        )
    return rows


def render_markets_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    del config
    data = api_market_spreads(board_path, query)
    summary = data.get("summary") or {}
    groups = data.get("groups") or []
    source_health = data.get("source_health") or {}
    api_health_data = source_health.get("canonical_api") or {}
    pagination = data.get("pagination") or {}
    source_ready = data.get("ok") and api_health_data.get("status") == "fresh"
    # Prices arrive over the stream, so a reload is only needed to pick up
    # structural changes -- a token entering or leaving the board. Reloading the
    # page every 30s on top of the push just made the board flicker.
    refresh_seconds = 300 if source_ready else 5
    heading = f"""
      <header class="terminal-heading">
        <div>
          <span class="page-kicker">Arbitrage</span>
          <h1>One asset, every live route</h1>
          <p>Executable public order-book prices grouped by token. Expand an asset to compare every venue pair, funding leg, transfer rail, and chart.</p>
        </div>
        <div class="terminal-live-box {'live' if source_ready else 'unavailable'}">
          <span>{'Live' if source_ready else 'Reconnecting'}</span>
          <strong data-live-stamp>{fmt_age(api_health_data.get('age_min'))}</strong>
          <em>streaming order books · no refresh needed</em>
        </div>
      </header>
    """
    if not source_ready:
        body = f"""
        <section class="markets-page terminal-page" data-refresh="{refresh_seconds}" data-refresh-force="1">
          {heading}
          {render_market_reconnecting(api_health_data, query, refresh_seconds)}
        </section>
        """
        return shell("Markets - SpreadBoard", "markets", body)

    body = f"""
    <section class="markets-page terminal-page" data-refresh="{refresh_seconds}" data-refresh-silent="1">
      {heading}
      <section class="terminal-kpis compact-kpis" aria-label="Market summary">
        {render_market_metric('Assets', min(int(summary.get('matching_tokens') or 0), api_spreads.DEFAULT_LIMIT), 'top 25, grouped')}
        {render_market_metric('Venue pairs', summary.get('matching_rows'), 'expandable routes')}
        {render_market_metric('Funding pairs', summary.get('funding_rows'), 'paired carry')}
        {render_market_metric('Largest edge', fmt_pct(summary.get('max_executable_spread_pct')), 'live ask → bid')}
      </section>
      {render_market_filter_bar(data, query)}
      <section class="market-layout terminal-layout grouped-layout">
        <main class="market-main">
          <div class="panel-head flat token-board-title">
            <div>
              <h2>Live Assets</h2>
              <p>Top {h(min(int(pagination.get('returned_rows') or 0), api_spreads.DEFAULT_LIMIT))} assets by live open spread. Select a token to reveal every venue route.</p>
            </div>
            <a class="mini-action primary-link" href="/api/spreads?{h(urlencode(_query_with(query, limit=500, offset=0)))}">JSON</a>
          </div>
          <div class="token-group-list">
            {''.join(render_market_token_group(group) for group in groups) or render_live_market_empty(api_health_data)}
          </div>
          {render_market_pagination(query, pagination)}
          {render_board_stream_script(query)}
        </main>
        <aside class="market-side">
          {render_market_lane('Top Arbitrage Edges', data.get('top_edges') or [], 'edge')}
          {render_market_lane('Top Funding Pairs', data.get('top_funding') or [], 'funding')}
          <section class="market-side-panel chart-purpose">
            <div class="panel-head flat"><div><h2>Why Charts</h2><p>See whether an edge is persistent, converging, or a single print.</p></div></div>
            <a class="side-chart-link" href="/charts">Open spread history <span aria-hidden="true">→</span></a>
          </section>
        </aside>
      </section>
    </section>
    """
    return shell("Markets - SpreadBoard", "markets", body)


#: The free board is built from the query the warmer already keeps hot, so a
#: visitor costs no board build at all. It is pinned here rather than read from
#: the request: a public page that honoured `limit` would hand the whole board
#: to anyone who typed `?limit=100000`.
FREE_BOARD_QUERY: dict[str, list[str]] = {}

#: How many tokens a visitor sees. Enough to show real, complete, live routes --
#: venues included -- and small next to the full board, which is what the
#: membership is for.
FREE_TOKEN_LIMIT = 6


def render_free_row(group: dict[str, Any], *, metric: str) -> str:
    """One token on the free board, complete enough to be worth trusting.

    Venues are shown. A number with the venues stripped out is not a smaller
    version of this product, it is an unverifiable claim -- and this board
    already spends enough effort proving its spreads are not mirages.
    """
    route = (
        group.get("best_funding_route") if metric == "funding" else group.get("best_route")
    ) or {}
    route_key = str(route.get("route_key") or "")
    long_leg = f"{route.get('long_venue') or '-'} {leg_market_label(route.get('long_venue'), route.get('long_market_type'))}".strip()
    short_leg = f"{route.get('short_venue') or '-'} {leg_market_label(route.get('short_venue'), route.get('short_market_type'))}".strip()
    spread = _float_or_none(group.get("best_edge_pct"))
    funding = _float_or_none(group.get("best_funding_24h_pct"))
    return f"""
      <article class="free-row" data-route-key="{h(route_key)}">
        <div class="free-row-token">
          <strong>{h(group.get("token"))}</strong>
          <span>{h(group.get("token_name") or "")}</span>
        </div>
        <div class="free-row-route">
          <span class="free-leg"><em>Long</em>{h(long_leg)}</span>
          <span class="free-leg"><em>Short</em>{h(short_leg)}</span>
        </div>
        <div class="free-row-metric">
          <em>Spread</em>
          <strong data-live-spread>{fmt_signed_pct(spread, digits=2)}</strong>
        </div>
        <div class="free-row-metric">
          <em>Funding 24h</em>
          <strong data-live-funding>{fmt_signed_pct(funding, digits=3)}</strong>
        </div>
      </article>
    """


def render_free_page(board_path: Path) -> str:
    """The board without an account: a real, live, deliberately small slice."""
    data = api_market_spreads(board_path, dict(FREE_BOARD_QUERY))
    summary = data.get("summary") or {}
    health = (data.get("source_health") or {}).get("canonical_api") or {}
    live = bool(data.get("ok")) and health.get("status") == "fresh"
    edges = (data.get("top_edges") or [])[:FREE_TOKEN_LIMIT]
    carries = (data.get("top_funding") or [])[:FREE_TOKEN_LIMIT]
    tokens = int(summary.get("matching_tokens") or 0)
    routes = int(summary.get("matching_rows") or 0)
    venues = len(data.get("exchange_options") or [])
    body = f"""
    <style>
      .free-page {{ width:min(1240px,calc(100% - 36px)); margin:30px auto 64px; display:grid; gap:26px; }}
      .free-hero {{ display:grid; grid-template-columns:minmax(0,1.5fr) minmax(280px,.7fr); border:1px solid var(--terminal-line); background:var(--terminal-panel); }}
      .free-hero-copy {{ padding:34px 32px; }}
      .free-hero-copy h1 {{ margin:8px 0 12px; font-size:clamp(30px,4.4vw,52px); line-height:1.04; max-width:16ch; }}
      .free-hero-copy p {{ margin:0; max-width:60ch; color:var(--terminal-muted); font-size:16px; line-height:1.55; }}
      .free-hero-side {{ padding:28px 26px; border-left:1px solid var(--terminal-line); display:grid; align-content:center; gap:12px; }}
      .free-stats {{ display:grid; grid-template-columns:repeat(3,1fr); border:1px solid var(--terminal-line); }}
      .free-stats div {{ padding:16px 18px; border-right:1px solid var(--terminal-line); }}
      .free-stats div:last-child {{ border-right:0; }}
      .free-stats strong {{ display:block; font-size:26px; line-height:1.1; }}
      .free-stats span {{ color:var(--terminal-muted); font-size:11px; font-weight:900; text-transform:uppercase; letter-spacing:.06em; }}
      .free-columns {{ display:grid; grid-template-columns:1fr 1fr; gap:22px; }}
      .free-column h2 {{ margin:0 0 4px; font-size:21px; }}
      .free-column > p {{ margin:0 0 12px; color:var(--terminal-muted); font-size:13px; line-height:1.45; }}
      .free-row {{ display:grid; grid-template-columns:minmax(0,1.1fr) minmax(0,1.3fr) auto auto; gap:14px; align-items:center;
                   padding:13px 16px; border:1px solid var(--terminal-line); border-bottom:0; background:var(--terminal-panel); }}
      .free-list .free-row:last-child {{ border-bottom:1px solid var(--terminal-line); }}
      .free-row-token strong {{ display:block; font-size:15px; }}
      .free-row-token span {{ color:var(--terminal-muted); font-size:11px; }}
      .free-row-route {{ display:grid; gap:3px; }}
      .free-leg {{ font-size:12px; color:var(--terminal-text); }}
      .free-leg em {{ display:inline-block; min-width:42px; color:var(--terminal-muted); font-style:normal; font-size:10px;
                      font-weight:900; text-transform:uppercase; letter-spacing:.06em; }}
      .free-row-metric {{ text-align:right; }}
      .free-row-metric em {{ display:block; color:var(--terminal-muted); font-style:normal; font-size:10px; font-weight:900;
                             text-transform:uppercase; letter-spacing:.06em; }}
      .free-row-metric strong {{ font-variant-numeric:tabular-nums; font-size:15px; }}
      .free-cta {{ padding:26px 28px; border:1px solid var(--terminal-line); background:var(--terminal-panel);
                   display:flex; gap:20px; align-items:center; justify-content:space-between; flex-wrap:wrap; }}
      .free-cta h2 {{ margin:0 0 6px; font-size:22px; }}
      .free-cta p {{ margin:0; max-width:62ch; color:var(--terminal-muted); line-height:1.5; }}
      .free-actions {{ display:flex; gap:9px; flex-wrap:wrap; }}
      .free-button {{ min-height:43px; padding:11px 18px; border:1px solid var(--terminal-line); color:var(--terminal-text);
                      text-decoration:none; font-weight:900; display:inline-flex; align-items:center; }}
      .free-button.primary {{ background:var(--accent); border-color:var(--accent); color:var(--accent-ink); }}
      .free-note {{ margin:0; color:var(--terminal-muted); font-size:12px; line-height:1.5; }}
      @media(max-width:900px) {{ .free-hero {{ grid-template-columns:1fr; }} .free-hero-side {{ border-left:0; border-top:1px solid var(--terminal-line); }}
        .free-columns {{ grid-template-columns:1fr; }} }}
      @media(max-width:620px) {{ .free-page {{ width:min(100% - 20px,1240px); }} .free-hero-copy,.free-hero-side {{ padding:22px 18px; }}
        .free-stats {{ grid-template-columns:1fr; }} .free-stats div {{ border-right:0; border-bottom:1px solid var(--terminal-line); }}
        .free-stats div:last-child {{ border-bottom:0; }}
        .free-row {{ grid-template-columns:1fr 1fr; }} .free-row-route {{ grid-column:1 / -1; order:3; }} }}
    </style>
    <section class="free-page">
      <header class="free-hero">
        <div class="free-hero-copy">
          <span class="page-kicker">Free preview</span>
          <h1>Live spreads, no account.</h1>
          <p>These are real routes off public order books, priced continuously and pushed to this
             page as the books move &mdash; nothing here waits for a refresh. It is the same feed
             the full board runs on, showing {FREE_TOKEN_LIMIT} tokens instead of {tokens:,}.</p>
        </div>
        <aside class="free-hero-side">
          <div class="terminal-live-box {'live' if live else 'unavailable'}">
            <span>{'Live' if live else 'Reconnecting'}</span>
            <strong data-live-stamp>{fmt_age(health.get('age_min'))}</strong>
            <em>streaming order books</em>
          </div>
          <p class="free-note">Watch a number for a few seconds. If it moves, the feed is live.</p>
        </aside>
      </header>
      <div class="free-stats">
        <div><span>Tokens tracked</span><strong>{tokens:,}</strong></div>
        <div><span>Routes priced</span><strong>{routes:,}</strong></div>
        <div><span>Venues</span><strong>{venues:,}</strong></div>
      </div>
      <div class="free-columns">
        <section class="free-column">
          <h2>Widest spreads</h2>
          <p>Buy the long leg at its ask, sell the short leg at its bid.</p>
          <div class="free-list">{''.join(render_free_row(group, metric="edge") for group in edges) or '<p class="free-note">No fresh route right now. The board is re-pricing.</p>'}</div>
        </section>
        <section class="free-column">
          <h2>Best funding</h2>
          <p>What the position pays over 24h at the current settled rate.</p>
          <div class="free-list">{''.join(render_free_row(group, metric="funding") for group in carries) or '<p class="free-note">No fresh funding route right now.</p>'}</div>
        </section>
      </div>
      <section class="free-cta">
        <div>
          <h2>The other {max(tokens - FREE_TOKEN_LIMIT, 0):,} tokens</h2>
          <p>A membership opens every lane &mdash; Futures-Futures, Futures-Spot, Spot-Spot and both
             DEX lanes &mdash; with filters, convergence charts, saved pairs, transfer-rail checks
             and alerts when a spread opens or a funding rate flips.</p>
        </div>
        <div class="free-actions">
          <a class="free-button primary" href="/register">Create account</a>
          <a class="free-button" href="/pricing">Membership</a>
          <a class="free-button" href="/login">Sign in</a>
        </div>
      </section>
      <p class="free-note">Spreads and funding rates are public market data, not advice. Every route
         carries execution risk, and a number on a screen is not a filled order.</p>
    </section>
    {render_board_stream_script(dict(FREE_BOARD_QUERY), endpoint="/api/stream/free")}
    """
    return shell("Live spreads - SpreadBoard", "free", body)


def render_fair_price_page() -> str:
    """Contracts trading away from the price their own venue marks them at.

    Every other lane here compares two venues. This one compares a contract
    against its own exchange's fair price -- the mark used for liquidations --
    which is a mean-reversion signal rather than an arbitrage, and it shows up
    exactly where the cross-venue board is weakest: thin, newly listed
    contracts whose last trade drifts from the index between fills.
    """
    payload = fair_price.load()
    rows = payload.get("rows") or []
    updated = payload.get("updated_at")
    body_rows = "".join(
        f"""
        <article class="fair-row">
          <div class="fair-token">
            <strong>{h(str(row.get("symbol") or "").split("/")[0])}</strong>
            <span>{h(row.get("venue"))} · {h(row.get("symbol"))}</span>
          </div>
          <div class="fair-metric"><em>Last</em><strong>{fmt_price(row.get("last_price"))}</strong></div>
          <div class="fair-metric"><em>Fair</em><strong>{fmt_price(row.get("fair_price"))}</strong></div>
          <div class="fair-metric"><em>Volume 24h</em><strong>{fmt_money(row.get("volume_24h_usd"))}</strong></div>
          <div class="fair-metric"><em>Gap</em><strong class="{'spread-good' if (row.get('deviation_pct') or 0) > 0 else 'spread-negative'}">{fmt_signed_pct(row.get("deviation_pct"), digits=2)}</strong></div>
          <div class="fair-side {'long' if row.get('side') == 'Long' else 'short'}">{h(row.get("side"))}</div>
        </article>
        """
        for row in rows[:60]
    )
    empty = '<p class="free-note">No contract is far enough from its fair price right now.</p>'
    body = f"""
    <style>
      .fair-page {{ width:min(1240px,calc(100% - 36px)); margin:30px auto 64px; display:grid; gap:20px; }}
      .fair-row {{ display:grid; grid-template-columns:minmax(0,1.5fr) repeat(4,auto) 84px; gap:14px; align-items:center;
                   padding:12px 16px; border:1px solid var(--terminal-line); border-bottom:0; background:var(--terminal-panel); }}
      .fair-list .fair-row:last-child {{ border-bottom:1px solid var(--terminal-line); }}
      .fair-token strong {{ display:block; font-size:15px; }}
      .fair-token span {{ color:var(--terminal-muted); font-size:11px; }}
      .fair-metric {{ text-align:right; }}
      .fair-metric em {{ display:block; color:var(--terminal-muted); font-style:normal; font-size:10px; font-weight:900;
                         text-transform:uppercase; letter-spacing:.06em; }}
      .fair-metric strong {{ font-variant-numeric:tabular-nums; font-size:14px; }}
      .fair-side {{ text-align:center; font-weight:900; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
                    padding:5px 0; border:1px solid var(--terminal-line); }}
      .fair-side.long {{ color:var(--accent-ink); background:var(--accent); border-color:var(--accent); }}
      .fair-side.short {{ color:var(--terminal-text); }}
      @media(max-width:820px) {{ .fair-row {{ grid-template-columns:1fr 1fr; }} .fair-token {{ grid-column:1 / -1; }} }}
    </style>
    <section class="fair-page">
      <header class="terminal-heading">
        <div>
          <span class="page-kicker">Fair price</span>
          <h1>Trading away from its own mark</h1>
          <p>Not an arbitrage between venues &mdash; a contract against the fair price its own
             exchange computes and liquidates against. Last below fair reads Long, above reads
             Short. Thin and newly listed contracts drift furthest, so size to the volume shown.</p>
        </div>
        <div class="terminal-live-box {'live' if rows else 'unavailable'}">
          <span>{len(rows)} flagged</span>
          <strong data-live-stamp>{h(updated or "—")}</strong>
          <em>from the same sweep that prices the board</em>
        </div>
      </header>
      <div class="fair-list">{body_rows or empty}</div>
      <p class="free-note">Public market data, not advice. A gap can persist or widen, and a thin
         contract can be hard to leave.</p>
    </section>
    """
    return shell("Fair price - SpreadBoard", "fair", body)


def leg_market_label(venue: Any, market_type: Any) -> str:
    """What kind of market this leg is, as a member reads it.

    An OKX DEX leg carries market_type "Spot", because an on-chain swap is a
    spot trade. So a DEX-Futures route rendered as
    "OKX DEX 56 Spot -> Gate Futures", which reads exactly like a Spot-Futures
    route -- the lane was right, the label was not, and that is why the DEX
    farms looked mixed in with the Futures-Spot ones.
    """
    if "DEX" in str(venue or "").upper():
        return "DEX"
    return str(market_type or "").strip()


def render_market_reconnecting(
    health: dict[str, Any],
    query: dict[str, list[str]],
    refresh_seconds: int,
) -> str:
    params = urlencode(_query_with(query, offset=None))
    retry_href = "/markets" + (f"?{params}" if params else "")
    row_count = health.get("row_count")
    last_snapshot = f"{int(row_count):,} routes" if isinstance(row_count, (int, float)) else "Not available"
    return f"""
    <section class="market-reconnect" aria-live="polite">
      <div class="market-reconnect-panel">
        <div class="market-reconnect-head">
          <span class="market-reconnect-dot" aria-hidden="true"></span>
          <div>
            <span>Market refresh</span>
            <h2>Restoring live prices</h2>
          </div>
        </div>
        <p>The latest API snapshot is no longer current. Previous routes are kept off the live board while a fresh public-market cycle completes.</p>
        <div class="market-reconnect-stats">
          <article><span>Last snapshot</span><strong>{h(last_snapshot)}</strong></article>
          <article><span>Last update</span><strong>{fmt_age(health.get('age_min'))}</strong></article>
          <article><span>Automatic check</span><strong>{h(refresh_seconds)} seconds</strong></article>
        </div>
        <div class="market-reconnect-actions">
          <a class="sheet-button primary" href="{h(retry_href)}">Check now</a>
          <span>Live assets appear as soon as the next cycle is published.</span>
        </div>
      </div>
    </section>
    """


def render_live_market_empty(health: dict[str, Any]) -> str:
    status = str(health.get("status") or "unavailable")
    if status == "fresh":
        return '<p class="empty market-empty">No live API routes match these filters.</p>'
    return f"""
    <article class="live-market-empty">
      <strong>Refreshing public markets</strong>
      <p>No old snapshot is shown while the scanner updates. This page will populate automatically when a fresh API cycle completes.</p>
      <span>Last update {fmt_age(health.get('age_min'))}</span>
    </article>
    """


def render_funding_farm_empty(selected_farm: str, health: dict[str, Any]) -> str:
    """Explain an empty farm tab instead of rendering a blank list.

    Futures-DEX in particular stays empty whenever the OKX DEX quote source is
    skipped, which happens when its API credentials are absent. Silently showing
    nothing made that look like "no opportunities" rather than "not configured".
    """

    if selected_farm != "futures-dex":
        return render_live_market_empty(health)
    source = health.get("dex_spot_source") or {}
    status = str(source.get("status") or "absent")
    blockers = [str(item) for item in source.get("blockers") or []]
    if status in {"ok", "partial"}:
        return (
            '<p class="empty market-empty">OKX DEX quoting ran but no DEX route matched a '
            'futures leg this cycle.</p>'
        )
    if "api_credentials_missing" in blockers or status in {"skipped", "absent"}:
        return """
    <article class="live-market-empty">
      <strong>OKX DEX feed is temporarily unavailable</strong>
      <p>Exact-chain DEX routes will return automatically when verified quotes resume.</p>
      <span>Source status: reconnecting</span>
    </article>
    """
    return f"""
    <article class="live-market-empty">
      <strong>OKX DEX quoting is unavailable</strong>
      <p>{h("; ".join(blockers) or "The DEX quote source did not return rows this cycle.")}</p>
      <span>Source status: {h(status)}</span>
    </article>
    """


def render_market_token_group(group: dict[str, Any]) -> str:
    best = group.get("best_route") or {}
    best_chart_url = (
        f"/charts?route_key={board.route_key_url(str(best.get('route_key') or ''))}"
        if best.get("route_key")
        else f"/charts?token={quote(str(group.get('token') or ''))}"
    )
    name = group.get("token_name") or "Metadata pending"
    venues = group.get("venues") or []
    kinds = group.get("route_kinds") or []
    funding = (
        best.get("funding_24h_pct")
        if best.get("funding_24h_pct") is not None
        else best.get("funding_projected_24h_pct")
    )
    funding_basis = (
        "settled 24h"
        if best.get("funding_24h_pct") is not None
        else "24h at current"
        if best.get("funding_projected_24h_pct") is not None
        else "history unavailable"
    )
    funding_route = best
    funding_pair = " → ".join(
        venue
        for venue in (
            funding_route.get("long_venue"),
            funding_route.get("short_venue"),
        )
        if venue
    )
    return f"""
    <details class="token-route-group" id="token-{h(group.get('token'))}"
             data-route-key="{h(best.get('route_key') or '')}">
      <summary class="token-route-summary">
        <div class="asset-identity">
          <span class="asset-monogram">{h(str(group.get('token') or '?')[:2])}</span>
          <span><a class="asset-chart-symbol" href="{h(best_chart_url)}" onclick="event.stopPropagation()" title="Open the best live route chart">{h(group.get('token'))}</a><em>{h(name)}</em></span>
        </div>
        <div class="best-route">
          <span>Best pair</span>
          <strong>{h(best.get('long_venue'))} <i>→</i> {h(best.get('short_venue'))}</strong>
          <em>{h(route_kind_display(best.get('route_kind')))}</em>
        </div>
        <div class="group-number">
          <span>Best spread</span>
          <strong class="{spread_class(group.get('best_edge_pct'))}" data-live-spread>{fmt_pct(group.get('best_edge_pct'))}</strong>
          <em>{fmt_pct(best.get('depth_weighted_spread_pct'))} matched $50 VWAP · {fmt_pct(best.get('executable_spread_pct'))} top book</em>
        </div>
        <div class="group-number">
          <span>Best-route funding</span>
          <strong data-live-funding>{fmt_signed_pct(funding, digits=3) if funding is not None else '—'}</strong>
          <em>{h(funding_basis)} · {h(funding_economic_label(funding, best))} · {h(funding_pair) if funding_pair else 'not applicable'}</em>
        </div>
        <div class="group-routes">
          <span>Routes</span>
          <strong>{h(group.get('route_count') or 0)}</strong>
          <em>{h(len(venues))} venues · {h(len(kinds))} types</em>
        </div>
        <div class="group-age"><strong>{fmt_age(group.get('age_min'))}</strong><span aria-hidden="true">⌄</span></div>
      </summary>
      <div class="token-route-body">
        <div class="expanded-asset-bar">
          <span>{h(', '.join(venues))}</span>
          <div>
            <a href="/charts?token={h(group.get('token'))}">Token charts</a>
            <a href="{h(group.get('href') or '/markets')}">Token overview</a>
          </div>
        </div>
        <div class="route-detail-table">
          <div class="route-detail-head">
            <span>Buy leg</span><span>Sell leg</span><span>Matched prices</span><span>Edge</span>
            <span>Funding 24h</span><span>D/W rails</span><span></span>
          </div>
          {''.join(render_market_group_route(route) for route in group.get('routes') or [])}
        </div>
      </div>
    </details>
    """


def render_market_group_route(row: dict[str, Any]) -> str:
    settled_funding = row.get("funding_24h_pct")
    shown_funding = (
        settled_funding
        if settled_funding is not None
        else row.get("funding_projected_24h_pct")
    )
    funding_basis = (
        "settled 24h"
        if settled_funding is not None
        else "24h at current"
        if shown_funding is not None
        else "history unavailable"
    )
    return f"""
    <article class="route-detail-row" data-route-key="{h(row.get('route_key') or '')}">
      <div class="route-leg buy">
        <span>Buy</span>{render_exchange_link(row, 'long')}<em>{h(leg_market_label(row.get('long_venue'), row.get('long_market_type')))}</em>
      </div>
      <div class="route-leg sell">
        <span>Sell</span>{render_exchange_link(row, 'short')}<em>{h(leg_market_label(row.get('short_venue'), row.get('short_market_type')))}</em>
      </div>
      <div class="route-prices">
        <strong>{fmt_price(row.get('long_price'))}</strong><span>→</span><strong>{fmt_price(row.get('short_price'))}</strong>
      </div>
      <div class="route-edge">
        <strong class="{spread_class(row.get('depth_weighted_spread_pct'))}" data-live-spread>{fmt_pct(row.get('depth_weighted_spread_pct'))}</strong>
        <span>{fmt_pct(row.get('executable_spread_pct'))} top book{' · depth not measured' if row.get('depth_unverified') else ''}</span>
      </div>
      <div class="route-funding">
        <strong data-live-funding>{fmt_signed_pct(shown_funding, digits=3) if shown_funding is not None else '—'}</strong>
        <b>{h(funding_basis)} · {h(funding_economic_label(shown_funding, row))}</b>
        <span>{fmt_signed_pct(row.get('long_funding_pct'), digits=4)} / {fmt_signed_pct(row.get('short_funding_pct'), digits=4)}</span>
        <em>{h(funding_cadence_pair(row))}</em>
      </div>
      <div class="route-rails">{render_market_dw(row)}</div>
      <div class="route-actions">
        {render_alert_draft_button(row, alert_type='token_spread', compact=True)}
        <a href="/pair/{h(board.route_key_url(str(row.get('route_key') or '')))}" title="Open route details">Details</a>
        <a href="/charts?route_key={h(board.route_key_url(str(row.get('route_key') or '')))}" title="Open route chart">Chart</a>
      </div>
    </article>
    """


def render_market_source_card(title: str, health: dict[str, Any], count: Any, note: str = "") -> str:
    status = str(health.get("status") or "unknown")
    return f"""
    <article class="market-source-card terminal-kpi {h(status)}">
      <span>{h(title)}</span>
      <strong>{h(count if count is not None else 0)}</strong>
      <em>{label_text(status)} · {fmt_age(health.get('age_min'))}</em>
      <small>{h(note)}</small>
    </article>
    """


def render_market_filter_bar(data: dict[str, Any], query: dict[str, list[str]]) -> str:
    selected_kind = _query_first(query, "kind") or ""
    exchange = _query_first(query, "exchange") or ""
    selected_sort = _query_first(query, "sort") or "edge"
    selected_direction = _query_first(query, "direction") or "desc"
    summary = data.get("summary") or {}
    kind_counts = data.get("route_kind_token_counts") or {}
    lane_counts = data.get("lane_token_counts") or {}
    # Futures-Spot and Spot-Futures are one directional pair family. All routes
    # stays last so the primary lane order remains predictable.
    kind_tabs = [
        ("FUTURES", "Futures-Futures"),
        ("FUTURES-SPOT-PAIR", "Futures-Spot"),
        ("SPOT", "Spot-Spot"),
        ("DEX-FUTURES", "Futures-DEX"),
        ("DEX-SPOT", "Spot-DEX"),
        ("", "All routes"),
    ]
    exchange_options = data.get("exchange_options") or []
    return f"""
    <section class="market-filter-panel terminal-filter-panel">
      <div class="terminal-filter-row route-row">
        <span>Route</span>
        <div class="market-tabs route-tabs" aria-label="Route filters">
          {''.join(render_market_tab(label, _query_with(query, kind=value or None, offset=None), str(selected_kind).upper() == value, market_kind_count(value, kind_counts, summary, lane_counts)) for value, label in kind_tabs)}
        </div>
      </div>
      <form class="market-filter-form" method="get" action="/markets">
        <label><span>Token</span><input name="q" value="{h(_query_first(query, 'q') or '')}" placeholder="SIREN, VANRY, GUA"></label>
        <label><span>Exchanges</span><select name="exchange">
          <option value="">All exchanges</option>
          {''.join(f'<option value="{h(item)}" {"selected" if item == exchange else ""}>{h(item)}</option>' for item in exchange_options)}
        </select></label>
        <label><span>Min edge %</span><input name="min_spread_pct" value="{h(_query_first(query, 'min_spread_pct') or '')}" placeholder="0.50"></label>
        <label><span>Min 24h %</span><input name="min_abs_funding_24h_pct" value="{h(_query_first(query, 'min_abs_funding_24h_pct') or '')}" placeholder="0.10"></label>
        <label><span>Sort</span><select name="sort">
          {''.join(f'<option value="{value}" {"selected" if value == selected_sort else ""}>{label}</option>' for value, label in [('edge', 'Spread'), ('funding', 'Funding 24h'), ('funding_abs', 'Funding magnitude'), ('depth', 'Depth'), ('age', 'Age'), ('token', 'Token')])}
        </select></label>
        <label><span>Direction</span><select name="direction">
          <option value="desc" {'selected' if selected_direction == 'desc' else ''}>High to low</option>
          <option value="asc" {'selected' if selected_direction == 'asc' else ''}>Low to high</option>
        </select></label>
        <input type="hidden" name="limit" value="25">
        <input type="hidden" name="kind" value="{h(selected_kind)}">
        <label class="market-check"><input type="checkbox" name="funding_only" value="1" {'checked' if _query_bool(query, 'funding_only') else ''}> Funding</label>
        <button class="sheet-button primary" type="submit">Apply</button>
        <a class="sheet-button" href="/markets">Reset</a>
      </form>
      {render_market_active_filters(query)}
    </section>
    """


def market_kind_count(
    value: str,
    counts: dict[str, Any],
    summary: dict[str, Any],
    lane_counts: dict[str, Any] | None = None,
) -> Any:
    if not value:
        return min(int(summary.get("matching_tokens") or 0), api_spreads.DEFAULT_LIMIT)
    if value == "FUTURES-SPOT-PAIR":
        count = (lane_counts or {}).get("FUTURES-SPOT", 0) + (
            lane_counts or {}
        ).get("SPOT-FUTURES", 0)
    else:
        count = counts.get(value, 0)
    return min(int(count or 0), api_spreads.DEFAULT_LIMIT)


def render_market_tab(label: str, query: dict[str, str], active: bool, count: Any = None) -> str:
    href = "/markets"
    if query:
        href += "?" + urlencode(query)
    count_html = f"<em>{h(count)}</em>" if count is not None else ""
    return f'<a class="market-tab {"active" if active else ""}" href="{h(href)}"><span>{h(label)}</span>{count_html}</a>'


def render_market_active_filters(query: dict[str, list[str]]) -> str:
    chips: list[tuple[str, str]] = []
    for key, label in [
        ("q", "Token"),
        ("exchange", "Exchange"),
        ("min_spread_pct", "Edge"),
        ("min_abs_funding_24h_pct", "Funding 24h"),
        ("kind", "Route"),
    ]:
        value = _query_first(query, key)
        if value:
            chips.append((key, f"{label} {value}"))
    if _query_bool(query, "funding_only"):
        chips.append(("funding_only", "Funding rows"))
    if not chips:
        return '<div class="terminal-active-filters"><span>Active</span><em>No filters</em></div>'
    items = []
    for key, label in chips:
        href = "/markets?" + urlencode(_query_with(query, **{key: None, "offset": None}))
        items.append(f'<a href="{h(href)}">{h(label)} <b>×</b></a>')
    items.append('<a class="clear" href="/markets">Clear all</a>')
    chips_html = "".join(items)
    return f'<div class="terminal-active-filters"><span>Active</span>{chips_html}</div>'


def render_market_metric(label: str, value: Any, note: str = "") -> str:
    return f"<article><span>{h(label)}</span><strong>{h(value if value is not None else '?')}</strong><em>{h(note)}</em></article>"


def render_market_row(row: dict[str, Any]) -> str:
    blockers = row.get("blockers") or []
    source = row.get("source_label") or row.get("source_group")
    headline = row.get("displayed_open_spread_pct")
    if headline is None:
        headline = row.get("executable_spread_pct")
    return f"""
    <article class="market-terminal-grid market-row terminal-grid {h(row.get('freshness'))} {h(row.get('source_group'))}">
      <div class="market-token-cell">
        <div class="market-token-head">
          <a class="market-row-link" href="{h(row.get('href') or '/markets')}"><strong>{h(row.get('token'))}</strong>{render_source_count_chip(row)}</a>
          {render_mirage_badge(row)}
          {render_alert_draft_button(row, alert_type='token_spread', compact=True)}
        </div>
        <span>{h(source)} · {h(route_kind_display(row.get('route_kind')))}</span>
      </div>
      <div class="market-route-cell">
        {render_market_leg_line(row, 'long')}
        {render_market_leg_line(row, 'short')}
      </div>
      <div class="market-number-cell"><strong>{fmt_pct(headline)}</strong><span>open</span></div>
      <div class="market-number-cell"><strong>{fmt_signed_pct(row.get('executable_spread_pct'))}</strong><span>{fmt_signed_pct(row.get('depth_weighted_spread_pct'))} vwap</span></div>
      <div class="market-funding-cell">{render_market_funding(row)}</div>
      <div class="market-number-cell"><strong>{fmt_money(row.get('depth_usd'))}</strong><span>min leg</span></div>
      <div class="market-age-cell"><strong>{fmt_age(row.get('age_min'))}</strong><span>{label_text(row.get('freshness'))}</span></div>
      <div class="market-dw-cell">{render_market_dw(row)}</div>
      <div class="market-blocker-cell">
        <span class="market-status {h(row.get('status'))}">{label_text(row.get('status'))}</span>
        <strong>{h(row.get('next_action'))}</strong>
        <em>{label_list(blockers[:3]) or 'No blocker details'}</em>
      </div>
    </article>
    """


def render_mirage_badge(row: dict[str, Any]) -> str:
    """Badge rows whose route feasibility or identity is unproven.

    These rows used to be hidden outright, which silently removed real
    opportunities (all MEXC routes and most Futures-Spot routes). They are now
    shown and flagged instead, so the spread is visible but clearly marked as
    not-yet-verified.
    """

    if not row.get("mirage_guarded"):
        return ""
    reasons = [
        str(item).split("mirage_guard:", 1)[-1].replace("_", " ")
        for item in row.get("blockers") or []
        if str(item).startswith("mirage_guard:")
    ]
    title = "; ".join(reasons) or "route feasibility unproven"
    return f'<span class="mirage-badge" title="{h(title)}">unproven</span>'


def render_source_count_chip(row: dict[str, Any]) -> str:
    source_name = str(row.get("source_name") or "")
    if source_name and source_name != "legacy_public_verification":
        return f"<em>{h(source_name[:12])}</em>"
    return ""


def route_kind_display(kind: Any) -> str:
    text = str(kind or "")
    for item in board.ROUTE_KINDS:
        if item.kind == text:
            return item.label
    return text or "Route"


def market_type_compact(value: Any) -> str:
    key = str(value or "").casefold()
    if key == "futures":
        return "FUT"
    if key == "spot":
        return "spot"
    if key == "dex":
        return "DEX"
    return str(value or "?")


def render_market_leg_line(row: dict[str, Any], side: str) -> str:
    is_long = side == "long"
    venue = row.get("long_venue" if is_long else "short_venue") or "?"
    market_type = row.get("long_market_type" if is_long else "short_market_type")
    price = row.get("long_price" if is_long else "short_price")
    funding = row.get("long_funding_pct" if is_long else "short_funding_pct")
    arrow = "↑" if is_long else "↓"
    label = "Long" if is_long else "Short"
    return f"""
    <span class="terminal-leg {'long' if is_long else 'short'}">
      <b>{h(arrow)}</b>
      <strong>{h(venue)}</strong>
      <em>{h(market_type_compact(market_type))}</em>
      <i>{fmt_price(price)}</i>
      <small>{label[0]} {fmt_signed_pct(funding, digits=4)}</small>
    </span>
    """


def render_market_funding(row: dict[str, Any]) -> str:
    funding_24h = row.get("funding_24h_pct")
    if funding_24h is None:
        projected = row.get("funding_projected_24h_pct")
        if projected is None:
            return '<strong class="muted">?</strong><span>history unavailable</span>'
        return (
            f"<strong>{fmt_signed_pct(projected, digits=3)}</strong>"
            f"<span>24h at current · {h(funding_cadence_pair(row))}</span>"
        )
    direction = "▲" if (_float_or_none(funding_24h) or 0.0) >= 0 else "▼"
    return (
        f"<strong>{h(direction)} {fmt_signed_pct(funding_24h, digits=3)}</strong>"
        f"<span>{h(funding_cadence_pair(row))}</span>"
    )


def funding_interval_label(hours: Any, assumed: Any = False) -> str:
    value = _float_or_none(hours)
    if value is None or value <= 0:
        return "cadence unknown"
    nearest_hour = round(value)
    if nearest_hour > 0 and abs(value - nearest_hour) <= 0.02:
        value = float(nearest_hour)
    shown = str(int(value)) if value.is_integer() else f"{value:g}"
    return f"{shown}h estimated" if assumed else f"every {shown}h"


def funding_cadence_pair(row: dict[str, Any]) -> str:
    labels = []
    for side in ("long", "short"):
        if row.get(f"{side}_market_type") != "Futures":
            continue
        venue = row.get(f"{side}_venue") or side.title()
        cadence = funding_interval_label(
            row.get(f"{side}_funding_interval_hours"),
            row.get(f"{side}_funding_interval_assumed"),
        )
        labels.append(f"{venue}: {cadence}")
    return " · ".join(labels) if labels else "no futures leg"


def funding_24h_value(row: dict[str, Any]) -> float | None:
    return _float_or_none(row.get("funding_24h_pct"))


def funding_economic_label(value: Any, row: dict[str, Any]) -> str:
    if not any(
        row.get(f"{side}_market_type") == "Futures"
        for side in ("long", "short")
    ):
        return "no futures funding"
    amount = _float_or_none(value)
    if amount is None:
        return "funding pending"
    if amount > 0:
        return "receive at current direction"
    if amount < 0:
        return "pay at current direction"
    return "neutral at current direction"


def render_market_dw(row: dict[str, Any]) -> str:
    long_spot = str(row.get("long_market_type") or "") == "Spot"
    short_spot = str(row.get("short_market_type") or "") == "Spot"
    if not long_spot and not short_spot:
        return '<span class="rail-na">No transfer</span>'
    parts = []
    if long_spot:
        parts.append(f'<span title="Withdrawal from buy venue">Buy W: {h(rail_text(row.get("long_withdraw_enabled")))}</span>')
    if short_spot:
        parts.append(f'<span title="Deposit to sell venue">Sell D: {h(rail_text(row.get("short_deposit_enabled")))}</span>')
    return "".join(parts)


def rail_char(value: Any) -> str:
    if value is True:
        return "open"
    if value is False:
        return "closed"
    return "?"


def rail_text(value: Any) -> str:
    if value is True:
        return "Open"
    if value is False:
        return "Closed"
    return "Not reported"


def render_market_lane(title: str, rows: list[dict[str, Any]], kind: str) -> str:
    return f"""
    <section class="market-side-panel">
      <div class="panel-head flat"><div><h2>{h(title)}</h2><p>{'Unique assets ranked by executable edge' if kind == 'edge' else 'Unique assets ranked by paired carry'}</p></div></div>
      <div class="market-mini-list">
        {''.join(render_market_mini(row, kind) for row in rows[:8]) or '<p class="watch-empty">No rows in this lane.</p>'}
      </div>
    </section>
    """


def render_market_mini(row: dict[str, Any], kind: str) -> str:
    if kind == "funding":
        best = row.get("best_funding_route") or row.get("best_route") or row
        primary = row.get("best_funding_24h_pct")
        if primary is None and row.get("best_funding_apr_pct") is not None:
            primary = (_float_or_none(row.get("best_funding_apr_pct")) or 0.0) / 365.0
        suffix = " / 24h"
        digits = 3
    else:
        best = row.get("best_route") or row
        primary = row.get("best_edge_pct")
        suffix = ""
        digits = 0
    return f"""
    <a class="market-mini-row" href="/markets#token-{h(row.get('token'))}">
      <strong>{h(row.get('token'))}</strong>
      <span>{h(row.get('token_name') or 'Metadata pending')}<small>{h(best.get('long_venue'))} → {h(best.get('short_venue'))}</small></span>
      <em>{fmt_signed_pct(primary, digits=digits)}{suffix}</em>
    </a>
    """


def render_market_pagination(query: dict[str, list[str]], pagination: dict[str, Any]) -> str:
    matching = int(pagination.get("matching_rows") or 0)
    returned = int(pagination.get("returned_rows") or 0)
    offset = int(pagination.get("offset") or 0)
    limit = int(pagination.get("limit") or api_spreads.DEFAULT_LIMIT)
    if matching <= 0:
        return ""
    start = offset + 1
    end = offset + returned
    previous_href = "/markets?" + urlencode(_query_with(query, offset=max(0, offset - limit) or None))
    next_href = "/markets?" + urlencode(_query_with(query, offset=offset + limit))
    return f"""
    <nav class="market-pagination" aria-label="Spread matrix pages">
      <span>{h(start)}-{h(end)} of {h(matching)} assets</span>
      <div>
        {'<a href="' + h(previous_href) + '">Previous</a>' if pagination.get('has_previous') else '<span class="disabled">Previous</span>'}
        {'<a href="' + h(next_href) + '">Next</a>' if pagination.get('has_more') else '<span class="disabled">Next</span>'}
      </div>
    </nav>
    """


def render_market_exchange_box(
    coverage: dict[str, Any],
    exchange_counts: dict[str, Any],
    query: dict[str, list[str]],
) -> str:
    active = coverage.get("active_venues") or sorted(exchange_counts)
    unavailable = coverage.get("unavailable_venues") or []
    return f"""
    <section class="market-side-panel">
      <div class="panel-head flat"><div><h2>Exchange coverage</h2><p>{h(len(active))} probed · {h(len(unavailable))} unavailable connectors</p></div></div>
      <div class="market-coverage-group">
        <span>Probed now</span>
        <div class="market-exchange-cloud">
          {''.join(f'<a href="/markets?{h(urlencode(_query_with(query, exchange=item, offset=None)))}"><strong>{h(item)}</strong><em>{h(exchange_counts.get(item, 0))}</em></a>' for item in active) or '<span>No active exchange list available.</span>'}
        </div>
      </div>
      <div class="market-coverage-group unavailable">
        <span>Not probed</span>
        <div class="market-exchange-cloud">
          {''.join(f'<span>{h(item)}</span>' for item in unavailable) or '<span>None reported</span>'}
        </div>
      </div>
    </section>
    """


def render_intel_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    data = api_intel(board_path, query)
    source = data.get("source_freshness") or {}
    hot = data.get("hot_symbols") or []
    action_queue = data.get("action_queue") or []
    change_digest = data.get("change_digest") or {}
    recent = data.get("recent_events") or {}
    brief = data.get("latest_brief") or {}
    profile = data.get("profile_shell") or {}
    alert_preview = data.get("alert_preview") or {}
    body = f"""
    <section class="intel-page" data-refresh="180">
      <div class="intel-hero">
        <div>
          <span class="page-kicker">Community Intel</span>
          <h1>What deserves attention now</h1>
          <p>Telegram context, board reality, funding, D/W rails, and read-only alert previews in one local view.</p>
        </div>
        <div class="intel-actions">
          <a class="secondary" href="/arbitrage?kind=FUTURES">Arbitrage</a>
          <a class="secondary" href="/charts">Charts</a>
        </div>
      </div>
      {render_change_digest(change_digest)}
      {render_action_queue(action_queue)}
      {render_intel_source_grid(source)}
      <section class="intel-layout">
        <main class="intel-main">
          <section class="intel-section">
            <div class="panel-head flat"><div><h2>What's Hot</h2><p>Ranked from recent community and Telegram messages, DEX/funding signal, identity, liquidity, and matched market routes.</p></div></div>
            <div class="hot-grid">{''.join(render_hot_symbol(item) for item in hot[:12]) or '<p class="empty">No recent Telegram symbols matched this window.</p>'}</div>
          </section>
          <section class="intel-section">
            <div class="panel-head flat"><div><h2>Route Reality</h2><p>For the hottest tokens, this shows whether the board has a route, what blocks it, and where to drill in.</p></div></div>
            <div class="reality-stack">{''.join(render_reality_card(item) for item in data.get('route_reality') or []) or '<p class="empty">No route reality rows available.</p>'}</div>
          </section>
        </main>
        <aside class="intel-side">
          {render_latest_brief(brief)}
          {render_questions(data.get('question_patterns') or [])}
          {render_alert_preview(alert_preview)}
          {render_profile_shell(profile)}
        </aside>
      </section>
      <section class="intel-section">
        <div class="panel-head flat"><div><h2>Recent Feed</h2><p>Alerts, closes, momentum, and funding candidates from the local Telegram listener.</p></div></div>
        <div class="feed-columns">
          {render_event_column('Alerts', recent.get('alerts') or [])}
          {render_event_column('Closes', recent.get('closes') or [])}
          {render_event_column('Momentum', recent.get('momentum') or [])}
          {render_event_column('Funding', recent.get('funding') or [])}
        </div>
      </section>
    </section>
    """
    return shell("Community Intel - SpreadBoard", "intel", body)


def render_triage_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    del config
    data = api_triage(board_path, query)
    summary = data.get("summary") or {}
    buckets = data.get("buckets") or {}
    body = f"""
    <section class="triage-page" data-refresh="180">
      <div class="intel-hero compact-hero">
        <div>
          <span class="page-kicker">Triage</span>
          <h1>What to inspect next</h1>
          <p>Local Telegram, board, funding, identity, and freshness signals collapsed into an operator queue. Read-only: this never sends alerts or touches exchange accounts.</p>
        </div>
        <div class="intel-actions">
          <a class="secondary" href="/intel">Intel</a>
          <a class="secondary" href="/arbitrage?kind=FUTURES">Board</a>
        </div>
      </div>
      <section class="triage-summary-grid">
        {render_triage_summary_card('Look Now', summary.get('look_now'), 'fresh routes')}
        {render_triage_summary_card('Data Review', summary.get('setup_needed'), 'incomplete fields')}
        {render_triage_summary_card('Funding', summary.get('funding_carry'), 'carry rows')}
        {render_triage_summary_card('Identity', summary.get('dex_identity'), 'DEX checks')}
        {render_triage_summary_card('Stale', summary.get('stale_routes'), 'route matches')}
        {render_triage_summary_card('Sources', summary.get('source_gaps'), 'stale/missing')}
      </section>
      <section class="triage-layout">
        <main class="triage-main">
          {render_triage_lane('Look Now', 'Fresh matched rows worth opening first.', buckets.get('look_now') or [], 'route')}
          {render_triage_lane('Data Review', 'Routes with incomplete public fields such as venue symbols, identity, or transfer rails.', buckets.get('setup_needed') or [], 'route')}
          {render_triage_lane('Funding Carry', 'Funding outliers and funding pings that may matter more than the headline spread.', buckets.get('funding_carry') or [], 'funding')}
        </main>
        <aside class="triage-side">
          {render_triage_lane('DEX Identity Work', 'Tokens that need exact chain and contract proof before any DEX row should be trusted.', buckets.get('dex_identity') or [], 'route')}
          {render_triage_lane('Community Spikes', 'Symbols getting repeated alerts, closes, calls, or funding discussion.', buckets.get('community_spike') or [], 'community')}
          {render_triage_lane('Stale Route Matches', 'Matched board rows that are too old for Look Now; use them as research context only.', buckets.get('stale_routes') or [], 'route')}
          {render_triage_lane('Source Gaps', 'Stale or missing local sources that should explain weak/empty views.', buckets.get('source_gaps') or [], 'source')}
        </aside>
      </section>
    </section>
    """
    return shell("Triage - SpreadBoard", "triage", body)


def render_triage_summary_card(title: str, value: Any, note: str) -> str:
    return f'<article class="chart-summary-card"><span>{h(title)}</span><strong>{h(value or 0)}</strong><em>{h(note)}</em></article>'


def render_triage_lane(title: str, note: str, rows: list[dict[str, Any]], kind: str) -> str:
    return f"""
    <section class="triage-lane">
      <div class="panel-head flat"><div><h2>{h(title)}</h2><p>{h(note)}</p></div><span>{h(len(rows))}</span></div>
      <div class="triage-card-list">{''.join(render_triage_card(row, kind) for row in rows[:8]) or '<p class="watch-empty">No rows in this bucket right now.</p>'}</div>
    </section>
    """


def render_triage_card(row: dict[str, Any], kind: str) -> str:
    if kind == "funding":
        return render_triage_funding_card(row)
    if kind == "source":
        return render_triage_source_card(row)
    if kind == "community":
        return render_triage_community_card(row)
    return render_triage_route_card(row)


def render_triage_route_card(row: dict[str, Any]) -> str:
    href = row.get("route_url") or f"/token/{h(row.get('symbol'))}"
    blockers = row.get("blockers") or []
    actions = row.get("next_actions") or []
    freshness = "fresh" if _triage_is_fresh_route(row) else "stale"
    decision = "stale_route" if freshness == "stale" and row.get("route_count") else (row.get("decision") or row.get("status"))
    return f"""
    <article class="triage-card {h(freshness)}">
      <div class="triage-card-head">
        <a href="{h(href)}">{h(row.get('symbol'))}</a>
        <span>{label_text(decision)}</span>
      </div>
      <p>{h(row.get('route_line'))}</p>
      <div class="triage-freshness {h(freshness)}">
        <span>{label_text(row.get('freshness') or freshness)} source</span>
        <strong>{fmt_age(row.get('age_min'))}</strong>
      </div>
      <div class="triage-metrics">
        <span>Open<strong>{fmt_pct(row.get('open_spread_pct'))}</strong></span>
        <span>F APR<strong>{fmt_signed_pct(row.get('funding_apr_pct'), digits=0)}</strong></span>
        <span>Age<strong>{fmt_age(row.get('age_min'))}</strong></span>
        <span>Signals<strong>{h(row.get('event_count') or 0)}</strong></span>
      </div>
      <div class="triage-tags">
        {''.join(f'<span>{label_text(item)}</span>' for item in actions[:2])}
        {''.join(f'<span>{label_text(item)}</span>' for item in blockers[:3]) or '<span>No blocker details</span>'}
      </div>
    </article>
    """


def render_triage_funding_card(row: dict[str, Any]) -> str:
    value = row.get("funding_apr_pct") if row.get("funding_apr_pct") is not None else row.get("funding_delta_pct")
    return f"""
    <article class="triage-card funding">
      <div class="triage-card-head">
        <a href="/token/{h(row.get('symbol'))}">{h(row.get('symbol'))}</a>
        <span>{label_text(row.get('decision'))}</span>
      </div>
      <p>{h(row.get('source'))} {h(row.get('kind'))}</p>
      <div class="triage-metrics">
        <span>Funding<strong>{fmt_signed_pct(value)}</strong></span>
        <span>Open<strong>{fmt_pct(row.get('open_spread_pct'))}</strong></span>
        <span>Next<strong>{h(row.get('minutes_to_funding') if row.get('minutes_to_funding') is not None else '?')} min</strong></span>
        <span>Age<strong>{fmt_age(row.get('age_min'))}</strong></span>
      </div>
    </article>
    """


def render_triage_community_card(row: dict[str, Any]) -> str:
    href = row.get("route_url") or f"/token/{h(row.get('symbol'))}"
    return f"""
    <article class="triage-card community">
      <div class="triage-card-head">
        <a href="{h(href)}">{h(row.get('symbol'))}</a>
        <span>{label_text(row.get('decision'))}</span>
      </div>
      <p>{h(row.get('route_line'))}</p>
      <div class="triage-metrics">
        <span>Msgs<strong>{h(row.get('event_count') or 0)}</strong></span>
        <span>New<strong>{h(row.get('new_count') or 0)}</strong></span>
        <span>Funding<strong>{h(row.get('funding_count') or 0)}</strong></span>
        <span>Open<strong>{fmt_pct(row.get('open_spread_pct'))}</strong></span>
      </div>
    </article>
    """


def render_triage_source_card(row: dict[str, Any]) -> str:
    return f"""
    <article class="triage-card source">
      <div class="triage-card-head">
        <strong>{h(str(row.get('source') or '').replace('_', ' '))}</strong>
        <span>{label_text(row.get('status'))}</span>
      </div>
      <p>{h(row.get('title') or row.get('path') or 'No local source detail')}</p>
      <div class="triage-metrics">
        <span>Age<strong>{fmt_age(row.get('age_min'))}</strong></span>
        <span>Next<strong>{label_text(row.get('decision'))}</strong></span>
      </div>
    </article>
    """


def render_signals_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    data = api_signals(board_path, query)
    recent = data.get("recent_events") or {}
    community = data.get("community") or {}
    body = f"""
    <section class="signals-page" data-refresh="120">
      <div class="intel-hero compact-hero">
        <div>
          <span class="page-kicker">Signals</span>
          <h1>Telegram signal tape</h1>
          <p>Alerts, closes, momentum, funding pings, community calls, and results from the local listener. Read-only and sanitized.</p>
        </div>
        <div class="intel-actions">
          <a class="secondary" href="/signals?window_hours=1">1h</a>
          <a class="secondary" href="/signals?window_hours=12">12h</a>
          <a class="secondary" href="/signals?window_hours=48">48h</a>
        </div>
      </div>
      {render_intel_source_grid(data.get('source_freshness') or {})}
      <section class="signal-board">
        {render_signal_lane('New Alerts', recent.get('alerts') or [])}
        {render_signal_lane('Funding Pings', recent.get('funding') or [])}
        {render_signal_lane('Closes', recent.get('closes') or [])}
        {render_signal_lane('Momentum', recent.get('momentum') or [])}
      </section>
      <section class="signal-split">
        <div class="intel-section">
          <div class="panel-head flat"><div><h2>Community Calls</h2><p>Potential setup posts and discussion markers from local Telegram topics.</p></div></div>
          <div class="signal-list">{''.join(render_signal_event({**item, 'bucket': 'call'}) for item in community.get('calls') or []) or '<p class="empty">No community calls in this window.</p>'}</div>
        </div>
        <div class="intel-section">
          <div class="panel-head flat"><div><h2>Results</h2><p>Result-topic rows for later backfill into route lessons and PnL stories.</p></div></div>
          <div class="signal-list">{''.join(render_signal_event({**item, 'bucket': 'result'}) for item in community.get('results') or []) or '<p class="empty">No results rows in this window.</p>'}</div>
        </div>
      </section>
      <section class="intel-section">
        <div class="panel-head flat"><div><h2>Question Pulse</h2><p>Recurring things people ask about, useful for future alert/profile features.</p></div></div>
        {render_questions(data.get('question_patterns') or [])}
      </section>
    </section>
    """
    return shell("Signals - SpreadBoard", "signals", body)


#: How the funding lanes can be ranked: the live rate, or what each window
#: actually paid.
FUNDING_RANK_TABS: tuple[tuple[str, str], ...] = (
    ("now", "Now"),
    ("1d", "Last 24h"),
    ("7d", "Last 7d"),
    ("30d", "Last 30d"),
)


def render_funding_windows(route: dict[str, Any] | None, route_key: Any) -> str:
    """Realised 1d/7d/30d carry for a route, or an honest blank.

    A rate tells you what a farm pays right now; these tell you what it has
    actually paid. The reference product shows them on every row and it is how
    a member separates a durable farm from this morning's spike. A window we
    have not observed for at least half its length shows a dash rather than a
    number built from a fraction of the period.
    """
    # The venue's own settlement history is the better source: it reaches back
    # thirty days where our samples hold about eighty-six hours, and each entry
    # is a payment that really happened. Our samples remain the fallback for a
    # leg whose venue publishes no history.
    venue_windows = venue_funding_history.route_windows(route or {})
    sampled = market_history.load_funding_windows().get(str(route_key or "")) or {}
    cells = []
    for label in ("1d", "7d", "30d"):
        value = venue_windows.get(label)
        if value is None:
            value = (sampled.get(label) or {}).get("net")
        if value is None:
            cells.append(f'<span class="funding-window unknown"><em>{label}</em><strong>—</strong></span>')
        else:
            tone = "positive" if value > 0 else "negative" if value < 0 else "flat"
            cells.append(
                f'<span class="funding-window {tone}"><em>{label}</em>'
                f"<strong>{fmt_signed_pct(value, digits=2)}</strong></span>"
            )
    return f'<div class="funding-window-strip" title="Carry actually realised over each window">{"".join(cells)}</div>'


def render_funding_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    del config
    selected_farm = (_query_first(query, "farm") or "futures-futures").casefold()
    farm_kinds = {
        "futures-futures": "FUTURES",
        "futures-spot": "FUTURES-SPOT-PAIR",
        "futures-dex": "DEX-FUTURES",
    }
    if selected_farm not in farm_kinds:
        selected_farm = "futures-futures"
    # `rank` and `farm` decide presentation, not data. Letting them into the
    # query gives each tab its own cache key for an identical payload, which is
    # what left /funding?rank=7d at 7.9s beside /funding at 0.03s.
    data_query = {k: v for k, v in query.items() if k not in {"rank", "farm"}}
    funding_query = _query_lists_with(
        data_query,
        funding_only="1",
        kind=farm_kinds[selected_farm],
        # Signed, not magnitude: the best carry to collect belongs at the top.
        sort="funding",
        direction=_query_first(query, "direction") or "desc",
        limit=_query_first(query, "limit") or "25",
    )
    market_data = api_market_spreads(board_path, funding_query)
    funding_groups = market_data.get("groups") or []
    # "Now" keeps the rate-based order the board already computed. The realised
    # windows are what a member wants when deciding whether a farm has actually
    # been paying, so they re-rank on the settled figure instead.
    selected_window = (_query_first(query, "rank") or "now").casefold()
    if selected_window not in {value for value, _ in FUNDING_RANK_TABS}:
        selected_window = "now"
    if selected_window != "now":
        def realised(group: dict[str, Any]) -> float:
            best = group.get("best_funding_route") or {}
            value = venue_funding_history.route_windows(best).get(selected_window)
            # A group we have no settled figure for sorts last rather than
            # jumping to the top on a zero.
            return value if value is not None else float("-inf")

        funding_groups = sorted(funding_groups, key=realised, reverse=True)
    summary = market_data.get("summary") or {}
    api_health_data = (market_data.get("source_health") or {}).get("canonical_api") or {}
    tabs = [
        ("futures-futures", "Futures-Futures"),
        ("futures-spot", "Futures-Spot"),
        ("futures-dex", "Futures-DEX"),
    ]
    body = f"""
    <section class="funding-page terminal-page" data-refresh="300" data-refresh-silent="1">
      {render_board_stream_script(funding_query)}
      <div class="terminal-heading">
        <div>
          <span class="page-kicker">Funding</span>
          <h1>Paired carry farms</h1>
          <p>Funding is ranked as a hedge pair, never as a floating single contract. Expand a token to compare both legs, settled 24h carry, payout cadence, basis, and alerts.</p>
        </div>
        <div class="terminal-live-box">
          <span>{'Live' if market_data.get('ok') else 'Updating'}</span>
          <strong>{fmt_age(api_health_data.get('age_min'))}</strong>
          <em>public funding APIs</em>
        </div>
      </div>
      <nav class="funding-farm-tabs" aria-label="Funding farm type">
        {''.join(f'<a class="{"active" if value == selected_farm else ""}" href="/funding?farm={h(value)}">{h(label)}</a>' for value, label in tabs)}
      </nav>
      <nav class="funding-window-tabs" aria-label="Rank by">
        <span>Rank by</span>
        {''.join(
            f'<a class="{"active" if value == selected_window else ""}" '
            f'href="/funding?farm={h(selected_farm)}&amp;rank={h(value)}">{h(label)}</a>'
            for value, label in FUNDING_RANK_TABS
        )}
      </nav>
      <section class="terminal-tape funding-tape" aria-label="Funding summary">
        {render_market_metric('Assets', summary.get('matching_tokens'), 'unique tokens')}
        {render_market_metric('Funding pairs', summary.get('matching_rows'), 'live venue routes')}
        {render_market_metric('Largest 24h', fmt_signed_pct(summary.get('max_abs_funding_24h_pct'), digits=3), 'absolute paired carry')}
        {render_market_metric('Largest basis', fmt_pct(summary.get('max_executable_spread_pct')), 'entry spread')}
      </section>
      <section class="funding-terminal-panel">
        <div class="panel-head flat terminal-table-title">
          <div>
            <h2>{h(dict(tabs).get(selected_farm))} Farms</h2>
            <p>Positive net values mean the displayed long-short pair receives funding under the exchange sign convention.</p>
          </div>
          <a class="mini-action primary-link" href="/api/spreads?{h(urlencode(_query_with(funding_query, limit=500, offset=0)))}">JSON</a>
        </div>
        <div class="funding-group-list">
          {''.join(render_funding_token_group(group) for group in funding_groups) or render_funding_farm_empty(selected_farm, api_health_data)}
        </div>
      </section>
    </section>
    """
    return shell("Funding - SpreadBoard", "funding", body)


def render_funding_token_group(group: dict[str, Any]) -> str:
    best = group.get("best_funding_route") or group.get("best_route") or {}
    funding_24h = (
        best.get("funding_24h_pct")
        if best.get("funding_24h_pct") is not None
        else best.get("funding_projected_24h_pct")
    )
    funding_basis = (
        "settled 24h"
        if best.get("funding_24h_pct") is not None
        else "24h at current rate"
        if funding_24h is not None
        else "history unavailable"
    )
    name = group.get("token_name") or "Metadata pending"
    best_chart_url = (
        f"/charts?route_key={board.route_key_url(str(best.get('route_key') or ''))}"
        if best.get("route_key")
        else f"/charts?token={quote(str(group.get('token') or ''))}"
    )
    return f"""
    <details class="funding-token-group" data-route-key="{h(best.get('route_key') or '')}">
      <summary>
        <div class="asset-identity">
          <span class="asset-monogram">{h(str(group.get('token') or '?')[:2])}</span>
          <span><a class="asset-chart-symbol" href="{h(best_chart_url)}" onclick="event.stopPropagation()" title="Open the best funding-pair chart">{h(group.get('token'))}</a><em>{h(name)}</em></span>
        </div>
        <div><span>Best farm</span><strong>{h(best.get('long_venue'))} → {h(best.get('short_venue'))}</strong></div>
        <div><span>Net 24h</span><strong data-live-funding>{fmt_signed_pct(funding_24h, digits=3)}</strong><em>{h(funding_basis)}</em></div>
        <div><span>Payouts</span><strong>{h(funding_cadence_pair(best))}</strong></div>
        <div><span>Entry basis</span><strong data-live-spread>{fmt_pct(best.get('executable_spread_pct'))}</strong></div>
        <div><span>Realised</span>{render_funding_windows(best, best.get('route_key'))}</div>
        <div><span>Pairs</span><strong>{h(group.get('route_count') or 0)}</strong></div>
        <span class="funding-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div class="funding-pair-list">
        {''.join(render_funding_pair(route) for route in group.get('routes') or [])}
      </div>
    </details>
    """


def render_funding_pair(row: dict[str, Any]) -> str:
    funding_24h = (
        row.get("funding_24h_pct")
        if row.get("funding_24h_pct") is not None
        else row.get("funding_projected_24h_pct")
    )
    funding_basis = (
        "settled"
        if row.get("funding_24h_pct") is not None
        else "at current rate"
        if funding_24h is not None
        else "history unavailable"
    )
    return f"""
    <article class="funding-pair-row" data-route-key="{h(row.get('route_key') or '')}">
      <div><span>Long</span>{render_exchange_link(row, 'long', include_market_type=True)}<em>{fmt_signed_pct(row.get('long_funding_pct'), digits=4)} · {h(funding_interval_label(row.get('long_funding_interval_hours'), row.get('long_funding_interval_assumed')))}</em></div>
      <div><span>Short</span>{render_exchange_link(row, 'short', include_market_type=True)}<em>{fmt_signed_pct(row.get('short_funding_pct'), digits=4)} · {h(funding_interval_label(row.get('short_funding_interval_hours'), row.get('short_funding_interval_assumed')))}</em></div>
      <div><span>Net 24h</span><strong data-live-funding>{fmt_signed_pct(funding_24h, digits=3)}</strong><em>{h(funding_basis)} · {h(funding_cadence_pair(row))}</em></div>
      <div><span>Basis / VWAP</span><strong data-live-spread>{fmt_pct(row.get('executable_spread_pct'))}</strong><em>{fmt_pct(row.get('depth_weighted_spread_pct'))}</em></div>
      <div><span>Updated</span><strong>{fmt_age(row.get('age_min'))}</strong></div>
      <div class="route-actions">{render_alert_draft_button(row, alert_type='funding', compact=True)}<a href="/pair/{h(board.route_key_url(str(row.get('route_key') or '')))}">Details</a><a href="/charts?route_key={h(board.route_key_url(str(row.get('route_key') or '')))}">Chart</a></div>
    </article>
    """


def render_funding_route_row(row: dict[str, Any]) -> str:
    daily = _float_or_none(row.get("funding_daily_pct"))
    direction_class = "receives" if (daily or 0.0) >= 0 else "pays"
    headline = row.get("displayed_open_spread_pct")
    if headline is None:
        headline = row.get("executable_spread_pct")
    funding_24h = (
        row.get("funding_24h_pct")
        if row.get("funding_24h_pct") is not None
        else row.get("funding_projected_24h_pct")
    )
    return f"""
    <article class="funding-terminal-grid funding-route-row {direction_class} {h(row.get('freshness'))}">
      <div class="funding-token-cell">
        <div class="funding-token-head">
          <a href="{h(row.get('href') or '/markets')}"><strong>{h(row.get('token'))}</strong></a>
          {render_alert_draft_button(row, alert_type='funding', compact=True)}
        </div>
        <span>{h(route_kind_display(row.get('route_kind')))} · {h(row.get('long_venue'))} -> {h(row.get('short_venue'))}</span>
      </div>
      <div>{fmt_signed_pct(row.get('long_funding_pct'), digits=4)}</div>
      <div>{fmt_signed_pct(row.get('short_funding_pct'), digits=4)}</div>
      <div><b>{fmt_signed_pct(row.get('funding_daily_pct'), digits=3)}</b></div>
      <div><strong>{fmt_signed_pct(funding_24h, digits=3)}</strong></div>
      <div>{fmt_pct(headline)}</div>
      <div>{fmt_money(row.get('depth_usd'))}</div>
      <div>{fmt_age(row.get('age_min'))}</div>
      <div><span class="market-status {h(row.get('status'))}">{label_text(row.get('status'))}</span></div>
    </article>
    """


def render_alert_draft_button(
    row: dict[str, Any],
    *,
    alert_type: str,
    compact: bool = False,
    label: str | None = None,
) -> str:
    symbol = row.get("token") or row.get("symbol") or ""
    if alert_type == "funding":
        current_value = (
            row.get("funding_24h_pct")
            if row.get("funding_24h_pct") is not None
            else row.get("funding_daily_pct")
        )
    else:
        current_value = (
            row.get("displayed_open_spread_pct")
            if row.get("displayed_open_spread_pct") is not None
            else row.get("executable_spread_pct")
            if row.get("executable_spread_pct") is not None
            else row.get("spread_pct")
        )
    button_label = label or ("Funding alert" if alert_type == "funding" else "Spread alert")
    compact_class = " compact" if compact else ""
    return (
        f'<button class="route-alert-btn js-alert-draft{compact_class}" type="button" '
        f'aria-label="Create {h(button_label.casefold())} for {h(symbol)}" '
        f'title="{h(button_label)}" '
        f'data-alert-type="{h(alert_type)}" '
        f'data-symbol="{h(symbol)}" '
        f'data-route-key="{h(row.get("route_key") or "")}" '
        f'data-route-kind="{h(row.get("route_kind") or row.get("kind") or "")}" '
        f'data-long-venue="{h(row.get("long_venue") or "")}" '
        f'data-long-market-type="{h(row.get("long_market_type") or "")}" '
        f'data-short-venue="{h(row.get("short_venue") or "")}" '
        f'data-short-market-type="{h(row.get("short_market_type") or "")}" '
        f'data-current-value="{h(current_value if current_value is not None else "")}">'
        f'<span aria-hidden="true">+</span><span>{h("Alert" if compact else button_label)}</span>'
        "</button>"
    )


def render_community_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    data = api_community(board_path, query)
    insights = data.get("community_insights") or {}
    body = f"""
    <section class="community-page" data-refresh="180">
      <div class="intel-hero compact-hero">
        <div>
          <span class="page-kicker">Community</span>
          <h1>Calls, results, and repeated questions</h1>
          <p>Community scoreboard, result threads, and discussion patterns joined to local board reality. Read-only and sanitized.</p>
        </div>
        <div class="intel-actions">
          <a class="secondary" href="/intel">Intel</a>
          <a class="secondary" href="/signals">Signal tape</a>
          <a class="secondary" href="/playbook">Playbook</a>
        </div>
      </div>
      {render_intel_source_grid(data.get('source_freshness') or {})}
      <section class="community-layout">
        <main class="community-main">
          {render_community_scoreboard(insights.get('scoreboard') or {})}
          {render_community_call_ledger(insights.get('call_ledger') or [])}
          {render_community_discussion(insights.get('discussion') or [])}
          <section class="community-events">
            {render_community_event_group('Community Calls', insights.get('calls') or [], 'call')}
            {render_community_event_group('Results', insights.get('results') or [], 'result')}
          </section>
        </main>
        <aside class="community-side">
          {render_community_brief(insights)}
          {render_questions(insights.get('question_patterns') or data.get('question_patterns') or [])}
        </aside>
      </section>
    </section>
    """
    return shell("Community - SpreadBoard", "community", body)


def render_playbook_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    data = api_playbook(board_path, query)
    source_note = data.get("source_note") or {}
    body = f"""
    <section class="playbook-page" data-refresh="180">
      <div class="intel-hero compact-hero">
        <div>
          <span class="page-kicker">Community Playbook</span>
          <h1>Answers for the questions that keep coming back</h1>
          <p>Telegram question patterns turned into read-only operator checklists: alerts, funding farms, D/W rails, missed spreads, convergence, and future profile/PnL context.</p>
        </div>
        <div class="intel-actions">
          <a class="secondary" href="/community">Community</a>
          <a class="secondary" href="/signals">Signals</a>
          <a class="secondary" href="/alerts">Alerts</a>
        </div>
      </div>
      <section class="playbook-status">
        <article class="source-card {h(source_note.get('telegram_status') or 'missing')}">
          <span>Telegram events</span>
          <strong>{label_text(source_note.get('telegram_status') or 'missing')}</strong>
          <em>{fmt_age(source_note.get('telegram_age_min'))}</em>
        </article>
        <article class="source-card {h(source_note.get('brief_status') or 'missing')}">
          <span>Topic brief</span>
          <strong>{label_text(source_note.get('brief_status') or 'missing')}</strong>
          <em>{fmt_age(source_note.get('brief_age_min'))}</em>
        </article>
        <article class="playbook-note">
          <span>How to read this page</span>
          <strong>{h(source_note.get('message'))}</strong>
        </article>
      </section>
      <section class="playbook-grid">
        {''.join(render_playbook_card(card) for card in data.get('cards') or [])}
      </section>
      <section class="playbook-guard">
        <div>
          <span class="page-kicker">Read-only boundary</span>
          <h2>What this page will not do</h2>
          <p>It is a community answer surface and checklist. It never sends Pushover, places orders, calls private balance APIs, signs transactions, or starts executor paths.</p>
        </div>
        <div class="playbook-guard-list">{''.join(f'<span>{h(item)}</span>' for item in data.get('read_only_guards') or [])}</div>
      </section>
    </section>
    """
    return shell("Playbook - SpreadBoard", "playbook", body)


def render_playbook_card(card: dict[str, Any]) -> str:
    examples = card.get("examples") or []
    links = card.get("links") or []
    checks = card.get("checks") or []
    return f"""
    <article class="playbook-card {h(card.get('status') or 'ready')}">
      <div class="playbook-card-head">
        <div>
          <span>{h(card.get('category'))}</span>
          <h2>{h(card.get('title'))}</h2>
        </div>
        <strong>{h(card.get('count') or 0)}</strong>
      </div>
      <p>{h(card.get('why'))}</p>
      <div class="playbook-answer">{h(card.get('answer'))}</div>
      <ol class="playbook-steps">{''.join(f'<li>{h(item)}</li>' for item in checks[:4])}</ol>
      <div class="playbook-links">{''.join(f'<a href="{h(href)}">{h(label)}</a>' for label, href in links[:3])}</div>
      <div class="playbook-examples">
        {''.join(render_playbook_example(item) for item in examples[:2]) or '<span>No fresh matching questions in the selected window.</span>'}
      </div>
    </article>
    """


def render_playbook_example(item: dict[str, Any]) -> str:
    return (
        '<span>'
        f'<strong>{h(item.get("symbol") or "Example")}</strong>'
        f'{h(item.get("text_excerpt") or item.get("first_line") or "No excerpt available.")}'
        '</span>'
    )


def render_board_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    snapshot_data = _legacy_board_snapshot(board_path, query)
    health = api_source_health(board_path, config)
    rows = snapshot_data["rows"][:120]
    selected_kind = _query_first(query, "kind") or ""
    include_stale = _query_bool(query, "include_stale")
    rows_html = "".join(render_board_row(row) for row in rows)
    mobile_rows_html = "".join(render_board_mobile_card(row) for row in rows)
    empty_state_html = render_board_empty_state(selected_kind, snapshot_data, health)
    mobile_empty_state_html = render_board_empty_state(selected_kind, snapshot_data, health, mobile=True)
    body = f"""
    <section class="arbitrage-page">
      <div class="arb-toolbar">
        <div class="tab-selector" aria-label="Route type">
          {''.join(render_kind_tab(item, selected_kind, query, health) for item in board.ROUTE_KINDS)}
        </div>
        <div class="quick-tools">
          <details class="filter-menu">
            <summary class="round-tool" aria-label="Filters"><span aria-hidden="true"></span></summary>
            {render_filters(query)}
          </details>
        </div>
        <div class="live-tools">
          <span class="select-tool"><span class="clock-dot" aria-hidden="true"></span>Live</span>
          <a class="select-tool" href="/learn"><span class="link-dot" aria-hidden="true"></span>Ref links</a>
        </div>
      </div>

      <div class="board-meta">
        <span>{h(snapshot_data.get('fresh_count'))} fresh</span>
        <span>{h(snapshot_data.get('stale_count'))} stale</span>
        <span>{h(render_age_text(snapshot_data.get("age_min"), snapshot_data.get("error")))}</span>
        {render_stale_toggle(query, include_stale)}
      </div>

      <section class="arb-table-wrapper-wide" aria-label="Current Board">
        <div class="arb-scroll">
          <div class="arb-grid-futures-main arb-grid-head" role="row">
            <div class="arb-header-cell-start"><span>sort</span>Token</div>
            <div class="arb-header-grid-futures-market-full">Buy<br>Sell</div>
            <div>Fund</div>
            <div><span>sort</span>1d</div>
            <div><span>sort</span>7d</div>
            <div><span>sort</span>30d</div>
            <div>D / W status</div>
            <div>F Spread</div>
            <div>Funding 24h</div>
            <div>Open Spread</div>
          </div>
          <div class="arb-rows">
            {rows_html or empty_state_html}
          </div>
        </div>
      </section>
      <section class="mobile-board-cards" aria-label="Current Board Mobile">
        {mobile_rows_html or mobile_empty_state_html}
      </section>
    </section>
    """
    return shell("SpreadBoard", "board", body)


def render_pair_page(route_key: str, board_path: Path, config: dict[str, Any]) -> str:
    detail = api_pair(route_key, board_path, config)
    if not detail.get("ok"):
        body = """
        <section class="detail-frame">
          <div class="detail-head">
            <a class="back" href="/arbitrage?kind=FUTURES">Arbitrage</a>
            <div><span class="page-kicker">Route</span><h1>Route Not Found</h1></div>
          </div>
          <div class="panel text">The local JSONL source no longer has that route key.</div>
        </section>
        """
        return shell("Route not found - SpreadBoard", "board", body)
    row = detail["board_row"]
    legs = detail.get("legs") or {}
    pair_intel = api_intel(board_path, {"symbol": [str(row.get("symbol") or "")], "limit": ["6"]})
    history = api_history(route_key, board_path, {"max_points": ["1440"]}).get("rows") or []
    body = f"""
    <section class="pair-page">
      {render_pair_snapshot_banner(row)}
      {render_pair_cockpit(row, detail, pair_intel, history)}
      {render_pair_intel_strip(row, pair_intel)}

      <section class="pair-layout">
        <main class="pair-main">
          {render_pair_checklist(row, detail)}
          {render_route_timeline(row, history)}
          <div class="metric-tape">
            {metric_card('Executable', fmt_pct(row.get('spread_pct')), 'VWAP')}
            {metric_card('Open', fmt_pct(row.get('displayed_open_spread_pct')), 'headline')}
            {metric_card('F Spread', fmt_signed_pct(row.get('funding_spread_pct')), 'funding')}
            {metric_card('Funding 24h', fmt_signed_pct((detail.get('funding') or {}).get('net_24h_pct'), digits=3), 'settled / projected')}
            {metric_card('Age', fmt_age(row.get('age_min')), 'row')}
          </div>
          <div class="detail-grid">
            {render_volatility_card(detail)}
            {render_funding_card(detail)}
            {render_okx_dex_card(detail.get('okx_dex_quote'))}
            {render_route_health_card(detail.get('route_health'))}
          </div>
          {render_pair_telegram_context(pair_intel)}
        </main>
        <aside class="pair-side">
          {render_leg_card('Buy leg', legs.get('long') or {})}
          {render_leg_card('Sell leg', legs.get('short') or {})}
          {render_pair_health_summary(detail)}
        </aside>
      </section>
      {render_funding_history_dialog(detail)}
    </section>
    {render_funding_history_script()}
    """
    return shell(f"{row.get('symbol')} route - SpreadBoard", "board", body)


def render_saved_charts_panel(
    user: Any, selected_route: str, accounts_path: Any
) -> str:
    """A member's own pinned pairs, and the control to pin the one on screen.

    Any route can be pinned, including one whose spread is negative: a pair that
    never converges can still be worth watching. SKHY against SKHX on
    Hyperliquid is the case in point -- the same asset at a fixed 10:1, so the
    ratio is stored beside the route and the spread only reads correctly once
    one side is scaled by it.
    """
    if user is None:
        return ""
    try:
        charts = accounts.list_saved_charts(user.id, db_path=accounts_path)
    except Exception:  # noqa: BLE001 - the page must render without the list.
        charts = []
    saved_keys = {str(item.get("route_key")) for item in charts}
    rows = "".join(
        f"""
        <li>
          <a href="/charts?route_key={h(quote(str(item.get('route_key') or ''), safe=''))}">
            <strong>{h(item.get('label') or str(item.get('route_key') or '').split('|')[0])}</strong>
            <em>{h(' → '.join(str(item.get('route_key') or '').split('|')[1:5:2]))}</em>
            {f"<span class='saved-ratio'>ratio {h(item.get('ratio'))}:1</span>" if float(item.get('ratio') or 1) != 1 else ''}
          </a>
          <button type="button" class="saved-chart-remove" data-route="{h(item.get('route_key'))}">Remove</button>
        </li>"""
        for item in charts
    ) or "<li class='saved-empty'>No pinned charts yet. Open a route and choose Pin this chart.</li>"
    pin = ""
    if selected_route:
        already = selected_route in saved_keys
        pin = f"""
        <div class="saved-chart-pin">
          <input id="savedChartLabel" placeholder="Name this chart" maxlength="120">
          <input id="savedChartRatio" placeholder="Ratio (e.g. 10 for SKHY:SKHX)" inputmode="decimal">
          <button type="button" id="savedChartPin" data-route="{h(selected_route)}">
            {'Update pin' if already else 'Pin this chart'}
          </button>
        </div>"""
    return f"""
    <section class="saved-charts-panel">
      <div class="panel-head flat"><div><h2>My charts</h2><p>Pairs you track, including ones that never converge.</p></div></div>
      {pin}
      <ul class="saved-chart-list">{rows}</ul>
    </section>
    <script>
    (() => {{
      const pinBtn = document.getElementById("savedChartPin");
      // Every state-changing POST carries the session CSRF token; without it
      // the server answers 400 before the handler ever runs.
      const csrf = () => document.querySelector("[data-logout]")?.dataset.csrf || "";
      const post = (path, body) => fetch(path, {{
        method: "POST",
        credentials: "same-origin",
        headers: {{"Content-Type": "application/json", "X-CSRF-Token": csrf()}},
        body: JSON.stringify(body),
      }}).then(() => window.location.reload());
      if (pinBtn) {{
        pinBtn.addEventListener("click", () => post("/api/saved-charts", {{
          route_key: pinBtn.dataset.route,
          label: (document.getElementById("savedChartLabel") || {{}}).value || "",
          ratio: Number((document.getElementById("savedChartRatio") || {{}}).value || 1) || 1,
        }}));
      }}
      for (const button of document.querySelectorAll(".saved-chart-remove")) {{
        button.addEventListener("click", () => post("/api/saved-charts/delete", {{
          route_key: button.dataset.route,
        }}));
      }}
    }})();
    </script>"""


def render_charts_page(
    board_path: Path,
    config: dict[str, Any],
    query: dict[str, list[str]],
    *,
    user: Any = None,
    accounts_path: Any = None,
) -> str:
    selected_route = _query_first(query, "route_key") or ""
    market_query: dict[str, list[str]] = {
        "limit": ["500"],
        "sort": ["edge"],
        "direction": ["desc"],
    }
    market_data = api_market_spreads(board_path, market_query)
    catalogue = chart_catalog.load()
    markets = catalogue.get("markets") or []
    selected_row = _find_canonical_route(selected_route, board_path) if selected_route else None
    window = (_query_first(query, "window") or "1h").casefold()
    if window not in CHART_WINDOWS:
        window = "1h"
    window_config = chart_window_config(window)
    history_payload = (
        api_history(
            selected_route,
            board_path,
            {
                "max_points": [str(window_config["max_points"])],
                "hours": [str(window_config["hours"])],
                "bucket_seconds": [str(window_config["bucket_seconds"])],
                "live": ["1"],
            },
        )
        if selected_row is not None
        else {"rows": [], "meta": {}}
    )
    sampled_row = (history_payload.get("sample") or {}).get("row")
    if isinstance(sampled_row, dict):
        selected_row = sampled_row
    detail = (
        {"ok": True, **live.get_route_detail(_canonical_pair_row(selected_row), config=config)}
        if selected_row is not None
        else None
    )
    history = history_payload.get("rows") or []
    history = filter_chart_history(history, window)
    body = f"""
    <section class="charts-page">
      <header class="terminal-heading chart-heading">
        <div>
          <span class="page-kicker">Charts</span>
          <h1>Build a spread chart</h1>
          <p>Select a token and the exact long and short venue. Nothing is plotted until a route is chosen.</p>
        </div>
        <div class="terminal-live-box {'live' if market_data.get('ok') else 'unavailable'}">
          <span>{'Live' if market_data.get('ok') else 'Updating'}</span>
          <strong>{fmt_age(((market_data.get('source_health') or {}).get('canonical_api') or {}).get('age_min'))}</strong>
          <em>canonical public APIs</em>
        </div>
      </header>
      {render_saved_charts_panel(user, selected_route, accounts_path)}
      {render_chart_builder(markets, selected_row, catalogue)}
      {render_selected_chart(selected_row, detail, history, window, history_payload.get('meta') or {}) if selected_row and detail else render_chart_blank_state()}
      {render_funding_history_dialog(detail) if detail else ''}
    </section>
    {render_chart_builder_script([item for item in markets if item.get('token') == str((selected_row or {}).get('token') or '')], selected_row)}
    {render_funding_history_script() if detail else ''}
    """
    return shell("Charts - SpreadBoard", "charts", body)


def render_chart_builder(
    markets: list[dict[str, Any]],
    selected_row: dict[str, Any] | None,
    catalogue: dict[str, Any],
) -> str:
    selected_token = str((selected_row or {}).get("token") or "")
    skhx_route = board.route_key_url(chart_catalog.skhx_skhynix_route_key())
    return f"""
    <section class="chart-builder">
      <div class="chart-builder-title">
        <div><span class="chart-builder-icon" aria-hidden="true">+</span><strong>Custom chart</strong><em>Choose any active stablecoin market in the public venue catalogue.</em></div>
        <div class="chart-builder-tools"><a href="/charts?route_key={h(skhx_route)}&window=1h" title="Open the normalized Hyperliquid relative-value chart">SKHX / SK Hynix · 10:1</a><span class="chart-builder-state" data-chart-state>{h(catalogue.get('token_count') or 0)} tokens · {h(catalogue.get('count') or 0)} markets</span></div>
      </div>
      <form class="chart-builder-form" action="/charts" method="get" data-chart-builder>
        <label class="chart-token-field"><span>Token</span><input data-chart-token list="chart-token-list" value="{h(selected_token)}" placeholder="Type a symbol, e.g. COTI" autocomplete="off" spellcheck="false"><datalist id="chart-token-list"></datalist></label>
        <div class="chart-leg-picker long">
          <span>Long</span>
          <select data-chart-long aria-label="Long venue and market"><option value="">Select venue / market</option></select>
          <div class="chart-quote-preview"><span>Bid<strong data-long-bid>—</strong></span><span>Ask<strong data-long-ask>—</strong></span></div>
        </div>
        <button class="chart-swap" type="button" data-chart-swap title="Swap long and short legs" aria-label="Swap long and short legs">⇄</button>
        <div class="chart-leg-picker short">
          <span>Short</span>
          <select data-chart-short aria-label="Short venue and market"><option value="">Select venue / market</option></select>
          <div class="chart-quote-preview"><span>Bid<strong data-short-bid>—</strong></span><span>Ask<strong data-short-ask>—</strong></span></div>
        </div>
        <input type="hidden" name="route_key" data-chart-route-key>
        <button class="chart-create-button" type="submit" data-chart-create disabled>Create chart</button>
      </form>
    </section>
    """


def render_chart_builder_script(
    markets: list[dict[str, Any]],
    selected_row: dict[str, Any] | None,
) -> str:
    route_data = [{key: row.get(key) for key in ("token", "venue", "market_type", "symbol", "quote")} for row in markets]
    selected_long = (
        f"{selected_row.get('long_venue')}|{selected_row.get('long_market_type')}|{selected_row.get('long_market_symbol') or ((selected_row.get('notes') or {}).get('route_inputs') or {}).get('long', {}).get('symbol', '')}"
        if selected_row
        else ""
    )
    selected_short = (
        f"{selected_row.get('short_venue')}|{selected_row.get('short_market_type')}|{selected_row.get('short_market_symbol') or ((selected_row.get('notes') or {}).get('route_inputs') or {}).get('short', {}).get('symbol', '')}"
        if selected_row
        else ""
    )
    return f"""
    <script type="application/json" id="chart-route-data">{json_script_data(route_data)}</script>
    <script>
    (() => {{
      const form = document.querySelector('[data-chart-builder]');
      if (!form) return;
      let markets = JSON.parse(document.getElementById('chart-route-data').textContent || '[]');
      let catalogTokens = [];
      const token = form.querySelector('[data-chart-token]');
      const longSelect = form.querySelector('[data-chart-long]');
      const shortSelect = form.querySelector('[data-chart-short]');
      const routeKey = form.querySelector('[data-chart-route-key]');
      const create = form.querySelector('[data-chart-create]');
      const state = document.querySelector('[data-chart-state]');
      const selectedLong = {json.dumps(selected_long)};
      const selectedShort = {json.dumps(selected_short)};
      const combo = (item) => `${{item.venue || ''}}|${{item.market_type || ''}}|${{item.symbol || ''}}`;
      const tokenMarkets = () => markets.filter((row) => row.token === token.value);
      const tokenList = document.getElementById('chart-token-list');
      function fillTokenSuggestions(query='') {{
        const needle=String(query||'').trim().toUpperCase();
        const matches=catalogTokens.filter(value=>!needle||value.startsWith(needle)||value.includes(needle)).slice(0,80);
        tokenList.replaceChildren(...matches.map(value=>new Option(value)));
      }}
      function optionsForToken() {{
        const values = new Map();
        tokenMarkets().forEach((row) => {{
          values.set(combo(row), `${{row.venue}} · ${{row.market_type}} · ${{row.symbol}}`);
        }});
        return [...values.entries()].sort((a, b) => a[1].localeCompare(b[1]));
      }}
      function fill(select, preferred) {{
        const current = preferred || select.value;
        select.innerHTML = '<option value="">Select venue / market</option>';
        optionsForToken().forEach(([value, label]) => {{
          const option = document.createElement('option');
          option.value = value;
          option.textContent = label;
          option.selected = value === current;
          select.appendChild(option);
        }});
      }}
      function selectedMarket(value) {{
        return tokenMarkets().find((item) => combo(item) === value) || null;
      }}
      function customKey(longLeg, shortLeg) {{
        const payload=JSON.stringify({{token:token.value,long:{{market_type:longLeg.market_type,symbol:longLeg.symbol,venue:longLeg.venue}},short:{{market_type:shortLeg.market_type,symbol:shortLeg.symbol,venue:shortLeg.venue}}}});
        const bytes=new TextEncoder().encode(payload); let binary=''; bytes.forEach(byte=>binary+=String.fromCharCode(byte));
        return `CUSTOM:${{btoa(binary).replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'')}}`;
      }}
      function update() {{
        const longLeg=selectedMarket(longSelect.value); const shortLeg=selectedMarket(shortSelect.value);
        form.querySelector('[data-long-bid]').textContent = 'quoted live'; form.querySelector('[data-long-ask]').textContent = 'on create';
        form.querySelector('[data-short-bid]').textContent = 'quoted live'; form.querySelector('[data-short-ask]').textContent = 'on create';
        const ready=Boolean(longLeg&&shortLeg&&longSelect.value!==shortSelect.value);
        routeKey.value=ready?customKey(longLeg,shortLeg):''; create.disabled=!ready;
        state.textContent=ready?'Route ready':(longLeg&&shortLeg?'Choose two different markets':'Choose both legs');
      }}
      async function rebuild(useSelected = false) {{
        token.value=token.value.trim().toUpperCase();
        fillTokenSuggestions(token.value);
        if (token.value && catalogTokens.includes(token.value) && !markets.some(item => item.token === token.value)) {{
          state.textContent='Loading venue markets';
          try {{ const response=await fetch(`/api/chart-catalog?token=${{encodeURIComponent(token.value)}}`); const data=await response.json(); markets=data.markets||[]; }}
          catch(error) {{ markets=[]; state.textContent='Catalogue unavailable'; }}
        }}
        if (token.value && !catalogTokens.includes(token.value)) {{
          longSelect.innerHTML='<option value="">Select a suggested token first</option>';
          shortSelect.innerHTML='<option value="">Select a suggested token first</option>';
          routeKey.value=''; create.disabled=true; state.textContent='Choose a token from the suggestions'; return;
        }}
        fill(longSelect, useSelected ? selectedLong : '');
        fill(shortSelect, useSelected ? selectedShort : '');
        update();
      }}
      let tokenTimer=null;
      token.addEventListener('input', () => {{ clearTimeout(tokenTimer); token.value=token.value.toUpperCase(); fillTokenSuggestions(token.value); tokenTimer=setTimeout(()=>rebuild(false),180); }});
      token.addEventListener('change', () => rebuild(false));
      longSelect.addEventListener('change', update);
      shortSelect.addEventListener('change', update);
      form.querySelector('[data-chart-swap]').addEventListener('click', () => {{
        const previous = longSelect.value;
        longSelect.value = shortSelect.value;
        shortSelect.value = previous;
        update();
      }});
      form.addEventListener('submit', (event) => {{
        if (!routeKey.value) event.preventDefault();
      }});
      fetch('/api/chart-catalog').then(response=>response.json()).then(data=>{{catalogTokens=(data.tokens||[]).filter(Boolean).sort();fillTokenSuggestions(token.value);return rebuild(Boolean(token.value));}}).catch(()=>{{state.textContent='Catalogue unavailable';}});
    }})();
    </script>
    """


def render_chart_blank_state() -> str:
    return """
    <section class="chart-blank-state">
      <div aria-hidden="true">+</div>
      <strong>No chart selected</strong>
      <p>Choose a token and both venue legs above to inspect spread persistence and funding history.</p>
    </section>
    """


CHART_WINDOWS: dict[str, dict[str, float | int | str]] = {
    "1m": {"label": "1M", "hours": 1 / 60, "bucket_seconds": 1, "max_points": 1200},
    "5m": {"label": "5M", "hours": 5 / 60, "bucket_seconds": 1, "max_points": 1200},
    "30m": {"label": "30M", "hours": 0.5, "bucket_seconds": 2, "max_points": 1200},
    "1h": {"label": "1H", "hours": 1, "bucket_seconds": 3, "max_points": 1200},
    "4h": {"label": "4H", "hours": 4, "bucket_seconds": 12, "max_points": 1200},
    "12h": {"label": "12H", "hours": 12, "bucket_seconds": 36, "max_points": 1200},
    "1d": {"label": "1D", "hours": 24, "bucket_seconds": 60, "max_points": 1800},
    "3d": {"label": "3D", "hours": 72, "bucket_seconds": 216, "max_points": 1200},
    "7d": {"label": "7D", "hours": 168, "bucket_seconds": 504, "max_points": 1200},
}


def chart_window_config(window: str) -> dict[str, float | int | str]:
    return CHART_WINDOWS.get(str(window).casefold(), CHART_WINDOWS["1h"])


def filter_chart_history(history: list[dict[str, Any]], window: str) -> list[dict[str, Any]]:
    hours = float(chart_window_config(window)["hours"])
    cutoff_us = int((time.time() - hours * 3600) * 1_000_000)
    return [
        row
        for row in history
        if (_float_or_none(row.get("quote_ts_us")) or 0) >= cutoff_us
    ]


def render_selected_chart(
    row: dict[str, Any],
    detail: dict[str, Any],
    history: list[dict[str, Any]],
    window: str,
    history_meta: dict[str, Any] | None = None,
) -> str:
    legs = detail.get("legs") or {}
    long_leg = legs.get("long") or {}
    short_leg = legs.get("short") or {}
    route_key = board.route_key_url(str(row.get("route_key") or ""))
    windows = [(value, str(config["label"])) for value, config in CHART_WINDOWS.items()]
    history_meta = history_meta or {}
    relative_value = ((row.get("notes") or {}).get("relative_value") or {}) if isinstance(row.get("notes"), dict) else {}
    normalization_note = (
        f" · normalized {float(relative_value.get('long_multiplier') or 1):g}:{float(relative_value.get('short_multiplier') or 1):g}"
        if relative_value
        else ""
    )
    coverage_note = (
        f"{float(history_meta.get('coverage_pct') or 0):.0f}% window coverage"
        + (f" · older points use {h(history_meta.get('historical_proxy_timeframe'))} close-price proxy" if history_meta.get("historical_proxy") else " · exact book samples only")
    )
    return f"""
    <section class="selected-chart">
      <header class="selected-chart-head">
        <div><span>Spread chart</span><strong>{h(row.get('token'))}</strong><em>{h(row.get('long_venue'))} {h(leg_market_label(row.get('long_venue'), row.get('long_market_type')))} → {h(row.get('short_venue'))} {h(leg_market_label(row.get('short_venue'), row.get('short_market_type')))}{h(normalization_note)}</em></div>
        <nav class="chart-window-tabs" aria-label="Chart window">
          {''.join(f'<a class="{"active" if value == window else ""}" href="/charts?route_key={h(route_key)}&window={value}">{label}</a>' for value, label in windows)}
        </nav>
      </header>
      <div class="selected-chart-layout">
        <aside class="chart-leg-stats">
          {render_chart_leg_stats('Long', long_leg)}
          {render_chart_leg_stats('Short', short_leg)}
        </aside>
        <div class="chart-plot-stack">
          <section class="chart-plot-panel">
            <div class="chart-plot-title">
              <span>Spread progression</span>
              <strong data-chart-headline>Open {fmt_pct(row.get('displayed_open_spread_pct'))}</strong>
              <button type="button" data-funding-open>Funding history</button>
              <em data-chart-live-state>Connecting to exact route...</em>
            </div>
            {render_live_spread_chart(str(row.get('route_key') or ''), history, window)}
          </section>
        </div>
      </div>
      <footer class="selected-chart-foot">
        <a href="/pair/{h(route_key)}">Open full pair details</a>
        <span data-chart-observation-count>{h(len(history))} observations · {coverage_note}</span>
      </footer>
    </section>
    """


def render_chart_leg_stats(label: str, leg: dict[str, Any]) -> str:
    has_funding = leg.get("market_type") == "Futures"
    settled = leg.get("funding_24h_pct")
    funding_24h = settled if settled is not None else leg.get("projected_funding_24h_pct")
    return f"""
    <article>
      <header><span>{h(label)}</span>{render_venue_link(leg.get('venue'), leg.get('market_type'), leg.get('exchange_url'))}<em>{h(leg.get('market_type'))}</em></header>
      <div><span>Volume 24h</span><strong>{fmt_money(leg.get('volume_24h_usd'))}</strong></div>
      <div><span>Live funding</span><strong data-live-funding="{h(leg.get('side'))}">{fmt_signed_pct(leg.get('current_funding_pct'), digits=4) if has_funding else 'not applicable'}</strong></div>
      <div><span>{'Settled 24h' if settled is not None else '24h at current' if has_funding else 'Funding 24h'}</span><strong>{fmt_signed_pct(funding_24h, digits=4) if has_funding else 'not applicable'}</strong></div>
      <div><span>Payout</span><strong data-live-cadence="{h(leg.get('side'))}">{h(funding_interval_label(leg.get('funding_interval_hours'), leg.get('funding_interval_assumed'))) if has_funding else 'not applicable'}</strong></div>
      <div><span>Next</span><strong data-live-next="{h(leg.get('side'))}">{h(fmt_next_funding(leg.get('next_funding_ts_us'))) if has_funding else 'not applicable'}</strong></div>
    </article>
    """


def render_spread_history_chart(history: list[dict[str, Any]]) -> str:
    series = [
        ("Entry", "entry", _chart_points(history, "quote_ts_us", "executable_spread_pct", 1_000)),
        ("Exit", "exit", _chart_points(history, "quote_ts_us", "exit_spread_pct", 1_000)),
    ]
    return render_dual_chart_svg(
        series,
        "Entry and exit spread history",
        independent_lanes=True,
    )


def render_live_spread_chart(
    route_key: str,
    history: list[dict[str, Any]],
    window: str,
) -> str:
    window_config = chart_window_config(window)
    hours = float(window_config["hours"])
    bucket_seconds = int(window_config["bucket_seconds"])
    max_points = int(window_config["max_points"])
    initial = {
        "ok": True,
        "rows": history,
        "meta": _history_meta(history),
        "count": len(history),
        "sample": {"status": "idle"},
    }
    return f"""
    <script src="/assets/lightweight-charts.js"></script>
    <div class="live-spread-chart" data-live-spread-chart>
      <div class="live-chart-legend" aria-label="Chart series">
        <button class="entry active" type="button" data-series-toggle="entry"><i></i>Open ask → bid <strong data-latest-entry>—</strong></button>
        <button class="matched" type="button" data-series-toggle="matched"><i></i>$50 VWAP <strong data-latest-matched>—</strong></button>
        <button class="exit active" type="button" data-series-toggle="exit"><i></i>Out top book <strong data-latest-exit>—</strong></button>
        <span class="funding-a"><i></i>Long fund <strong data-latest-long-funding>—</strong></span>
        <span class="funding-b"><i></i>Short fund <strong data-latest-short-funding>—</strong></span>
      </div>
      <div class="live-chart-canvas" data-live-chart-canvas aria-label="Interactive entry, exit and funding chart"></div>
      <div class="live-chart-tooltip" data-live-chart-tooltip hidden></div>
      <div class="live-chart-note">
        <span>Drag to pan · scroll to zoom · In buys the long ask and sells the short bid · Out reverses both legs.</span>
        <strong data-live-chart-age>Waiting for sample</strong>
      </div>
    </div>
    <script type="application/json" id="live-chart-initial">{json_script_data(initial)}</script>
    <script>
    (() => {{
      const root = document.querySelector('[data-live-spread-chart]');
      if (!root) return;
      const routeKey = {json.dumps(route_key)};
      const hours = {hours};
      const bucketSeconds = {bucket_seconds};
      const maxPoints = {max_points};
      const canvas = root.querySelector('[data-live-chart-canvas]');
      const tooltip = root.querySelector('[data-live-chart-tooltip]');
      const state = document.querySelector('[data-chart-live-state]');
      const headline = document.querySelector('[data-chart-headline]');
      const count = document.querySelector('[data-chart-observation-count]');
      const age = root.querySelector('[data-live-chart-age]');
      let timer = null;
      let stream = null;
      let controller = null;
      let refreshing = false;
      let chart = null;
      let resizeObserver = null;
      let themeObserver = null;
      const chartSeries = {{}};
      let latestRows = [];
      let historyRows = [];
      const pct = (value) => Number.isFinite(Number(value))
        ? `${{Number(value) >= 0 ? '+' : ''}}${{Number(value).toFixed(3)}}%`
        : '—';
      const num = (value) => value === null || value === undefined || value === ''
        ? null
        : (Number.isFinite(Number(value)) ? Number(value) : null);
      const esc = (value) => String(value).replace(/[&<>"']/g, (char) =>
        ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[char]);
      const timeLabel = (timestamp, long = false) => new Intl.DateTimeFormat(undefined, {{
        month: long ? 'short' : undefined,
        day: long ? '2-digit' : undefined,
        hour: '2-digit',
        minute: '2-digit',
        second: hours <= 1 ? '2-digit' : undefined,
      }}).format(new Date(timestamp));

      function palette() {{
        const style = getComputedStyle(document.documentElement);
        return {{
          text: style.getPropertyValue('--terminal-text').trim() || '#dce9e5',
          muted: style.getPropertyValue('--terminal-muted').trim() || '#7f9690',
          panel: style.getPropertyValue('--terminal-panel').trim() || '#07120f',
          grid: style.getPropertyValue('--terminal-line').trim() || '#223a34',
          matched: style.getPropertyValue('--terminal-accent').trim() || '#24c7ad',
          exit: style.getPropertyValue('--terminal-danger').trim() || '#ff7184',
        }};
      }}

      function buildChart() {{
        if (!window.LightweightCharts) return false;
        canvas.innerHTML = '';
        const colors = palette();
        chart = LightweightCharts.createChart(canvas, {{
          autoSize: true,
          layout: {{
            background: {{ type: 'solid', color: colors.panel }},
            textColor: colors.muted,
            fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif',
            fontSize: 13,
            panes: {{
              separatorColor: colors.grid,
              separatorHoverColor: colors.matched,
              enableResize: true,
            }},
          }},
          grid: {{
            vertLines: {{ color: colors.grid }},
            horzLines: {{ color: colors.grid }},
          }},
          rightPriceScale: {{
            borderColor: colors.grid,
            scaleMargins: {{ top: .1, bottom: .1 }},
          }},
          timeScale: {{
            borderColor: colors.grid,
            timeVisible: true,
            secondsVisible: hours <= 1,
            rightOffset: 5,
            barSpacing: hours <= 1 ? 7 : 4,
            minBarSpacing: .5,
          }},
          crosshair: {{
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: {{ color: colors.muted, width: 1, style: 2, labelBackgroundColor: colors.text }},
            horzLine: {{ color: colors.muted, width: 1, style: 2, labelBackgroundColor: colors.text }},
          }},
          handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false }},
          handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
        }});
        const addLine = (color, title, width = 2) => chart.addSeries(
          LightweightCharts.LineSeries,
          {{
            color,
            title,
            lineWidth: width,
            priceFormat: {{
              type: 'custom',
              formatter: (value) => `${{Number(value).toFixed(3)}}%`,
            }},
            crosshairMarkerVisible: true,
            lastValueVisible: true,
            priceLineVisible: true,
          }},
        );
        chartSeries.entry = addLine(colors.matched, 'Open ask → bid', 3);
        chartSeries.matched = addLine('#4f8cff', '$50 VWAP', 1);
        chartSeries.exit = addLine(colors.exit, 'Out top book', 2);
        chartSeries.longFunding = addLine('#1ebf8f', 'Long funding', 2);
        chartSeries.shortFunding = addLine('#ff7a82', 'Short funding', 2);
        chartSeries.longFunding.moveToPane(1);
        chartSeries.shortFunding.moveToPane(1);
        chartSeries.matched.applyOptions({{ visible: false }});
        sizeChartPanes();
        chart.subscribeCrosshairMove(showTooltip);
        resizeObserver = new ResizeObserver(() => {{
          if (chart && canvas.clientWidth && canvas.clientHeight) {{
            chart.resize(canvas.clientWidth, canvas.clientHeight);
            sizeChartPanes();
          }}
        }});
        resizeObserver.observe(canvas);
        themeObserver = new MutationObserver(applyChartTheme);
        themeObserver.observe(document.documentElement, {{
          attributes: true,
          attributeFilter: ['data-theme'],
        }});
        return true;
      }}

      function sizeChartPanes() {{
        if (!chart || !canvas.clientHeight) return;
        const panes = chart.panes();
        const fundingHeight = Math.max(90, Math.round(canvas.clientHeight * .22));
        if (panes[0]) panes[0].setHeight(canvas.clientHeight - fundingHeight);
        if (panes[1]) panes[1].setHeight(fundingHeight);
      }}

      function applyChartTheme() {{
        if (!chart) return;
        const colors = palette();
        chart.applyOptions({{
          layout: {{
            background: {{ type: 'solid', color: colors.panel }},
            textColor: colors.muted,
          }},
          grid: {{
            vertLines: {{ color: colors.grid }},
            horzLines: {{ color: colors.grid }},
          }},
          rightPriceScale: {{ borderColor: colors.grid }},
          timeScale: {{ borderColor: colors.grid }},
        }});
        chartSeries.entry?.applyOptions({{ color: colors.matched }});
        chartSeries.exit?.applyOptions({{ color: colors.exit }});
      }}

      function seriesData(rows, key, gapSeconds) {{
        const output = [];
        let previous = null;
        rows.forEach((row) => {{
          const value = row[key];
          const time = Math.floor(row.ts / 1000);
          if (previous !== null && time - previous > gapSeconds) {{
            output.push({{ time: previous + 1 }});
          }}
          if (!Number.isFinite(value)) return;
          const last = output[output.length - 1];
          if (last?.time === time) last.value = value;
          else output.push({{ time, value }});
          previous = time;
        }});
        return output;
      }}

      function nearestRow(time) {{
        if (!latestRows.length || !Number.isFinite(Number(time))) return null;
        let low = 0;
        let high = latestRows.length - 1;
        while (low < high) {{
          const mid = Math.floor((low + high) / 2);
          if (latestRows[mid].ts / 1000 < Number(time)) low = mid + 1;
          else high = mid;
        }}
        const after = latestRows[low];
        const before = latestRows[Math.max(0, low - 1)];
        return Math.abs(after.ts / 1000 - Number(time)) <
          Math.abs(before.ts / 1000 - Number(time)) ? after : before;
      }}

      function showTooltip(param) {{
        if (!param?.time || !param.point || param.point.x < 0 || param.point.y < 0 ||
            param.point.x > canvas.clientWidth || param.point.y > canvas.clientHeight) {{
          tooltip.hidden = true;
          return;
        }}
        const row = nearestRow(param.time);
        if (!row) {{
          tooltip.hidden = true;
          return;
        }}
        tooltip.innerHTML = `
          <time>${{timeLabel(row.ts, true)}}</time>
          <span>Open ask → bid<strong>${{pct(row.entry)}}</strong></span>
          <span>$50 VWAP<strong>${{pct(row.matched)}}</strong></span>
          <span>Out top book<strong>${{pct(row.exit)}}</strong></span>
          <span>Long funding<strong>${{pct(row.longFunding)}}</strong></span>
          <span>Short funding<strong>${{pct(row.shortFunding)}}</strong></span>`;
        tooltip.hidden = false;
        const box = tooltip.getBoundingClientRect();
        const left = param.point.x + 18 + box.width > canvas.clientWidth
          ? param.point.x - box.width - 18
          : param.point.x + 18;
        tooltip.style.left = `${{Math.max(8, left)}}px`;
        tooltip.style.top = `${{Math.max(42, Math.min(
          canvas.clientHeight - box.height - 8,
          param.point.y - box.height / 2,
        ))}}px`;
      }}

      function render(payload) {{
        historyRows = payload.rows || [];
        const rows = historyRows.map((row) => ({{
          ts: (num(row.quote_ts_us) ?? Number.NaN) / 1000,
          matched: num(row.depth_weighted_spread_pct),
          entry: num(row.executable_spread_pct),
          exit: num(row.exit_spread_pct),
          longFunding: num(row.long_current_funding_pct),
          shortFunding: num(row.short_current_funding_pct),
          longInterval: num(row.long_funding_interval_hours),
          shortInterval: num(row.short_funding_interval_hours),
          longNext: num(row.long_next_funding_ts_us),
          shortNext: num(row.short_next_funding_ts_us),
        }})).filter((row) => Number.isFinite(row.ts)).sort((a, b) => a.ts - b.ts);
        latestRows = rows;
        if (count) count.textContent = `${{rows.length}} observations in this window`;
        if (!rows.length) {{
          if (!chart) canvas.innerHTML = '<div class="chart-data-empty">Collecting the first exact-route observation.</div>';
          return;
        }}
        if (!chart && !buildChart()) {{
          canvas.innerHTML = '<div class="chart-data-empty">Interactive chart engine unavailable.</div>';
          return;
        }}
        const latest = rows[rows.length - 1];
        root.querySelector('[data-latest-matched]').textContent = pct(latest.matched);
        root.querySelector('[data-latest-entry]').textContent = pct(latest.entry);
        root.querySelector('[data-latest-exit]').textContent = pct(latest.exit);
        root.querySelector('[data-latest-long-funding]').textContent = pct(latest.longFunding);
        root.querySelector('[data-latest-short-funding]').textContent = pct(latest.shortFunding);
        if (headline) headline.textContent = `Open ${{pct(latest.entry)}} · $50 VWAP ${{pct(latest.matched)}}`;
        for (const side of ['long', 'short']) {{
          const funding = latest[`${{side}}Funding`];
          const interval = latest[`${{side}}Interval`];
          const next = latest[`${{side}}Next`];
          const fundingNode = document.querySelector(`[data-live-funding="${{side}}"]`);
          const cadenceNode = document.querySelector(`[data-live-cadence="${{side}}"]`);
          const nextNode = document.querySelector(`[data-live-next="${{side}}"]`);
          if (fundingNode && Number.isFinite(funding)) fundingNode.textContent = pct(funding);
          if (cadenceNode && Number.isFinite(interval)) cadenceNode.textContent = `every ${{interval}}h`;
          if (nextNode && Number.isFinite(next) && next > 0) {{
            const nextMs = next / 1000;
            const remaining = Math.max(0, Math.round((nextMs - Date.now()) / 60000));
            nextNode.textContent = `${{new Date(nextMs).toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit',timeZone:'UTC'}})}} UTC (${{Math.floor(remaining/60)}}h ${{String(remaining%60).padStart(2,'0')}}m)`;
          }}
        }}
        const gapSeconds = Math.max(90, Number(payload.meta?.gap_threshold_seconds || 90));
        chartSeries.matched.setData(seriesData(rows, 'matched', gapSeconds));
        chartSeries.entry.setData(seriesData(rows, 'entry', gapSeconds));
        chartSeries.exit.setData(seriesData(rows, 'exit', gapSeconds));
        chartSeries.longFunding.setData(seriesData(rows, 'longFunding', gapSeconds));
        chartSeries.shortFunding.setData(seriesData(rows, 'shortFunding', gapSeconds));
        if (!chart.__fitted) {{
          chart.timeScale().fitContent();
          chart.__fitted = true;
        }} else {{
          chart.timeScale().scrollToRealTime();
        }}
        const ageSeconds = Number(payload.meta?.age_seconds);
        age.textContent = Number.isFinite(ageSeconds)
          ? `Latest sample ${{Math.round(ageSeconds)}}s ago`
          : 'Sample time unavailable';
        age.classList.toggle('stale', Number.isFinite(ageSeconds) && ageSeconds > 60);
      }}

      async function refresh() {{
        if (document.hidden || refreshing) return;
        refreshing = true;
        controller = new AbortController();
        state.textContent = 'Sampling exact public order books...';
        try {{
          const response = await fetch(`/api/history/${{encodeURIComponent(routeKey)}}?live=1&hours=${{hours}}&bucket_seconds=${{bucketSeconds}}&max_points=${{maxPoints}}`, {{
            cache: 'no-store',
            signal: controller.signal,
          }});
          if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
          const payload = await response.json();
          render(payload);
          const sample = payload.sample || {{}};
          state.textContent = sample.status === 'ok'
            ? `Live · exact books · ${{sample.duration_ms || 0}}ms`
            : sample.cached && payload.meta?.age_seconds <= 60
              ? 'Live · recent exact sample'
              : `Sampler ${{sample.status || 'unavailable'}}`;
          state.classList.toggle('stale', !['ok','idle'].includes(sample.status) && !(sample.cached && payload.meta?.age_seconds <= 60));
        }} catch (error) {{
          if (error.name !== 'AbortError') {{
            state.textContent = 'Live sample unavailable; retained history shown';
            state.classList.add('stale');
          }}
        }} finally {{
          refreshing = false;
        }}
      }}
      function mergeStreamRow(row) {{
        if (!row || !Number.isFinite(Number(row.quote_ts_us))) return;
        const ts = Number(row.quote_ts_us);
        const index = historyRows.findIndex((item) => Number(item.quote_ts_us) === ts);
        if (index >= 0) historyRows[index] = row;
        else historyRows.push(row);
        historyRows.sort((a, b) => Number(a.quote_ts_us) - Number(b.quote_ts_us));
        if (historyRows.length > maxPoints) historyRows = historyRows.slice(-maxPoints);
      }}
      function startStream() {{
        if (!window.EventSource) return false;
        stream?.close();
        stream = new EventSource(`/api/stream/${{encodeURIComponent(routeKey)}}?hours=${{hours}}`);
        stream.addEventListener('quote', (event) => {{
          try {{
            const payload = JSON.parse(event.data);
            mergeStreamRow(payload.row);
            render({{
              rows: historyRows,
              meta: payload.meta || {{}},
              sample: payload.sample || {{}},
            }});
            const sample = payload.sample || {{}};
            state.textContent = sample.status === 'ok'
              ? `Streaming · exact books · ${{sample.duration_ms || 0}}ms`
              : sample.cached && payload.meta?.age_seconds <= 60
                ? 'Streaming · recent exact sample'
                : `Stream sampler ${{sample.status || 'unavailable'}}`;
            state.classList.toggle(
              'stale',
              !['ok','idle'].includes(sample.status) &&
                !(sample.cached && payload.meta?.age_seconds <= 60),
            );
          }} catch (_error) {{
            state.textContent = 'Stream data unavailable; reconnecting';
            state.classList.add('stale');
          }}
        }});
        stream.onerror = () => {{
          state.textContent = 'Reconnecting live stream';
          state.classList.add('stale');
        }};
        return true;
      }}
      root.querySelectorAll('[data-series-toggle]').forEach((button) => {{
        button.addEventListener('click', () => {{
          const key = button.dataset.seriesToggle;
          button.classList.toggle('active');
          chartSeries[key]?.applyOptions({{ visible: button.classList.contains('active') }});
        }});
      }});
      render(JSON.parse(document.getElementById('live-chart-initial').textContent || '{{}}'));
      refresh();
      if (!startStream()) timer = window.setInterval(refresh, 5000);
      document.addEventListener('visibilitychange', () => {{
        if (!document.hidden) {{
          refresh();
          startStream();
        }} else {{
          stream?.close();
        }}
      }});
      window.addEventListener('pagehide', () => {{
        window.clearInterval(timer);
        stream?.close();
        controller?.abort();
        resizeObserver?.disconnect();
        themeObserver?.disconnect();
        chart?.remove();
      }}, {{once: true}});
    }})();
    </script>
    """


def render_funding_history_chart(
    long_leg: dict[str, Any],
    short_leg: dict[str, Any],
) -> str:
    series = [
        (
            str(long_leg.get("venue") or "Long"),
            "entry",
            _chart_points(long_leg.get("funding_history") or [], "timestamp_ms", "rate_pct", 1),
        ),
        (
            str(short_leg.get("venue") or "Short"),
            "exit",
            _chart_points(short_leg.get("funding_history") or [], "timestamp_ms", "rate_pct", 1),
        ),
    ]
    return render_dual_chart_svg(series, "Funding event history", compact=True)


def _chart_points(
    rows: list[dict[str, Any]],
    timestamp_key: str,
    value_key: str,
    timestamp_divisor: int,
) -> list[tuple[float, float]]:
    points = []
    for row in rows:
        timestamp = _float_or_none(row.get(timestamp_key))
        value = _float_or_none(row.get(value_key))
        if timestamp is not None and value is not None:
            points.append((timestamp / timestamp_divisor, value))
    return points


def render_dual_chart_svg(
    series: list[tuple[str, str, list[tuple[float, float]]]],
    label: str,
    *,
    compact: bool = False,
    independent_lanes: bool = False,
) -> str:
    available = [(name, class_name, points) for name, class_name, points in series if points]
    captured = max((len(points) for _, _, points in available), default=0)
    if not available or captured < 2:
        return (
            '<div class="chart-data-empty">'
            f"Collecting history: {captured} of 2 observations captured."
            "</div>"
        )
    all_points = [point for _, _, points in available for point in points]
    min_ts = min(point[0] for point in all_points)
    max_ts = max(point[0] for point in all_points)
    if max_ts == min_ts:
        max_ts += 1
    width = 900.0
    height = 170.0 if compact else 330.0
    left, right, top, bottom = 54.0, 18.0, 18.0, 28.0
    usable_w = width - left - right
    usable_h = height - top - bottom
    grid = []
    lines = []
    legend = []
    lane_ranges: list[tuple[float, float, float, float]] = []
    if independent_lanes:
        lane_height = usable_h / len(available)
        for lane_index, (_, _, points) in enumerate(available):
            values = [point[1] for point in points]
            low, high = min(values), max(values)
            if high == low:
                padding = max(abs(high) * 0.002, 0.001)
                low -= padding
                high += padding
            lane_top = top + lane_index * lane_height + 7
            lane_bottom = top + (lane_index + 1) * lane_height - 7
            lane_ranges.append((low, high, lane_top, lane_bottom))
            for grid_index in range(3):
                y = lane_top + (lane_bottom - lane_top) * grid_index / 2
                value = high - (high - low) * grid_index / 2
                grid.append(
                    f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}"></line>'
                    f'<text x="{left - 7}" y="{y + 4:.1f}">{value:.3f}%</text>'
                )
    else:
        values = [point[1] for point in all_points]
        low, high = min(values), max(values)
        if high == low:
            high += 1
            low -= 1
        lane_ranges = [(low, high, top, top + usable_h) for _ in available]
        for index in range(5):
            y = top + (usable_h * index / 4)
            value = high - ((high - low) * index / 4)
            grid.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}"></line>'
                f'<text x="{left - 7}" y="{y + 4:.1f}">{value:.3f}%</text>'
            )
    for series_index, (name, class_name, points) in enumerate(available):
        low, high, lane_top, lane_bottom = lane_ranges[series_index]
        coordinates = []
        for timestamp, value in points:
            x = left + ((timestamp - min_ts) / (max_ts - min_ts)) * usable_w
            y = lane_top + ((high - value) / (high - low)) * (lane_bottom - lane_top)
            coordinates.append(f"{x:.1f},{y:.1f}")
        lines.append(
            f'<polyline class="{h(class_name)}" points="{" ".join(coordinates)}"></polyline>'
        )
        legend.append(f'<span class="{h(class_name)}">{h(name)} <strong>{fmt_signed_pct(points[-1][1], digits=3)}</strong></span>')
    return f"""
    <div class="dual-chart-wrap">
      <div class="dual-chart-legend">{''.join(legend)}</div>
      <svg class="dual-chart {'compact' if compact else ''}" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{h(label)}">
        <g class="dual-chart-grid">{''.join(grid)}</g>
        <g class="dual-chart-lines">{''.join(lines)}</g>
      </svg>
    </div>
    """


def render_chart_kind_tabs(query: dict[str, list[str]], selected_kind: str) -> str:
    tabs = ['<a class="tab-button%s" href="/charts">All</a>' % ("" if selected_kind else " active")]
    for item in board.ROUTE_KINDS:
        href = "/charts?" + urlencode(_query_with(query, kind=item.kind))
        active = " active" if selected_kind == item.kind else ""
        tabs.append(f'<a class="tab-button{active}" href="{h(href)}">{h(route_tab_label(item))}</a>')
    return "".join(tabs)


def render_board_empty_state(
    selected_kind: str,
    snapshot_data: dict[str, Any],
    health: dict[str, Any],
    *,
    mobile: bool = False,
) -> str:
    tab = source_tab_for_kind(health, selected_kind)
    status = str((tab or {}).get("status") or "empty")
    label = str((tab or {}).get("label") or board.kind_label(selected_kind) or "Board")
    detail = str((tab or {}).get("detail") or "No rows matched the current filters.")
    classes = f"board-empty route-empty {h(status)}"
    if mobile:
        classes += " mobile-empty"
    if status == "unavailable":
        title = f"{label} source unavailable"
        description = (
            f"{detail}. No product lock is applied here; SpreadBoard just has no captured local source rows "
            "for this route family right now."
        )
    elif status == "stale" and not snapshot_data.get("rows"):
        title = f"{label} has only stale rows"
        description = "Fresh filtering is hiding older rows. Use Show stale rows when you want historical/research context."
    elif selected_kind:
        title = f"No fresh {label} rows"
        description = "This filter has no fresh local board rows. Check source health or widen the route family."
    else:
        title = "No fresh rows match this view"
        description = "The current filters returned no fresh local board rows."
    kind_href = f"/arbitrage?kind={h(selected_kind or 'FUTURES')}"
    metrics = [
        ("Status", label_text(status)),
        ("Fresh", str((tab or {}).get("fresh_row_count") if tab else snapshot_data.get("fresh_count") or 0)),
        ("Total", str((tab or {}).get("row_count") if tab else len(snapshot_data.get("rows") or []))),
        ("Newest", fmt_age((tab or {}).get("newest_age_min") if tab else snapshot_data.get("age_min"))),
    ]
    return f"""
    <article class="{classes}">
      <div class="route-empty-head">
        <div>
          <span>Source status</span>
          <strong>{h(title)}</strong>
          <p>{h(description)}</p>
        </div>
        <b>{label_text(status)}</b>
      </div>
      <div class="route-empty-metrics">
        {''.join(f'<span>{h(name)}<strong>{h(value)}</strong></span>' for name, value in metrics)}
      </div>
      <div class="route-empty-actions">
        <a href="/">Live assets</a>
        <a href="/intel">Intel</a>
        <a href="/playbook">Playbook</a>
        <a href="{kind_href}">Refresh tab</a>
      </div>
    </article>
    """


def source_tab_for_kind(health: dict[str, Any], selected_kind: str) -> dict[str, Any] | None:
    kind = str(selected_kind or "").upper()
    if not kind:
        return None
    for tab in health.get("tabs") or []:
        if str(tab.get("kind") or "").upper() == kind:
            return tab
    return None


def render_chart_summary(rows: list[dict[str, Any]], history_map: dict[str, list[dict[str, Any]]]) -> str:
    if not rows:
        return '<article class="chart-summary-card"><span>Routes</span><strong>0</strong><em>No fresh rows</em></article>'
    biggest = max(rows, key=lambda item: abs(_float_or_none(item.get("executable_spread_pct") or item.get("displayed_open_spread_pct") or item.get("spread_pct")) or 0.0))
    funding = max(rows, key=lambda item: abs(_float_or_none(item.get("funding_apr_pct")) or 0.0))
    moved = max(rows, key=lambda item: abs(history_delta(history_map.get(str(item.get("route_key") or ""), []), "executable_spread_pct") or 0.0))
    sample_count = sum(len(points) for points in history_map.values())
    cards = [
        ("Live routes", str(len(rows)), "current API view"),
        ("History points", str(sample_count), "retained for 30 days"),
        ("Largest spread", f"{h(biggest.get('token') or biggest.get('symbol'))} {fmt_pct(biggest.get('executable_spread_pct') or biggest.get('displayed_open_spread_pct') or biggest.get('spread_pct'))}", h(route_kind_display(biggest.get("route_kind") or biggest.get("kind")))),
        ("Largest move", f"{h(moved.get('token') or moved.get('symbol'))} {fmt_signed_pct(history_delta(history_map.get(str(moved.get('route_key') or ''), []), 'executable_spread_pct'))}", "captured history"),
        ("Funding APR", f"{h(funding.get('token') or funding.get('symbol'))} {fmt_signed_pct(funding.get('funding_apr_pct'), digits=0)}", "paired annualized"),
    ]
    return "".join(
        f'<article class="chart-summary-card"><span>{h(title)}</span><strong>{value}</strong><em>{note}</em></article>'
        for title, value, note in cards
    )


def render_chart_route_card(row: dict[str, Any], history: list[dict[str, Any]]) -> str:
    open_spread = (
        row.get("executable_spread_pct")
        if row.get("executable_spread_pct") is not None
        else row.get("displayed_open_spread_pct")
        if row.get("displayed_open_spread_pct") is not None
        else row.get("spread_pct")
    )
    delta = history_delta(history, "executable_spread_pct")
    sample_count = len(history)
    external = (
        f'<a class="mini-action" href="{h(row.get("chart_url"))}" rel="noreferrer">Source chart</a>'
        if row.get("chart_url")
        else ""
    )
    return f"""
    <article class="chart-route-card">
      <div class="chart-card-head">
        <a href="/markets#token-{h(row.get('token') or row.get('symbol'))}"><strong>{h(row.get('token') or row.get('symbol'))}</strong><span>{h(route_kind_display(row.get('route_kind') or row.get('kind')))}</span></a>
        <b class="{spread_class(open_spread)}">{fmt_pct(open_spread)}</b>
      </div>
      <p>{h(row.get('long_venue'))} {h(leg_market_label(row.get('long_venue'), row.get('long_market_type')))} → {h(row.get('short_venue'))} {h(leg_market_label(row.get('short_venue'), row.get('short_market_type')))}</p>
      {render_sparkline(history, 'executable_spread_pct', label='executable spread')}
      <div class="chart-card-metrics">
        <span>Move<strong>{fmt_signed_pct(delta)}</strong></span>
        <span>F APR<strong>{fmt_signed_pct(row.get('funding_apr_pct'), digits=0)}</strong></span>
        <span>Depth<strong>{fmt_money(row.get('depth_usd'))}</strong></span>
        <span>Samples<strong>{h(sample_count)}</strong></span>
      </div>
      <div class="chart-card-actions">
        <a class="mini-action primary-link" href="/markets#token-{h(row.get('token') or row.get('symbol'))}">All routes</a>
        {render_alert_draft_button(row, alert_type='token_spread', compact=True)}
        {external}
      </div>
    </article>
    """


def market_history_by_route(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        route_key = str(row.get("route_key") or "")
        if route_key:
            output.setdefault(route_key, []).append(row)
    return output


def history_by_route(rows: list[board.BoardRow]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        output.setdefault(row.route_key, []).append(_decorate_history_row(row))
    return output


def history_values(history: list[dict[str, Any]], key: str) -> list[float]:
    values = []
    for item in history:
        number = _float_or_none(item.get(key))
        if number is not None:
            values.append(number)
    return values


def history_delta(history: list[dict[str, Any]], key: str) -> float | None:
    values = history_values(history, key)
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def history_range(history: list[dict[str, Any]], key: str) -> float | None:
    values = history_values(history, key)
    if len(values) < 2:
        return None
    return max(values) - min(values)


def pair_spread_trend(open_spread: Any, delta: Any) -> str:
    number = _float_or_none(delta)
    current = _float_or_none(open_spread)
    if number is None:
        return "not_enough_data"
    if abs(number) < 0.05:
        return "range_bound"
    if current is not None and current < 0:
        return "widening" if number < 0 else "converging"
    return "widening" if number > 0 else "converging"


def render_sparkline(
    history: list[dict[str, Any]],
    key: str,
    *,
    label: str,
    large: bool = False,
) -> str:
    values = history_values(history, key)
    if len(values) < 2:
        return '<div class="spark-empty">Not enough local history yet</div>'
    low = min(values)
    high = max(values)
    if high == low:
        high += 1.0
        low -= 1.0
    width = 240.0
    height = 88.0 if large else 64.0
    pad = 8.0
    usable_h = height - pad * 2
    points = []
    for index, value in enumerate(values):
        x = 0 if len(values) == 1 else (index / (len(values) - 1)) * width
        y = pad + ((high - value) / (high - low)) * usable_h
        points.append(f"{x:.1f},{y:.1f}")
    baseline = ""
    if low <= 0 <= high:
        y_zero = pad + ((high - 0) / (high - low)) * usable_h
        baseline = f'<line class="spark-zero" x1="0" y1="{y_zero:.1f}" x2="{width:.1f}" y2="{y_zero:.1f}"></line>'
    last = values[-1]
    direction = "positive" if last >= 0 else "negative"
    size = " large" if large else ""
    return (
        f'<svg class="sparkline {direction}{size}" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{h(label)}">'
        f"<title>{h(label)}: {fmt_signed_pct(last)}</title>"
        f"{baseline}<polyline points=\"{' '.join(points)}\"></polyline>"
        f'<circle cx="{points[-1].split(",")[0]}" cy="{points[-1].split(",")[1]}" r="3.5"></circle>'
        "</svg>"
    )


def render_token_page(symbol: str, board_path: Path) -> str:
    data = api_token(symbol, board_path, include_live=False)
    market_data = api_market_spreads(
        board_path,
        {"q": [symbol], "limit": ["50"], "sort": ["edge"], "direction": ["desc"]},
    )
    groups = [
        group
        for group in market_data.get("groups") or []
        if str(group.get("token") or "").upper() == symbol.upper()
    ]
    group = groups[0] if groups else {}
    token_name = group.get("token_name") or "Metadata pending"
    community = data.get("community_pulse") or {}
    body = f"""
    <section class="intro compact">
      <div>
        <a class="back" href="/">Back to arbitrage</a>
        <h1>{h(symbol)} <small>{h(token_name)}</small></h1>
        <p>All current public-API routes for this asset, followed by public venue details and community context.</p>
      </div>
      <div class="intel-actions">
        <a class="secondary" href="/charts?token={h(symbol)}">Charts</a>
        <a class="secondary" href="/signals?symbol={h(symbol)}">Signals</a>
      </div>
    </section>
      <section class="token-canonical-routes">
        {render_market_token_group(group) if group else render_live_market_empty((market_data.get('source_health') or {}).get('canonical_api') or {})}
      </section>
	    {render_token_market_enrichment(symbol, data)}
      {render_token_community_pulse(community)}
	    {render_token_market_script(symbol)}
	    """
    return shell(f"{symbol} - SpreadBoard", "board", body)


def render_token_market_enrichment(symbol: str, data: dict[str, Any]) -> str:
    exchange_rows = data.get("exchange_rows") or []
    spreads = data.get("best_spreads") or []
    dex = data.get("dex")
    enrichment = data.get("public_enrichment") or {}
    is_deferred = enrichment.get("status") == "deferred"
    status_label = "Queued public scan" if is_deferred else "Ready"
    row_html = (
        '<tr class="market-loading-row"><td colspan="7" class="empty">Public markets load when this panel is visible. Local Telegram and board context are already available above.</td></tr>'
        if is_deferred
        else ''.join(render_exchange_row(row) for row in exchange_rows)
        or '<tr><td colspan="7" class="empty">No public markets found or public APIs did not answer.</td></tr>'
    )
    spreads_html = (
        '<div class="enrichment-state"><strong>Queued last-price spread scan</strong><span>It starts when this section is visible or when you press Load now.</span></div>'
        if is_deferred
        else render_spread_list(spreads)
    )
    dex_html = (
        '<div class="enrichment-state"><strong>Queued DEX fallback</strong><span>DexScreener is used only as a labeled discovery fallback.</span></div>'
        if is_deferred
        else render_dex_line(dex) + render_hint(data.get("convergence_hint"))
    )
    return f"""
    <section class="panel token-market-enrichment {'queued' if is_deferred else 'ready'}" id="tokenMarketEnrichment" data-token-api="/api/token/{h(symbol)}">
      <div class="panel-head">
        <div>
          <h2>Exchange Prices</h2>
          <p>Funding means a periodic payment on perpetual futures. Positive funding here means shorts get paid.</p>
        </div>
        <div class="token-market-actions">
          <span class="status-pill {'stale' if is_deferred else 'fresh'}" id="tokenMarketStatus">{h(status_label)}</span>
          <button class="mini-action secondary" id="tokenMarketLoad" type="button">Load now</button>
        </div>
      </div>
      <div class="enrichment-note">
        <span>Local-first</span>
        <strong>Community Pulse and board routes render before public exchange APIs answer.</strong>
      </div>
      <div class="table-wrap token-exchange-wrap">
        <table class="token-exchange-table">
          <thead>
            <tr>
              <th>Exchange</th>
              <th>Perp price</th>
              <th>Spot price</th>
              <th>Funding</th>
              <th>24h volume</th>
              <th>Deposit</th>
              <th>Withdraw</th>
            </tr>
          </thead>
          <tbody id="tokenExchangeRows">{row_html}</tbody>
        </table>
      </div>
    </section>
    <section class="two-col token-market-grid">
      <div class="panel inset">
        <h2>Live Best Spreads</h2>
        <p class="small">These use last prices only. They do not prove that the order book is deep enough to trade.</p>
        <div id="tokenSpreadsBody">{spreads_html}</div>
      </div>
      <div class="panel inset">
        <h2>DEX Fallback</h2>
        <div id="tokenDexBody">{dex_html}</div>
      </div>
    </section>
    """


def render_token_market_script(symbol: str) -> str:
    return f"""
    <script>
    (() => {{
      const symbol = {json.dumps(symbol)};
      const root = document.getElementById("tokenMarketEnrichment");
      if (!root) return;
      const status = document.getElementById("tokenMarketStatus");
      const exchangeBody = document.getElementById("tokenExchangeRows");
      const spreadsBody = document.getElementById("tokenSpreadsBody");
      const dexBody = document.getElementById("tokenDexBody");
      const loadButton = document.getElementById("tokenMarketLoad");
      const apiUrl = root.dataset.tokenApi || `/api/token/${{encodeURIComponent(symbol)}}`;
      let started = false;

      function escapeHtml(value) {{
        return String(value ?? "").replace(/[&<>"']/g, (char) => ({{
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        }}[char]));
      }}

      function labelText(value) {{
        return String(value || "?").replace(/[_-]+/g, " ").replace(/\\b\\w/g, (char) => char.toUpperCase());
      }}

      function numberValue(value) {{
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
      }}

      function formatPrice(value) {{
        const number = numberValue(value);
        if (number === null) return "?";
        if (number >= 1) return `$${{number.toLocaleString(undefined, {{minimumFractionDigits: 4, maximumFractionDigits: 4}})}}`;
        return `$${{number.toFixed(8).replace(/0+$/, "").replace(/\\.$/, "")}}`;
      }}

      function formatPct(value, digits = 1) {{
        const number = numberValue(value);
        return number === null ? "?" : `${{number.toFixed(digits)}}%`;
      }}

      function formatSignedPct(value, digits = 1) {{
        const number = numberValue(value);
        return number === null ? "?" : `${{number >= 0 ? "+" : ""}}${{number.toFixed(digits)}}%`;
      }}

      function formatMoney(value) {{
        const number = numberValue(value);
        return number === null ? "?" : `$${{number.toLocaleString(undefined, {{maximumFractionDigits: 0}})}}`;
      }}

      function markStatus(value) {{
        if (value === true) return "open";
        if (value === false) return "closed";
        return "?";
      }}

      function setStatus(text, cls) {{
        if (!status) return;
        status.textContent = text;
        status.className = `status-pill ${{cls || ""}}`.trim();
      }}

      function renderExchangeRows(rows) {{
        if (!exchangeBody) return;
        if (!Array.isArray(rows) || !rows.length) {{
          exchangeBody.innerHTML = `<tr><td colspan="7" class="empty">No public markets found or public APIs did not answer.</td></tr>`;
          return;
        }}
        exchangeBody.innerHTML = rows.map((row) => `
          <tr class="token-exchange-row">
            <td data-label="Exchange"><strong>${{escapeHtml(row.venue)}}</strong></td>
            <td data-label="Perp price">${{escapeHtml(formatPrice(row.perp_price))}}</td>
            <td data-label="Spot price">${{escapeHtml(formatPrice(row.spot_price))}}</td>
            <td data-label="Funding">${{escapeHtml(formatSignedPct(row.funding_rate_pct, 4))}}</td>
            <td data-label="24h volume">${{escapeHtml(formatMoney(row.volume_usd))}}</td>
            <td data-label="Deposit"><span class="status">${{escapeHtml(markStatus(row.deposit))}}</span></td>
            <td data-label="Withdraw"><span class="status">${{escapeHtml(markStatus(row.withdraw))}}</span></td>
          </tr>
        `).join("");
      }}

      function renderSpreads(spreads) {{
        if (!spreadsBody) return;
        if (!Array.isArray(spreads) || !spreads.length) {{
          spreadsBody.innerHTML = `<p class="muted">No cross-market last-price gap above 0.3% was found.</p>`;
          return;
        }}
        spreadsBody.innerHTML = `<ol class="spread-list">${{spreads.map((spread) => {{
          const transfer = spread.transfer_note ? ` <span class="muted">${{escapeHtml(spread.transfer_note)}}</span>` : "";
          const disputed = spread.price_disputed ? ` <span class="pill warn">one venue's price disagrees with the rest — verify before trading</span>` : "";
          return `<li>Buy on <strong>${{escapeHtml(spread.buy_venue)}} ${{escapeHtml(spread.buy_leg)}}</strong> at ${{escapeHtml(formatPrice(spread.buy_price))}}; sell on <strong>${{escapeHtml(spread.sell_venue)}} ${{escapeHtml(spread.sell_leg)}}</strong> at ${{escapeHtml(formatPrice(spread.sell_price))}}. <strong>${{escapeHtml(formatPct(spread.spread_pct))}}</strong>${{transfer}}${{disputed}}</li>`;
        }}).join("")}}</ol>`;
      }}

      function renderDex(dex, hint) {{
        if (!dexBody) return;
        if (!dex || !dex.price_usd) {{
          dexBody.innerHTML = `<p class="muted">No DexScreener fallback pair survived the price-sanity check.</p>${{hint ? `<div class="callout"><strong>Why hasn't this converged?</strong><br>${{escapeHtml(hint)}}</div>` : ""}}`;
          return;
        }}
        const link = dex.url ? ` <a href="${{escapeHtml(dex.url)}}" rel="noreferrer">open pair</a>` : "";
        dexBody.innerHTML = `<p><strong>${{escapeHtml(dex.chain_id)}}</strong> on ${{escapeHtml(dex.dex_id)}}: price ${{escapeHtml(formatPrice(dex.price_usd))}}, liquidity ${{escapeHtml(formatMoney(dex.liquidity_usd))}}, 24h DEX volume ${{escapeHtml(formatMoney(dex.volume_24h_usd))}}.${{link}}</p>${{hint ? `<div class="callout"><strong>Why hasn't this converged?</strong><br>${{escapeHtml(hint)}}</div>` : ""}}`;
      }}

      function renderFailure(message) {{
        setStatus("Unavailable", "stale");
        root.classList.remove("queued", "loading");
        root.classList.add("unavailable");
        if (loadButton) loadButton.disabled = false;
        if (exchangeBody) exchangeBody.innerHTML = `<tr><td colspan="7" class="empty">Public exchange enrichment unavailable: ${{escapeHtml(message || "request failed")}}</td></tr>`;
        if (spreadsBody) spreadsBody.innerHTML = `<p class="muted">No live last-price spread scan yet. Local board routes remain visible above.</p>`;
        if (dexBody) dexBody.innerHTML = `<p class="muted">DEX fallback unavailable in this enrichment pass.</p>`;
      }}

      function startFetch() {{
        if (started) return;
        started = true;
        setStatus("Loading public scan", "stale");
        root.classList.remove("queued");
        root.classList.add("loading");
        if (loadButton) loadButton.disabled = true;
        if (exchangeBody) exchangeBody.innerHTML = `<tr class="market-loading-row"><td colspan="7" class="empty">Loading public markets without blocking local Telegram and board context.</td></tr>`;
        if (spreadsBody) spreadsBody.innerHTML = `<div class="enrichment-state"><strong>Loading last-price spread scan</strong><span>Local routes and Telegram context are already visible above.</span></div>`;
        if (dexBody) dexBody.innerHTML = `<div class="enrichment-state"><strong>Loading DEX fallback</strong><span>DexScreener is used only as a labeled discovery fallback.</span></div>`;
        fetch(apiUrl, {{headers: {{"Accept": "application/json"}}}})
          .then((response) => {{
            if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
            return response.json();
          }})
          .then((data) => {{
            renderExchangeRows(data.exchange_rows || []);
            renderSpreads(data.best_spreads || []);
            renderDex(data.dex, data.convergence_hint);
            root.classList.remove("queued", "loading");
            root.classList.add("ready");
            setStatus("Ready", "fresh");
            if (loadButton) loadButton.remove();
          }})
          .catch((error) => renderFailure(error && error.message));
      }}

      loadButton?.addEventListener("click", startFetch);
      if ("IntersectionObserver" in window) {{
        const observer = new IntersectionObserver((entries) => {{
          if (entries.some((entry) => entry.isIntersecting)) {{
            observer.disconnect();
            startFetch();
          }}
        }}, {{rootMargin: "240px"}});
        observer.observe(root);
      }}
    }})();
    </script>
    """


def render_token_community_pulse(pulse: dict[str, Any]) -> str:
    events = pulse.get("events") or []
    blockers = pulse.get("blockers") or []
    lifecycle = pulse.get("signal_lifecycle") or {}
    return f"""
    <section class="panel token-community-pulse" aria-label="Token community pulse">
      <div class="panel-head flat">
        <div>
          <h2>Community Pulse</h2>
          <p>Filtered local Telegram context for this token, joined to board and preflight reality.</p>
        </div>
        <a class="mini-action primary-link" href="{h(pulse.get('signals_url') or '/signals')}">Signal tape</a>
      </div>
      <div class="token-pulse-grid">
        <article>
          <span>Intel verdict</span>
          <strong>{label_text(pulse.get('status') or 'no_local_signal')}</strong>
          <em>{label_text(pulse.get('next_action') or 'watch')}</em>
        </article>
        <article>
          <span>Community heat</span>
          <strong>{h(round(_float_or_none(pulse.get('score')) or 0, 1))}</strong>
          <em>{h(pulse.get('event_count') or 0)} events</em>
        </article>
        <article>
          <span>Route status</span>
          <strong>{label_text(pulse.get('route_status') or 'telegram_only')}</strong>
          <em>{label_list(blockers[:2]) or 'No blocker details'}</em>
        </article>
        <article>
          <span>Sources</span>
          <strong>{label_text(pulse.get('telegram_status') or 'missing')} / {label_text(pulse.get('board_status') or 'missing')}</strong>
          <em>Telegram {fmt_age(pulse.get('telegram_age_min'))} - board {fmt_age(pulse.get('board_age_min'))}</em>
        </article>
      </div>
      {render_signal_lifecycle(lifecycle)}
      <div class="signal-list">{''.join(render_signal_event(item) for item in events[:8]) or '<p class="empty">No recent Telegram events for this token in the selected window.</p>'}</div>
    </section>
    """


def render_learn_page() -> str:
    body = """
    <section class="intro compact">
      <div>
        <h1>Learn The Board</h1>
        <p>Plain-English notes for reading spreads without assuming trading experience.</p>
      </div>
    </section>
    <section class="learn-grid">
      <article class="panel text"><h2>Route types</h2><p>Futures-Futures compares two perp/futures markets. Spot-Futures and Futures-Spot mix a spot leg with a perp leg. Spot-Spot needs transfer rails. DEX routes need exact chain and token identity.</p></article>
      <article class="panel text"><h2>Funding</h2><p>Perpetual futures use funding payments to keep futures prices near spot. Funding can be more important than the visible open spread for a basis farm.</p></article>
      <article class="panel text"><h2>Volatility</h2><p>24h realized volatility is computed from public hourly candles when exchanges provide them. If candles are missing, the app says so instead of guessing.</p></article>
      <article class="panel text"><h2>Freshness</h2><p>Rows older than the fresh window are hidden from the main board by default. The source-health panel shows whether a tab is fresh, stale, empty, or unavailable in the local source.</p></article>
      <article class="panel text"><h2>OKX DEX</h2><p>OKX DEX is treated as the preferred DEX source. Quote enrichment is blocked unless the row has exact chain and contract identity.</p></article>
      <article class="panel text"><h2>Safety</h2><p>This app is read-only. It does not approve, swap, transfer, borrow, repay, withdraw, sign, broadcast, or place live orders.</p></article>
    </section>
    """
    return shell("Learn - SpreadBoard", "learn", body)


def render_sources_page(board_path: Path, config: dict[str, Any]) -> str:
    health = api_source_health(board_path, config)
    flags = alerts.config_flags(config)
    community = health.get("community") or {}
    canonical = health.get("canonical_api") or {}
    market = health.get("market") or {}
    reconciliation = health.get("reconciliation") or {}
    body = f"""
    <section class="sources-page" data-refresh="180">
      <div class="intel-hero compact-hero">
        <div>
          <span class="page-kicker">System</span>
          <h1>Operational diagnostics</h1>
          <p>The product uses public exchange APIs. This unlisted page exists for uptime checks, parser freshness, and background reconciliation.</p>
        </div>
        <div class="intel-actions">
          <a class="secondary" href="/api/source-health">JSON</a>
          <a class="secondary" href="/">Arbitrage</a>
        </div>
      </div>
      <section class="source-summary-grid">
        {render_source_summary_card('Market API', canonical.get('age_min'), label_text(canonical.get('status') or 'unavailable'))}
        {render_source_summary_card('Assets', None, f"{h(market.get('asset_count') or 0)} grouped tokens")}
        {render_source_summary_card('Routes', None, f"{h(market.get('route_count') or 0)} live venue pairs")}
        {render_source_summary_card('Reconciliation', reconciliation.get('website_age_min'), label_text(reconciliation.get('status') or 'unavailable'))}
        {render_source_summary_card('Telegram events', (community.get('telegram_events') or {}).get('age_min'), label_text((community.get('telegram_events') or {}).get('status') or 'missing'))}
      </section>
      <section class="sources-layout">
        <main class="sources-main">
          <section class="intel-section">
            <div class="panel-head flat"><div><h2>Canonical Market Feed</h2><p>Current background refresh state for the public-API snapshot used by Arbitrage, Funding, and Charts.</p></div></div>
            <div class="source-files-grid">
              {render_source_artifact_card('public market snapshot', {'status': canonical.get('status'), 'age_min': canonical.get('age_min'), 'path': canonical.get('path')})}
              {render_source_artifact_card('token names', {'status': 'fresh', 'age_min': None, 'path': api_spreads.token_metadata.DEFAULT_CACHE_PATH})}
              {render_source_artifact_card('transfer rails', {'status': 'fresh', 'age_min': None, 'path': api_spreads.public_rails.DEFAULT_CACHE_PATH})}
              {render_source_artifact_card('spread history', {'status': 'fresh', 'age_min': None, 'path': market_history.DEFAULT_DB_PATH})}
            </div>
          </section>
          <section class="intel-section">
            <div class="panel-head flat"><div><h2>Community Inputs</h2><p>Optional local context used by Intel, not by canonical market prices.</p></div></div>
            <div class="source-files-grid">{''.join(render_source_artifact_card(key, item) for key, item in community.items() if isinstance(item, dict))}</div>
          </section>
        </main>
        <aside class="sources-side">
          <section class="side-card">
            <div class="side-head"><h2>Runtime Mode</h2><span>read-only</span></div>
            <div class="source-config-grid">
              {render_source_mode_row('Alerts', 'preview_only_no_send')}
              {render_source_mode_row('Watcher', 'disabled')}
              {render_source_mode_row('Pushover', 'configured' if flags.get('pushover_configured') else 'not configured')}
              {render_source_mode_row('Recipients', flags.get('pushover_user_count'))}
              {render_source_mode_row('OKX DEX quotes', 'enabled' if flags.get('okx_dex_quotes_enabled') else 'disabled')}
            </div>
            <p class="plain">No orders, swaps, approvals, transfers, borrows, repayments, withdrawals, signatures, broadcasts, private balance reads, or Pushover sends are exposed here.</p>
          </section>
        </aside>
      </section>
    </section>
    """
    return shell("Sources - SpreadBoard", "sources", body)


def api_portfolio(
    user: accounts.User,
    board_path: Path,
    accounts_path: Path | str,
) -> dict[str, Any]:
    return portfolio.portfolio_snapshot(
        user,
        board_path=board_path,
        accounts_path=accounts_path,
    )


def render_login_page(query: dict[str, list[str]]) -> str:
    next_path = _query_first(query, "next") or "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in - SpreadBoard</title>
<style>
:root {{ color-scheme: dark; --bg:#07110f; --panel:#101d1a; --line:#29443d; --ink:#edf8f4; --muted:#9bb1aa; --accent:#38d4bd; --danger:#ff8695; }}
* {{ box-sizing:border-box; }} body {{ margin:0; min-height:100vh; background:var(--bg); color:var(--ink); font-family:Arial,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; display:grid; place-items:center; padding:24px; }}
.login-shell {{ width:min(420px,100%); }} .login-brand {{ display:flex; align-items:center; gap:12px; margin-bottom:28px; font-size:24px; font-weight:800; }}
.login-mark {{ width:26px; height:26px; border-radius:50%; background:var(--accent); border:3px solid #dffff8; box-shadow:12px 9px 0 -5px #7fdccf; }}
.login-panel {{ border:1px solid var(--line); background:var(--panel); padding:28px; border-radius:8px; }} h1 {{ margin:0 0 8px; font-size:28px; letter-spacing:0; }} p {{ color:var(--muted); margin:0 0 24px; line-height:1.5; }}
label {{ display:grid; gap:7px; margin:0 0 16px; color:var(--muted); font-size:12px; font-weight:800; text-transform:uppercase; }} input {{ width:100%; min-height:46px; border:1px solid var(--line); background:#081310; color:var(--ink); border-radius:5px; padding:0 13px; font:inherit; }} input:focus {{ outline:2px solid var(--accent); outline-offset:1px; }}
button {{ width:100%; min-height:46px; border:0; border-radius:5px; background:var(--accent); color:#052c26; font:inherit; font-weight:900; cursor:pointer; }} button:disabled {{ opacity:.55; cursor:wait; }}
.login-error {{ min-height:20px; margin:14px 0 0; color:var(--danger); font-size:13px; }} .login-note {{ margin-top:18px; color:var(--muted); font-size:12px; text-align:center; }} .login-note a {{ color:var(--accent); font-weight:800; }}
</style></head><body><main class="login-shell"><div class="login-brand"><span class="login-mark"></span>SpreadBoard</div>
<section class="login-panel"><h1>Welcome back</h1><p>Sign in to your private market workspace and position journal.</p>
<form id="loginForm"><label>Email<input name="email" type="email" autocomplete="username" required autofocus></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><button type="submit">Sign in</button><div class="login-error" role="alert"></div></form></section>
<div class="login-note">New here? <a href="/register">Create an account</a><br><br><a href="/pricing">See membership details</a> · secure, opaque session cookie</div></main>
<script>
document.getElementById('loginForm').addEventListener('submit', async (event) => {{
  event.preventDefault(); const form=event.currentTarget; const button=form.querySelector('button'); const error=form.querySelector('.login-error'); button.disabled=true; error.textContent='';
  try {{ const response=await fetch('/api/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(Object.fromEntries(new FormData(form)))}}); const data=await response.json(); if(!response.ok) throw new Error(data.error==='too_many_login_attempts'?'Too many attempts. Try again later.':'Email or password is incorrect.'); window.location.assign({json.dumps(next_path)}); }}
  catch(exc) {{ error.textContent=exc.message || 'Sign in failed.'; }} finally {{ button.disabled=false; }}
}});
</script></body></html>"""


def render_register_page() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Create account - SpreadBoard</title>
<style>
:root { color-scheme:dark;--bg:#07110f;--panel:#101d1a;--line:#29443d;--ink:#edf8f4;--muted:#9bb1aa;--accent:#38d4bd;--danger:#ff8695; }
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);font-family:Arial,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:grid;place-items:center;padding:24px}.login-shell{width:min(440px,100%)}.login-brand{display:flex;align-items:center;gap:12px;margin-bottom:28px;font-size:24px;font-weight:800}.login-mark{width:26px;height:26px;border-radius:50%;background:var(--accent);border:3px solid #dffff8;box-shadow:12px 9px 0 -5px #7fdccf}.login-panel{border:1px solid var(--line);background:var(--panel);padding:28px;border-radius:8px}h1{margin:0 0 8px;font-size:28px}p{color:var(--muted);margin:0 0 24px;line-height:1.5}label{display:grid;gap:7px;margin:0 0 16px;color:var(--muted);font-size:12px;font-weight:800;text-transform:uppercase}input{width:100%;min-height:46px;border:1px solid var(--line);background:#081310;color:var(--ink);border-radius:5px;padding:0 13px;font:inherit}input:focus{outline:2px solid var(--accent);outline-offset:1px}button{width:100%;min-height:46px;border:0;border-radius:5px;background:var(--accent);color:#052c26;font:inherit;font-weight:900;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.login-error{min-height:20px;margin:14px 0 0;color:var(--danger);font-size:13px}.login-note{margin-top:18px;color:var(--muted);font-size:12px;text-align:center}.login-note a{color:var(--accent);font-weight:800}
</style></head><body><main class="login-shell"><div class="login-brand"><span class="login-mark"></span>SpreadBoard</div><section class="login-panel"><h1>Create your account</h1><p>Set up your private workspace, then choose monthly access.</p><form id="registerForm"><label>Name<input name="display_name" maxlength="100" autocomplete="name" required autofocus></label><label>Email<input name="email" type="email" maxlength="254" autocomplete="email" required></label><label>Password<input name="password" type="password" minlength="12" autocomplete="new-password" required></label><button type="submit">Continue</button><div class="login-error" role="alert"></div></form></section><div class="login-note">Already registered? <a href="/login">Sign in</a><br><br><a href="/pricing">See membership details</a></div></main>
<script>document.getElementById('registerForm').addEventListener('submit',async(event)=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('button'),error=form.querySelector('.login-error');button.disabled=true;error.textContent='';try{const response=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});const data=await response.json();if(!response.ok)throw new Error(({email_already_registered:'An account already exists for this email.',invalid_email:'Enter a valid email address.',password_must_be_at_least_12_characters:'Use at least 12 characters.',too_many_registration_attempts:'Too many attempts. Try again later.'})[data.error]||'Could not create the account.');location.assign(data.next||'/subscription')}catch(exc){error.textContent=exc.message||'Could not create the account.';button.disabled=false}});</script></body></html>"""


#: What a membership actually includes, one line each. The pricing page had
#: fourteen paragraphs of prose and the reference product has seven ticks; a
#: member deciding whether to pay reads the ticks.
MEMBERSHIP_FEATURES = (
    "Every venue we track \u2014 22 exchanges plus OKX DEX",
    "Every lane: Futures-Futures, Futures-Spot, Spot-Spot, Spot-DEX, Futures-DEX",
    "Live prices that move on screen",
    "Spread, funding, token price and token funding alerts",
    "Convergence charts, custom pairs and saved charts",
    "Fair-price gaps: contracts trading away from their own venue's mark",
    "Free updates and priority support",
)

#: Three reasons, one line each.
MEMBERSHIP_REASONS = (
    ("\u26a1", "Live, not polled", "Prices move as the books move."),
    ("\u25f1", "Depth behind the number", "Matched-size VWAP beside top of book."),
    ("\u2713", "Identity-checked routes", "Exact chain and contract, never ticker matching."),
)


def membership_terms() -> list[dict[str, Any]]:
    """The real terms, derived from the prices that are actually charged.

    Written out by hand they drift from `crypto_billing.PERIODS` the first time
    a price changes, and a pricing page that lies is worse than a verbose one.
    """
    periods = dict(crypto_billing.PERIODS)
    if not periods:
        return []
    monthly_days = min(periods)
    base_monthly = periods[monthly_days] / (monthly_days / 30.0)
    terms = []
    for days in sorted(periods):
        months = max(1, round(days / 30.0))
        total = periods[days] / 100.0
        per_month = total / months
        saving = 0 if base_monthly <= 0 else round((1 - (per_month * 100) / base_monthly) * 100)
        terms.append({
            "days": days,
            "months": months,
            "label": "1 month" if months == 1 else f"{months} months",
            "total": total,
            "per_month": per_month,
            "saving_pct": max(0, saving),
        })
    return terms


def render_membership_ticks() -> str:
    return "".join(
        f'<li><span aria-hidden="true">\u2713</span>{h(item)}</li>'
        for item in MEMBERSHIP_FEATURES
    )


def render_membership_terms(*, selected_days: int | None = None) -> str:
    terms = membership_terms()
    if not terms:
        return ""
    best = max(term["saving_pct"] for term in terms)
    cards = []
    for term in terms:
        classes = ["term-card"]
        if term["saving_pct"] == best and best > 0:
            classes.append("best")
        if selected_days is not None and term["days"] == selected_days:
            classes.append("current")
        saving = (
            f'<em class="term-saving">-{term["saving_pct"]}%</em>'
            if term["saving_pct"]
            else ""
        )
        cards.append(
            f'<div class="{" ".join(classes)}">{saving}'
            f'<strong>{h(term["label"])}</strong>'
            f'<span class="term-rate">${term["per_month"]:,.2f}<em>/mo</em></span>'
            f'<span class="term-total">${term["total"]:,.0f} billed once</span></div>'
        )
    return f'<div class="term-grid">{"".join(cards)}</div>'


def render_membership_reasons() -> str:
    return "".join(
        f'<div class="reason"><span aria-hidden="true">{mark}</span>'
        f'<strong>{h(title)}</strong><p>{h(line)}</p></div>'
        for mark, title, line in MEMBERSHIP_REASONS
    )


MEMBERSHIP_STYLE = """
      .tick-list { list-style:none; margin:0; padding:0; display:grid; gap:9px; }
      .tick-list li { display:grid; grid-template-columns:20px 1fr; gap:9px; align-items:start; line-height:1.4; }
      .tick-list li span { color:var(--accent); font-weight:900; }
      .term-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
      .term-card { position:relative; padding:16px 14px; border:1px solid var(--terminal-line); display:grid; gap:3px; background:var(--terminal-panel); }
      .term-card.best { border-color:var(--accent); }
      .term-card.current { background:var(--terminal-row); }
      .term-card strong { font-size:14px; }
      .term-rate { font-size:22px; font-weight:900; font-variant-numeric:tabular-nums; }
      .term-rate em { font-size:12px; font-weight:700; font-style:normal; color:var(--terminal-muted); }
      .term-total { color:var(--terminal-muted); font-size:11px; }
      .term-saving { position:absolute; top:-9px; right:10px; padding:2px 7px; background:var(--accent); color:var(--accent-ink); font-size:10px; font-weight:900; font-style:normal; }
      .reason-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:18px; text-align:center; }
      .reason span { font-size:22px; }
      .reason strong { display:block; margin:6px 0 4px; font-size:15px; }
      .reason p { margin:0; color:var(--terminal-muted); font-size:13px; line-height:1.45; }
"""


def render_pricing_page() -> str:
    user = accounts.current_user()
    if user and user.subscription_active:
        primary_action = '<a class="pricing-button primary" href="/">Open market terminal</a>'
        secondary_action = '<a class="pricing-button" href="/account">Open portfolio</a>'
    elif user:
        primary_action = '<a class="pricing-button primary" href="/subscription">Continue to payment</a>'
        secondary_action = '<a class="pricing-button" href="/account">Open account</a>'
    else:
        primary_action = '<a class="pricing-button primary" href="/register">Create account</a>'
        secondary_action = '<a class="pricing-button" href="/login">Sign in</a>'
    terms = membership_terms()
    monthly = terms[0]["per_month"] if terms else 180.0
    body = f"""
    <style>
      .pricing-page {{ width:min(1000px,calc(100% - 36px)); margin:36px auto 72px; display:grid; gap:28px; }}
      .pricing-intro {{ display:grid; grid-template-columns:minmax(0,1.2fr) minmax(260px,.8fr); border:1px solid var(--terminal-line); background:var(--terminal-panel); }}
      .pricing-copy {{ padding:34px 32px; }}
      .pricing-copy h1 {{ margin:8px 0 10px; font-size:clamp(30px,4.4vw,48px); line-height:1.05; max-width:16ch; }}
      .pricing-copy p {{ margin:0; color:var(--terminal-muted); font-size:16px; line-height:1.5; max-width:44ch; }}
      .pricing-plan {{ padding:30px 28px; border-left:1px solid var(--terminal-line); display:grid; align-content:center; gap:14px; }}
      .pricing-price strong {{ font-size:44px; line-height:1; }}
      .pricing-price em {{ color:var(--terminal-muted); font-style:normal; }}
      .pricing-actions {{ display:flex; gap:8px; flex-wrap:wrap; }}
      .pricing-button {{ min-height:43px; padding:11px 16px; border:1px solid var(--terminal-line); color:var(--terminal-text); text-decoration:none; font-weight:900; display:inline-flex; align-items:center; }}
      .pricing-button.primary {{ background:var(--accent); border-color:var(--accent); color:var(--accent-ink); }}
      .pricing-block {{ padding:26px 28px; border:1px solid var(--terminal-line); background:var(--terminal-panel); }}
      .pricing-block h2 {{ margin:0 0 16px; font-size:20px; }}
      .pricing-note {{ margin:0; color:var(--terminal-muted); font-size:12px; line-height:1.5; }}
      {MEMBERSHIP_STYLE}
      @media(max-width:820px) {{ .pricing-intro {{ grid-template-columns:1fr; }} .pricing-plan {{ border-left:0; border-top:1px solid var(--terminal-line); }} }}
    </style>
    <section class="pricing-page">
      <header class="pricing-intro">
        <div class="pricing-copy">
          <span class="page-kicker">Membership</span>
          <h1>Every spread, live.</h1>
          <p>Cross-venue spreads and funding across 22 exchanges and OKX DEX, priced continuously.</p>
        </div>
        <aside class="pricing-plan">
          <div class="pricing-price"><strong>${monthly:,.0f}</strong> <em>/ month</em></div>
          <div class="pricing-actions">{primary_action}{secondary_action}</div>
          <p class="pricing-note">Cancel any time.</p>
        </aside>
      </header>

      <section class="pricing-block">
        <h2>What you get</h2>
        <ul class="tick-list">{render_membership_ticks()}</ul>
      </section>

      <section class="pricing-block">
        <h2>Terms</h2>
        {render_membership_terms()}
      </section>

      <section class="pricing-block">
        <h2>Why membership</h2>
        <div class="reason-grid">{render_membership_reasons()}</div>
      </section>

      <p class="pricing-note">Public market data, not investment advice. Every route carries execution
         risk. See the <a href="/terms">Terms</a> and <a href="/refunds">Refund Policy</a>.</p>
    </section>
    """
    return shell("Membership - SpreadBoard", "pricing", body)

def render_crypto_checkout_panel() -> str:
    """Prepaid crypto checkout: pick a period, pay the exact amount, get access."""
    state = crypto_billing.status()
    if not state.get("checkout_ready"):
        return (
            '<section class="account-empty-panel"><strong>Crypto payment</strong>'
            "<p>Crypto checkout is being configured. No payment can be taken yet.</p></section>"
        )
    periods = "".join(
        f'<button class="sheet-button crypto-period" type="button" data-crypto-period="{p["days"]}">'
        f'<span class="crypto-period-price">{h(p["label"])}</span>'
        f'<span class="crypto-period-days">{p["days"]} days</span></button>'
        for p in state.get("periods", [])
    )
    tokens = " or ".join(state.get("tokens", []))
    return f"""
    <section class="account-empty-panel crypto-checkout" data-crypto-checkout>
      <strong>Pay with crypto</strong>
      <p class="crypto-lede">Prepaid access on <b>{h(state.get('chain'))}</b> in <b>{h(tokens)}</b>.
      There is no auto-renewal &mdash; access simply lapses at the end of the period.</p>
      <div class="crypto-periods">{periods}</div>
      <div class="crypto-invoice" data-crypto-invoice hidden>
        <div class="crypto-row"><span>Send exactly</span>
          <b data-crypto-amount></b>
          <button class="sheet-button ghost" type="button" data-copy="amount">Copy</button></div>
        <div class="crypto-row"><span>To address</span>
          <code data-crypto-address></code>
          <button class="sheet-button ghost" type="button" data-copy="address">Copy</button></div>
        <div class="crypto-qr" data-crypto-qr></div>
        <p class="crypto-warn">Send only <b>{h(tokens)}</b> on <b>{h(state.get('chain'))}</b>.
        Funds sent on another chain or in another token cannot be credited.
        The amount shown is unique to your order &mdash; send it exactly.</p>
        <p class="crypto-status" data-crypto-status>Waiting for payment&hellip;</p>
        <p class="crypto-expiry">Expires in <b data-crypto-countdown>60:00</b></p>
      </div>
      <p role="alert" data-crypto-error></p>
    </section>
    """


def render_crypto_checkout_script() -> str:
    """Client logic: create an invoice, render a QR, poll until it settles.

    Emits nothing when checkout is unconfigured so no dead code is shipped.
    """
    if not crypto_billing.status().get("checkout_ready"):
        return ""
    return """
<style>
.crypto-checkout{margin-top:16px}
.crypto-lede{opacity:.85;margin:.4rem 0 .8rem}
.crypto-periods{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.8rem}
.crypto-period{display:flex;flex-direction:column;align-items:flex-start;gap:.15rem;min-width:120px}
.crypto-period-price{font-weight:700}
.crypto-period-days{font-size:.78rem;opacity:.7}
.crypto-period[aria-pressed="true"]{outline:2px solid var(--terminal-accent,#2f9e79)}
.crypto-row{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin:.35rem 0}
.crypto-row code,.crypto-row b{font-family:ui-monospace,Menlo,monospace;word-break:break-all}
.crypto-qr{margin:.6rem 0}
.crypto-qr img{image-rendering:pixelated;width:180px;height:180px;background:#fff;padding:8px;border-radius:6px}
.crypto-warn{font-size:.82rem;opacity:.85;border-left:3px solid #d08a00;padding-left:.6rem}
.crypto-status{font-weight:600}
.sheet-button.ghost{padding:.2rem .5rem;font-size:.78rem}
</style>
<script>
(function(){
  var root=document.querySelector('[data-crypto-checkout]'); if(!root) return;
  var box=root.querySelector('[data-crypto-invoice]');
  var amountEl=root.querySelector('[data-crypto-amount]');
  var addrEl=root.querySelector('[data-crypto-address]');
  var qrEl=root.querySelector('[data-crypto-qr]');
  var statusEl=root.querySelector('[data-crypto-status]');
  var errEl=root.querySelector('[data-crypto-error]');
  var cdEl=root.querySelector('[data-crypto-countdown]');
  var timer=null, poll=null, invoice=null;

  function csrf(){ try{ return JSON.parse(document.getElementById('account-session').textContent).csrf_token; }catch(e){ return null; } }
  function consent(){ var c=document.querySelector('[data-subscription-consent]'); return !!(c && c.checked); }

  function qr(text){
    // Minimal QR via a data-URI-free canvas fallback: link out if unavailable.
    qrEl.innerHTML='';
    var a=document.createElement('a');
    a.textContent='Open in wallet';
    a.className='sheet-button';
    a.href=text; qrEl.appendChild(a);
  }

  function countdown(iso){
    if(timer) clearInterval(timer);
    timer=setInterval(function(){
      var left=Math.max(0,(new Date(iso)-new Date())/1000);
      var m=Math.floor(left/60), s=Math.floor(left%60);
      cdEl.textContent=m+':'+(s<10?'0':'')+s;
      if(left<=0){ clearInterval(timer); if(poll) clearInterval(poll); statusEl.textContent='This invoice expired. Choose a period to start again.'; }
    },1000);
  }

  function watch(id){
    if(poll) clearInterval(poll);
    poll=setInterval(function(){
      fetch('/api/billing/crypto/invoice/'+id,{credentials:'same-origin'})
        .then(function(r){return r.json();})
        .then(function(d){
          if(!d || !d.invoice) return;
          if(d.invoice.status==='paid'){
            clearInterval(poll); if(timer) clearInterval(timer);
            statusEl.textContent='Payment confirmed. Activating your access\u2026';
            setTimeout(function(){ location.href='/account'; },1500);
          }
        }).catch(function(){});
    },5000);
  }

  root.querySelectorAll('[data-crypto-period]').forEach(function(btn){
    btn.addEventListener('click',function(){
      errEl.textContent='';
      if(!consent()){ errEl.textContent='Please accept the Terms and Refund Policy first.'; return; }
      root.querySelectorAll('[data-crypto-period]').forEach(function(b){ b.setAttribute('aria-pressed','false'); });
      btn.setAttribute('aria-pressed','true');
      fetch('/api/billing/crypto/invoice',{
        method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()||''},
        body:JSON.stringify({period_days:parseInt(btn.dataset.cryptoPeriod,10),terms_accepted:true,immediate_access_consent:true})
      }).then(function(r){return r.json();}).then(function(d){
        if(!d||!d.ok||!d.invoice){ errEl.textContent=(d&&d.error)||'Could not create an invoice.'; return; }
        invoice=d.invoice;
        amountEl.textContent=invoice.amount_display+' '+(invoice.tokens||[]).join(' or ');
        addrEl.textContent=invoice.receiving_address;
        qr('ethereum:'+invoice.receiving_address+'@'+invoice.chain_id);
        box.hidden=false;
        statusEl.textContent='Waiting for payment\u2026 confirmed automatically once it lands.';
        countdown(invoice.expires_at); watch(invoice.id);
      }).catch(function(){ errEl.textContent='Network error. Nothing was charged.'; });
    });
  });

  root.querySelectorAll('[data-copy]').forEach(function(b){
    b.addEventListener('click',function(){
      if(!invoice) return;
      var v=b.dataset.copy==='amount'?invoice.amount_display:invoice.receiving_address;
      navigator.clipboard&&navigator.clipboard.writeText(v);
      var t=b.textContent; b.textContent='Copied'; setTimeout(function(){b.textContent=t;},1200);
    });
  });
})();
</script>
"""


def fmt_renewal_date(value: Any) -> str:
    """The renewal date as a member reads it, from the stored ISO timestamp."""
    text = str(value or "").strip()
    if not text:
        return "\u2014"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "\u2014"
    return moment.strftime("%d %B %Y")


def render_subscription_page() -> str:
    user = accounts.current_user()
    billing_state = billing.status()
    action = (
        '<button class="sheet-button primary" type="button" data-billing-action="portal">Manage billing</button>'
        if user and user.billing_customer_id
        else f'<button class="sheet-button primary" type="button" data-billing-action="checkout">Subscribe · {h(billing_state["plan_label"])}</button>'
    ) if billing_state["checkout_ready"] else '<p class="pricing-note">Card checkout is being configured. Pay with crypto below.</p>'
    active = bool(user and user.subscription_active)
    renews = fmt_renewal_date(getattr(user, "subscription_expires_at", None)) if user else "\u2014"
    terms = membership_terms()
    monthly = terms[0]["per_month"] if terms else 180.0
    body = f"""
    <style>
      .sub-page {{ width:min(860px,calc(100% - 36px)); margin:32px auto 64px; display:grid; gap:22px; }}
      .sub-plan {{ border:1px solid var(--terminal-line); background:var(--terminal-panel); padding:26px 28px; display:grid; gap:18px; }}
      .sub-badge {{ justify-self:start; padding:3px 10px; background:var(--accent); color:var(--accent-ink); font-size:10px; font-weight:900; text-transform:uppercase; letter-spacing:.06em; }}
      .sub-facts {{ display:grid; gap:8px; }}
      .sub-facts div {{ display:flex; justify-content:space-between; gap:14px; padding-bottom:8px; border-bottom:1px solid var(--terminal-line); }}
      .sub-facts span {{ color:var(--terminal-muted); font-size:13px; }}
      .sub-facts strong {{ font-size:15px; font-variant-numeric:tabular-nums; }}
      .sub-actions {{ display:flex; gap:8px; flex-wrap:wrap; }}
      .subscription-consent {{ display:flex; gap:9px; align-items:flex-start; color:var(--terminal-muted); font-size:12px; line-height:1.45; }}
      .pricing-note {{ margin:0; color:var(--terminal-muted); font-size:12px; line-height:1.5; }}
      {MEMBERSHIP_STYLE}
    </style>
    <section class="sub-page" data-account-page>
      <header class="terminal-heading">
        <div>
          <span class="page-kicker">Membership</span>
          <h1>{'Your membership' if active else 'Activate membership'}</h1>
          <p>{'Renews automatically. Cancel any time.' if active else 'Your account is signed in; market access is not active yet.'}</p>
        </div>
      </header>

      <section class="sub-plan">
        <span class="sub-badge">{'Current plan' if active else 'Not active'}</span>
        <div class="sub-facts">
          <div><span>Monthly price</span><strong>${monthly:,.2f}</strong></div>
          <div><span>Billing cycle</span><strong>Monthly</strong></div>
          <div><span>{'Next payment' if active else 'Status'}</span><strong>{h(renews if active else (user.subscription_status if user else 'inactive'))}</strong></div>
        </div>
        <ul class="tick-list">{render_membership_ticks()}</ul>
        <label class="subscription-consent"><input type="checkbox" data-subscription-consent>
          <span>I accept the <a href="/terms" target="_blank">Terms</a> and
          <a href="/refunds" target="_blank">Refund Policy</a>, request immediate access, and
          acknowledge that the statutory cancellation right may be affected once digital access
          begins.</span></label>
        <div class="sub-actions">{action}<a class="sheet-button" href="/account">Open account</a></div>
        <p role="alert" data-billing-error></p>
      </section>

      <section class="sub-plan">
        <span class="sub-badge">Longer terms</span>
        {render_membership_terms()}
        <p class="pricing-note">Longer terms are prepaid in one payment.</p>
      </section>

      {render_crypto_checkout_panel()}
    </section>
    <script type="application/json" id="account-session">{json_script_data({'csrf_token': user.csrf_token if user else None})}</script>
    {render_billing_script()}
    {render_crypto_checkout_script()}
    """
    return shell("Membership - SpreadBoard", "profile", body)


GUIDE_LANES = [
    {
        "id": "futures-futures",
        "title": "Futures / Futures",
        "one_line": "The same coin costs different amounts on two futures exchanges. You buy the cheap one and sell the expensive one at the same time.",
        "how": [
            "Find a row on the <b>Futures-Futures</b> tab with an edge you are happy with.",
            "On the cheaper exchange, open a <b>long</b> futures position.",
            "On the more expensive exchange, open a <b>short</b> futures position of the <b>same size</b>.",
            "You now own nothing on net. If the coin doubles or halves, you neither win nor lose from that.",
            "You wait. The two prices drift back together. When the gap is near zero you close both legs and keep the difference.",
        ],
        "earn": "Two ways. The gap closing is one. <b>Funding</b> is the other: every few hours one side pays the other, and if your short is on the side that receives, you get paid just for holding.",
        "watch": [
            "<b>Both legs must be the same size.</b> If they are not, you are quietly betting on the price direction.",
            "<b>Liquidation.</b> Use low leverage. A 1x position on each side is the safe default. High leverage can liquidate one leg and leave you exposed.",
            "<b>Funding can flip</b> and start costing you instead of paying you. Check the funding column, not just the spread.",
            "<b>The gap can widen before it closes.</b> You need enough spare margin to sit through that.",
        ],
        "shot": "The Futures-Futures tab with a row expanded, showing both venues, the edge, and the funding column.",
    },
    {
        "id": "futures-spot",
        "title": "Futures / Spot",
        "one_line": "The futures price and the ordinary (spot) price of the same coin have drifted apart. You buy the cheap side and sell the expensive side.",
        "how": [
            "Find a row on the <b>Futures-Spot</b> tab.",
            "If futures are more expensive: <b>buy the coin on spot</b> and <b>short the futures</b>, same size.",
            "If spot is more expensive: the trade reverses, but that needs borrowing, so beginners should skip it.",
            "Hold. Futures and spot always converge as the contract settles or funding drags them together.",
            "Close both sides when the gap is gone.",
        ],
        "earn": "Mostly funding. When futures trade above spot, longs pay shorts, so your short leg collects a fee every few hours while you simply hold the coin. This is the calmest trade on the board and is often held for days.",
        "watch": [
            "<b>You actually own the coin</b> on the spot side. It must sit on that exchange.",
            "<b>Same size on both sides</b>, or you are exposed to the price.",
            "<b>Funding is not guaranteed.</b> It is reset every few hours and can turn negative.",
            "<b>Check the coin is the same coin.</b> Some exchanges list a different token under the same ticker.",
        ],
        "shot": "The Futures-Spot tab, plus one exchange screen showing the spot buy and the futures short side by side.",
    },
    {
        "id": "spot-spot",
        "title": "Spot / Spot",
        "one_line": "The same coin is simply cheaper on one exchange than another. You buy it where it is cheap, move it, and sell it where it is expensive.",
        "how": [
            "Find a row on the <b>Spot-Spot</b> tab.",
            "<b>Check the deposit / withdrawal column first.</b> Both must be open. This is the step beginners skip and it is the one that loses money.",
            "Buy the coin on the cheap exchange.",
            "Withdraw it to the expensive exchange. Pick the same network on both ends.",
            "When it arrives, sell it. The difference is yours.",
        ],
        "earn": "Only the price gap. There is no funding here.",
        "watch": [
            "<b>This is the only trade where you are exposed while it happens.</b> The coin is in transit and the price can move against you.",
            "<b>If withdrawals are shut, you cannot do this trade at all</b> -- your money is stuck on the cheap exchange. Our board marks a closed rail as <b>SHUT</b>.",
            "<b>Withdrawal fees and network fees</b> come out of your profit. A 0.3% gap can easily be nothing after fees.",
            "<b>Wrong network = lost coins.</b> Always match the network on both sides.",
        ],
        "shot": "The Spot-Spot tab with the D/W status column clearly visible, plus an exchange withdrawal screen showing the network selector.",
    },
    {
        "id": "futures-dex",
        "title": "Futures / DEX",
        "one_line": "The coin trades at one price on a decentralised exchange (on-chain) and another on a normal futures exchange. You take both sides.",
        "how": [
            "Find a row on the <b>Futures-DEX</b> tab.",
            "Buy the coin on the DEX with your own wallet, or sell it there if the DEX is the expensive side.",
            "Take the opposite side as a futures position on the exchange, same size.",
            "Hold, collect funding, and close both when the gap closes.",
        ],
        "earn": "Funding is usually the main prize here, and it is often the largest on the board, because fewer people can be bothered with the on-chain leg.",
        "watch": [
            "<b>Check the contract address</b>, not the name. Anyone can create a token called anything. Our board marks routes where we have not confirmed the token identity with a <b>?</b>.",
            "<b>Gas fees</b> are paid in the chain's own coin and come out of your profit.",
            "<b>Slippage.</b> On-chain, a large order moves the price against you. The quoted price is for a small size.",
            "<b>You need a wallet</b> and the coin must actually be withdrawable to it.",
        ],
        "shot": "The Futures-DEX tab, plus a wallet swap screen showing the token contract address.",
    },
]


def render_guide_page() -> str:
    """Plain-language tutorial on how each spread type is actually traded."""
    lanes = ""
    for lane in GUIDE_LANES:
        steps = "".join(f"<li>{step}</li>" for step in lane["how"])
        risks = "".join(f"<li>{item}</li>" for item in lane["watch"])
        lanes += f"""
        <article class="guide-lane" id="{lane['id']}">
          <h2>{h(lane['title'])}</h2>
          <p class="guide-lede">{lane['one_line']}</p>
          <h3>How you do it</h3>
          <ol class="guide-steps">{steps}</ol>
          <h3>Where the money comes from</h3>
          <p>{lane['earn']}</p>
          <h3>What can go wrong</h3>
          <ul class="guide-risks">{risks}</ul>
        </article>
        """
    return shell("How to trade spreads - SpreadBoard", "guide", f"""
    <section class="guide-page">
      <header class="terminal-heading">
        <div><span class="page-kicker">Tutorial</span>
        <h1>How to actually trade a spread</h1>
        <p>Written for someone who has never done this before. No jargon, and nothing assumed.</p></div>
      </header>

      <article class="guide-lane">
        <h2>The idea in one paragraph</h2>
        <p class="guide-lede">The same coin does not cost the same everywhere. SpreadBoard watches many
        exchanges at once and shows you where the prices disagree. The trade is almost always the same
        shape: <b>buy the cheap side and sell the expensive side at the same time, in the same size</b>.
        Because you are long and short at once, it does not matter to you whether the coin goes up or
        down. You are only betting that the two prices come back together -- which they almost always do.</p>
        <p>That "same size, both directions" idea is called being <b>delta neutral</b>. It is the whole
        game. If you remember one thing, remember that.</p>
      </article>

      <article class="guide-lane">
        <h2>How to read a row on the board</h2>
        <ul class="guide-risks">
          <li><b>Edge %</b> -- how far apart the two prices are right now. Bigger is better, but see the warnings below.</li>
          <li><b>Funding</b> -- a fee paid every few hours between longs and shorts. A positive number on your route means you get paid while you wait. This is often worth more than the gap itself.</li>
          <li><b>APR</b> -- what that funding works out to per year if it stayed the same. It will not stay the same, so treat it as a hint, not a promise.</li>
          <li><b>Depth</b> -- roughly how much you can trade before you move the price. A big edge with tiny depth is not a real opportunity.</li>
          <li><b>Age</b> -- how old the quote is. Older quotes are less reliable.</li>
          <li><b>D / W</b> -- whether deposits and withdrawals are open. <b>SHUT</b> means you cannot move the coin, which kills any trade that needs a transfer.</li>
          <li><b>?</b> -- we have not confirmed that both venues list the same underlying token. Check the contract yourself before trusting the number.</li>
        </ul>
      </article>

      <article class="guide-lane">
        <h2>Before your first trade</h2>
        <ol class="guide-steps">
          <li><b>Start small.</b> Do the whole thing once with an amount you would not mind losing entirely. The goal of trade one is to learn the mechanics, not to make money.</li>
          <li><b>Use 1x leverage.</b> No borrowing. It removes liquidation risk almost entirely.</li>
          <li><b>Open both legs quickly.</b> The time between opening one and the other is the only moment you are truly exposed.</li>
          <li><b>Write down your entry.</b> You need to know what the gap was when you entered to know when to exit.</li>
          <li><b>Have a plan for the gap widening.</b> It often gets worse before it gets better. Decide in advance how much you can sit through.</li>
        </ol>
      </article>

      {lanes}

      <article class="guide-lane">
        <h2>Very large spreads</h2>
        <p class="guide-lede">You will sometimes see edges of 20%, 50%, even over 100%. These are real and
        they are shown deliberately -- some of the best opportunities on the board look like this, and they
        can last only a minute or two.</p>
        <p>But a very large gap is also the shape a mistake makes. Before trading one, check three things:
        that <b>both venues list the same token</b> (watch for the <b>?</b> marker), that there is
        <b>real depth</b> behind the quote, and that you can actually <b>get in and out</b> -- deposits and
        withdrawals open, and a way to close both legs. If any of those fails, the number is not money.</p>
      </article>

      <article class="guide-lane">
        <h2>The honest warnings</h2>
        <ul class="guide-risks">
          <li>SpreadBoard is a <b>research tool</b>. It does not place trades, hold your money, or tell you what to buy.</li>
          <li>A displayed spread can disappear before you finish opening both legs.</li>
          <li>Fees, funding, slippage and withdrawal costs all come out of your profit. Work them out before you enter, not after.</li>
          <li>Nothing here is financial advice. If you are unsure, trade smaller than you think you should.</li>
        </ul>
      </article>

      <p class="pricing-disclaimer">Questions? Ask in the subscriber group.
      <a href="/markets">Open the board</a> &middot; <a href="/pricing">Membership</a> &middot; <a href="/terms">Terms</a></p>
    </section>
    <style>
    .guide-page{{max-width:820px;margin:0 auto;padding-bottom:48px}}
    .guide-lane{{margin:26px 0;padding:18px 20px;border:1px solid rgba(128,128,128,.25);border-radius:10px}}
    .guide-lane h2{{margin:0 0 .4rem}}
    .guide-lane h3{{margin:1.1rem 0 .35rem;font-size:.95rem;opacity:.8;text-transform:uppercase;letter-spacing:.04em}}
    .guide-lede{{font-size:1.02rem;line-height:1.6}}
    .guide-steps li,.guide-risks li{{margin:.4rem 0;line-height:1.55}}
    .guide-steps{{padding-left:1.2rem}}
    .guide-risks{{padding-left:1.1rem}}
    </style>
    """)


def render_legal_page(page: str) -> str:
    support = os.environ.get("SPREADBOARD_SUPPORT_EMAIL", "support@spreadarbitrage.ink")
    support_url = os.environ.get(
        "SPREADBOARD_SUPPORT_URL",
        "https://t.me/spreadarbitragesubscription_bot",
    )
    pages = {
        "terms": (
            "Terms of Service",
            "These terms govern access to SpreadBoard, a read-only market-information service.",
            [
                ("Service", "SpreadBoard presents public-market data, calculated spreads, funding information, charts, alerts, and research tools. It does not execute trades, hold client assets, provide custody, or provide personalised investment advice."),
                ("Market risk", "Prices, liquidity, funding, transfer status, and availability can change without notice. Displayed values may be delayed, incomplete, or unavailable. You remain responsible for checking any decision directly with the relevant venue."),
                ("Membership", "Membership is billed monthly at the price shown before checkout and renews until cancelled. Access is personal and may not be resold, shared, scraped, or used to disrupt the service."),
                ("Acceptable use", "Do not attempt to bypass access controls, overload data providers, reverse engineer credentials, or use the service for unlawful activity. We may suspend access needed to protect users, providers, or the service."),
                ("Availability", "We aim to run continuously but do not guarantee uninterrupted access or that every venue, token, route, chart, or alert will always be available."),
                ("Liability", "Nothing excludes liability that cannot lawfully be excluded. To the extent permitted by law, SpreadBoard is not liable for trading losses, missed opportunities, exchange failures, or decisions based on market information."),
                ("Contact", f"Questions can be sent to {support} or through {support_url}. Version {TERMS_VERSION}."),
            ],
        ),
        "privacy": (
            "Privacy Notice",
            "What SpreadBoard stores and why.",
            [
                ("Account data", "We store your name, email address, password hash, subscription state, linked Telegram identifier, settings, alerts, and journal entries to operate your account."),
                ("Payments", "Stripe processes payment-card details. SpreadBoard stores provider customer, subscription, and event identifiers, but not full card numbers."),
                ("Notifications", "Pushover user keys are encrypted at rest. Telegram and Pushover identifiers are used only to deliver the features you enable."),
                ("Technical data", "We may retain security and operational records such as session identifiers, IP address, browser information, consent records, and service logs."),
                ("Sharing and retention", "Data is shared only with providers needed to run the service, such as Stripe, Telegram, Pushover, hosting, and market-data providers. We keep it only as long as needed for service, security, accounting, and legal obligations."),
                ("Your choices", f"You may request access, correction, deletion, or account closure through {support_url} or by contacting {support}. Some records may need to be retained for legal or fraud-prevention purposes."),
            ],
        ),
        "refunds": (
            "Cancellation and Refund Policy",
            "How recurring membership cancellation and service problems are handled.",
            [
                ("Cancel any time", "You can cancel recurring billing through the account billing portal. Access normally continues until the end of the paid billing period."),
                ("Immediate access", "At checkout you are asked to request immediate digital access and acknowledge that beginning supply may affect the statutory 14-day cancellation right. This does not remove rights that cannot legally be waived."),
                ("Service faults", "If paid access is materially unavailable or not supplied as described, contact us promptly. We will investigate and provide the remedy required by applicable consumer law, which may include restoration, a credit, or a refund."),
                ("Duplicate or incorrect charges", "Report a duplicate or incorrect charge with the account email and Stripe receipt identifier. Do not send card details."),
                ("How to request", f"Contact {support} or {support_url}. Include the account email, payment date, and reason. Refunds, when due, are returned through the original payment method."),
            ],
        ),
    }
    title, intro, sections = pages.get(page, pages["terms"])
    body = f"""
    <section class="legal-page">
      <header><span class="page-kicker">SpreadBoard</span><h1>{h(title)}</h1><p>{h(intro)}</p></header>
      <main>{''.join(f'<section><h2>{h(heading)}</h2><p>{h(copy)}</p></section>' for heading, copy in sections)}</main>
      <nav><a href="/pricing">Membership</a><a href="/terms">Terms</a><a href="/privacy">Privacy</a><a href="/refunds">Refunds</a></nav>
    </section>
    """
    return shell(f"{title} - SpreadBoard", "pricing", body)


def render_account_page(
    board_path: Path,
    accounts_path: Path | str = accounts.DEFAULT_DB_PATH,
) -> str:
    user = accounts.current_user()
    if user is None:
        return render_login_page({})
    data = api_portfolio(user, board_path, accounts_path)
    summary = data.get("summary") or {}
    positions = data.get("positions") or []
    notifications = data.get("notifications") or []
    body = f"""
    <section class="account-page" data-account-page>
      <header class="terminal-heading account-heading">
        <div><span class="page-kicker">Private workspace</span><h1>{h(user.display_name)}</h1><p>Track multi-exchange positions, price PnL, funding income, returns, and exit conditions in one place.</p></div>
        <div class="account-membership"><span>Membership</span><strong>{h(user.subscription_status)}</strong><em>{h(user.subscription_expires_at or 'No expiry')}</em></div>
      </header>
      <section class="account-kpis">
        {render_account_kpi('Open positions', summary.get('open_positions'), 'actively marked')}
        {render_account_kpi('Portfolio PnL', fmt_signed_money(summary.get('price_and_funding_pnl_usd')), 'price + funding - fees')}
        {render_account_kpi('Funding income', fmt_signed_money(summary.get('funding_income_usd')), 'recorded cashflows')}
        {render_account_kpi('Return', fmt_signed_pct(summary.get('monthly_return_pct'), digits=2), 'on tracked capital')}
      </section>
      <nav class="account-tabs" aria-label="Account sections"><button class="active" data-account-tab="positions">Positions</button><button data-account-tab="alerts">Alerts <i>{h(len([item for item in notifications if not item.get('read_at')]))}</i></button><button data-account-tab="settings">Settings</button>{'<button data-account-tab="members">Members</button>' if user.is_admin else ''}</nav>
      <section data-account-panel="positions">
        <div class="account-panel-head"><div><h2>Position journal</h2><p>Manual records are marked with current public books whenever the exact route is available.</p></div><button class="sheet-button primary" type="button" data-position-new>Add position</button></div>
        <div class="position-list">{''.join(render_position_card(item) for item in positions) or '<div class="account-empty-panel"><strong>No positions yet</strong><p>Add the first spread or funding farm to start tracking it.</p></div>'}</div>
      </section>
      <section data-account-panel="alerts" hidden><div class="account-panel-head"><div><h2>Notifications</h2><p>Exit-spread, PnL, and funding rules are evaluated continuously, even while you are signed out.</p></div><button class="sheet-button" type="button" data-notifications-read>Mark all read</button></div><div class="notification-list">{''.join(render_account_notification(item) for item in notifications) or '<div class="account-empty-panel"><strong>No notifications</strong><p>Position alerts will appear here when a rule crosses its threshold.</p></div>'}</div></section>
      <section data-account-panel="settings" hidden>{render_account_settings(user, accounts_path)}</section>
      {render_member_admin() if user.is_admin else ''}
      {render_position_dialog()}
      {render_position_action_dialog()}
    </section>
    <script type="application/json" id="account-session">{json_script_data({'csrf_token': user.csrf_token})}</script>
    {render_account_script()}
    {render_billing_script()}
    """
    return shell("My portfolio - SpreadBoard", "profile", body)


def render_account_kpi(label: str, value: Any, note: str) -> str:
    return f'<article><span>{h(label)}</span><strong>{h(value if value is not None else "—")}</strong><em>{h(note)}</em></article>'


def render_position_card(item: dict[str, Any]) -> str:
    market_live = item.get("market_status") == "live"
    return f"""
    <article class="position-card" data-position-id="{h(item.get('id'))}">
      <header><div><span class="position-token">{h(item.get('token'))}</span><strong>{h(item.get('long_venue'))} → {h(item.get('short_venue'))}</strong><em>{h(item.get('long_market_type'))} / {h(item.get('short_market_type'))}</em></div><div class="position-status {'live' if market_live else 'unavailable'}"><span>{h(item.get('status'))}</span><strong>{'Live books' if market_live else 'Market unavailable'}</strong></div></header>
      <div class="position-metrics">
        <span>Total PnL<strong class="{spread_class(item.get('total_pnl_usd'))}">{fmt_signed_money(item.get('total_pnl_usd'))}</strong></span>
        <span>Price PnL<strong>{fmt_signed_money(item.get('price_pnl_usd'))}</strong></span>
        <span>Funding<strong>{fmt_signed_money(item.get('funding_income_usd'))}</strong></span>
        <span>Return<strong>{fmt_signed_pct(item.get('return_pct'), digits=2)}</strong></span>
        <span>Exit spread<strong>{fmt_signed_pct(item.get('current_exit_spread_pct'), digits=3)}</strong></span>
        <span>Open spread<strong>{fmt_signed_pct(item.get('current_open_spread_pct'), digits=3)}</strong></span>
      </div>
      <div class="position-legs"><div><span>Long</span><strong>{h(item.get('long_venue'))} · {h(item.get('long_quantity'))}</strong><em>{fmt_price(item.get('long_entry_price'))} → {fmt_price(item.get('long_mark_price'))}</em></div><div><span>Short</span><strong>{h(item.get('short_venue'))} · {h(item.get('short_quantity'))}</strong><em>{fmt_price(item.get('short_entry_price'))} → {fmt_price(item.get('short_mark_price'))}</em></div></div>
      <footer><span>Opened {h(item.get('opened_at'))}</span><div>{render_position_rules(item.get('alert_rules') or [])}<button type="button" data-position-action="funding">Add funding</button><button type="button" data-position-action="alert">Add alert</button>{'<button type="button" data-position-action="close">Close position</button>' if item.get('status') == 'open' else ''}<a href="/charts?route_key={h(board.route_key_url(str(item.get('route_key') or '')))}">Chart</a></div></footer>
    </article>"""


def render_position_rules(rules: list[dict[str, Any]]) -> str:
    enabled = [rule for rule in rules if rule.get("enabled")]
    return f'<span class="position-rule-count">{len(enabled)} alert{"s" if len(enabled) != 1 else ""}</span>'


def render_account_notification(item: dict[str, Any]) -> str:
    return f'<article><span>{h(item.get("created_at"))}</span><strong>{h(item.get("title"))}</strong><p>{h(item.get("body"))}</p></article>'


def render_account_settings(user: accounts.User, accounts_path: Path | str = accounts.DEFAULT_DB_PATH) -> str:
    state = billing.status()
    if user.billing_customer_id:
        billing_action = '<button class="sheet-button" type="button" data-billing-action="portal">Manage billing</button>'
    elif state["checkout_ready"] and not user.is_admin:
        billing_action = f'<a class="sheet-button" href="/subscription">Subscribe · {h(billing.status()["plan_label"])}</a>'
    else:
        billing_action = '<span>Online billing is not active for this account.</span>'
    cancel_note = "Cancellation scheduled at period end." if user.subscription_cancel_at_period_end else "Renews monthly while active."
    telegram_state = telegram_bot.status(db_path=accounts_path)
    telegram_link = accounts.telegram_link_status(user.id, db_path=accounts_path)
    if telegram_state["configured"] and telegram_link["linked"]:
        telegram_action = '<button class="sheet-button" type="button" data-telegram-action="unlink">Disconnect Telegram</button>'
        telegram_note = f"Linked since {h(telegram_link.get('linked_at') or '')}."
    elif telegram_state["configured"]:
        telegram_action = '<button class="sheet-button primary" type="button" data-telegram-action="link">Connect Telegram bot</button>'
        telegram_note = "The one-time link expires after 10 minutes."
    else:
        telegram_action = '<span>Telegram subscription commands are awaiting the dedicated bot credentials.</span>'
        telegram_note = "No Telegram account data is stored until you explicitly link it."
    push = accounts.notification_preferences(user.id, db_path=accounts_path)
    push_checked = "checked" if push.get("pushover_enabled") else ""
    push_key_note = "Key saved securely" if push.get("pushover_configured") else "No key saved"
    return f"""
    <section class="account-settings">
      <div class="account-panel-head"><div><h2>Account settings</h2><p>Capital is used only as the denominator for your return statistics.</p></div></div>
      <form data-account-settings><label><span>Display name</span><input name="display_name" value="{h(user.display_name)}" required></label><label><span>Tracked monthly capital, USD</span><input name="monthly_capital_usd" type="number" min="0" step="0.01" value="{h(user.monthly_capital_usd or '')}"></label><button class="sheet-button primary" type="submit">Save settings</button></form>
      <div class="account-empty-panel"><strong>Monthly membership</strong><p>{h(user.subscription_status)} · {h(cancel_note)}</p>{billing_action}<p role="alert" data-billing-error></p></div>
      <div class="account-empty-panel"><strong>Telegram subscriber access</strong><p>{telegram_note}</p>{telegram_action}<p>{'Subscriber group connected. Use /access in the private bot.' if telegram_state.get('community_configured') else 'The community owner still needs to run /setupgroup after granting the bot invite permissions.'}</p><p role="alert" data-telegram-error></p></div>
      <form data-pushover-settings>
        <label><span>Pushover user key</span><input name="pushover_user_key" type="password" autocomplete="off" placeholder="{h(push_key_note)}"></label>
        <label><span>Device</span><input name="pushover_device" value="{h(push.get('pushover_device') or '')}" placeholder="Optional"></label>
        <label><span>Sound</span><select name="pushover_sound">{''.join(f'<option value="{h(sound)}" {"selected" if sound == push.get("pushover_sound") else ""}>{h(sound)}</option>' for sound in ["pushover", "default", "siren", "magic", "cashregister", "vibrate"])}</select></label>
        <label><span>Delivery enabled</span><input name="pushover_enabled" type="checkbox" {push_checked}></label>
        <button class="sheet-button primary" type="submit">Save Pushover</button>
        <button class="sheet-button" type="button" data-pushover-test>Send test</button>
        <p role="alert" data-pushover-status>{h(push_key_note)}</p>
      </form>
    </section>"""


def render_billing_script() -> str:
    return """<script>
(() => {
  const session=JSON.parse(document.getElementById('account-session')?.textContent||'{}');
  document.querySelectorAll('[data-billing-action]').forEach(button=>button.addEventListener('click',async()=>{
    button.disabled=true;const error=document.querySelector('[data-billing-error]');if(error)error.textContent='';
    try{const checkout=button.dataset.billingAction==='checkout';const consent=document.querySelector('[data-subscription-consent]');if(checkout&&!consent?.checked)throw new Error('Accept the terms and immediate-access acknowledgement before continuing.');const payload=checkout?{terms_accepted:true,immediate_access_consent:true}:{};const response=await fetch(`/api/billing/${button.dataset.billingAction}`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf_token},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok||!data.url)throw new Error(data.error||'Billing is temporarily unavailable.');location.assign(data.url);}catch(exc){if(error)error.textContent=exc.message||'Billing is temporarily unavailable.';button.disabled=false;}
  }));
})();
</script>"""


def render_member_admin() -> str:
    return """<section data-account-panel="members" hidden><div class="account-panel-head"><div><h2>Member access</h2><p>Create monthly accounts and manage subscription status. Passwords are hashed immediately and never shown again.</p></div></div><form class="member-create-form" data-member-create><label><span>Name</span><input name="display_name" autocomplete="name" required></label><label><span>Email</span><input name="email" type="email" autocomplete="email" required></label><label><span>Temporary password</span><input name="password" type="password" minlength="12" autocomplete="new-password" required></label><label><span>Access days</span><input name="subscription_days" type="number" min="1" max="3660" value="30"></label><button class="sheet-button primary" type="submit">Create member</button></form><div data-member-list></div></section>"""


def render_position_dialog() -> str:
    return """<dialog class="account-dialog" data-position-dialog><form method="dialog" data-position-form><header><div><span>Position journal</span><h2>Add position</h2></div><button value="cancel" aria-label="Close">×</button></header><div class="position-form-grid"><input name="route_key" type="hidden"><input name="entry_spread_pct" type="hidden"><label><span>Token</span><input name="token" list="position-token-options" autocomplete="off" required><datalist id="position-token-options"></datalist></label><label><span>Capital, USD</span><input name="capital_usd" type="number" min="0" step="0.01"></label><label class="wide"><span>Suggested live route</span><select data-position-route><option value="">Select a token to see current routes</option></select><em data-position-suggestion-note>Suggestions use current public books. Confirm them against your actual fills.</em></label><label><span>Long venue</span><input name="long_venue" list="position-long-venues" required><datalist id="position-long-venues"></datalist></label><label><span>Long market</span><select name="long_market_type"><option>Spot</option><option>Futures</option></select></label><label><span>Long symbol</span><input name="long_symbol" list="position-long-symbols" placeholder="COTI/USDT"><datalist id="position-long-symbols"></datalist></label><label><span>Long quantity</span><input name="long_quantity" type="number" min="0" step="any" required></label><label><span>Long entry</span><input name="long_entry_price" type="number" min="0" step="any" required></label><label><span>Short venue</span><input name="short_venue" list="position-short-venues" required><datalist id="position-short-venues"></datalist></label><label><span>Short market</span><select name="short_market_type"><option>Futures</option><option>Spot</option></select></label><label><span>Short symbol</span><input name="short_symbol" list="position-short-symbols" placeholder="COTI/USDT:USDT"><datalist id="position-short-symbols"></datalist></label><label><span>Short quantity</span><input name="short_quantity" type="number" min="0" step="any" required></label><label><span>Short entry</span><input name="short_entry_price" type="number" min="0" step="any" required></label><label><span>Entry fees, USD</span><input name="entry_fees_usd" type="number" min="0" step="0.01" value="0"></label><label><span>Opened at</span><input name="opened_at" type="datetime-local"></label><label class="wide"><span>Notes</span><textarea name="notes" rows="3"></textarea></label></div><footer><button value="cancel">Cancel</button><button class="primary" type="submit" value="default">Add position</button></footer><p role="alert" data-form-error></p></form></dialog>"""


def render_position_action_dialog() -> str:
    return """<dialog class="account-dialog compact" data-action-dialog><form method="dialog" data-action-form><header><div><span>Position</span><h2 data-action-title>Update</h2></div><button value="cancel" aria-label="Close">×</button></header><div data-action-fields></div><footer><button value="cancel">Cancel</button><button class="primary" type="submit" value="default">Save</button></footer><p role="alert" data-form-error></p></form></dialog>"""


def render_account_script() -> str:
    return """<script>
(() => {
  const root=document.querySelector('[data-account-page]'); if(!root) return;
  const {csrf_token:csrf}=JSON.parse(document.getElementById('account-session').textContent||'{}');
  const request=async(url,body)=>{const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw new Error(data.error||'Request failed');return data;};
  root.querySelectorAll('[data-account-tab]').forEach(button=>button.addEventListener('click',()=>{root.querySelectorAll('[data-account-tab]').forEach(item=>item.classList.toggle('active',item===button));root.querySelectorAll('[data-account-panel]').forEach(panel=>panel.hidden=panel.dataset.accountPanel!==button.dataset.accountTab);if(button.dataset.accountTab==='members')loadMembers();}));
  const positionDialog=root.querySelector('[data-position-dialog]');root.querySelector('[data-position-new]')?.addEventListener('click',()=>{positionDialog.showModal();refreshSuggestions('');});
  const positionForm=positionDialog?.querySelector('form'),tokenInput=positionForm?.elements.token,routeSelect=positionForm?.querySelector('[data-position-route]');let suggestedRoutes=[],suggestionTimer;
  const setOptions=(id,values)=>{const list=document.getElementById(id);if(!list)return;list.replaceChildren(...[...new Set(values.filter(Boolean))].map(value=>new Option(value)));};
  const legLabel=(venue,marketType)=>String(venue||'').toLowerCase().endsWith(String(marketType||'').toLowerCase())?venue:`${venue} ${marketType}`;
  async function refreshSuggestions(value){const token=String(value||'').trim().toUpperCase();try{const response=await fetch('/api/position-suggestions?'+new URLSearchParams({q:token,limit:'30'}));const data=await response.json();if(!response.ok)throw new Error(data.error||'Suggestions unavailable');setOptions('position-token-options',data.tokens||[]);suggestedRoutes=data.routes||[];routeSelect.replaceChildren(new Option(token?(suggestedRoutes.length?'Choose a current route':'No current route for this token'):'Select a token to see current routes',''),...suggestedRoutes.map((route,index)=>new Option(`${legLabel(route.long_venue,route.long_market_type)} → ${legLabel(route.short_venue,route.short_market_type)} · ${Number(route.entry_spread_pct??0).toFixed(3)}%`,String(index))));const legs=data.legs||[];setOptions('position-long-venues',legs.map(item=>item.venue));setOptions('position-short-venues',legs.map(item=>item.venue));setOptions('position-long-symbols',legs.map(item=>item.symbol));setOptions('position-short-symbols',legs.map(item=>item.symbol));}catch(error){positionForm.querySelector('[data-position-suggestion-note]').textContent=error.message;}}
  tokenInput?.addEventListener('input',()=>{clearTimeout(suggestionTimer);tokenInput.value=tokenInput.value.toUpperCase();suggestionTimer=setTimeout(()=>refreshSuggestions(tokenInput.value),180);});
  routeSelect?.addEventListener('change',()=>{if(routeSelect.value==='')return;const route=suggestedRoutes[Number(routeSelect.value)];if(!route)return;for(const [name,key] of Object.entries({token:'token',route_key:'route_key',entry_spread_pct:'entry_spread_pct',long_venue:'long_venue',long_market_type:'long_market_type',long_symbol:'long_symbol',long_entry_price:'long_entry_price',short_venue:'short_venue',short_market_type:'short_market_type',short_symbol:'short_symbol',short_entry_price:'short_entry_price'})){positionForm.elements[name].value=route[key]??'';}positionForm.querySelector('[data-position-suggestion-note]').textContent=`Suggested from live public books · ${Number(route.age_min??0).toFixed(1)} min old. Replace prices with your actual fills.`;});
  positionDialog?.querySelector('form').addEventListener('submit',async event=>{if(event.submitter?.value==='cancel')return;event.preventDefault();const form=event.currentTarget;const payload=Object.fromEntries(new FormData(form));try{await request('/api/positions',payload);location.reload();}catch(error){form.querySelector('[data-form-error]').textContent=error.message;}});
  const actionDialog=root.querySelector('[data-action-dialog]');let actionPosition=null;let actionType='';
  const fields={funding:'<label><span>Venue</span><input name="venue" required></label><label><span>Amount, USD</span><input name="amount_usd" type="number" step="any" required></label><label><span>Occurred at</span><input name="occurred_at" type="datetime-local"></label>',alert:'<label><span>Metric</span><select name="metric"><option value="exit_spread_pct">Exit spread %</option><option value="open_spread_pct">Open spread %</option><option value="pnl_usd">Total PnL USD</option><option value="funding_usd">Funding USD</option></select></label><label><span>Condition</span><select name="operator"><option value="lte">At or below</option><option value="gte">At or above</option></select></label><label><span>Threshold</span><input name="threshold" type="number" step="any" required></label>',close:'<label><span>Long exit price</span><input name="long_exit_price" type="number" min="0" step="any" required></label><label><span>Short exit price</span><input name="short_exit_price" type="number" min="0" step="any" required></label><label><span>Exit fees, USD</span><input name="exit_fees_usd" type="number" min="0" step="0.01" value="0"></label>'};
  root.addEventListener('click',event=>{const button=event.target.closest('[data-position-action]');if(!button)return;actionPosition=button.closest('[data-position-id]').dataset.positionId;actionType=button.dataset.positionAction;actionDialog.querySelector('[data-action-title]').textContent={funding:'Add funding cashflow',alert:'Create alert rule',close:'Close position'}[actionType];actionDialog.querySelector('[data-action-fields]').innerHTML=fields[actionType];actionDialog.showModal();});
  actionDialog?.querySelector('form').addEventListener('submit',async event=>{if(event.submitter?.value==='cancel')return;event.preventDefault();const form=event.currentTarget;const suffix={funding:'funding',alert:'alerts',close:'close'}[actionType];try{await request(`/api/positions/${actionPosition}/${suffix}`,Object.fromEntries(new FormData(form)));location.reload();}catch(error){form.querySelector('[data-form-error]').textContent=error.message;}});
  root.querySelector('[data-account-settings]')?.addEventListener('submit',async event=>{event.preventDefault();await request('/api/account-settings',Object.fromEntries(new FormData(event.currentTarget)));location.reload();});
  root.querySelector('[data-pushover-settings]')?.addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,status=form.querySelector('[data-pushover-status]');const payload=Object.fromEntries(new FormData(form));payload.pushover_enabled=form.elements.pushover_enabled.checked;try{await request('/api/notification-preferences',payload);form.elements.pushover_user_key.value='';status.textContent='Pushover settings saved securely.';}catch(error){status.textContent=error.message;}});
  root.querySelector('[data-pushover-test]')?.addEventListener('click',async event=>{const status=root.querySelector('[data-pushover-status]');event.currentTarget.disabled=true;try{const result=await request('/api/alert-test',{});status.textContent=result.ok?'Test sent to Pushover.':(result.error||'Test failed.');}catch(error){status.textContent=error.message;}finally{event.currentTarget.disabled=false;}});
  root.querySelector('[data-telegram-action]')?.addEventListener('click',async event=>{const button=event.currentTarget,error=root.querySelector('[data-telegram-error]');button.disabled=true;if(error)error.textContent='';try{const data=await request(`/api/telegram/${button.dataset.telegramAction}`,{});if(data.url){window.open(data.url,'_blank','noopener');button.textContent='Link opened';}else location.reload();}catch(exc){if(error)error.textContent=exc.message;button.disabled=false;}});
  root.querySelector('[data-notifications-read]')?.addEventListener('click',async()=>{await request('/api/notifications/read',{});location.reload();});
  root.querySelector('[data-member-create]')?.addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget;try{await request('/api/account-users',Object.fromEntries(new FormData(form)));form.reset();loadMembers();}catch(error){alert(error.message);}});
  async function loadMembers(){const target=root.querySelector('[data-member-list]');if(!target)return;const response=await fetch('/api/account-users');const data=await response.json();target.innerHTML=(data.users||[]).map(user=>`<article class="member-row"><div><strong>${escapeHtml(user.display_name)}</strong><span>${escapeHtml(user.email)}</span></div><span>${escapeHtml(user.subscription_status)}</span><em>${escapeHtml(user.subscription_expires_at||'No expiry')}</em></article>`).join('');}
  const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
})();
</script>"""


def render_source_summary_card(title: str, age_min: Any, note: str) -> str:
    return f"""
    <article class="chart-summary-card">
      <span>{h(title)}</span>
      <strong>{fmt_age(age_min) if age_min is not None else 'local'}</strong>
      <em>{note}</em>
    </article>
    """


def render_route_source_card(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "empty")
    href = "/arbitrage?" + urlencode({"kind": str(item.get("kind") or "")})
    session = "session source" if item.get("source_requires_session") else "public source"
    return f"""
    <article class="source-status-card {h(status)}">
      <div class="source-status-head">
        <div>
          <span>{h(item.get('tab') or item.get('kind'))}</span>
          <strong>{h(item.get('label') or item.get('kind'))}</strong>
        </div>
        <b>{label_text(status)}</b>
      </div>
      <div class="source-status-metrics">
        <span>Fresh<strong>{h(item.get('fresh_row_count') or 0)}</strong></span>
        <span>Total<strong>{h(item.get('row_count') or 0)}</strong></span>
        <span>Newest<strong>{fmt_age(item.get('newest_age_min'))}</strong></span>
        <span>Source<strong>{h(session)}</strong></span>
      </div>
      <p>{h(item.get('detail') or 'No local source detail.')}</p>
      <a class="mini-action primary-link" href="{h(href)}">Open board</a>
    </article>
    """


def render_source_artifact_card(key: str, item: dict[str, Any]) -> str:
    status = str(item.get("status") or "missing")
    title = item.get("title") or short_path(item.get("path")) or key.replace("_", " ")
    return f"""
    <article class="source-artifact-card {h(status)}">
      <div>
        <span>{h(key.replace('_', ' '))}</span>
        <strong>{label_text(status)}</strong>
      </div>
      <em>{fmt_age(item.get('age_min'))}</em>
      <p>{h(title)}</p>
    </article>
    """


def render_source_mode_row(label: str, value: Any) -> str:
    return f'<div class="source-mode-row"><span>{h(label)}</span><strong>{label_text(value) if isinstance(value, str) else h(value)}</strong></div>'


def render_profile_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    section = (_query_first(query, "section") or "general").casefold()
    if section not in {"general", "telegram", "pushover"}:
        section = "general"
    flags = alerts.config_flags(config)
    if section == "general":
        market = api_market_spreads(
            board_path,
            {
                "include_stale": ["1"],
                "sort": ["edge"],
                "direction": ["desc"],
                "limit": ["20"],
            },
        )
        content = render_profile_general(market)
    elif section == "telegram":
        content = render_profile_telegram()
    else:
        content = render_profile_pushover(flags)
    body = f"""
    <section class="profile-page" data-profile-section="{h(section)}">
      <header class="profile-heading">
        <div>
          <span class="page-kicker">Local Operator Profile</span>
          <h1>Profile</h1>
          <p>Saved preferences, route-bound alert rules, and future portfolio surfaces for this browser.</p>
        </div>
        <div class="profile-mode">
          <span>Delivery mode</span>
          <strong>Preview only</strong>
          <em>No external notification is sent</em>
        </div>
      </header>
      <div class="profile-layout">
        <aside class="profile-nav-panel">
          <span>My profile</span>
          {render_profile_nav_item('general', 'General information', section)}
          {render_profile_nav_item('telegram', 'Telegram bot', section)}
          {render_profile_nav_item('pushover', 'Pushover', section)}
          <div class="profile-local-note">
            <strong>Local by design</strong>
            <p>Preferences and alert templates stay in this browser. Secrets are never returned by an API.</p>
          </div>
        </aside>
        <main class="profile-main">{content}</main>
      </div>
    </section>
    """ + PROFILE_SCRIPT
    return shell("Profile - SpreadBoard", "profile", body)


def render_profile_nav_item(key: str, label: str, selected: str) -> str:
    current = 'aria-current="page"' if key == selected else ""
    return (
        f'<a class="profile-nav-item {"active" if key == selected else ""}" '
        f'href="/profile?section={h(key)}" {current}>'
        f'<span aria-hidden="true">{h({"general": "01", "telegram": "02", "pushover": "03"}[key])}</span>'
        f"<strong>{h(label)}</strong></a>"
    )


def render_profile_general(market: dict[str, Any]) -> str:
    summary = market.get("summary") or {}
    kind_counts = market.get("route_kind_counts") or {}
    available_tabs = sum(1 for item in board.ROUTE_KINDS if kind_counts.get(item.kind))
    return f"""
    <section class="profile-section">
      <div class="profile-section-title">
        <div><span class="page-kicker">General information</span><h2>Your local workspace</h2></div>
        <span class="profile-state good">Active</span>
      </div>
      <section class="profile-summary-grid">
        <article><span>Profile scope</span><strong>Local operator</strong><em>No sign-in required</em></article>
        <article><span>Route families</span><strong>{h(available_tabs)} / {h(len(board.ROUTE_KINDS))}</strong><em>with local source rows</em></article>
        <article><span>Parsed routes</span><strong>{h(summary.get('total_rows') or 0)}</strong><em>{h(summary.get('fresh_rows') or 0)} fresh</em></article>
        <article><span>Saved alert rules</span><strong data-profile-rule-count>0</strong><em>browser storage</em></article>
      </section>
      <section class="profile-panel">
        <div class="profile-panel-head"><div><h3>Workspace status</h3><p>Profile-ready surfaces joined to the current local parser state.</p></div></div>
        <div class="profile-status-list">
          <div><span>Watchlist</span><strong data-profile-watch-count>0 tokens</strong><em>available now</em></div>
          <div><span>Alert templates</span><strong data-profile-rule-summary>No saved rules</strong><em>spread and funding evaluation available</em></div>
          <div><span>Portfolio and PnL</span><strong>Reserved</strong><em>future authenticated account layer</em></div>
          <div><span>External delivery</span><strong>Disabled</strong><em>preview-only boundary</em></div>
        </div>
      </section>
      <section class="profile-panel">
        <div class="profile-panel-head"><div><h3>Local activity</h3><p>Recent preference and alert-rule changes made in this browser.</p></div></div>
        <div class="profile-activity-table" id="profileActivity"></div>
      </section>
    </section>
    """


def render_profile_telegram() -> str:
    venues = [
        "Aster",
        "Binance",
        "BingX",
        "Bitget",
        "Bybit",
        "Gate",
        "Hyperliquid",
        "KuCoin",
        "MEXC",
        "OKX",
        "Ourbit",
        "XT",
    ]
    route_types = [
        ("FUTURES", "Futures"),
        ("SPOT-FUTURES", "Spot-Futures"),
        ("FUTURES-SPOT-PAIR", "Futures-Spot"),
        ("SPOT", "Spot"),
        ("DEX-FUTURES", "Futures-DEX"),
        ("DEX-SPOT", "Spot-DEX"),
    ]
    return f"""
    <section class="profile-section">
      <div class="profile-section-title">
        <div><span class="page-kicker">Telegram bot</span><h2>Notification filter template</h2><p>Configure local filter preferences. Bot delivery remains disabled.</p></div>
        <span class="profile-state neutral">Template</span>
      </div>
      <form class="profile-form" data-profile-form="telegram">
        <section class="profile-panel">
          <div class="profile-panel-head"><div><h3>Bot configuration</h3><p>The token field is never persisted; only a configured/not-configured flag is stored.</p></div></div>
          <div class="profile-field-grid two">
            <label><span>Bot token</span><input type="password" autocomplete="off" data-secret-field="telegramToken" placeholder="Enter bot token"></label>
            <label class="profile-switch-field"><span>Enabled</span><input type="checkbox" data-profile-field="telegramEnabled"><i></i></label>
          </div>
          <div class="profile-actions"><button class="sheet-button primary" type="submit">Save local preferences</button><span data-secret-status="telegramToken">Token not stored</span></div>
        </section>
        <section class="profile-panel">
          <div class="profile-panel-head"><div><h3>Message filters</h3><p>Route, venue, spread, turnover, and interval preferences matching the scanner surface.</p></div></div>
          <div class="profile-chip-grid route-filter-chips">
            {''.join(f'<label><input type="checkbox" value="{h(value)}" data-profile-list="telegramRoutes"><span>{h(label)}</span></label>' for value, label in route_types)}
          </div>
          <div class="profile-field-grid three">
            <label><span>Open spread, %</span><input type="number" step="0.01" min="0" data-profile-field="telegramMinSpread" value="5"></label>
            <label><span>Turnover, M</span><input type="number" step="0.1" min="0" data-profile-field="telegramMinTurnover" value="0.5"></label>
            <label class="profile-switch-field"><span>Different intervals</span><input type="checkbox" data-profile-field="telegramDifferentIntervals"><i></i></label>
          </div>
          <div class="profile-exchange-grid">
            {''.join(f'<label><input type="checkbox" value="{h(venue)}" data-profile-list="telegramVenues"><span>{h(venue)}</span></label>' for venue in venues)}
          </div>
          <label class="profile-wide-field"><span>Muted tokens or contract addresses</span><textarea data-profile-field="telegramMuted" rows="3" placeholder="One symbol or contract per line"></textarea></label>
          <div class="profile-actions"><button class="sheet-button primary" type="submit">Save filters</button><a class="sheet-button" href="/markets">Open markets</a></div>
        </section>
      </form>
    </section>
    """


def render_profile_pushover(flags: dict[str, Any]) -> str:
    sounds = ["default", "pushover", "siren", "magic", "cashregister", "vibrate"]
    # A member who saves a key and then hears nothing has no way to tell whether
    # their key is wrong or the server simply cannot send. Say which it is.
    ready = bool(os.environ.get("SPREADBOARD_PUSHOVER_APP_TOKEN", "").strip())
    delivery_note = (
        '<p class="profile-note ok">Alerts are delivered to your Pushover account. '
        'Save your user key below and keep Enabled switched on.</p>'
        if ready
        else '<p class="profile-note warn"><strong>Delivery is not active yet.</strong> '
        'Your key is stored safely, but this server has no Pushover application token, '
        'so nothing can be sent until the operator adds one. You do not need to do anything else.</p>'
    )
    return f"""
    <section class="profile-section">
      <div class="profile-section-title">
        <div><span class="page-kicker">Pushover</span><h2>Pushover notifications</h2><p>Spread, funding and rail-reopen alerts sent to your own phone.</p></div>
        <span class="profile-state {'ok' if ready else 'warn'}">{'Delivery ready' if ready else 'Delivery inactive'}</span>
      </div>
      {delivery_note}
      <form class="profile-form" data-profile-form="pushover">
        <section class="profile-panel">
          <div class="profile-panel-head"><div><h3>Personal configuration</h3><p>Credential values are never exposed or persisted by this page.</p></div><span>{h(flags.get('pushover_user_count') or 0)} server recipients hidden</span></div>
          <div class="profile-field-grid three">
            <label><span>User key</span><input type="password" autocomplete="off" data-secret-field="pushoverKey" placeholder="Enter Pushover user key"></label>
            <label><span>Device</span><input type="text" data-profile-field="pushoverDevice" placeholder="Optional device name"></label>
            <label><span>Sound</span><select data-profile-field="pushoverSound">{''.join(f'<option value="{h(sound)}">{h(sound)}</option>' for sound in sounds)}</select></label>
            <label class="profile-switch-field"><span>Enabled</span><input type="checkbox" data-profile-field="pushoverEnabled"><i></i></label>
          </div>
          <div class="profile-actions">
            <button class="sheet-button primary" type="submit">Save local preferences</button>
            <button class="sheet-button" type="button" data-preview-test>Preview test</button>
            <span data-secret-status="pushoverKey">Key not stored</span>
          </div>
        </section>
        <section class="profile-panel">
          <div class="profile-panel-head"><div><h3>Fair price configuration</h3><p>Shared defaults for future price/fair-value alert templates.</p></div></div>
          <div class="profile-field-grid three">
            <label><span>Token cooldown, min</span><input type="number" min="0" data-profile-field="fairCooldown" value="10"></label>
            <label><span>Min leverage</span><input type="number" min="0" step="0.1" data-profile-field="fairMinLeverage" value="1"></label>
            <label><span>Min spread, %</span><input type="number" min="0" step="0.1" data-profile-field="fairMinSpread" value="5"></label>
            <label><span>Min limit</span><input type="number" min="0" step="0.1" data-profile-field="fairMinLimit" placeholder="Optional"></label>
            <label><span>Min volume, M</span><input type="number" min="0" step="0.1" data-profile-field="fairMinVolume" value="0.3"></label>
            <label class="profile-switch-field"><span>Enabled</span><input type="checkbox" data-profile-field="fairEnabled"><i></i></label>
          </div>
          <div class="profile-segmented" aria-label="Fair price sides">
            <label><input type="checkbox" value="long" data-profile-list="fairSides"><span>Long</span></label>
            <label><input type="checkbox" value="short" data-profile-list="fairSides"><span>Short</span></label>
          </div>
        </section>
        <section class="profile-panel">
          <div class="profile-panel-head"><div><h3>Community calls alert</h3><p>Local template for later Telegram-community call notifications.</p></div></div>
          <div class="profile-field-grid two">
            <label class="profile-switch-field"><span>Enabled</span><input type="checkbox" data-profile-field="communityEnabled"><i></i></label>
            <label class="profile-switch-field"><span>Use whitelist</span><input type="checkbox" data-profile-field="communityUseWhitelist"><i></i></label>
          </div>
          <label class="profile-wide-field"><span>Telegram usernames</span><textarea rows="2" data-profile-field="communityWhitelist" placeholder="One username per line"></textarea></label>
          <div class="profile-actions"><button class="sheet-button primary" type="submit">Save all Pushover preferences</button></div>
        </section>
      </form>
      <section class="profile-panel alert-library">
        <div class="profile-panel-head">
          <div><h3>Alerts <span id="profileAlertCount">(0)</span></h3><p>Spread, funding, token price and token funding rules all evaluate against the live board. Other rule types remain templates.</p></div>
          <button class="sheet-button primary js-alert-draft" type="button" data-alert-type="token_spread">Add route alert</button>
        </div>
        <form class="token-alert-form" id="tokenAlertForm">
          <div>
            <strong>Watch a token</strong>
            <p class="pricing-note">No route to pick. Price is the median across every venue quoting it,
               so one stale print cannot trip the alert; funding is the best 24h carry on the asset.</p>
          </div>
          <label><span>Token</span><input name="symbol" placeholder="DOGE" maxlength="40" required></label>
          <label><span>Watch</span><select name="type">
            <option value="price">Price</option>
            <option value="token_funding">Funding 24h</option>
          </select></label>
          <label><span>When it is</span><select name="direction">
            <option value="above">At or above</option>
            <option value="below">At or below</option>
          </select></label>
          <label><span>Level</span><input name="threshold" type="number" step="any" placeholder="0.25" required></label>
          <label><span>Hold for</span><input name="stability_seconds" type="number" min="0" max="3600" value="30"></label>
          <button class="sheet-button primary" type="submit">Create</button>
          <span class="token-alert-status" data-token-alert-status></span>
        </form>
        <div class="profile-alert-filters">
          <label><span>Alert type</span><select id="profileAlertTypeFilter">
            <option value="all">All</option><option value="token_spread">Token spread</option><option value="funding">Funding</option><option value="price">Price</option><option value="exchange_spread">Exchange spread</option><option value="custom_pair_spread">Custom pair spread</option><option value="dw_tracking">D/W tracking</option><option value="freshness">Freshness</option>
          </select></label>
          <label><span>Status</span><select id="profileAlertStatusFilter"><option value="all">All</option><option value="triggered">Triggered</option><option value="active">Active</option><option value="review">Review</option><option value="inactive">Inactive</option><option value="template">Template</option></select></label>
          <label><span>Sort</span><select id="profileAlertSort"><option value="status">Status</option><option value="updated">Recently updated</option><option value="symbol">Symbol</option><option value="value_desc">Value high to low</option></select></label>
        </div>
        <div class="profile-alert-grid" id="profileAlertGrid"></div>
      </section>
    </section>
    """


def render_member_alert_rules(board_path: Path) -> str:
    """A member's own alerts, each against the value it is watching right now.

    Creating an alert was possible but nothing showed it afterwards, so a member
    could not tell what they had armed, how far the market was from it, or change
    their mind. Threshold, direction, stability and on/off are all editable here.
    """
    user = accounts.current_user()
    if user is None:
        return ""
    rules = accounts.list_market_alert_rules(user.id)
    if not rules:
        return """
    <section class="member-alerts">
      <div class="profile-section-title"><div><span class="page-kicker">My alerts</span>
        <h2>You have no alerts yet</h2>
        <p>Open any route on the board and use "Alert" to watch its spread or funding.
           Alerts are delivered to your own Pushover account.</p></div></div>
    </section>"""
    market = api_market_spreads(board_path, {"limit": ["0"]})
    current: dict[str, dict[str, Any]] = {
        str(row.get("route_key") or ""): row
        for row in (market.get("rows") or [])
        if isinstance(row, dict)
    }
    cards = "".join(
        render_member_alert_card(rule, current.get(str(rule.get("route_key") or "")))
        for rule in rules
    )
    return f"""
    <section class="member-alerts">
      <div class="profile-section-title">
        <div><span class="page-kicker">My alerts</span><h2>{len(rules)} alert{"s" if len(rules) != 1 else ""} armed</h2>
        <p>Delivered to your Pushover account. An alert fires once when the level holds
           for its stability window, and re-arms after the market moves back.</p></div>
      </div>
      <div class="member-alert-grid">{cards}</div>
    </section>
    {render_member_alert_script()}"""


def render_member_alert_card(rule: dict[str, Any], row: dict[str, Any] | None) -> str:
    metric = str(rule.get("metric") or "")
    is_funding = metric == "funding_24h_pct"
    label = "24h funding" if is_funding else "open spread"
    value = None
    if row is not None:
        value = row.get("funding_24h_pct") if is_funding else row.get("executable_spread_pct")
    threshold = _float_or_none(rule.get("threshold")) or 0.0
    above = str(rule.get("operator") or "gte") == "gte"
    enabled = bool(rule.get("enabled"))
    live = _float_or_none(value)
    met = live is not None and ((live >= threshold) if above else (live <= threshold))
    state = "armed" if enabled else "paused"
    return f"""
    <article class="member-alert-card {h(state)} {'met' if met and enabled else ''}" data-alert-id="{h(rule.get('id'))}">
      <div class="member-alert-head">
        <span class="member-alert-state">{'Armed' if enabled else 'Paused'}</span>
        <span class="member-alert-kind">{h(label)}</span>
      </div>
      <strong class="member-alert-token">{h(rule.get('symbol'))}</strong>
      <div class="member-alert-route">{h(route_label_from_key(str(rule.get('route_key') or '')))}</div>
      <div class="member-alert-now">Now <strong>{fmt_signed_pct(live, digits=3) if live is not None else 'no live quote'}</strong></div>
      <label><span>Fires when {'at or above' if above else 'at or below'}</span>
        <input type="number" step="0.0001" name="threshold" value="{h(threshold)}"></label>
      <label><span>Direction</span>
        <select name="direction">
          <option value="above" {'selected' if above else ''}>at or above</option>
          <option value="below" {'selected' if not above else ''}>at or below</option>
        </select></label>
      <label><span>Hold for (seconds)</span>
        <input type="number" min="0" max="3600" name="stability_seconds" value="{h(rule.get('stability_seconds') or 0)}"></label>
      <label class="member-alert-toggle"><span>Enabled</span>
        <input type="checkbox" name="enabled" {'checked' if enabled else ''}></label>
      <div class="member-alert-actions">
        <button type="button" data-alert-save>Save</button>
        <button type="button" data-alert-delete>Delete</button>
      </div>
      <em data-alert-status></em>
    </article>"""


def route_label_from_key(route_key: str) -> str:
    parts = [part for part in route_key.split("|") if part]
    if len(parts) >= 5:
        return f"{parts[1]} {parts[2]} -> {parts[3]} {parts[4]}"
    return route_key or "route"


def render_member_alert_script() -> str:
    return """
    <script>
    (function(){
      const csrf = () => document.querySelector('[data-logout]')?.dataset.csrf
        || JSON.parse(document.getElementById('account-session')?.textContent || '{}').csrf_token || '';
      async function send(path, body){
        const response = await fetch(path, {method:'POST',
          headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},
          body: JSON.stringify(body || {})});
        const data = await response.json().catch(() => ({}));
        if(!response.ok || data.ok === false) throw new Error(data.error || 'Could not save');
        return data;
      }
      const tokenForm = document.getElementById('tokenAlertForm');
      if(tokenForm){
        tokenForm.addEventListener('submit', async (event) => {
          event.preventDefault();
          const status = tokenForm.querySelector('[data-token-alert-status]');
          const data = new FormData(tokenForm);
          const symbol = String(data.get('symbol') || '').trim().toUpperCase();
          const threshold = Number(data.get('threshold'));
          if(!symbol || !Number.isFinite(threshold)){
            status.textContent = 'Enter a token and a level.'; return;
          }
          status.textContent = 'Creating...';
          try{
            await send('/api/market-alert-rules', {
              type: data.get('type'),
              symbol,
              direction: data.get('direction'),
              threshold,
              stability_seconds: Number(data.get('stability_seconds') || 0)});
            status.textContent = symbol + ' alert created.';
            tokenForm.reset();
            setTimeout(() => window.location.reload(), 700);
          }catch(error){ status.textContent = error.message; }
        });
      }
      document.addEventListener('click', async (event) => {
        const card = event.target.closest('[data-alert-id]');
        if(!card) return;
        const id = card.dataset.alertId;
        const status = card.querySelector('[data-alert-status]');
        if(event.target.matches('[data-alert-save]')){
          try{
            await send(`/api/market-alert-rules/${id}`, {
              threshold: Number(card.querySelector('[name=threshold]').value),
              direction: card.querySelector('[name=direction]').value,
              stability_seconds: Number(card.querySelector('[name=stability_seconds]').value || 0),
              enabled: card.querySelector('[name=enabled]').checked});
            status.textContent = 'Saved.';
          }catch(error){ status.textContent = error.message; }
        }
        if(event.target.matches('[data-alert-delete]')){
          try{
            await send(`/api/market-alert-rules/${id}/delete`, {});
            card.remove();
          }catch(error){ status.textContent = error.message; }
        }
      });
    })();
    </script>"""


def render_alerts_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    flags = alerts.config_flags(config)
    preview = api_alert_preview(board_path, query)
    telegram = str(config.get("telegram_channel_url") or "").strip()
    telegram_button = (
        f'<a class="primary" href="{h(telegram)}" rel="noreferrer">Join Telegram channel</a>'
        if telegram
        else ""
    )
    body = f"""
    <section class="alerts-page" data-refresh="180">
      {render_member_alert_rules(board_path)}
      <div class="intel-hero compact-hero">
        <div>
          <span class="page-kicker">Alerts</span>
          <h1>Your alerts</h1>
          <p>Watch any route's spread or funding and get a push on your own phone when it hits your level.</p>
        </div>
        <div class="intel-actions">
          {telegram_button}
          <a class="primary" href="/profile?section=pushover">Manage alert rules</a>
          <a class="secondary" href="/intel">Intel</a>
        </div>
      </div>
      <section class="alert-status-grid">
        <article class="chart-summary-card"><span>Delivery</span><strong>Preview only</strong><em>No Pushover send path is active</em></article>
        <article class="chart-summary-card"><span>Would trigger</span><strong>{h(preview.get('would_trigger_count') or 0)}</strong><em>current local data</em></article>
        <article class="chart-summary-card"><span>Spread threshold</span><strong>{h(flags['alert_min_spread_pct'])}%</strong><em>configured reference</em></article>
        <article class="chart-summary-card"><span>Recipients</span><strong>{h(flags['pushover_user_count'])}</strong><em>stored locally, not used here</em></article>
      </section>
      <section class="alert-rule-grid">
        {''.join(render_alert_rule_card(card) for card in preview.get('cards') or []) or '<p class="empty">No alert preview rows available.</p>'}
      </section>
      <section class="community-panel">
        <div class="panel-head flat"><div><h2>Profile Alert Library</h2><p>Create route-bound spread and funding rules from Markets, Funding, pair detail, or the Profile tab.</p></div></div>
        <div class="alert-template-grid">
          {render_alert_template('Spread', 'Token or route crosses an open-spread threshold, optionally filtered by route kind and venue.')}
          {render_alert_template('Funding', 'Funding APR or next-funding delta crosses a threshold while route freshness is healthy.')}
          {render_alert_template('Freshness', 'A source goes stale, recovers, or a premium/session-backed tab becomes unavailable.')}
          {render_alert_template('Route Change', 'A hot symbol appears, closes, changes route direction, or gets a new blocker/next action.')}
          {render_alert_template('Community Call', 'Telegram/community discussion spikes for a watched token or matches a repeated question category.')}
        </div>
      </section>
    </section>
    """
    return shell("Alerts - SpreadBoard", "alerts", body)


def render_watchlist_page(board_path: Path, config: dict[str, Any], query: dict[str, list[str]]) -> str:
    del config
    data = api_watchlist_suggestions(board_path, query)
    profile = data.get("profile_shell") or {}
    suggestions = _watchlist_seed_symbols(data)
    route_count = sum(1 for item in data.get("route_reality") or [] if item.get("routes"))
    trigger_count = (data.get("alert_preview") or {}).get("would_trigger_count") or 0
    source = data.get("source_freshness") or {}
    stale_sources = [
        name
        for name, item in source.items()
        if isinstance(item, dict) and item.get("status") in {"stale", "missing", "error"}
    ]
    body = f"""
    <section class="watchlist-page" data-refresh="120">
      <div class="intel-hero compact-hero">
        <div>
          <span class="page-kicker">Watchlist</span>
          <h1>Local watchlist</h1>
          <p>Browser-only pins joined to hot symbols, route reality, funding, and preview alert context.</p>
        </div>
        <div class="intel-actions">
          <a class="secondary" href="/intel">Intel</a>
          <a class="secondary" href="/alerts">Alert previews</a>
        </div>
      </div>
      <section class="watch-status-grid">
        <article class="chart-summary-card"><span>Storage</span><strong>Browser only</strong><em>No server profile writes</em></article>
        <article class="chart-summary-card"><span>Profile</span><strong>{label_text(profile.get('status') or 'profile_shell_only')}</strong><em>Auth comes later</em></article>
        <article class="chart-summary-card"><span>PnL</span><strong>Planned</strong><em>Not active in this pass</em></article>
        <article class="chart-summary-card"><span>Matched routes</span><strong>{h(route_count)}</strong><em>from local intel</em></article>
        <article class="chart-summary-card"><span>Would trigger</span><strong>{h(trigger_count)}</strong><em>preview only</em></article>
      </section>
      <section class="watchlist-layout">
        <main class="watchlist-main">
          <section class="watch-panel">
            <div class="panel-head flat"><div><h2>Your Watchlist</h2><p>Tokens pinned in this browser, with no account storage or notification sending.</p></div></div>
            <form class="watch-control-row" id="watchForm">
              <input class="watch-input" id="watchInput" name="symbol" placeholder="Token symbol" autocomplete="off">
              <button class="sheet-button primary" type="submit">Add</button>
              <button class="sheet-button" id="seedWatchlist" type="button">Use Hot List</button>
              <button class="sheet-button" id="clearWatchlist" type="button">Clear</button>
            </form>
            <div class="watch-items" id="watchItems"></div>
          </section>
          <section class="watch-panel">
            <div class="panel-head flat"><div><h2>Matched Routes</h2><p>Current board/preflight context for pinned tokens.</p></div></div>
            <div class="watch-route-list" id="watchRoutes"></div>
          </section>
          <section class="watch-panel">
            <div class="panel-head flat"><div><h2>Alert Context</h2><p>Read-only examples from spread, funding, freshness, route-change, and community-call previews.</p></div></div>
            <div class="watch-alert-list" id="watchAlerts"></div>
          </section>
        </main>
        <aside class="watchlist-side">
          <section class="watch-panel">
            <div class="panel-head flat"><div><h2>Suggested Pins</h2><p>Seeded from Community Intel and route matches.</p></div></div>
            <div class="suggestion-grid">
              {''.join(render_watch_suggestion(symbol) for symbol in suggestions) or '<p class="watch-empty">No hot-symbol suggestions found.</p>'}
            </div>
          </section>
          <section class="watch-panel">
            <div class="panel-head flat"><div><h2>Profile Shell</h2><p>Reserved surfaces for the later account/PnL pass.</p></div></div>
            <div class="profile-shell-list">
              {''.join(render_watch_profile_row(item) for item in profile.get('sections') or []) or '<p class="watch-empty">No profile sections available.</p>'}
            </div>
          </section>
          <section class="watch-panel">
            <div class="panel-head flat"><div><h2>Source Notes</h2><p>Unavailable or stale sources are shown explicitly.</p></div></div>
            <div class="source-note-list">
              {''.join(f'<span>{h(name.replace("_", " "))}</span>' for name in stale_sources[:8]) or '<span>All tracked intel sources look usable.</span>'}
            </div>
          </section>
        </aside>
      </section>
    </section>
    <script id="watchlistData" type="application/json">{json_script_data(data)}</script>
    """ + WATCHLIST_SCRIPT
    return shell("Watchlist - SpreadBoard", "watchlist", body)


def _watchlist_seed_symbols(data: dict[str, Any], *, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    pools: list[list[Any]] = [
        list((data.get("profile_shell") or {}).get("watchlist") or []),
        [item.get("symbol") for item in data.get("hot_symbols") or [] if isinstance(item, dict)],
        [item.get("symbol") for item in data.get("route_reality") or [] if isinstance(item, dict)],
    ]
    for pool in pools:
        for raw in pool:
            symbol = _clean_symbol(str(raw or ""))
            if symbol and symbol not in seen:
                output.append(symbol)
                seen.add(symbol)
            if len(output) >= limit:
                return output
    return output


def render_watch_suggestion(symbol: str) -> str:
    return (
        f'<button class="suggestion-chip" type="button" data-watch-symbol="{h(symbol)}">'
        f"<strong>{h(symbol)}</strong><span>Pin</span>"
        "</button>"
    )


def render_watch_profile_row(item: dict[str, Any]) -> str:
    return (
        '<div class="watch-profile-row">'
        f'<span>{h(item.get("label"))}</span>'
        f'<strong>{label_text(item.get("status"))}</strong>'
        f'<em>{h(item.get("count") or 0)}</em>'
        "</div>"
    )


PROFILE_SCRIPT = """
<script>
(() => {
  const root = document.querySelector(".profile-page");
  if (!root || !window.SpreadBoardAlerts) return;
  const prefsKey = "spreadboard.profile.v1";
  const watchKey = "spreadboard.watchlist.v1";
  const activityKey = "spreadboard.profileActivity.v1";
  const alertsApi = window.SpreadBoardAlerts;
  let marketRows = [];
  let marketRowsStatus = "idle";
  let refreshInFlight = false;

  function readJson(key, fallback) {
    try {
      const parsed = JSON.parse(localStorage.getItem(key) || "null");
      return parsed ?? fallback;
    } catch (error) {
      return fallback;
    }
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    })[char]);
  }

  function prefs() {
    const value = readJson(prefsKey, {});
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function savePrefs(value, detail) {
    localStorage.setItem(prefsKey, JSON.stringify(value));
    alertsApi.logActivity("Profile preferences saved", detail);
    alertsApi.showToast("Local profile preferences saved");
    refreshProfileSummary();
  }

  function hydratePreferences() {
    const value = prefs();
    document.querySelectorAll("[data-profile-field]").forEach((field) => {
      const key = field.dataset.profileField;
      if (!(key in value)) return;
      if (field.type === "checkbox") field.checked = Boolean(value[key]);
      else field.value = value[key] ?? "";
    });
    document.querySelectorAll("[data-profile-list]").forEach((field) => {
      const selected = Array.isArray(value[field.dataset.profileList]) ? value[field.dataset.profileList] : [];
      field.checked = selected.includes(field.value);
    });
    document.querySelectorAll("[data-secret-status]").forEach((status) => {
      const key = `${status.dataset.secretStatus}Configured`;
      status.textContent = value[key] ? "Credential marked as configured" : "Credential not stored";
    });
  }

  function collectPreferences(form) {
    const value = prefs();
    form.querySelectorAll("[data-profile-field]").forEach((field) => {
      value[field.dataset.profileField] = field.type === "checkbox" ? field.checked : field.value;
    });
    const listKeys = new Set(Array.from(form.querySelectorAll("[data-profile-list]")).map((field) => field.dataset.profileList));
    listKeys.forEach((key) => {
      value[key] = Array.from(form.querySelectorAll(`[data-profile-list="${key}"]:checked`)).map((field) => field.value);
    });
    form.querySelectorAll("[data-secret-field]").forEach((field) => {
      if (field.value.trim()) value[`${field.dataset.secretField}Configured`] = true;
      field.value = "";
    });
    return value;
  }

  document.querySelectorAll("[data-profile-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      savePrefs(collectPreferences(form), form.dataset.profileForm || "profile");
      hydratePreferences();
    });
  });

  const previewButton = document.querySelector("[data-preview-test]");
  if (previewButton) {
    previewButton.addEventListener("click", () => {
      alertsApi.logActivity("Pushover preview tested", "No message sent");
      alertsApi.showToast("Preview passed. No Pushover message was sent.");
      renderActivity();
    });
  }

  function refreshProfileSummary() {
    const rules = alertsApi.readRules();
    const watchlist = readJson(watchKey, []);
    document.querySelectorAll("[data-profile-rule-count]").forEach((node) => { node.textContent = String(rules.length); });
    document.querySelectorAll("[data-profile-watch-count]").forEach((node) => {
      node.textContent = `${Array.isArray(watchlist) ? watchlist.length : 0} tokens`;
    });
    document.querySelectorAll("[data-profile-rule-summary]").forEach((node) => {
      const active = rules.filter((item) => item.enabled !== false).length;
      node.textContent = rules.length ? `${active} active / ${rules.length} total` : "No saved rules";
    });
  }

  function renderActivity() {
    const target = document.getElementById("profileActivity");
    if (!target) return;
    const rows = readJson(activityKey, []);
    if (!Array.isArray(rows) || !rows.length) {
      target.innerHTML = '<p class="profile-empty">No local profile activity yet.</p>';
      return;
    }
    target.innerHTML = rows.slice(0, 12).map((item) => {
      const at = item.at ? new Date(item.at).toLocaleString() : "Unknown time";
      return `<div><span>${escapeHtml(at)}</span><strong>${escapeHtml(item.action || "Updated")}</strong><em>${escapeHtml(item.detail || "")}</em></div>`;
    }).join("");
  }

  function rowForRule(rule) {
    if (rule.routeKey) {
      const exact = marketRows.find((row) => row.route_key === rule.routeKey);
      if (exact) return exact;
    }
    return marketRows.find((row) => {
      if (rule.symbol && row.token !== rule.symbol) return false;
      if (rule.longVenue && row.long_venue !== rule.longVenue) return false;
      if (rule.shortVenue && row.short_venue !== rule.shortVenue) return false;
      return true;
    }) || null;
  }

  function evaluateRule(rule) {
    if (rule.enabled === false) return {status: "inactive", current: null, note: "Rule disabled"};
    if (!["token_spread", "funding"].includes(rule.type)) {
      return {status: "template", current: null, note: "Template saved; evaluator comes later"};
    }
    const row = rowForRule(rule);
    if (!row) {
      if (marketRowsStatus === "loading") {
        return {status: "loading", current: null, note: "Loading current local route"};
      }
      if (marketRowsStatus === "unavailable") {
        return {status: "review", current: null, note: "Local route source unavailable"};
      }
      return {status: "review", current: null, note: "No matching local route row"};
    }
    const current = rule.type === "funding"
      ? Number(row.funding_apr_pct)
      : Number(row.displayed_open_spread_pct ?? row.executable_spread_pct);
    if (!Number.isFinite(current)) return {status: "review", current: null, note: "Current value unavailable"};
    if (row.freshness === "stale") return {status: "review", current, note: "Matching route is stale"};
    const threshold = Number(rule.threshold);
    const triggered = rule.direction === "below" ? current <= threshold : current >= threshold;
    return {
      status: triggered ? "triggered" : "active",
      current,
      note: triggered ? "Would trigger now" : "Monitoring current local row"
    };
  }

  function routeLabel(rule) {
    const long = [rule.longVenue, rule.longMarketType].filter(Boolean).join(" ");
    const short = [rule.shortVenue, rule.shortMarketType].filter(Boolean).join(" ");
    return long || short ? `${long || "?"} -> ${short || "?"}` : (rule.routeKind || "Any route");
  }

  function filteredRules() {
    const typeFilter = document.getElementById("profileAlertTypeFilter")?.value || "all";
    const statusFilter = document.getElementById("profileAlertStatusFilter")?.value || "all";
    const sort = document.getElementById("profileAlertSort")?.value || "status";
    const values = alertsApi.readRules().map((rule) => ({rule, evaluation: evaluateRule(rule)}))
      .filter(({rule}) => typeFilter === "all" || rule.type === typeFilter)
      .filter(({evaluation}) => statusFilter === "all" || evaluation.status === statusFilter);
    values.sort((left, right) => {
      if (sort === "updated") return String(right.rule.updatedAt || "").localeCompare(String(left.rule.updatedAt || ""));
      if (sort === "symbol") return String(left.rule.symbol || "").localeCompare(String(right.rule.symbol || ""));
      if (sort === "value_desc") return (right.evaluation.current ?? -Infinity) - (left.evaluation.current ?? -Infinity);
      const order = {triggered: 0, active: 1, loading: 2, review: 3, template: 4, inactive: 5};
      return (order[left.evaluation.status] ?? 9) - (order[right.evaluation.status] ?? 9);
    });
    return values;
  }

  function renderAlertRules() {
    const grid = document.getElementById("profileAlertGrid");
    if (!grid) return;
    const allRules = alertsApi.readRules();
    const rows = filteredRules();
    const count = document.getElementById("profileAlertCount");
    if (count) count.textContent = `(${rows.length} / ${allRules.length})`;
    if (!rows.length) {
      grid.innerHTML = '<div class="profile-alert-empty"><strong>No matching alert rules</strong><p>Use a route Alert button or Add new alert to create one.</p></div>';
      return;
    }
    grid.innerHTML = rows.map(({rule, evaluation}) => {
      const direction = rule.direction === "below" ? "below" : "above";
      const current = evaluation.current === null ? "not available" : `${evaluation.current.toFixed(4)}%`;
      return `
        <article class="profile-alert-card ${escapeHtml(evaluation.status)}">
          <header><div><span>${escapeHtml(evaluation.status)}</span><strong>${escapeHtml(alertsApi.labelForType(rule.type))}</strong></div><em>${escapeHtml(direction)} ${escapeHtml(rule.threshold)}</em></header>
          <div class="profile-alert-symbol"><strong>${escapeHtml(rule.symbol || "Any token")}</strong><span>${escapeHtml(routeLabel(rule))}</span></div>
          <div class="profile-alert-live"><span>Current</span><strong>${escapeHtml(current)}</strong><em>${escapeHtml(evaluation.note)}</em></div>
          <div class="profile-alert-meta"><span>Stability ${escapeHtml(rule.stabilitySeconds || 0)}s</span><span>${rule.delivery === "preview_only" ? "Preview only" : "Local"}</span></div>
          <footer>
            <label><input class="profile-alert-toggle" type="checkbox" data-rule-id="${escapeHtml(rule.id)}" ${rule.enabled !== false ? "checked" : ""}><span>Enabled</span></label>
            <button class="profile-alert-edit js-alert-draft" type="button" data-rule-id="${escapeHtml(rule.id)}">Edit</button>
            <button class="profile-alert-delete" type="button" data-rule-id="${escapeHtml(rule.id)}">Delete</button>
          </footer>
        </article>`;
    }).join("");
  }

  const alertGrid = document.getElementById("profileAlertGrid");
  if (alertGrid) {
    alertGrid.addEventListener("change", (event) => {
      const toggle = event.target.closest(".profile-alert-toggle");
      if (!toggle) return;
      const rules = alertsApi.readRules();
      const rule = rules.find((item) => item.id === toggle.dataset.ruleId);
      if (!rule) return;
      rule.enabled = toggle.checked;
      rule.updatedAt = new Date().toISOString();
      alertsApi.writeRules(rules);
      alertsApi.logActivity(toggle.checked ? "Alert enabled" : "Alert disabled", `${alertsApi.labelForType(rule.type)} ${rule.symbol || ""}`);
    });
    alertGrid.addEventListener("click", (event) => {
      const remove = event.target.closest(".profile-alert-delete");
      if (!remove) return;
      const rules = alertsApi.readRules();
      const rule = rules.find((item) => item.id === remove.dataset.ruleId);
      if (!rule || !window.confirm(`Delete ${alertsApi.labelForType(rule.type)} alert for ${rule.symbol || "all tokens"}?`)) return;
      alertsApi.writeRules(rules.filter((item) => item.id !== rule.id));
      alertsApi.logActivity("Alert deleted", `${alertsApi.labelForType(rule.type)} ${rule.symbol || ""}`);
      alertsApi.showToast("Alert template deleted");
    });
    ["profileAlertTypeFilter", "profileAlertStatusFilter", "profileAlertSort"].forEach((id) => {
      document.getElementById(id)?.addEventListener("change", renderAlertRules);
    });
  }

  async function refreshMarketRows() {
    if (!alertGrid || refreshInFlight) return;
    const rules = alertsApi.readRules();
    const routeKeys = Array.from(new Set(rules.map((rule) => rule.routeKey).filter(Boolean)));
    const symbols = Array.from(new Set(rules.filter((rule) => !rule.routeKey).map((rule) => rule.symbol).filter(Boolean)));
    if (!routeKeys.length && !symbols.length) {
      marketRows = [];
      marketRowsStatus = "ready";
      renderAlertRules();
      return;
    }
    refreshInFlight = true;
    marketRowsStatus = marketRows.length ? "refreshing" : "loading";
    renderAlertRules();
    const params = new URLSearchParams();
    routeKeys.forEach((routeKey) => params.append("route_key", routeKey));
    symbols.forEach((symbol) => params.append("symbol", symbol));
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(`/api/alert-context?${params.toString()}`, {signal: controller.signal});
      if (!response.ok) throw new Error(`Alert context failed: ${response.status}`);
      const data = await response.json();
      marketRows = Array.isArray(data.rows) ? data.rows : [];
      marketRowsStatus = data.ok === false ? "unavailable" : "ready";
    } catch (error) {
      if (!marketRows.length) marketRowsStatus = "unavailable";
    } finally {
      window.clearTimeout(timeout);
      refreshInFlight = false;
    }
    renderAlertRules();
  }

  window.addEventListener("spreadboard:alerts-changed", () => {
    refreshProfileSummary();
    renderAlertRules();
    renderActivity();
    refreshMarketRows();
  });

  hydratePreferences();
  refreshProfileSummary();
  renderActivity();
  renderAlertRules();
  refreshMarketRows();
  if (alertGrid) window.setInterval(refreshMarketRows, 30000);
})();
</script>
"""


WATCHLIST_SCRIPT = """
<script>
(() => {
  const storageKey = "spreadboard.watchlist.v1";
  const dataEl = document.getElementById("watchlistData");
  const data = JSON.parse((dataEl && dataEl.textContent) || "{}");
  const hotSymbols = Array.isArray(data.hot_symbols) ? data.hot_symbols : [];
  const routeReality = Array.isArray(data.route_reality) ? data.route_reality : [];
  const alertCards = Array.isArray((data.alert_preview || {}).cards) ? data.alert_preview.cards : [];
  const profileWatchlist = Array.isArray((data.profile_shell || {}).watchlist) ? data.profile_shell.watchlist : [];
  const routeBySymbol = new Map(routeReality.map((item) => [normaliseSymbol(item.symbol), item]).filter(([symbol]) => symbol));

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\\"": "&quot;",
      "'": "&#39;"
    }[char]));
  }

  function normaliseSymbol(value) {
    return String(value ?? "").toUpperCase().replace(/[^A-Z0-9_-]/g, "").slice(0, 24);
  }

  function labelText(value) {
    const raw = String(value ?? "").trim();
    if (!raw) return "";
    const spaced = raw.replace(/[_-]+/g, " ");
    return spaced.charAt(0).toUpperCase() + spaced.slice(1);
  }

  function formatPct(value, digits = 1) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(digits)}%` : "?";
  }

  function formatSignedPct(value, digits = 1) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${number.toFixed(digits)}%` : "?";
  }

  function loadTokens() {
    try {
      const parsed = JSON.parse(localStorage.getItem(storageKey) || "[]");
      if (Array.isArray(parsed)) {
        return [...new Set(parsed.map(normaliseSymbol).filter(Boolean))];
      }
    } catch (error) {
      return [];
    }
    return [];
  }

  function saveTokens(tokens) {
    localStorage.setItem(storageKey, JSON.stringify([...new Set(tokens.map(normaliseSymbol).filter(Boolean))]));
  }

  function addToken(symbol) {
    const clean = normaliseSymbol(symbol);
    if (!clean) return;
    const tokens = loadTokens();
    if (!tokens.includes(clean)) tokens.unshift(clean);
    saveTokens(tokens.slice(0, 40));
    renderAll();
  }

  function removeToken(symbol) {
    saveTokens(loadTokens().filter((item) => item !== symbol));
    renderAll();
  }

  function seedTokens() {
    const seeds = [...profileWatchlist, ...hotSymbols.map((item) => item.symbol)].map(normaliseSymbol).filter(Boolean);
    saveTokens([...new Set(seeds)].slice(0, 10));
    renderAll();
  }

  function routeCard(token, reality) {
    const routes = Array.isArray(reality.routes) ? reality.routes : [];
    const routeRows = routes.slice(0, 3).map((route) => `
      <a class="watch-route-link" href="${escapeHtml(route.pair_url || `/token/${token}`)}">
        <span>${escapeHtml(route.kind || "Route")}</span>
        <strong>${formatPct(route.open_spread_pct)}</strong>
        <em>${escapeHtml(labelText(route.freshness || "unknown"))}</em>
      </a>
    `).join("");
    const blockers = Array.isArray(reality.top_blockers) ? reality.top_blockers : [];
    const actions = Array.isArray(reality.next_actions) ? reality.next_actions : [];
    return `
      <article class="watch-route-card">
        <div class="watch-route-head">
          <a href="${escapeHtml((routes[0] || {}).pair_url || `/token/${token}`)}">${escapeHtml(token)}</a>
          <span>${escapeHtml(labelText(reality.status || "watch"))}</span>
        </div>
        <div class="watch-route-links">${routeRows || `<p class="watch-empty compact">No matched board route.</p>`}</div>
        <div class="watch-route-meta">
          <span>Next<strong>${escapeHtml(actions.slice(0, 2).map(labelText).join(", ") || "Watch")}</strong></span>
          <span>OKX DEX<strong>${escapeHtml(labelText(reality.okx_dex_identity || "unknown"))}</strong></span>
        </div>
        <p>${escapeHtml(blockers.slice(0, 3).map(labelText).join(", ") || "No blocker details in local intel.")}</p>
      </article>
    `;
  }

  function renderTokens(tokens) {
    const target = document.getElementById("watchItems");
    if (!target) return;
    if (!tokens.length) {
      target.innerHTML = `<p class="watch-empty">No pinned tokens yet. Add a symbol or seed from the current hot list.</p>`;
      return;
    }
    target.innerHTML = tokens.map((token) => {
      const hot = hotSymbols.find((item) => normaliseSymbol(item.symbol) === token) || {};
      const best = hot.best_board || {};
      return `
        <article class="watch-token-card">
          <div><strong>${escapeHtml(token)}</strong><span>${escapeHtml(hot.event_count || 0)} msgs</span></div>
          <p>${escapeHtml(best.route_line || "No matched board route in this window.")}</p>
          <div class="watch-token-metrics">
            <span>Open<strong>${formatPct(best.open_spread_pct)}</strong></span>
            <span>Funding<strong>${formatSignedPct(best.funding_apr_pct, 0)}</strong></span>
            <span>Score<strong>${escapeHtml(hot.score || "?")}</strong></span>
          </div>
          <button class="watch-remove" type="button" data-remove-symbol="${escapeHtml(token)}" aria-label="Remove ${escapeHtml(token)}">Remove</button>
        </article>
      `;
    }).join("");
  }

  function renderRoutes(tokens) {
    const target = document.getElementById("watchRoutes");
    if (!target) return;
    if (!tokens.length) {
      target.innerHTML = `<p class="watch-empty">Pinned routes will appear here after you add tokens.</p>`;
      return;
    }
    const cards = tokens.map((token) => {
      const reality = routeBySymbol.get(token) || {symbol: token, routes: [], status: "telegram_only"};
      return routeCard(token, reality);
    });
    target.innerHTML = cards.join("");
  }

  function alertExampleSymbol(example) {
    return normaliseSymbol(example.symbol || example.source || example.key || example.title || "");
  }

  function renderAlerts(tokens) {
    const target = document.getElementById("watchAlerts");
    if (!target) return;
    if (!tokens.length) {
      target.innerHTML = `<p class="watch-empty">Preview examples will match against pinned tokens.</p>`;
      return;
    }
    const rows = [];
    for (const card of alertCards) {
      const examples = Array.isArray(card.examples) ? card.examples : [];
      const matching = examples.filter((example) => card.key === "source_freshness" || tokens.includes(alertExampleSymbol(example)));
      for (const example of matching.slice(0, 4)) {
        const symbol = alertExampleSymbol(example) || example.source || card.title || "Source";
        const value = example.open_spread_pct ?? example.funding_apr_pct ?? example.funding_delta_pct;
        rows.push(`
          <article class="watch-alert-card ${card.would_trigger ? "on" : "off"}">
            <span>${escapeHtml(card.title || card.key)}</span>
            <strong>${escapeHtml(symbol)}</strong>
            <em>${Number.isFinite(Number(value)) ? formatSignedPct(value) : escapeHtml(labelText(example.status || example.kind || ""))}</em>
          </article>
        `);
      }
    }
    target.innerHTML = rows.join("") || `<p class="watch-empty">No preview triggers match the pinned tokens right now.</p>`;
  }

  function renderAll() {
    const tokens = loadTokens();
    renderTokens(tokens);
    renderRoutes(tokens);
    renderAlerts(tokens);
  }

  document.getElementById("watchForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.getElementById("watchInput");
    addToken(input?.value);
    if (input) input.value = "";
  });
  document.getElementById("seedWatchlist")?.addEventListener("click", seedTokens);
  document.getElementById("clearWatchlist")?.addEventListener("click", () => {
    saveTokens([]);
    renderAll();
  });
  document.addEventListener("click", (event) => {
    const suggestion = event.target.closest("[data-watch-symbol]");
    if (suggestion) addToken(suggestion.getAttribute("data-watch-symbol"));
    const remove = event.target.closest("[data-remove-symbol]");
    if (remove) removeToken(remove.getAttribute("data-remove-symbol"));
  });

  renderAll();
})();
</script>
"""


def render_source_health(health: dict[str, Any]) -> str:
    tabs = health.get("tabs") or []
    return f"""
    <section class="source-line">
      <div class="source-path">
        <span>Source</span>
        <strong>{h(health.get('source_path'))}</strong>
      </div>
      <div class="health-pills">
        {''.join(render_health_pill(item) for item in tabs)}
      </div>
    </section>
    """


def render_health_pill(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "empty")
    return (
        f'<span class="health {h(status)}" title="{h(item.get("detail"))}">'
        f'<b>{h(item.get("label"))}</b><em>{h(status)}</em>'
        f'</span>'
    )


def render_kind_tab(
    item: board.RouteKind,
    selected_kind: str,
    query: dict[str, list[str]],
    health: dict[str, Any],
) -> str:
    tab_health = next((tab for tab in health.get("tabs") or [] if tab.get("kind") == item.kind), {})
    row_count = tab_health.get("fresh_row_count") or 0
    href = "/arbitrage?" + urlencode(_query_with(query, kind=item.kind, include_stale=None))
    active = " active" if selected_kind == item.kind or (not selected_kind and item.kind == "FUTURES") else ""
    return (
        f'<a class="tab-button{active}" href="{h(href)}" title="{h(item.label)}: {h(row_count)} fresh">'
        f'<span>{h(route_tab_label(item))}</span>'
        f'</a>'
    )


def route_tab_label(item: board.RouteKind) -> str:
    return {
        "FUTURES": "Futures",
        "SPOT": "Spot",
        "DEX-FUTURES": "Futures-Dex",
        "DEX-SPOT": "Spot-Dex",
    }.get(item.kind, item.label)


def render_filters(query: dict[str, list[str]]) -> str:
    return f"""
    <form class="filter-sheet" method="get" action="/arbitrage">
      <input type="hidden" name="kind" value="{h(_query_first(query, "kind") or "")}">
      <label><span>Search</span><input name="q" value="{h(_query_first(query, "q") or "")}" placeholder="Token"></label>
      <label><span>Exchange</span><input name="exchange" value="{h(_query_first(query, "exchange") or "")}" placeholder="MEXC, Gate, OKX"></label>
      <label><span>Min Open</span><input name="min_open_spread_pct" value="{h(_query_first(query, "min_open_spread_pct") or "")}" placeholder="2"></label>
      <label><span>Fresh</span><input name="max_age_min" value="{h(_query_first(query, "max_age_min") or str(int(board.DEFAULT_FRESH_MAX_AGE_MIN)))}"></label>
      <button class="sheet-button primary" type="submit">Apply</button>
      <a class="sheet-button" href="/arbitrage?kind=FUTURES">Reset</a>
    </form>
    """


def render_stale_toggle(query: dict[str, list[str]], include_stale: bool) -> str:
    params = _query_with(query, include_stale=None if include_stale else "1")
    href = "/arbitrage?" + urlencode(params)
    label = "Hide stale rows" if include_stale else "Show stale rows"
    return f'<a class="meta-link" href="{h(href)}">{h(label)}</a>'


def render_intel_source_grid(source: dict[str, Any]) -> str:
    order = [
        ("telegram_events", "Telegram"),
        ("board", "Board"),
        ("topic_brief", "Brief"),
        ("preflight_candidates", "Preflight"),
        ("strategy_prompts", "Prompts"),
        ("private_preflight", "Private"),
        ("website_digest", "Website"),
    ]
    cards = []
    for key, label in order:
        item = source.get(key) if isinstance(source.get(key), dict) else {}
        cards.append(
            f'<article class="source-card {h(item.get("status") or "missing")}">'
            f'<span>{h(label)}</span>'
            f'<strong>{h(item.get("status") or "missing")}</strong>'
            f'<em>{fmt_age(item.get("age_min"))}</em>'
            f'</article>'
        )
    return f'<section class="intel-source-grid">{"".join(cards)}</section>'


def render_change_digest(digest: dict[str, Any]) -> str:
    counts = digest.get("counts") if isinstance(digest.get("counts"), dict) else {}
    new_symbols = digest.get("new_symbols") or []
    source_gaps = digest.get("source_gaps") or []
    highlights = digest.get("highlights") or []
    count_cards = [
        ("Events", h(digest.get("recent_event_count") or 0), f"last {h(int(_float_or_none(digest.get('window_min')) or 60))} min"),
        ("New", h(digest.get("new_symbol_count") or 0), label_list(new_symbols[:4]) or "no new symbols"),
        ("Alerts", h(counts.get("alerts") or 0), f"{h(counts.get('closes') or 0)} closes"),
        ("Funding", h(counts.get("funding") or 0), f"{h(counts.get('community') or 0)} community"),
        ("Sources", h(digest.get("source_gap_count") or 0), label_list([item.get("source") for item in source_gaps[:2]]) or "healthy"),
    ]
    highlights_html = []
    for item in highlights[:4]:
        value = item.get("spread_pct") if item.get("spread_pct") is not None else item.get("funding_delta_pct")
        highlights_html.append(
            f"""
            <a class="change-highlight" href="{h(item.get('href') or '/signals')}">
              <span>{label_text(item.get('event'))}</span>
              <strong>{h(item.get('symbol') or '?')}</strong>
              <em>{fmt_age(item.get('age_min'))} · {h(item.get('kind') or '?')} · {fmt_signed_pct(value)}</em>
            </a>
            """
        )
    return f"""
    <section class="change-digest" aria-label="What changed">
      <div class="panel-head flat">
        <div>
          <h2>What Changed</h2>
          <p>Last-hour Telegram/source changes, so returning users can see what moved before opening the full feed.</p>
        </div>
        <span>{label_text(digest.get('status') or 'quiet')}</span>
      </div>
      <div class="change-counts">
        {''.join(f'<article><span>{title}</span><strong>{value}</strong><em>{h(note)}</em></article>' for title, value, note in count_cards)}
      </div>
      <div class="change-highlights">
        {''.join(highlights_html) or '<p class="empty">No fresh changes in the last hour.</p>'}
      </div>
    </section>
    """


def render_hot_symbol(item: dict[str, Any]) -> str:
    best = item.get("best_board") or {}
    route = best.get("route_line") or "No matched board route"
    tags = []
    for kind, count in (item.get("kinds") or {}).items():
        tags.append(f"<span>{h(kind)} {h(count)}</span>")
    if item.get("chains"):
        tags.append(f"<span>{h(', '.join(item.get('chains') or []))}</span>")
    if item.get("contract_count"):
        tags.append(f"<span>{h(item.get('contract_count'))} contract</span>")
    if item.get("lead_analyst_count"):
        tags.append(f"<span>Lead analyst {h(item.get('lead_analyst_count'))}</span>")
    href = best.get("pair_url") or f"/token/{h(item.get('symbol'))}"
    return f"""
    <article class="hot-card">
      <a class="hot-head" href="{h(href)}">
        <strong>{h(item.get('symbol'))}</strong>
        <span>{h(item.get('event_count'))} msgs</span>
      </a>
      <div class="hot-score">
        <b>{fmt_pct(best.get('open_spread_pct'))}</b>
        <em>{fmt_signed_pct(best.get('funding_24h_pct') if best.get('funding_24h_pct') is not None else ((_float_or_none(best.get('funding_apr_pct')) or 0.0) / 365.0), digits=3)} funding 24h</em>
      </div>
      <p>{h(route)}</p>
      <div class="tag-row">{''.join(tags) or '<span>telegram only</span>'}</div>
      <div class="mini-kv">
        <span>Liquidity <strong>{fmt_money(item.get('liquidity_usd'))}</strong></span>
        <span>Volume <strong>{fmt_money(item.get('max_volume_usd'))}</strong></span>
      </div>
    </article>
    """


def render_action_queue(rows: list[dict[str, Any]]) -> str:
    rendered = []
    for index, item in enumerate(rows[:8], start=1):
        blockers = item.get("blockers") or []
        badges = "".join(f"<span>{label_text(badge)}</span>" for badge in (item.get("badges") or [])[:5])
        rendered.append(
            f"""
            <article class="action-row {h(item.get('status') or 'watch')}">
              <div class="action-rank">{index}</div>
              <div class="action-symbol">
                <a href="{h(item.get('href') or item.get('token_href') or '#')}">{h(item.get('symbol'))}</a>
                <span>{label_text(item.get('status'))}</span>
              </div>
              <div class="action-route">
                <strong>{h(item.get('route_line'))}</strong>
                <em>{h(item.get('reason'))}</em>
                <div class="tag-row tight">{badges or '<span>watch</span>'}</div>
              </div>
              <div class="action-metrics">
                <span>Spread <strong>{fmt_pct(item.get('spread_pct'))}</strong></span>
                <span>Funding <strong>{fmt_signed_pct(item.get('funding_24h_pct') if item.get('funding_24h_pct') is not None else ((_float_or_none(item.get('funding_apr_pct')) or 0.0) / 365.0), digits=3)} / 24h</strong></span>
                <span>Fresh <strong>{label_text(item.get('freshness'))}</strong></span>
              </div>
              <div class="action-next">
                <strong>{label_text(item.get('next_action'))}</strong>
                <em>{label_list(blockers[:2]) or 'No blockers found'}</em>
                <div class="action-links">
                  <a href="{h(item.get('href') or item.get('token_href') or '#')}">Open</a>
                  <a href="{h(item.get('board_href') or '/arbitrage?kind=FUTURES')}">Board</a>
                </div>
              </div>
            </article>
            """
        )
    return f"""
    <section class="intel-section action-queue-section">
      <div class="panel-head flat">
        <div>
          <h2>Action Queue</h2>
          <p>Joined from Telegram heat, board routes, preflight context, D/W and funding clues. Read-only: this only tells you what to inspect next.</p>
        </div>
      </div>
      <div class="action-queue">{''.join(rendered) or '<p class="empty">No actionable local intelligence in this window.</p>'}</div>
    </section>
    """


def render_reality_card(item: dict[str, Any]) -> str:
    routes = item.get("routes") or []
    top_route = routes[0] if routes else {}
    route_rows = []
    for route in routes[:3]:
        route_rows.append(
            f'<a class="reality-route" href="{h(route.get("pair_url"))}">'
            f'<span>{h(route.get("kind"))}</span>'
            f'<strong>{fmt_pct(route.get("open_spread_pct"))}</strong>'
            f'<em>{h(route.get("freshness"))}</em>'
            f'</a>'
        )
    blockers = item.get("top_blockers") or []
    actions = item.get("next_actions") or []
    fallback_href = f"/token/{h(item.get('symbol'))}"
    return f"""
    <article class="reality-card">
      <div class="reality-head">
        <a href="{h(top_route.get('pair_url') or fallback_href)}">{h(item.get('symbol'))}</a>
        <span>{label_text(item.get('status'))}</span>
      </div>
      <div class="reality-routes">{''.join(route_rows) or '<p class="muted">Telegram-only signal. No current board route matched this symbol.</p>'}</div>
      <div class="reality-meta">
        <span>Next <strong>{label_list(actions[:2]) or 'Watch'}</strong></span>
        <span>Vol <strong>{label_text(item.get('volatility'))}</strong></span>
        <span>OKX DEX <strong>{label_text(item.get('okx_dex_identity'))}</strong></span>
      </div>
      <ul class="plain-list compact">{''.join(f'<li>{label_text(blocker)}</li>' for blocker in blockers[:4]) or '<li>No blocker details found.</li>'}</ul>
    </article>
    """


def render_latest_brief(brief: dict[str, Any]) -> str:
    return f"""
    <article class="side-card">
      <div class="side-head"><h2>Latest Brief</h2><span class="{h(brief.get('status') or 'missing')}">{h(brief.get('status') or 'missing')}</span></div>
      <p class="small">{h(brief.get('title'))} · {fmt_age(brief.get('age_min'))}</p>
      <div class="brief-body">{brief_excerpt(brief.get('body'))}</div>
    </article>
    """


def render_questions(patterns: list[dict[str, Any]]) -> str:
    rows = []
    for item in patterns[:7]:
        example = (item.get("examples") or [{}])[0]
        rows.append(
            f'<li><strong>{h(item.get("category"))}</strong><span>{h(item.get("count"))}</span>'
            f'<em>{h(example.get("text_excerpt") or example.get("first_line"))}</em></li>'
        )
    return f"""
    <article class="side-card">
      <div class="side-head"><h2>People Are Asking</h2><span>{h(len(patterns))}</span></div>
      <ul class="question-list">{''.join(rows) or '<li><strong>No question clusters</strong><span>0</span><em>Nothing matched in this window.</em></li>'}</ul>
    </article>
    """


def render_alert_preview(preview: dict[str, Any]) -> str:
    cards = []
    for card in preview.get("cards") or []:
        status = card.get("status") or ("would_trigger" if card.get("would_trigger") else "quiet")
        status_label = (
            "would trigger"
            if card.get("would_trigger")
            else "review stale"
            if status == "review_only"
            else "quiet"
        )
        cards.append(
            f'<div class="alert-preview-row {h(status)}">'
            f'<span>{h(card.get("title"))}</span>'
            f'<strong>{h(status_label)}</strong>'
            f'</div>'
        )
    return f"""
    <article class="side-card">
      <div class="side-head"><h2>Alert Preview</h2><span>{h(preview.get('would_trigger_count') or 0)}</span></div>
      <p class="small">Preview only. No Pushover message is sent from this panel.</p>
      <div class="alert-preview-list">{''.join(cards)}</div>
    </article>
    """


def render_alert_rule_card(card: dict[str, Any]) -> str:
    examples = card.get("examples") if isinstance(card.get("examples"), list) else []
    status = card.get("status") or ("would_trigger" if card.get("would_trigger") else "quiet")
    status_text = (
        "would trigger now"
        if card.get("would_trigger")
        else "review stale context"
        if status == "review_only"
        else "quiet now"
    )
    review_note = (
        f'<span>{h(card.get("review_count") or 0)} stale examples kept for review</span>'
        if status == "review_only" or card.get("review_count")
        else ""
    )
    return f"""
    <article class="alert-rule-card {h(status)}">
      <div class="alert-rule-head">
        <span>{label_text(card.get('key'))}</span>
        <strong>{h(card.get('title'))}</strong>
        <em>{h(status_text)}</em>
      </div>
      <div class="alert-review-note">{review_note}</div>
      <div class="alert-example-list">
        {''.join(render_alert_example(item) for item in examples[:4]) or '<p class="muted">No matching examples in the current local window.</p>'}
      </div>
    </article>
    """


def render_alert_example(item: Any) -> str:
    if not isinstance(item, dict):
        return f'<div class="alert-example"><span>Example</span><strong>{h(item)}</strong></div>'
    symbol = item.get("symbol") or item.get("source") or item.get("key") or item.get("title") or "Source"
    freshness = str(item.get("freshness") or item.get("status") or "").casefold()
    value = (
        item.get("open_spread_pct")
        if item.get("open_spread_pct") is not None
        else item.get("funding_apr_pct")
        if item.get("funding_apr_pct") is not None
        else item.get("funding_delta_pct")
    )
    note = (
        item.get("route_line")
        or item.get("status")
        or item.get("first_line")
        or item.get("text_excerpt")
        or item.get("kind")
        or item.get("source")
        or ""
    )
    age = fmt_age(item.get("age_min")) if item.get("age_min") is not None else ""
    meta = " · ".join(part for part in [label_text(note), label_text(freshness) if freshness else "", age] if part)
    return (
        f'<div class="alert-example {h(freshness)}">'
        f'<span>{h(symbol)}</span>'
        f'<strong>{fmt_signed_pct(value) if value is not None else label_text(note)}</strong>'
        f'<em>{h(meta) if value is not None else h(label_text(freshness) or age)}</em>'
        '</div>'
    )


def render_alert_template(title: str, body: str) -> str:
    return f"""
    <article class="alert-template">
      <strong>{h(title)}</strong>
      <p>{h(body)}</p>
      <span>Profile template</span>
    </article>
    """


def render_profile_shell(profile: dict[str, Any]) -> str:
    sections = []
    for item in profile.get("sections") or []:
        sections.append(
            f'<div class="profile-row"><span>{h(item.get("label"))}</span><strong>{h(item.get("status"))}</strong><em>{h(item.get("count"))}</em></div>'
        )
    watchlist = ", ".join(profile.get("watchlist") or []) or "No pinned tokens yet"
    return f"""
    <article class="side-card">
      <div class="side-head"><h2>Profile Shell</h2><span>{h(profile.get('status'))}</span></div>
      <p class="small">Local-only placeholder for the later profile and PnL layer.</p>
      <p class="watchlist-line">{h(watchlist)}</p>
      {''.join(sections)}
    </article>
    """


def render_event_column(title: str, rows: list[dict[str, Any]]) -> str:
    items = []
    for row in rows[:8]:
        symbol = row.get("symbol") or "?"
        href = f"/token/{h(symbol)}"
        value = row.get("spread_pct") if row.get("spread_pct") is not None else row.get("funding_delta_pct")
        items.append(
            f'<a class="feed-row" href="{h(href)}">'
            f'<span>{h(symbol)}</span><strong>{h(row.get("kind") or row.get("event"))}</strong>'
            f'<em>{fmt_signed_pct(value)}</em><small>{fmt_age(row.get("age_min"))}</small>'
            f'</a>'
        )
    return f"""
    <article class="feed-card">
      <h3>{h(title)}</h3>
      {''.join(items) or '<p class="muted">No rows in this window.</p>'}
    </article>
    """


def render_signal_lane(title: str, rows: list[dict[str, Any]]) -> str:
    return f"""
    <section class="signal-lane">
      <div class="side-head"><h2>{h(title)}</h2><span>{h(len(rows))}</span></div>
      <div class="signal-list">{''.join(render_signal_event({**item, 'bucket': title}) for item in rows) or '<p class="empty">No rows in this window.</p>'}</div>
    </section>
    """


def render_funding_watch_card(item: dict[str, Any]) -> str:
    funding_value = item.get("funding_apr_pct") if item.get("funding_apr_pct") is not None else item.get("funding_delta_pct")
    label = "APR" if item.get("funding_apr_pct") is not None else "delta"
    note = (
        f"Funding soon: {h(item.get('minutes_to_funding'))} min"
        if item.get("minutes_to_funding") is not None
        else "Board or Telegram funding signal."
    )
    return f"""
    <article class="funding-card">
      <div class="hot-head">
        <strong>{h(item.get('symbol'))}</strong>
        <span>{h(item.get('source'))}</span>
      </div>
      <div class="funding-value {spread_class(funding_value)}">{fmt_signed_pct(funding_value)}</div>
      <div class="mini-kv">
        <span>{h(label)}<strong>{fmt_signed_pct(funding_value)}</strong></span>
        <span>Open<strong>{fmt_pct(item.get('open_spread_pct'))}</strong></span>
        <span>Route<strong>{h(item.get('kind'))}</strong></span>
        <span>Age<strong>{fmt_age(item.get('age_min'))}</strong></span>
      </div>
      <p class="plain">{note}</p>
    </article>
    """


def render_community_scoreboard(scoreboard: dict[str, Any]) -> str:
    positive = scoreboard.get("top_positive") or []
    negative = scoreboard.get("net_negative") or []
    return f"""
    <section class="community-panel scoreboard-panel">
      <div class="panel-head flat">
        <div><h2>Community Scoreboard</h2><p>Reported winners and distrust rows parsed from the latest local topic brief.</p></div>
        <span class="status-pill {h(scoreboard.get('status') or 'missing')}">{label_text(scoreboard.get('status') or 'missing')}</span>
      </div>
      <div class="scoreboard-grid">
        <article class="score-lane positive">
          <h3>Top Reported Wins</h3>
          <div class="score-list">{''.join(render_score_row(item) for item in positive[:8]) or '<p class="muted">No positive scoreboard rows found.</p>'}</div>
        </article>
        <article class="score-lane negative">
          <h3>Distrust / Net Negative</h3>
          <div class="score-list">{''.join(render_score_row(item) for item in negative[:8]) or '<p class="muted">No negative scoreboard rows found.</p>'}</div>
        </article>
      </div>
    </section>
    """


def render_score_row(item: dict[str, Any]) -> str:
    value = _float_or_none(item.get("reported_pnl"))
    cls = "positive" if (value or 0) >= 0 else "negative"
    return (
        f'<div class="score-row {cls}">'
        f'<strong>{h(item.get("symbol"))}</strong>'
        f'<span>{fmt_signed_number(value)}</span>'
        f'<em>{label_text(item.get("sentiment"))}</em>'
        "</div>"
    )


def render_community_discussion(rows: list[dict[str, Any]]) -> str:
    cards = []
    for item in rows[:12]:
        href = f"/token/{h(item.get('symbol'))}"
        cards.append(
            f'<article class="discussion-card">'
            f'<a class="hot-head" href="{h(href)}"><strong>{h(item.get("symbol"))}</strong><span>{h(item.get("message_count"))} msgs</span></a>'
            f'<p>{h(item.get("reason"))}</p>'
            f'<div class="mini-kv">'
            f'<span>Calls<strong>{h(item.get("call_count") or 0)}</strong></span>'
            f'<span>Results<strong>{h(item.get("result_count") or 0)}</strong></span>'
            f'<span>Route<strong>{label_text(item.get("route_status"))}</strong></span>'
            f'<span>Next<strong>{label_text(item.get("next_action"))}</strong></span>'
            f'</div>'
            f'</article>'
        )
    return f"""
    <section class="community-panel">
      <div class="panel-head flat"><div><h2>Active Discussion</h2><p>Why a token is being talked about, with route status and next useful action.</p></div></div>
      <div class="discussion-grid">{''.join(cards) or '<p class="empty">No active discussion rows in this window.</p>'}</div>
    </section>
    """


def render_community_call_ledger(rows: list[dict[str, Any]]) -> str:
    rendered = []
    for item in rows[:10]:
        badges = "".join(f"<span>{label_text(badge)}</span>" for badge in (item.get("badges") or [])[:5])
        latest_call = item.get("latest_call") if isinstance(item.get("latest_call"), dict) else {}
        latest_result = item.get("latest_result") if isinstance(item.get("latest_result"), dict) else {}
        result_text = latest_result.get("first_line") or latest_result.get("text_excerpt") or "No result row yet"
        call_text = latest_call.get("first_line") or latest_call.get("text_excerpt")
        if not call_text:
            call_text = (
                "Close/fade activity from signal tape"
                if item.get("close_count")
                else "Community activity only"
            )
        rendered.append(
            f"""
            <article class="call-ledger-row {h(item.get('status') or 'watch')}">
              <div class="call-ledger-symbol">
                <a href="{h(item.get('href') or '#')}">{h(item.get('symbol'))}</a>
                <span>{label_text(item.get('status'))}</span>
              </div>
              <div class="call-ledger-story">
                <strong>{h(call_text)}</strong>
                <em>{h(result_text)}</em>
                <div class="tag-row tight">{badges or '<span>watch</span>'}</div>
              </div>
              <div class="call-ledger-route">
                <span>Route <strong>{label_text(item.get('route_status'))}</strong></span>
                <span>Spread <strong>{fmt_pct(item.get('spread_pct'))}</strong></span>
                <span>Funding <strong>{fmt_signed_pct(item.get('funding_apr_pct'), digits=0)} APR</strong></span>
                <span>Fresh <strong>{label_text(item.get('freshness'))}</strong></span>
              </div>
              <div class="call-ledger-next">
                <strong>{label_text(item.get('next_action'))}</strong>
                <em>{h(item.get('call_count') or 0)} calls · {h(item.get('result_count') or 0)} results · {h(item.get('close_count') or 0)} closes</em>
                <div class="action-links">
                  <a href="{h(item.get('href') or '#')}">Open</a>
                  <a href="{h(item.get('signals_href') or '/signals')}">Signals</a>
                </div>
              </div>
            </article>
            """
        )
    return f"""
    <section class="community-panel call-ledger-panel">
      <div class="panel-head flat">
        <div>
          <h2>Call / Outcome Ledger</h2>
          <p>Community calls joined to result rows, close/fade signals, matched board routes, and the next read-only check.</p>
        </div>
        <span>{h(len(rows))}</span>
      </div>
      <div class="call-ledger-list">{''.join(rendered) or '<p class="empty">No call lifecycle rows in this window.</p>'}</div>
    </section>
    """


def render_community_event_group(title: str, rows: list[dict[str, Any]], bucket: str) -> str:
    return f"""
    <section class="community-panel">
      <div class="panel-head flat"><div><h2>{h(title)}</h2><p>{h(len(rows))} local rows in the selected window.</p></div></div>
      <div class="signal-list">{''.join(render_signal_event({**item, 'bucket': bucket}) for item in rows[:12]) or '<p class="empty">No rows in this window.</p>'}</div>
    </section>
    """


def render_community_brief(insights: dict[str, Any]) -> str:
    lines = insights.get("brief_excerpt") or []
    return f"""
    <article class="side-card">
      <div class="side-head"><h2>Brief Excerpt</h2><span>{label_text((insights.get('scoreboard') or {}).get('status') or 'missing')}</span></div>
      <div class="brief-body">{''.join(f'<p>{h(line)}</p>' for line in lines) or '<p class="muted">No brief excerpt available.</p>'}</div>
    </article>
    """


def brief_excerpt(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return '<p class="muted">No brief body available.</p>'
    first = raw.splitlines()[0:6]
    return "".join(f"<p>{h(line)}</p>" for line in first if line.strip())


def render_board_row(row: dict[str, Any]) -> str:
    pair_url = row.get("pair_url")
    return f"""
    <a class="arb-grid-futures-main arb-result-row" href="{h(pair_url)}">
      <div class="token-column">
        <div class="token-topline">
          <strong title="{h(row.get('symbol'))}">{h(row.get('symbol'))}</strong>
          <span>{compact_spread_badge(row)}</span>
        </div>
        <div class="token-actions" aria-hidden="true"><i></i><i></i><i></i></div>
      </div>
      <div class="market-column">{render_leg_compact(row, "long")}{render_leg_compact(row, "short")}</div>
      <div class="funding-column">{render_funding_lines(row)}</div>
      <div class="metric-column">{fmt_metric(row.get('daily_pct'))}</div>
      <div class="metric-column">{fmt_metric(row.get('seven_day_pct'))}</div>
      <div class="metric-column">{fmt_metric(row.get('thirty_day_pct'))}</div>
      <div class="dw-status">{render_dw(row)}<span class="age-line">{fmt_age(row.get('age_min'))}</span></div>
      <div><span class="value-chip {signed_chip_class(row.get('depth_weighted_spread_pct'))}">{fmt_signed_pct(row.get('depth_weighted_spread_pct'))}</span></div>
      <div><span class="value-chip neutral">{fmt_signed_pct(funding_24h_value(row), digits=3)}</span></div>
      <div><span class="value-chip {spread_class(row.get('displayed_open_spread_pct') or row.get('spread_pct'))}">{fmt_pct(row.get('displayed_open_spread_pct') or row.get('spread_pct'))}</span></div>
    </a>
    """


def render_board_mobile_card(row: dict[str, Any]) -> str:
    pair_url = row.get("pair_url")
    open_spread = row.get("displayed_open_spread_pct") if row.get("displayed_open_spread_pct") is not None else row.get("spread_pct")
    route_kind = row.get("kind_label") or row.get("kind")
    return f"""
    <a class="mobile-board-card" href="{h(pair_url)}">
      <div class="mobile-board-head">
        <div>
          <span>{h(route_kind)}</span>
          <strong title="{h(row.get('symbol'))}">{h(row.get('symbol'))}</strong>
        </div>
        <b class="{spread_class(open_spread)}">{fmt_pct(open_spread)}</b>
      </div>
      <div class="mobile-leg-stack">
        {render_mobile_leg(row, "long", "Buy")}
        {render_mobile_leg(row, "short", "Sell")}
      </div>
      <div class="mobile-metric-grid">
        <span>Executable<strong>{fmt_pct(row.get('spread_pct'))}</strong></span>
        <span>F spread<strong>{fmt_signed_pct(row.get('funding_spread_pct'))}</strong></span>
        <span>Funding 24h<strong>{fmt_signed_pct(funding_24h_value(row), digits=3)}</strong></span>
        <span>Depth<strong>{fmt_money(row.get('depth_usd'))}</strong></span>
      </div>
      <div class="mobile-board-footer">
        <span>{fmt_age(row.get('age_min'))} old</span>
        <span>D/W {mobile_dw_summary(row)}</span>
        <em>Open pair</em>
      </div>
    </a>
    """


def render_mobile_leg(row: dict[str, Any], side: str, label: str) -> str:
    side_class = "buy" if side == "long" else "sell"
    venue = row.get(f"{side}_venue") or "?"
    market = row.get(f"{side}_market_type") or "?"
    price = row.get(f"{side}_price")
    depth = row.get(f"{side}_depth_usd")
    funding = row.get(f"{side}_funding_pct")
    return f"""
    <div class="mobile-leg {h(side_class)}">
      <span>{h(label)}</span>
      <strong>{h(venue)}</strong>
      <em>{h(market)} {fmt_price(price)}</em>
      <b>{fmt_money(depth)}</b>
      <small>{fmt_signed_pct(funding, digits=2)} funding</small>
    </div>
    """


def mobile_dw_summary(row: dict[str, Any]) -> str:
    buy = f"{status_char(row.get('long_deposit_enabled'))}/{status_char(row.get('long_withdraw_enabled'))}"
    sell = f"{status_char(row.get('short_deposit_enabled'))}/{status_char(row.get('short_withdraw_enabled'))}"
    return f"buy {buy}, sell {sell}"


def compact_spread_badge(row: dict[str, Any]) -> str:
    value = _float_or_none(row.get("displayed_headline_spread_pct"))
    if value is None:
        value = _float_or_none(row.get("displayed_open_spread_pct") or row.get("spread_pct"))
    if value is None:
        return "+?"
    return f"+{value:.0f}"


def render_funding_lines(row: dict[str, Any]) -> str:
    return (
        f'<span>{fmt_signed_pct(row.get("long_funding_pct"), digits=2)} <em>8 h</em></span>'
        f'<span>{fmt_signed_pct(row.get("short_funding_pct"), digits=2)} <em>8 h</em></span>'
    )


def fmt_metric(value: Any) -> str:
    number = _float_or_none(value)
    if number is None:
        return "-"
    return f"{number:.2f}"


def signed_chip_class(value: Any) -> str:
    number = _float_or_none(value)
    if number is None:
        return "neutral"
    if number > 0:
        return "positive"
    if number < 0:
        return "negative"
    return "neutral"


def dw_dot(value: bool | None) -> str:
    if value is True:
        state = "open"
    elif value is False:
        state = "closed"
    else:
        state = "unknown"
    return f'<i class="dw-dot {state}"></i>'


def render_leg_compact(row: dict[str, Any], side: str) -> str:
    side_class = "buy" if side == "long" else "sell"
    return (
        f'<div class="market-leg {h(side_class)}">'
        f'<span class="direction-dot" aria-hidden="true"></span>'
        f'<span class="venue-dot" aria-hidden="true"></span>'
        f'{render_exchange_link(row, side)}'
        f'<em>{fmt_money(row.get(f"{side}_depth_usd"))}</em>'
        f'<b>{fmt_price(row.get(f"{side}_price"))}</b>'
        f'</div>'
    )


def render_exchange_link(
    row: dict[str, Any],
    side: str,
    *,
    include_market_type: bool = False,
) -> str:
    venue = row.get(f"{side}_venue")
    market_type = row.get(f"{side}_market_type")
    # An on-chain swap is a spot trade, so a DEX leg carries market_type
    # "Spot" -- and printing it raw made every DEX farm read as a Futures-Spot
    # one. The route key keeps the raw value; only what a member reads changes.
    shown_type = leg_market_label(venue, market_type)
    label = (
        f"{venue or '?'} · {shown_type or '?'}"
        if include_market_type
        else str(venue or "?")
    )
    return render_venue_link(
        label,
        market_type,
        row.get(f"{side}_exchange_url"),
    )


def render_venue_link(
    venue: Any,
    market_type: Any,
    exchange_url: Any,
) -> str:
    label = str(venue or "?")
    url = str(exchange_url or "").strip()
    if not url.startswith("https://"):
        return f"<strong>{h(label)}</strong>"
    title = f"Open {label} {market_type or 'market'} chart"
    return (
        f'<a class="exchange-market-link" href="{h(url)}" target="_blank" '
        f'rel="noopener noreferrer" title="{h(title)}">'
        f"<strong>{h(label)}</strong><span aria-hidden=\"true\">&#8599;</span></a>"
    )


def render_dw(row: dict[str, Any]) -> str:
    return (
        '<span class="dw-row">'
        f'{dw_dot(row.get("long_deposit_enabled"))}{dw_dot(row.get("long_withdraw_enabled"))}'
        '</span>'
        '<span class="dw-row">'
        f'{dw_dot(row.get("short_deposit_enabled"))}{dw_dot(row.get("short_withdraw_enabled"))}'
        '</span>'
    )


def render_route_card(row: dict[str, Any]) -> str:
    return (
        f'<a class="route-card" href="{h(row.get("pair_url"))}">'
        f'<strong>{h(row.get("kind_label"))}</strong>'
        f'<span>{h(row.get("route_line"))}</span>'
        f'<b>{fmt_pct(row.get("spread_pct"))}</b>'
        f'</a>'
    )


def metric_card(title: str, value: str, note: str) -> str:
    return f'<article class="metric"><span>{h(title)}</span><strong>{value}</strong><small>{h(note)}</small></article>'


def render_pair_route_diagram(row: dict[str, Any], legs: dict[str, Any]) -> str:
    long_leg = legs.get("long") or {}
    short_leg = legs.get("short") or {}
    return f"""
    <div class="pair-diagram" aria-label="Route legs">
      <div class="pair-leg-pill buy">
        <span>Buy</span>
        {render_venue_link(long_leg.get('venue') or row.get('long_venue'), long_leg.get('market_type') or row.get('long_market_type'), long_leg.get('exchange_url') or row.get('long_exchange_url'))}
        <em>{h(long_leg.get('market_type') or row.get('long_market_type'))} {fmt_price(long_leg.get('price') or row.get('long_price'))}</em>
      </div>
      <div class="pair-connector"><span></span><b>spread</b><span></span></div>
      <div class="pair-leg-pill sell">
        <span>Sell</span>
        {render_venue_link(short_leg.get('venue') or row.get('short_venue'), short_leg.get('market_type') or row.get('short_market_type'), short_leg.get('exchange_url') or row.get('short_exchange_url'))}
        <em>{h(short_leg.get('market_type') or row.get('short_market_type'))} {fmt_price(short_leg.get('price') or row.get('short_price'))}</em>
      </div>
    </div>
    """


def render_pair_spread_badge(row: dict[str, Any], detail: dict[str, Any]) -> str:
    open_spread = row.get("displayed_open_spread_pct") if row.get("displayed_open_spread_pct") is not None else row.get("spread_pct")
    health = detail.get("route_health") or {}
    verdict = str(health.get("verdict") or "watch_only")
    return f"""
    <aside class="pair-score-card">
      <span>Open spread</span>
      <strong class="{spread_class(open_spread)}">{fmt_pct(open_spread)}</strong>
      <div class="score-meta">
        <em>{label_text(verdict)}</em>
        <em>{fmt_age(row.get('age_min'))} old</em>
      </div>
    </aside>
    """


def render_pair_cockpit(
    row: dict[str, Any],
    detail: dict[str, Any],
    pair_intel: dict[str, Any],
    history: list[dict[str, Any]],
) -> str:
    legs = detail.get("legs") or {}
    long_leg = legs.get("long") or {}
    short_leg = legs.get("short") or {}
    health = detail.get("route_health") or {}
    blockers = health.get("blockers") or []
    okx_quote = detail.get("okx_dex_quote") or {}
    events = pair_signal_events(pair_intel.get("recent_events") or {})
    open_spread = row.get("displayed_open_spread_pct") if row.get("displayed_open_spread_pct") is not None else row.get("spread_pct")
    executable = row.get("spread_pct")
    width = spread_width(open_spread if open_spread is not None else executable)
    direction = "positive" if (_float_or_none(open_spread) or _float_or_none(executable) or 0) >= 0 else "negative"
    age = _float_or_none(row.get("age_min"))
    is_canonical = bool(row.get("canonical_api"))
    fresh_ok = (age or 999999) <= board.DEFAULT_FRESH_MAX_AGE_MIN
    venue_ok = bool(long_leg.get("market_symbol")) and bool(short_leg.get("market_symbol"))
    funding_24h = (detail.get("funding") or {}).get("net_24h_pct")
    funding_ok = funding_24h is not None or row.get("funding_spread_pct") is not None
    dw_applicable = "Spot" in {
        row.get("long_market_type"),
        row.get("short_market_type"),
    }
    dw_ok = any(row.get(key) is not None for key in ("long_deposit_enabled", "long_withdraw_enabled", "short_deposit_enabled", "short_withdraw_enabled"))
    dex_status = str(okx_quote.get("status") or "not_applicable")
    dex_state = "ok" if dex_status in {"not_applicable", "available", "disabled"} or "blocked" not in dex_status else "missing"
    verdict = label_text(health.get("verdict") or "watch_only")
    next_action = label_text(health.get("next_action") or ("monitor_route" if is_canonical else "watch"))
    delta = history_delta(history, "open_spread_pct")
    spread_range = history_range(history, "open_spread_pct")
    trend = pair_spread_trend(open_spread, delta)
    gates = [
        ("Fresh API" if is_canonical else "Fresh row", "ok" if fresh_ok else "missing", "fresh" if fresh_ok else f"{fmt_age(row.get('age_min'))} old"),
        ("Venue symbols", "ok" if venue_ok else "missing", "resolved" if venue_ok else "unresolved"),
        (
            "D/W rails",
            "ok" if (not dw_applicable or dw_ok) else "missing",
            "not applicable" if not dw_applicable else mobile_dw_summary(row) if dw_ok else "not reported",
        ),
        ("Funding", "ok" if funding_ok else "warn", f"{fmt_signed_pct(funding_24h, digits=3)} / 24h" if funding_ok else "not enough data"),
        ("Community", "ok" if events else "warn", f"{len(events)} recent events" if events else "optional; no recent events"),
        ("OKX DEX", dex_state, label_text(dex_status)),
        ("Route monitor" if is_canonical else "Next action", "ok" if not blockers else "missing", next_action),
    ]
    ticket_heading = (
        "Compare basis, carry, and route history."
        if is_canonical
        else "Buy low, sell high, then verify reality."
    )
    return f"""
    <section id="spread" class="pair-cockpit {direction}" style="--spread-width: {width}%">
      <div class="pair-cockpit-head">
        <div class="detail-title">
          <a class="back" href="/arbitrage?kind=FUTURES">Arbitrage</a>
          <span class="page-kicker">Route Cockpit · {h(row.get('kind_label'))}</span>
          <h1>{h(row.get('symbol'))}</h1>
          <div class="route-subline">{h(row.get('route_line'))}</div>
        </div>
        <div class="detail-actions">
          <span class="trade-lock">Read-only</span>
          {render_alert_draft_button(row, alert_type='token_spread')}
          {render_alert_draft_button(row, alert_type='funding')}
          <a class="control-btn ghost" href="/token/{h(row.get('symbol'))}">Token overview</a>
          {render_chart_link(row)}
        </div>
      </div>

      <div class="pair-cockpit-grid">
        <div class="pair-trade-ticket" aria-label="Spread Equation">
          <div class="ticket-head">
            <div>
              <span class="page-kicker">Spread Equation</span>
              <strong>{ticket_heading}</strong>
            </div>
            <span class="read-only-pill">Observation only</span>
          </div>
          <div class="ticket-legs">
            <article class="ticket-leg buy">
              <span>Buy leg</span>
              {render_venue_link(long_leg.get('venue') or row.get('long_venue'), long_leg.get('market_type') or row.get('long_market_type'), long_leg.get('exchange_url') or row.get('long_exchange_url'))}
              <em>{h(long_leg.get('market_type') or row.get('long_market_type'))}</em>
              <b>{fmt_price(long_leg.get('price') or row.get('long_price'))}</b>
              <small>{fmt_money(long_leg.get('depth_usd') or row.get('long_depth_usd'))} visible depth</small>
            </article>
            <div class="ticket-bridge">
              <span></span>
              <b>spread</b>
              <em>{fmt_signed_pct(row.get('funding_spread_pct'), digits=4)} carry</em>
            </div>
            <article class="ticket-leg sell">
              <span>Sell leg</span>
              {render_venue_link(short_leg.get('venue') or row.get('short_venue'), short_leg.get('market_type') or row.get('short_market_type'), short_leg.get('exchange_url') or row.get('short_exchange_url'))}
              <em>{h(short_leg.get('market_type') or row.get('short_market_type'))}</em>
              <b>{fmt_price(short_leg.get('price') or row.get('short_price'))}</b>
              <small>{fmt_money(short_leg.get('depth_usd') or row.get('short_depth_usd'))} visible depth</small>
            </article>
          </div>
          <div class="spread-track" aria-hidden="true"><span></span></div>
          <div class="pair-proof-rail">{''.join(render_cockpit_gate(*gate) for gate in gates)}</div>
        </div>

        <aside class="pair-edge-panel">
          <span>Current open spread</span>
          <strong class="{spread_class(open_spread)}">{fmt_pct(open_spread)}</strong>
          <p>{verdict} · {next_action}</p>
          <div class="edge-story" aria-label="Spread story">
            <div>
              <span>Spread Story</span>
              <strong>{label_text(trend)}</strong>
              <em>{fmt_pct(open_spread)} now · {fmt_signed_pct(delta)} move · {fmt_pct(spread_range)} range</em>
            </div>
            {render_sparkline(history, 'open_spread_pct', label='pair open spread trend')}
          </div>
          <div class="edge-metrics">
            <span>Executable<strong>{fmt_pct(executable)}</strong></span>
            <span>Funding 24h<strong>{fmt_signed_pct(funding_24h, digits=3)}</strong></span>
            <span>Age<strong>{fmt_age(row.get('age_min'))}</strong></span>
            <span>Samples<strong>{h(len(history))}</strong></span>
          </div>
        </aside>
      </div>

      <div class="pair-cockpit-foot">
        <nav class="pair-anchors" aria-label="Pair sections">
          <a href="#spread">Spread</a>
          <a href="#timeline">Timeline</a>
          <a href="#funding">Funding</a>
          <a href="#community">Community</a>
        </nav>
        <p>{'Fresh public exchange API data with read-only charts, funding, and route context.' if is_canonical else 'Local research context with no execution controls.'}</p>
      </div>
    </section>
    """


def render_pair_snapshot_banner(row: dict[str, Any]) -> str:
    age = _float_or_none(row.get("age_min"))
    is_stale = age is None or age > board.DEFAULT_FRESH_MAX_AGE_MIN
    if not is_stale:
        return ""
    route_kind = row.get("kind") or "FUTURES"
    age_label = "age unknown" if age is None else f"{fmt_age(age)} old"
    symbol = str(row.get("symbol") or "")
    reason = (
        "This exact route snapshot is stale, so the spread, funding, depth, and D/W checks below are research context only."
        if age is not None
        else "This exact route snapshot has no reliable timestamp, so the spread, funding, depth, and D/W checks below are research context only."
    )
    return f"""
    <section class="pair-snapshot-banner stale" role="status" aria-label="Stale pair snapshot">
      <div>
        <span>Source stale</span>
        <strong>Stale route snapshot · {h(age_label)}</strong>
        <p>{h(reason)} Open the current board or source status before treating this as a live opportunity.</p>
      </div>
      <nav aria-label="Stale route actions">
        <a href="/arbitrage?kind={h(route_kind)}">Current board</a>
        <a href="/intel?symbol={h(symbol)}">Intel for {h(symbol) if symbol else 'symbol'}</a>
      </nav>
    </section>
    """


def render_pair_intel_strip(row: dict[str, Any], pair_intel: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "")
    queue = next(
        (
            item
            for item in pair_intel.get("action_queue") or []
            if str(item.get("symbol") or "").upper() == symbol.upper()
        ),
        None,
    )
    recent = pair_intel.get("recent_events") or {}
    events = pair_signal_events(recent)
    hot = (pair_intel.get("hot_symbols") or [{}])[0] if pair_intel.get("hot_symbols") else {}
    is_canonical = bool(row.get("canonical_api"))
    if is_canonical:
        status = "optional_community_context"
        reason = f"{len(events)} recent community events for this token."
        next_action = "compare_route_chart_and_funding"
        blockers = []
        badges = [row.get("kind_label") or row.get("kind") or "route"]
        blocker_note = "No community warnings"
        context_note = "optional community context"
    else:
        status = queue.get("status") if queue else "local_pair_context"
        reason = queue.get("reason") if queue else "No recent local queue row for this symbol."
        next_action = queue.get("next_action") if queue else "Review the route checklist."
        blockers = queue.get("blockers") if queue else []
        badges = queue.get("badges") if queue else [row.get("kind_label") or row.get("kind") or "route"]
        blocker_note = label_list(blockers[:2]) or "No queue blockers found"
        context_note = "read-only local context"
    return f"""
    <section class="pair-intel-strip" aria-label="Pair intel verdict">
      <article>
        <span>Intel verdict</span>
        <strong>{label_text(status)}</strong>
        <em>{h(reason)}</em>
      </article>
      <article>
        <span>Next check</span>
        <strong>{label_text(next_action)}</strong>
        <em>{blocker_note}</em>
      </article>
      <article>
        <span>Community heat</span>
        <strong>{h(len(events))} events</strong>
        <em>{h(round(_float_or_none(hot.get('score')) or 0, 1))} score</em>
      </article>
      <article>
        <span>Badges</span>
        <strong>{label_list(badges) or 'Route'}</strong>
        <em>{context_note}</em>
      </article>
      <nav class="pair-intel-links" aria-label="Pair intel links">
        <a href="/intel?symbol={h(symbol)}">Intel</a>
        <a href="/playbook">Playbook</a>
        <a href="/arbitrage?kind={h(row.get('kind') or 'FUTURES')}">Board</a>
      </nav>
    </section>
    """


def render_cockpit_gate(label: str, state: str, note: str) -> str:
    text = "OK" if state == "ok" else "Check" if state == "warn" else "Missing"
    return (
        f'<article class="cockpit-gate {h(state)}">'
        f'<span>{h(label)}</span>'
        f'<strong>{h(text)}</strong>'
        f'<em>{note}</em>'
        '</article>'
    )


def render_pair_decision_strip(
    row: dict[str, Any],
    detail: dict[str, Any],
    pair_intel: dict[str, Any],
    history: list[dict[str, Any]],
) -> str:
    health = detail.get("route_health") or {}
    recent = pair_intel.get("recent_events") or {}
    events = pair_signal_events(recent)
    okx_quote = detail.get("okx_dex_quote") or {}
    return f"""
    <section class="pair-decision-strip" aria-label="Route decision">
      <article>
        <span>Route decision</span>
        <strong>{label_text(health.get('verdict') or 'watch_only')}</strong>
        <em>{label_text(health.get('next_action') or 'watch')}</em>
      </article>
      <article>
        <span>Freshness</span>
        <strong>{fmt_age(row.get('age_min'))}</strong>
        <em>{'fresh row' if (_float_or_none(row.get('age_min')) or 999999) <= board.DEFAULT_FRESH_MAX_AGE_MIN else 'source stale'}</em>
      </article>
      <article>
        <span>Community</span>
        <strong>{h(len(events))}</strong>
        <em>local events</em>
      </article>
      <article>
        <span>History</span>
        <strong>{h(len(history))}</strong>
        <em>samples</em>
      </article>
      <article>
        <span>OKX DEX</span>
        <strong>{label_text(okx_quote.get('status') or 'not_applicable')}</strong>
        <em>quote only</em>
      </article>
    </section>
    """


def render_spread_lens(row: dict[str, Any], detail: dict[str, Any]) -> str:
    open_spread = _float_or_none(row.get("displayed_open_spread_pct"))
    executable = _float_or_none(row.get("spread_pct"))
    funding = funding_24h_value(row)
    width = spread_width(open_spread if open_spread is not None else executable)
    direction = "positive" if (open_spread or executable or 0) >= 0 else "negative"
    long_price = row.get("long_price")
    short_price = row.get("short_price")
    return f"""
    <section id="spread" class="spread-lens {direction}" style="--spread-width: {width}%">
      <div class="spread-lens-head">
        <div>
          <span class="page-kicker">Spread Equation</span>
          <h2>{fmt_pct(open_spread)} open spread</h2>
          <p>Buy price, sell price, executable VWAP, and carry are separated so the headline spread cannot hide liquidity, funding, or stale-source issues.</p>
        </div>
        <span class="read-only-pill">Observation only</span>
      </div>
      <div class="spread-equation">
        <article class="equation-leg buy">
          <span>Buy</span>
          {render_exchange_link(row, 'long')}
          <em>{h(leg_market_label(row.get('long_venue'), row.get('long_market_type')))} {fmt_price(long_price)}</em>
        </article>
        <div class="equation-operator">to</div>
        <article class="equation-leg sell">
          <span>Sell</span>
          {render_exchange_link(row, 'short')}
          <em>{h(leg_market_label(row.get('short_venue'), row.get('short_market_type')))} {fmt_price(short_price)}</em>
        </article>
        <div class="equation-operator">=</div>
        <article class="equation-result">
          <span>Result</span>
          <strong class="{spread_class(open_spread)}">{fmt_pct(open_spread)}</strong>
          <em>{fmt_pct(executable)} executable</em>
        </article>
      </div>
      <div class="spread-track" aria-hidden="true"><span></span></div>
      <div class="spread-breakdown">
        <span><b>{fmt_pct(executable)}</b><em>Executable VWAP</em></span>
        <span><b>{fmt_signed_pct(row.get('funding_spread_pct'), digits=4)}</b><em>Funding spread</em></span>
        <span><b>{fmt_signed_pct(funding, digits=3)}</b><em>Funding 24h</em></span>
        <span><b>{fmt_money(row.get('depth_usd'))}</b><em>Visible depth</em></span>
      </div>
    </section>
    """


def render_pair_checklist(row: dict[str, Any], detail: dict[str, Any]) -> str:
    legs = detail.get("legs") or {}
    long_leg = legs.get("long") or {}
    short_leg = legs.get("short") or {}
    health = detail.get("route_health") or {}
    okx_quote = detail.get("okx_dex_quote") or {}
    fresh_ok = (_float_or_none(row.get("age_min")) or 999999) <= board.DEFAULT_FRESH_MAX_AGE_MIN
    quote_ok = row.get("spread_pct") is not None
    venue_ok = bool(long_leg.get("market_symbol")) and bool(short_leg.get("market_symbol"))
    funding_24h = (detail.get("funding") or {}).get("net_24h_pct")
    funding_ok = funding_24h is not None or row.get("funding_spread_pct") is not None
    is_canonical = bool(row.get("canonical_api"))
    dw_applicable = "Spot" in {
        row.get("long_market_type"),
        row.get("short_market_type"),
    }
    dw_ok = any(row.get(key) is not None for key in ("long_deposit_enabled", "long_withdraw_enabled", "short_deposit_enabled", "short_withdraw_enabled"))
    dex_status = str(okx_quote.get("status") or "not_applicable")
    dex_ok = dex_status in {"not_applicable", "available", "disabled"} or "blocked" not in dex_status
    private_ok = not health.get("blockers")
    checks = [
        pair_check_item("Fresh row", fresh_ok, fmt_age(row.get("age_min")) if fresh_ok else f"{fmt_age(row.get('age_min'))} old"),
        pair_check_item("Public quote", quote_ok, fmt_pct(row.get("spread_pct")) if quote_ok else "quote missing"),
        pair_check_item("Venue symbols", venue_ok, "both legs resolved" if venue_ok else "venue symbol unresolved"),
        pair_check_item("Funding", funding_ok, f"{fmt_signed_pct(funding_24h, digits=3)} / 24h" if funding_ok else "funding missing"),
        pair_check_item(
            "D/W signal",
            not dw_applicable or dw_ok,
            "not applicable to futures pair"
            if not dw_applicable
            else "D/W known"
            if dw_ok
            else "not reported by public venue API",
        ),
        pair_check_item("DEX identity", dex_ok, label_text(dex_status)),
        pair_check_item(
            "Read-only mode" if is_canonical else "Private safety",
            True if is_canonical else private_ok,
            "no order or transfer controls"
            if is_canonical
            else "no local blockers"
            if private_ok
            else label_text(health.get("next_action") or "watch"),
        ),
    ]
    return f"""
    <section class="pair-checklist" aria-label="Route checklist">
      <div class="panel-head flat">
        <div>
          <h2>Route Checklist</h2>
          <p>{'Current public API fields for this exact venue pair.' if is_canonical else 'Read-only checks for the available local route context.'}</p>
        </div>
      </div>
      <div class="checklist-grid">{''.join(checks)}</div>
    </section>
    """


def pair_check_item(label: str, ok: bool, note: str) -> str:
    state = "ok" if ok else "missing"
    text = "OK" if ok else "Missing"
    return (
        f'<article class="check-item {state}">'
        f'<span>{h(label)}</span>'
        f'<strong>{h(text)}</strong>'
        f'<em>{note}</em>'
        '</article>'
    )


def render_route_timeline(row: dict[str, Any], history: list[dict[str, Any]]) -> str:
    open_spread = row.get("displayed_open_spread_pct") if row.get("displayed_open_spread_pct") is not None else row.get("spread_pct")
    spread_key = "executable_spread_pct" if row.get("canonical_api") else "open_spread_pct"
    delta = history_delta(history, spread_key)
    spread_range = history_range(history, spread_key)
    return f"""
    <section id="timeline" class="route-timeline">
      <div class="timeline-head">
        <div>
          <span class="page-kicker">{'API Timeline' if row.get('canonical_api') else 'Local Timeline'}</span>
          <h2>{fmt_pct(open_spread)} now, {fmt_signed_pct(delta)} move</h2>
          <p>{'Spread and funding observations captured from the canonical public API scanner for this exact route.' if row.get('canonical_api') else 'Historical observations captured for this exact route.'}</p>
        </div>
        <div class="timeline-stats">
          <span>Samples<strong>{h(len(history))}</strong></span>
          <span>Range<strong>{fmt_pct(spread_range)}</strong></span>
          <span>Age<strong>{fmt_age(row.get('age_min'))}</strong></span>
        </div>
      </div>
      {render_sparkline(history, spread_key, label='open spread', large=True)}
      <div class="timeline-dual">
        <div>
          <span>Executable VWAP</span>
          {render_sparkline(history, 'executable_spread_pct', label='executable spread')}
        </div>
        <div>
          <span>Funding 24h</span>
          {render_sparkline(history, 'funding_daily_pct', label='funding 24h')}
        </div>
      </div>
    </section>
    """


def render_pair_telegram_context(pair_intel: dict[str, Any]) -> str:
    recent = pair_intel.get("recent_events") or {}
    events = pair_signal_events(recent)
    hot = (pair_intel.get("hot_symbols") or [{}])[0] if pair_intel.get("hot_symbols") else {}
    reality = (pair_intel.get("route_reality") or [{}])[0] if pair_intel.get("route_reality") else {}
    lifecycle = pair_intel.get("signal_lifecycle") or {}
    return f"""
    <section id="community" class="pair-community">
      <div class="panel-head flat">
        <div>
          <h2>Telegram Context</h2>
          <p>Latest local group signals for this symbol, joined to board/preflight reality.</p>
        </div>
        <span class="context-score">{h(round(_float_or_none(hot.get('score')) or 0, 1))} score</span>
      </div>
      <div class="context-grid">
        <article class="context-card">
          <span>Status</span>
          <strong>{label_text(reality.get('status') or 'no_local_signal')}</strong>
          <em>{label_list(reality.get('next_actions') or []) or 'No next action in local rows.'}</em>
        </article>
        <article class="context-card">
          <span>Identity</span>
          <strong>{label_text(reality.get('okx_dex_identity') or 'unknown')}</strong>
          <em>{label_list(reality.get('top_blockers') or []) or 'No blocker details.'}</em>
        </article>
      </div>
      {render_signal_lifecycle(lifecycle)}
      <div class="signal-list">{''.join(render_signal_event(item) for item in events[:8]) or '<p class="empty">No recent Telegram events for this symbol in the selected window.</p>'}</div>
    </section>
    """


def render_pair_health_summary(detail: dict[str, Any]) -> str:
    health = detail.get("route_health") or {}
    blockers = health.get("blockers") or []
    is_canonical = health.get("verdict") == "current_api_data"
    return f"""
    <article class="route-summary-card">
      <span>{'Data status' if is_canonical else 'Route Decision'}</span>
      <strong>{label_text(health.get('verdict') or 'watch_only')}</strong>
      <p>{label_text(health.get('next_action') or ('monitor_route' if is_canonical else 'watch'))}</p>
      <ul class="plain-list compact">{''.join(f'<li>{label_text(item)}</li>' for item in blockers[:4]) or ('<li>Fresh public route, read-only.</li>' if is_canonical else '<li>No blocker details in this row.</li>')}</ul>
    </article>
    """


def render_signal_lifecycle(lifecycle: dict[str, Any]) -> str:
    rows = lifecycle.get("rows") if isinstance(lifecycle.get("rows"), list) else []
    summary = (
        f"{h(lifecycle.get('closed_count') or 0)} closed / "
        f"{h(lifecycle.get('unresolved_count') or 0)} unresolved"
    )
    median = fmt_duration(lifecycle.get("median_close_min"))
    rendered = []
    for item in rows[:5]:
        alert = item.get("alert") if isinstance(item.get("alert"), dict) else {}
        close = item.get("close") if isinstance(item.get("close"), dict) else {}
        headline = alert.get("first_line") or close.get("first_line") or item.get("takeaway")
        rendered.append(
            f"""
            <article class="lifecycle-row {h(item.get('status') or 'quiet')}">
              <div>
                <span>{h(item.get('symbol'))} · {h(item.get('kind') or '?')}</span>
                <strong>{label_text(item.get('status') or 'quiet')}</strong>
                <em>{h(headline)}</em>
              </div>
              <div class="lifecycle-metrics">
                <span>Alert <strong>{fmt_signed_pct(item.get('alert_spread_pct'))}</strong></span>
                <span>Close <strong>{fmt_signed_pct(item.get('close_spread_pct'))}</strong></span>
                <span>Move <strong>{fmt_signed_pct(item.get('spread_move_pct'))}</strong></span>
                <span>Time <strong>{fmt_duration(item.get('minutes_to_close'))}</strong></span>
              </div>
              <div class="action-links">
                <a href="{h(item.get('href') or '#')}">Open</a>
                <a href="{h(item.get('signals_href') or '/signals')}">Signals</a>
              </div>
            </article>
            """
        )
    return f"""
    <section class="signal-lifecycle">
      <div class="lifecycle-head">
        <div>
          <h3>Signal Lifecycle</h3>
          <p>Pairs Telegram alerts with local close/fade rows. This is signal history, not execution proof.</p>
        </div>
        <span>{summary} · median {median}</span>
      </div>
      <div class="lifecycle-list">{''.join(rendered) or '<p class="empty">No alert/close lifecycle rows in this local window.</p>'}</div>
    </section>
    """


def pair_signal_events(recent: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    for bucket in ("alerts", "funding", "closes", "momentum"):
        for item in recent.get(bucket) or []:
            row = dict(item)
            row["bucket"] = bucket
            events.append(row)
    events.sort(key=lambda item: _float_or_none(item.get("age_min")) or 999999)
    return events


def render_signal_event(item: dict[str, Any]) -> str:
    spread = item.get("spread_pct") if item.get("spread_pct") is not None else item.get("funding_delta_pct")
    return f"""
    <article class="signal-event">
      <div>
        <span>{h(item.get('bucket') or item.get('event'))}</span>
        <strong>{h(item.get('symbol'))}</strong>
        <p>{h(item.get('first_line') or item.get('text_excerpt'))}</p>
      </div>
      <aside>
        <b>{fmt_signed_pct(spread)}</b>
        <em>{fmt_age(item.get('age_min'))}</em>
      </aside>
    </article>
    """


def render_leg_card(title: str, leg: dict[str, Any]) -> str:
    volatility = leg.get("volatility_24h") or {}
    has_funding = leg.get("market_type") == "Futures"
    settled_24h = leg.get("funding_24h_pct")
    projected = leg.get("projected_funding_24h_pct")
    funding_24h = settled_24h if settled_24h is not None else projected
    funding_label = "24h settled" if settled_24h is not None else "24h at current" if has_funding else "24h funding"
    return f"""
    <article class="leg-card">
      <div class="leg-card-head">
        <span>{h(title)}</span>
        {render_venue_link(leg.get('venue'), leg.get('market_type'), leg.get('exchange_url'))}
      </div>
      <p>{h(leg.get('market_type'))} <span>{h(leg.get('market_symbol'))}</span></p>
      <div class="facts">
        <span>Price <strong>{fmt_price(leg.get('price'))}</strong></span>
        <span>Depth <strong>{fmt_money(leg.get('depth_usd'))}</strong></span>
        <span>24h volume <strong>{fmt_money(leg.get('volume_24h_usd'))}</strong></span>
        <span>Live funding <strong>{fmt_signed_pct(leg.get('current_funding_pct'), digits=4) if has_funding else 'not applicable'}</strong></span>
        <span>{h(funding_label)} <strong>{fmt_signed_pct(funding_24h, digits=4) if has_funding else 'not applicable'}</strong></span>
        <span>Payout <strong>{h(funding_interval_label(leg.get('funding_interval_hours'), leg.get('funding_interval_assumed'))) if has_funding else 'not applicable'}</strong></span>
        <span>Next payout <strong>{h(fmt_next_funding(leg.get('next_funding_ts_us'))) if has_funding else 'not applicable'}</strong></span>
        <span>D/W <strong>{status_char(leg.get('deposit_enabled'))}/{status_char(leg.get('withdraw_enabled'))}</strong></span>
        <span>24h vol <strong>{fmt_pct(volatility.get('realized_volatility_pct'))}</strong></span>
      </div>
      <p class="plain">{label_text(volatility.get('reason') or volatility.get('status'))}</p>
    </article>
    """


def render_volatility_card(detail: dict[str, Any]) -> str:
    route_vol = detail.get("route_volatility_24h") or {}
    return f"""
    <article class="data-card">
      <h2>24h Volatility</h2>
      <div class="kv-row"><span>Route</span><strong>{fmt_pct(route_vol.get('spread_volatility_pct_points'))}</strong></div>
      <div class="kv-row"><span>Range</span><strong>{fmt_pct(route_vol.get('spread_range_pct_points'))}</strong></div>
      <p class="plain">{label_text(route_vol.get('reason') or route_vol.get('status'))}</p>
    </article>
    """


def render_funding_card(detail: dict[str, Any]) -> str:
    funding = detail.get("funding") or {}
    legs = detail.get("legs") or {}
    long_leg = legs.get("long") or {}
    short_leg = legs.get("short") or {}
    return f"""
    <article id="funding" class="data-card">
      <h2>Funding</h2>
      <div class="kv-row"><span>Net 24h</span><strong>{fmt_signed_pct(funding.get('net_24h_pct'), digits=4)}</strong></div>
      <div class="kv-row"><span>Long 24h</span><strong>{fmt_signed_pct(funding.get('long_24h_pct'), digits=4)}</strong></div>
      <div class="kv-row"><span>Short 24h</span><strong>{fmt_signed_pct(funding.get('short_24h_pct'), digits=4)}</strong></div>
      <div class="kv-row"><span>Long payout</span><strong>{h(funding_interval_label(long_leg.get('funding_interval_hours'), long_leg.get('funding_interval_assumed')))}</strong></div>
      <div class="kv-row"><span>Short payout</span><strong>{h(funding_interval_label(short_leg.get('funding_interval_hours'), short_leg.get('funding_interval_assumed')))}</strong></div>
      <button class="funding-history-open" type="button" data-funding-open>Funding history</button>
      <p class="plain">{label_text(funding.get('note'))}</p>
    </article>
    """


def fmt_next_funding(timestamp_us: Any) -> str:
    value = _float_or_none(timestamp_us)
    if value is None:
        return "not reported"
    seconds = value / 1_000_000.0
    remaining = max(0, int(seconds - time.time()))
    when = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%H:%M UTC")
    if remaining <= 0:
        return when
    hours, remainder = divmod(remaining, 3600)
    minutes = remainder // 60
    return f"{when} ({hours}h {minutes:02d}m)"


def render_funding_history_dialog(detail: dict[str, Any]) -> str:
    legs = detail.get("legs") or {}
    long_leg = legs.get("long") or {}
    short_leg = legs.get("short") or {}
    long_history = {
        _funding_history_minute(item.get("timestamp_ms")): item
        for item in long_leg.get("funding_history") or []
        if item.get("timestamp_ms") is not None
    }
    short_history = {
        _funding_history_minute(item.get("timestamp_ms")): item
        for item in short_leg.get("funding_history") or []
        if item.get("timestamp_ms") is not None
    }
    timestamps = sorted(set(long_history) | set(short_history), reverse=True)
    rows = []
    for timestamp_ms in timestamps:
        long_item = long_history.get(timestamp_ms) or {}
        short_item = short_history.get(timestamp_ms) or {}
        stamp = datetime.fromtimestamp(float(timestamp_ms) / 1000.0, tz=timezone.utc).strftime(
            "%d %b %H:%M"
        )
        rows.append(
            f"""
            <tr>
              <td>{h(stamp)} UTC</td>
              <td>{fmt_signed_pct(long_item.get('rate_pct'), digits=4)}</td>
              <td>{fmt_signed_pct(long_item.get('cumulative_pct'), digits=4)}</td>
              <td>{fmt_signed_pct(short_item.get('rate_pct'), digits=4)}</td>
              <td>{fmt_signed_pct(short_item.get('cumulative_pct'), digits=4)}</td>
            </tr>
            """
        )
    statuses = {
        str(long_leg.get("funding_history_status") or ""),
        str(short_leg.get("funding_history_status") or ""),
    }
    if "current_only" in statuses:
        empty_text = "Current funding is available, but the venue returned no settled history."
    else:
        empty_text = "Historical funding is temporarily unavailable; current and projected 24h funding remain shown above."
    empty = f'<tr><td colspan="5">{h(empty_text)}</td></tr>'
    return f"""
    <dialog class="funding-history-dialog" data-funding-dialog>
      <div class="funding-history-head">
        <div><strong>Funding history</strong><span>{h(long_leg.get('market_type'))} / {h(short_leg.get('market_type'))}</span></div>
        <button type="button" data-funding-close aria-label="Close funding history">x</button>
      </div>
      <div class="funding-history-scroll">
        <table>
          <thead><tr><th>Time</th><th>{h(long_leg.get('venue') or 'Long')}</th><th>Long sum</th><th>{h(short_leg.get('venue') or 'Short')}</th><th>Short sum</th></tr></thead>
          <tbody>{''.join(rows) or empty}</tbody>
        </table>
      </div>
    </dialog>
    """


def _funding_history_minute(timestamp_ms: Any) -> int:
    value = _float_or_none(timestamp_ms) or 0.0
    return int(round(value / 60_000.0) * 60_000)


def render_funding_history_script() -> str:
    return """
    <script>
    (() => {
      const dialog = document.querySelector('[data-funding-dialog]');
      if (!dialog) return;
      document.querySelectorAll('[data-funding-open]').forEach((button) => {
        button.addEventListener('click', () => dialog.showModal());
      });
      dialog.querySelector('[data-funding-close]')?.addEventListener('click', () => dialog.close());
      dialog.addEventListener('click', (event) => {
        if (event.target === dialog) dialog.close();
      });
    })();
    </script>
    """


def render_okx_dex_card(quote: dict[str, Any] | None) -> str:
    quote = quote or {}
    return f"""
    <article class="data-card">
      <h2>OKX DEX</h2>
      <div class="kv-row"><span>Status</span><strong>{label_text(quote.get('status') or 'unknown')}</strong></div>
      <div class="kv-row"><span>Buy</span><strong>{fmt_price(quote.get('dex_buy_price_usd'))}</strong></div>
      <div class="kv-row"><span>Sell</span><strong>{fmt_price(quote.get('dex_sell_price_usd'))}</strong></div>
      <div class="kv-row"><span>Network fee / gas units</span><strong>${h(quote.get('trade_fee_usd') or '?')} / {h(quote.get('estimate_gas_fee') or '?')}</strong></div>
      <p class="plain">{label_list(quote.get('blockers') or []) or label_text(quote.get('note')) or 'Read-only quote data only.'}</p>
    </article>
    """


def render_route_health_card(health: dict[str, Any] | None) -> str:
    health = health or {}
    blockers = health.get("blockers") or []
    return f"""
    <article id="health" class="data-card">
      <h2>Route Health</h2>
      <div class="kv-row"><span>Verdict</span><strong>{label_text(health.get('verdict') or 'watch_only')}</strong></div>
      <div class="kv-row"><span>Next</span><strong>{label_text(health.get('next_action') or 'watch')}</strong></div>
      <ul class="plain-list">{''.join(f'<li>{label_text(item)}</li>' for item in blockers) or '<li>No blocker details in this row.</li>'}</ul>
    </article>
    """


def render_chart_link(row: dict[str, Any]) -> str:
    chart_url = row.get("chart_url")
    if not chart_url:
        return '<span class="control-btn ghost muted">No chart</span>'
    return f'<a class="control-btn ghost" href="{h(chart_url)}" rel="noreferrer">Chart</a>'


def render_exchange_row(row: dict[str, Any]) -> str:
    return f"""
    <tr class="token-exchange-row">
      <td data-label="Exchange"><strong>{h(row.get('venue'))}</strong></td>
      <td data-label="Perp price">{fmt_price(row.get('perp_price'))}</td>
      <td data-label="Spot price">{fmt_price(row.get('spot_price'))}</td>
      <td data-label="Funding">{fmt_signed_pct(row.get('funding_rate_pct'), digits=4)}</td>
      <td data-label="24h volume">{fmt_money(row.get('volume_usd'))}</td>
      <td data-label="Deposit"><span class="status">{h(live.mark_status(row.get('deposit')))}</span></td>
      <td data-label="Withdraw"><span class="status">{h(live.mark_status(row.get('withdraw')))}</span></td>
    </tr>
    """


def render_spread_list(spreads: list[dict[str, Any]]) -> str:
    if not spreads:
        return '<p class="muted">No cross-market last-price gap above 0.3% was found.</p>'
    items = []
    for spread in spreads:
        transfer = (
            f" <span class=\"muted\">{h(spread.get('transfer_note'))}</span>"
            if spread.get("transfer_note")
            else ""
        )
        items.append(
            "<li>"
            f"Buy on <strong>{h(spread['buy_venue'])} {h(spread['buy_leg'])}</strong> at {fmt_price(spread['buy_price'])}; "
            f"sell on <strong>{h(spread['sell_venue'])} {h(spread['sell_leg'])}</strong> at {fmt_price(spread['sell_price'])}. "
            f"<strong>{fmt_pct(spread['spread_pct'])}</strong>{transfer}"
            "</li>"
        )
    return f"<ol class=\"spread-list\">{''.join(items)}</ol>"


def render_dex_line(dex: dict[str, Any] | None) -> str:
    if not dex:
        return '<p class="muted">No DexScreener fallback pair survived the price-sanity check.</p>'
    url = dex.get("url")
    link = f' <a href="{h(url)}" rel="noreferrer">open pair</a>' if url else ""
    return (
        f"<p><strong>{h(dex.get('chain_id'))}</strong> on {h(dex.get('dex_id'))}: "
        f"price {fmt_price(dex.get('price_usd'))}, liquidity {fmt_money(dex.get('liquidity_usd'))}, "
        f"24h DEX volume {fmt_money(dex.get('volume_24h_usd'))}.{link}</p>"
    )


def render_hint(text: str | None) -> str:
    return f"<div class=\"callout\"><strong>Why hasn't this converged?</strong><br>{h(text)}</div>" if text else ""


def render_age_banner(age_min: float | None, error: str | None) -> str:
    danger = error is not None or (age_min is not None and age_min > board.DEFAULT_FRESH_MAX_AGE_MIN)
    if error:
        text = f"Board data unavailable: {error}"
    elif age_min is None:
        text = "Board data age unknown"
    else:
        text = f"newest row {fmt_age(age_min)} old"
        if danger:
            text += " - source is stale"
    return f'<div id="ageBanner" class="age-banner {"danger" if danger else "ok"}">{h(text)}</div>'


def render_age_text(age_min: float | None, error: str | None) -> str:
    if error:
        return f"source error: {error}"
    if age_min is None:
        return "age unknown"
    return f"newest row {fmt_age(age_min)} old"


def render_auto_refresh_script() -> str:
    return """
<script>
(() => {
  const root = document.querySelector("[data-refresh]");
  if (!root) return;
  const seconds = Math.max(3, Number.parseInt(root.dataset.refresh || "30", 10) || 30);
  const forceRunning = root.dataset.refreshForce === "1";
  const pauseKey = "spreadboard.autoRefreshPaused";
  const scrollKey = `spreadboard.scroll:${location.pathname}${location.search}`;
  const savedScroll = Number(sessionStorage.getItem(scrollKey) || "0");
  if (savedScroll > 0) {
    requestAnimationFrame(() => window.scrollTo(0, savedScroll));
    sessionStorage.removeItem(scrollKey);
  }
  // A page whose prices arrive over the stream has nothing to count down to.
  // The reload is only there to pick up a token entering or leaving the board,
  // so it runs silently rather than showing a timer that implies the numbers
  // are waiting on it.
  const silent = root.dataset.refreshSilent === "1";
  const pill = document.createElement("aside");
  pill.className = "auto-refresh-pill";
  pill.setAttribute("aria-live", "polite");
  pill.innerHTML = '<span id="autoRefreshStatus"></span><button id="autoRefreshToggle" type="button"></button>';
  if (!silent) document.body.appendChild(pill);
  const label = pill.querySelector("#autoRefreshStatus");
  const toggle = pill.querySelector("#autoRefreshToggle");
  let remaining = seconds;
  let paused = forceRunning ? false : localStorage.getItem(pauseKey) === "1";

  function editableActive() {
    const active = document.activeElement;
    return !!document.querySelector(".alert-modal-backdrop") || (!!active && (
      ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName) ||
      active.isContentEditable
    ));
  }

  function render() {
    pill.classList.toggle("paused", paused);
    label.textContent = paused ? "Refresh paused" : `Refresh ${remaining}s`;
    toggle.textContent = paused ? "Resume" : "Pause";
  }

  toggle.addEventListener("click", () => {
    paused = !paused;
    localStorage.setItem(pauseKey, paused ? "1" : "0");
    remaining = seconds;
    render();
  });

  render();
  setInterval(() => {
    if (paused || document.hidden || editableActive()) return;
    remaining -= 1;
    if (remaining <= 0) {
      label.textContent = "Refreshing";
      sessionStorage.setItem(scrollKey, String(window.scrollY || 0));
      location.reload();
      return;
    }
    render();
  }, 1000);
})();
</script>
"""


def render_theme_script() -> str:
    return """
<script>
(() => {
  const key = "spreadboard.theme.v1";
  const button = document.getElementById("themeToggle");
  if (!button) return;
  const label = button.querySelector("[data-theme-label]");

  function currentTheme() {
    return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(key, theme);
    button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    if (label) label.textContent = theme === "dark" ? "Dark" : "Light";
  }

  applyTheme(currentTheme());
  button.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });
})();
</script>
"""


def render_alert_draft_script() -> str:
    return """
<script>
(() => {
  const rulesKey = "spreadboard.alertRules.v1";
  const activityKey = "spreadboard.profileActivity.v1";

  function readJson(key, fallback) {
    try {
      const value = JSON.parse(localStorage.getItem(key) || "null");
      return value ?? fallback;
    } catch (error) {
      return fallback;
    }
  }

  function readRules() {
    const value = readJson(rulesKey, []);
    return Array.isArray(value) ? value : [];
  }

  function writeRules(rules) {
    localStorage.setItem(rulesKey, JSON.stringify(rules.slice(0, 200)));
    window.dispatchEvent(new CustomEvent("spreadboard:alerts-changed", {detail: {count: rules.length}}));
  }

  function logActivity(action, detail) {
    const rows = readJson(activityKey, []);
    const next = Array.isArray(rows) ? rows : [];
    next.unshift({at: new Date().toISOString(), action, detail});
    localStorage.setItem(activityKey, JSON.stringify(next.slice(0, 40)));
  }

  function showToast(message) {
    let toast = document.getElementById("profileToast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "profileToast";
      toast.className = "profile-toast";
      toast.setAttribute("role", "status");
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add("show");
    window.setTimeout(() => toast.classList.remove("show"), 2600);
  }

  function numberOrNull(value) {
    if (value === null || value === undefined || String(value).trim() === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function labelForType(value) {
    return ({
      token_spread: "Token spread",
      funding: "Funding",
      price: "Price",
      exchange_spread: "Exchange spread",
      custom_pair_spread: "Custom pair spread",
      dw_tracking: "D/W tracking",
      freshness: "Freshness",
      community_call: "Community call",
      hyperliquid: "Hyperliquid",
      token_index: "Token index"
    })[value] || "Alert";
  }

  function draftFromButton(button) {
    const data = button.dataset || {};
    const currentValue = numberOrNull(data.currentValue);
    return {
      id: data.ruleId || "",
      type: data.alertType || "token_spread",
      symbol: (data.symbol || "").toUpperCase(),
      routeKey: data.routeKey || "",
      routeKind: data.routeKind || "",
      longVenue: data.longVenue || "",
      longMarketType: data.longMarketType || "",
      shortVenue: data.shortVenue || "",
      shortMarketType: data.shortMarketType || "",
      currentValue,
      threshold: currentValue === null ? (data.alertType === "funding" ? 0.1 : 5) : currentValue,
      direction: currentValue !== null && currentValue < 0 ? "below" : "above",
      stabilitySeconds: 10,
      enabled: true
    };
  }

  function routeLine(draft) {
    const long = [draft.longVenue, draft.longMarketType].filter(Boolean).join(" ");
    const short = [draft.shortVenue, draft.shortMarketType].filter(Boolean).join(" ");
    return long || short ? `${long || "?"} -> ${short || "?"}` : "Choose a route from Markets or Funding";
  }

  function openAlertModal(input = {}) {
    const existing = input.id ? readRules().find((item) => item.id === input.id) : null;
    const draft = {...input, ...(existing || {})};
    const backdrop = document.createElement("div");
    backdrop.className = "alert-modal-backdrop";
    backdrop.innerHTML = `
      <section class="alert-modal" role="dialog" aria-modal="true" aria-labelledby="alertModalTitle">
        <header>
          <div><span>Route alert template</span><h2 id="alertModalTitle">Create alert</h2></div>
          <button class="icon-close" type="button" data-alert-close aria-label="Close alert dialog">x</button>
        </header>
        <div class="alert-modal-route">
          <strong data-alert-symbol>Any token</strong>
          <span data-alert-route>Choose a route from Markets or Funding</span>
          <em data-alert-current>Current value unavailable</em>
        </div>
        <form>
          <label><span>Alert type</span><select name="type">
            <option value="token_spread">Token spread</option>
            <option value="funding">Funding 24h</option>
            <option value="price">Price</option>
            <option value="exchange_spread">Exchange spread</option>
            <option value="custom_pair_spread">Custom pair spread</option>
            <option value="dw_tracking">D/W tracking</option>
            <option value="freshness">Freshness</option>
            <option value="community_call">Community call</option>
            <option value="hyperliquid">Hyperliquid</option>
            <option value="token_index">Token index</option>
          </select></label>
          <label><span>Token</span><input name="symbol" autocomplete="off" placeholder="Token symbol"></label>
          <label><span>Direction</span><select name="direction"><option value="above">Crosses above</option><option value="below">Crosses below</option></select></label>
          <label><span>Threshold</span><input name="threshold" type="number" step="any" required></label>
          <label><span>Stability check, seconds</span><input name="stability" type="number" min="0" step="1" value="10"></label>
          <label class="alert-modal-switch"><span>Enabled</span><input name="enabled" type="checkbox" checked></label>
          <p>Saved to your account. Fresh server rows are evaluated continuously; Pushover delivery uses your account settings.</p>
          <footer><button class="sheet-button" type="button" data-alert-close>Cancel</button><button class="sheet-button primary" type="submit">Save alert</button></footer>
        </form>
      </section>`;
    document.body.appendChild(backdrop);
    document.body.classList.add("alert-modal-open");
    const form = backdrop.querySelector("form");
    const type = form.elements.type;
    const symbol = form.elements.symbol;
    const direction = form.elements.direction;
    const threshold = form.elements.threshold;
    const stability = form.elements.stability;
    const enabled = form.elements.enabled;
    type.value = draft.type || "token_spread";
    symbol.value = draft.symbol || "";
    direction.value = draft.direction || "above";
    threshold.value = draft.threshold ?? "";
    stability.value = draft.stabilitySeconds ?? 10;
    enabled.checked = draft.enabled !== false;
    backdrop.querySelector("[data-alert-symbol]").textContent = draft.symbol || "Any token";
    backdrop.querySelector("[data-alert-route]").textContent = routeLine(draft);
    backdrop.querySelector("[data-alert-current]").textContent = draft.currentValue === null || draft.currentValue === undefined
      ? "Current value unavailable"
      : `Current ${labelForType(draft.type).toLowerCase()}: ${Number(draft.currentValue).toFixed(4)}`;
    backdrop.querySelector("#alertModalTitle").textContent = draft.id ? "Edit alert" : "Create alert";

    function close() {
      backdrop.remove();
      document.body.classList.remove("alert-modal-open");
    }

    backdrop.querySelectorAll("[data-alert-close]").forEach((button) => button.addEventListener("click", close));
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) close();
    });
    backdrop.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const now = new Date().toISOString();
      const rule = {
        ...draft,
        id: draft.id || (window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : `alert-${Date.now()}-${Math.random().toString(16).slice(2)}`),
        type: type.value,
        symbol: symbol.value.trim().toUpperCase(),
        threshold: Number(threshold.value),
        direction: direction.value,
        stabilitySeconds: Math.max(0, Number(stability.value || 0)),
        enabled: enabled.checked,
        createdAt: draft.createdAt || now,
        updatedAt: now,
        delivery: "account"
      };
      const csrf = document.querySelector('[data-logout]')?.dataset.csrf || JSON.parse(document.getElementById('account-session')?.textContent || '{}').csrf_token;
      try {
        const response = await fetch('/api/market-alert-rules', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf||''}, body:JSON.stringify({
          route_key: rule.routeKey, symbol: rule.symbol, type: rule.type,
          direction: rule.direction, threshold: rule.threshold,
          stability_seconds: rule.stabilitySeconds, enabled: rule.enabled
        })});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Alert could not be saved');
        rule.id = String(data.rule.id);
        const rules = readRules();
        rules.unshift(rule);
        writeRules(rules);
        logActivity("Alert created", `${labelForType(rule.type)} ${rule.symbol || "all tokens"}`);
        showToast("Live alert activated");
        close();
      } catch (error) {
        showToast(error.message || 'Alert could not be saved');
      }
    });
    threshold.focus();
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest(".js-alert-draft");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    openAlertModal(draftFromButton(button));
  });

  window.SpreadBoardAlerts = {readRules, writeRules, logActivity, showToast, openAlertModal, labelForType};
})();
</script>
"""


#: What a signed-out visitor is offered. The member nav was rendered for
#: everyone, so every link on the public pages bounced to the login form --
#: seven dead ends and no way to reach the free board.
_VISITOR_NAV: tuple[tuple[str, str, str], ...] = (
    ("free", "/free", "Live spreads"),
    ("pricing", "/pricing", "Membership"),
    ("guide", "/guide", "Guide"),
    ("login", "/login", "Sign in"),
)

_MEMBER_NAV: tuple[tuple[str, str, str], ...] = (
    ("markets", "/", "Arbitrage"),
    ("funding", "/funding", "Funding"),
    ("fair", "/fair", "Fair price"),
    ("charts", "/charts", "Charts"),
    ("intel", "/intel", "Intel"),
    ("watchlist", "/watchlist", "Watchlist"),
    ("profile", "/account", "Portfolio"),
    ("pricing", "/pricing", "Membership"),
)


_MOBILE_SECONDARY_NAV: tuple[tuple[str, str, str], ...] = (
    ("alerts", "/alerts", "Alerts"),
    ("signals", "/signals", "Signals"),
    ("triage", "/triage", "Triage"),
    ("playbook", "/playbook", "Playbook"),
    ("community", "/community", "Community"),
)


def render_primary_nav(active: str, *, signed_in: bool) -> str:
    links = _MEMBER_NAV if signed_in else _VISITOR_NAV
    return "".join(
        f'<a class="{active_class(active, key)}" href="{href}">{h(label)}</a>'
        for key, href, label in links
    )


def render_mobile_secondary_nav(active: str) -> str:
    links = "".join(
        f'<a class="{active_class(active, key)}" href="{href}">{h(label)}</a>'
        for key, href, label in _MOBILE_SECONDARY_NAV
    )
    return (
        '<nav class="mobile-secondary-nav" aria-label="Mobile community navigation">'
        f"{links}</nav>"
    )


def shell(title: str, active: str, body: str) -> str:
    user = accounts.current_user()
    account_action = (
        f'<a class="account-chip" href="/account"><span>{h(user.display_name)}</span><em>{h(user.subscription_status)}</em></a>'
        f'<button class="logout-button" type="button" data-logout data-csrf="{h(user.csrf_token or "")}" aria-label="Sign out" title="Sign out">&#x21AA;</button>'
        if user
        else ''
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)}</title>
<script>
(() => {{
  try {{
    const key = "spreadboard.theme.v1";
    const saved = localStorage.getItem(key);
    const theme = saved === "dark" || saved === "light"
      ? saved
      : (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
  }} catch (error) {{
    document.documentElement.dataset.theme = "light";
  }}
}})();
</script>
<style>
:root {{
  color-scheme: light;
  --surface: #eef2f0;
  --dark: #15201f;
  --ink: #17211f;
  --muted: #71807c;
  --line: #b9c8c3;
  --row: #dfe7e3;
  --row-hover: #d4ded9;
  --accent: #38d4bd;
  --accent-soft: #a9f2e5;
  --accent-ink: #063d36;
  --brand-secondary: #6f8cff;
  --green: #00b884;
  --green-soft: #73e8a1;
  --red: #f26d7d;
  --red-soft: #ff8a98;
  --yellow-chip: #c8e96f;
  --panel: #f7f7f7;
  --shadow: 0 4px 14px rgba(0,0,0,.08);
  --radius: 10px;
  --terminal-bg: #edf2ef;
  --terminal-panel: #fbfdfc;
  --terminal-panel-2: #f2f6f4;
  --terminal-row: #ffffff;
  --terminal-row-hover: #eef7f3;
  --terminal-text: #14201d;
  --terminal-muted: #65756f;
  --terminal-line: #d7e1dd;
  --terminal-shell: #0e1715;
  --terminal-shell-text: #e7f3ef;
  --terminal-accent: #1fb8a5;
  --terminal-accent-soft: #d9f7ef;
  --terminal-danger-soft: #ffe8ed;
  --terminal-warning-soft: #fff3cc;
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface: #0b1211;
  --ink: #e8f3f0;
  --muted: #91a9a2;
  --line: #29413a;
  --row: #15231f;
  --row-hover: #1d302a;
  --accent: #38d4bd;
  --accent-soft: #153d36;
  --accent-ink: #d7fff6;
  --green-soft: #123f32;
  --red-soft: #4b1c26;
  --yellow-chip: #4b3f18;
  --panel: #101a18;
  --shadow: 0 10px 24px rgba(0,0,0,.34);
  --terminal-bg: #0b1211;
  --terminal-panel: #101a18;
  --terminal-panel-2: #14211e;
  --terminal-row: #14211e;
  --terminal-row-hover: #1b2d28;
  --terminal-text: #e8f3f0;
  --terminal-muted: #9cb2ab;
  --terminal-line: #2b453e;
  --terminal-shell: #070d0c;
  --terminal-shell-text: #e8f3f0;
  --terminal-accent: #38d4bd;
  --terminal-accent-soft: #123d36;
  --terminal-danger-soft: #3a1720;
  --terminal-warning-soft: #372d12;
}}
* {{ box-sizing: border-box; }}
html {{ min-width: 0; }}
body {{ margin: 0; min-height: 100vh; background: var(--surface); color: var(--ink); font-family: Arial, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; letter-spacing: 0; }}
a {{ color: inherit; text-decoration: none; }}
button, input, select {{ font: inherit; }}
input:focus, select:focus, a:focus-visible, button:focus-visible, summary:focus-visible {{ outline: 2px solid rgba(31,31,31,.4); outline-offset: 2px; }}
.site-header {{ position: sticky; top: 0; z-index: 50; box-shadow: 0 1px 5px rgba(0,0,0,.22); }}
.topbar {{ height: 52px; padding: 0 24px; background: var(--terminal-shell); display: flex; align-items: center; justify-content: space-between; gap: 18px; color: var(--terminal-shell-text); font-weight: 800; border-bottom: 1px solid rgba(255,255,255,.08); }}
.brand {{ display: inline-flex; align-items: center; gap: 10px; font-size: 22px; white-space: nowrap; }}
.brand-mark {{ width: 23px; height: 23px; position: relative; display: inline-block; }}
.brand-mark::before, .brand-mark::after {{ content: ""; position: absolute; border-radius: 50%; background: var(--terminal-accent); border: 2px solid rgba(255,255,255,.78); }}
.brand-mark::before {{ width: 13px; height: 13px; left: 0; top: 2px; background: var(--terminal-accent); border-color: rgba(255,255,255,.78); }}
.brand-mark::after {{ width: 8px; height: 8px; right: 0; bottom: 1px; background: #7fdccf; }}
.main-nav {{ display: flex; align-items: stretch; gap: 24px; height: 100%; min-width: 0; }}
.main-nav a {{ position: relative; display: inline-flex; align-items: center; color: #b9cac5; font-size: 13px; white-space: nowrap; }}
.main-nav a.active {{ color: var(--terminal-shell-text); box-shadow: inset 0 -2px 0 var(--terminal-accent); }}
.header-actions {{ display: flex; align-items: center; gap: 14px; }}
.theme-toggle {{ min-height: 34px; display: inline-flex; align-items: center; gap: 8px; padding: 0 10px; border: 1px solid rgba(255,255,255,.16); border-radius: 7px; background: rgba(255,255,255,.08); color: var(--terminal-shell-text); cursor: pointer; font-size: 12px; font-weight: 900; }}
.theme-toggle:hover {{ background: rgba(255,255,255,.13); }}
.theme-swatch {{ width: 16px; height: 16px; border-radius: 50%; background: linear-gradient(90deg, var(--terminal-accent) 0 50%, var(--terminal-panel) 50% 100%); border: 1px solid rgba(255,255,255,.55); }}
.header-strip {{ height: 12px; background: var(--terminal-bg); border-bottom: 1px solid var(--terminal-line); }}
.mobile-primary-nav, .mobile-secondary-nav {{ display: none; }}
main {{ max-width: none; margin: 0; padding: 32px 24px 0; }}
.saved-charts-panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px; display: grid; gap: 10px; }}
.saved-chart-pin {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.saved-chart-pin input {{ flex: 1 1 180px; min-height: 34px; padding: 0 10px; border-radius: 6px; border: 1px solid var(--line); background: var(--row); color: inherit; }}
.saved-chart-pin button {{ min-height: 34px; padding: 0 14px; border-radius: 6px; border: 0; background: var(--accent); color: var(--accent-ink); font-weight: 800; cursor: pointer; }}
.saved-chart-list {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }}
.saved-chart-list li {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 10px; border-radius: 6px; background: var(--row); }}
.saved-chart-list a {{ text-decoration: none; color: inherit; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }}
.saved-chart-list em {{ color: var(--terminal-muted); font-size: 11px; font-style: normal; }}
.saved-ratio {{ font-size: 10px; padding: 2px 6px; border-radius: 999px; background: var(--yellow-chip); }}
.saved-empty {{ color: var(--terminal-muted); font-size: 12px; }}
.saved-chart-remove {{ border: 0; background: transparent; color: var(--terminal-muted); cursor: pointer; font-size: 11px; }}
.funding-window-tabs {{ display: flex; gap: 6px; align-items: center; margin: 6px 0 2px; flex-wrap: wrap; }}
.funding-window-tabs span {{ font-size: 10px; opacity: 0.55; letter-spacing: 0.06em; text-transform: uppercase; }}
.funding-window-tabs a {{ font-size: 11px; padding: 3px 9px; border-radius: 999px; text-decoration: none;
  background: rgba(255,255,255,0.05); color: inherit; }}
.funding-window-tabs a.active {{ background: #7dd3c0; color: #04211b; font-weight: 700; }}
.funding-window-strip {{ display: inline-flex; gap: 6px; align-items: stretch; }}
.funding-window {{ display: flex; flex-direction: column; gap: 1px; padding: 2px 6px; border-radius: 6px;
  background: rgba(255,255,255,0.04); line-height: 1.15; }}
.funding-window em {{ font-size: 9px; font-style: normal; opacity: 0.6; letter-spacing: 0.04em; }}
.funding-window strong {{ font-size: 11px; font-variant-numeric: tabular-nums; }}
.funding-window.positive strong {{ color: #4ade80; }}
.funding-window.negative strong {{ color: #f87171; }}
.funding-window.unknown strong {{ opacity: 0.45; }}
.markets-page {{ display: grid; gap: 12px; }}
.market-hero {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: end; padding: 16px 18px; border-radius: 8px; background: var(--dark); color: white; box-shadow: var(--shadow); }}
.market-hero-copy {{ display: grid; gap: 4px; max-width: 980px; }}
.market-hero h1 {{ margin: 0; font-size: 30px; line-height: 1.04; }}
.market-hero p {{ margin: 0; color: #d2dfdc; font-size: 13px; line-height: 1.35; }}
.market-hero .page-kicker {{ color: var(--accent); }}
.market-hero-actions {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
.market-source-strip {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }}
.market-tape {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }}
.market-source-card, .market-tape article {{ display: grid; gap: 4px; min-height: 70px; align-content: center; padding: 10px; border-radius: 7px; background: #f7f7f7; border: 1px solid #d0d0d0; box-shadow: var(--shadow); min-width: 0; }}
.market-source-card span, .market-tape span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.market-source-card strong, .market-tape strong {{ color: var(--dark); font-size: 20px; overflow-wrap: anywhere; }}
.market-source-card em {{ color: #52635e; font-size: 12px; font-style: normal; overflow-wrap: anywhere; }}
.market-source-card.fresh {{ border-color: rgba(0,184,132,.35); background: #f4fffb; }}
.market-source-card.stale, .market-source-card.missing, .market-source-card.error {{ border-color: rgba(242,109,125,.3); background: #fff7f8; }}
.market-filter-panel {{ display: grid; gap: 8px; padding: 10px; border-radius: 8px; background: #f7f7f7; border: 1px solid #d0d0d0; box-shadow: var(--shadow); }}
.market-tabs {{ display: flex; gap: 7px; align-items: center; flex-wrap: wrap; }}
.market-tab {{ min-height: 30px; display: inline-flex; align-items: center; padding: 0 10px; border-radius: 6px; background: #e5e5e5; color: var(--dark); font-size: 12px; font-weight: 900; white-space: nowrap; }}
.market-tab.active {{ background: var(--accent); color: var(--accent-ink); }}
.route-tabs .market-tab {{ min-height: 27px; font-size: 11px; }}
.market-filter-form {{ display: grid; grid-template-columns: repeat(6, minmax(110px, 1fr)); gap: 8px; align-items: end; }}
.market-filter-form label {{ display: grid; gap: 4px; min-width: 0; }}
.market-filter-form label span {{ color: #666; font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.market-filter-form input, .market-filter-form select {{ width: 100%; min-height: 34px; border: 1px solid #cfcfcf; border-radius: 6px; padding: 0 9px; background: white; color: var(--dark); font-size: 13px; }}
.market-check {{ min-height: 34px; display: inline-flex !important; grid-auto-flow: column; align-items: center; gap: 6px !important; padding: 0 9px; border-radius: 6px; background: #e5e5e5; font-size: 12px; font-weight: 900; white-space: nowrap; }}
.market-check input {{ width: auto; min-height: auto; }}
.market-layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 12px; align-items: start; }}
.market-main, .market-side {{ display: grid; gap: 12px; min-width: 0; }}
.market-terminal-scroll {{ overflow-x: auto; border-radius: 8px; background: #f7f7f7; border: 1px solid #d0d0d0; box-shadow: var(--shadow); }}
.market-terminal-grid {{ display: grid; grid-template-columns: 150px minmax(270px, 1fr) 82px 82px 92px 82px 74px 116px minmax(270px, 1fr); gap: 0; min-width: 1260px; align-items: stretch; }}
.market-terminal-head {{ padding: 0 8px; min-height: 38px; align-items: end; color: #52635e; font-size: 11px; font-weight: 900; text-transform: uppercase; border-bottom: 1px solid #dadada; }}
.market-terminal-head > div {{ padding: 0 8px 8px; }}
.market-terminal-rows {{ display: grid; gap: 6px; padding: 8px; }}
.market-row {{ min-height: 64px; border-radius: 7px; background: var(--row); color: var(--dark); font-size: 13px; font-weight: 800; }}
.market-row:hover {{ background: var(--row-hover); text-decoration: none; }}
.market-row.stale {{ opacity: .78; }}
.market-row > div {{ min-width: 0; padding: 9px 8px; display: grid; align-content: center; }}
.market-token-cell strong {{ font-size: 18px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.market-token-cell span, .market-route-cell span, .market-blocker-cell span {{ color: #52635e; font-size: 11px; overflow-wrap: anywhere; }}
.market-route-cell strong, .market-blocker-cell strong {{ overflow-wrap: anywhere; }}
.market-funding-cell {{ gap: 2px; }}
.market-funding-cell strong {{ font-size: 13px; }}
.market-funding-cell span {{ color: #52635e; font-size: 10px; white-space: nowrap; }}
.mirage-badge {{ display: inline-flex; align-items: center; margin-left: 6px; padding: 0 6px; border-radius: 5px; background: var(--yellow-chip, #ffe89a); color: var(--dark, #1c1c1c); font-size: 10px; font-weight: 900; letter-spacing: 0.02em; text-transform: uppercase; cursor: help; }}
.market-status {{ width: fit-content; min-height: 26px; display: inline-flex; align-items: center; padding: 0 7px; border-radius: 5px; background: white; color: #52635e; font-size: 11px; font-weight: 900; }}
.market-status.watch_only, .market-status.executor_ready {{ background: var(--accent-soft); color: var(--accent-ink); }}
.market-status.setup_needed {{ background: var(--yellow-chip); color: var(--dark); }}
.market-status.stale {{ background: #ffe1e6; color: #a1283d; }}
.market-side-panel {{ display: grid; gap: 9px; padding: 10px; border-radius: 8px; background: #f7f7f7; border: 1px solid #d0d0d0; box-shadow: var(--shadow); }}
.market-mini-list {{ display: grid; gap: 7px; }}
.market-mini-row {{ display: grid; grid-template-columns: 78px minmax(0, 1fr) auto; gap: 8px; align-items: center; min-height: 42px; padding: 8px; border-radius: 6px; background: white; border: 1px solid #dedede; color: var(--dark); }}
.market-mini-row.stale {{ background: #fff7f8; }}
.market-mini-row strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.market-mini-row span {{ color: #52635e; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.market-mini-row em {{ color: var(--dark); font-size: 12px; font-style: normal; font-weight: 900; white-space: nowrap; }}
.market-exchange-cloud {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.market-coverage-group {{ display: grid; gap: 6px; }}
.market-coverage-group > span {{ color: #666; font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.market-exchange-cloud a, .market-exchange-cloud span {{ min-height: 27px; display: inline-flex; align-items: center; gap: 5px; padding: 0 8px; border-radius: 6px; background: white; color: var(--dark); border: 1px solid #dedede; font-size: 11px; font-weight: 900; }}
.market-exchange-cloud a em {{ min-width: 18px; min-height: 18px; display: inline-grid; place-items: center; border-radius: 4px; background: var(--accent-soft); color: var(--accent-ink); font-size: 9px; font-style: normal; }}
.market-coverage-group.unavailable .market-exchange-cloud span {{ background: #fff7f8; color: #a1283d; border-color: rgba(242,109,125,.25); }}
.market-pagination {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; min-height: 42px; padding: 7px 10px; border-radius: 7px; background: #f7f7f7; border: 1px solid #d0d0d0; color: #52635e; font-size: 12px; font-weight: 900; }}
.market-pagination div {{ display: flex; gap: 7px; }}
.market-pagination a, .market-pagination .disabled {{ min-height: 28px; display: inline-flex; align-items: center; padding: 0 9px; border-radius: 6px; background: var(--dark); color: white; }}
.market-pagination .disabled {{ background: #e5e5e5; color: #9a9a9a; }}
.market-empty {{ min-width: 1260px; }}
.terminal-page {{ display: grid; gap: 12px; color: var(--terminal-text); }}
.terminal-heading {{ display: grid; grid-template-columns: minmax(0, 1fr) 150px; gap: 14px; align-items: stretch; min-height: 92px; padding: 16px; border-radius: 8px; background: var(--terminal-shell); color: var(--terminal-shell-text); box-shadow: var(--shadow); }}
.terminal-heading h1 {{ margin: 3px 0 5px; font-size: 28px; line-height: 1.04; }}
.terminal-heading p {{ margin: 0; max-width: 900px; color: #adc2bc; font-size: 13px; line-height: 1.35; }}
.terminal-heading .page-kicker {{ color: var(--terminal-accent); }}
.terminal-live-box {{ display: grid; gap: 3px; align-content: center; justify-items: end; min-width: 0; padding: 10px; border-radius: 7px; border: 1px solid rgba(255,255,255,.14); background: rgba(255,255,255,.07); }}
.terminal-live-box span, .terminal-live-box em {{ color: #adc2bc; font-size: 10px; font-style: normal; font-weight: 900; text-transform: uppercase; }}
.terminal-live-box strong {{ font-size: 26px; line-height: 1; }}
.terminal-kpis, .terminal-tape {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }}
.terminal-kpi, .terminal-kpis article, .terminal-tape article {{ min-height: 78px; display: grid; gap: 4px; align-content: center; padding: 10px; border-radius: 8px; background: var(--terminal-panel); border: 1px solid var(--terminal-line); box-shadow: none; min-width: 0; }}
.terminal-kpi span, .terminal-kpis span, .terminal-tape span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.terminal-kpi strong, .terminal-kpis strong, .terminal-tape strong {{ color: var(--terminal-text); font-size: 21px; overflow-wrap: anywhere; }}
.terminal-kpi em, .terminal-kpi small, .terminal-kpis em, .terminal-kpis small, .terminal-tape em {{ color: var(--terminal-muted); font-size: 11px; font-style: normal; overflow-wrap: anywhere; }}
.terminal-kpi.fresh {{ border-color: rgba(31,184,165,.38); }}
.terminal-kpi.stale, .terminal-kpi.missing, .terminal-kpi.error {{ border-color: rgba(242,109,125,.35); background: var(--terminal-danger-soft); }}
.terminal-filter-panel {{ gap: 9px; padding: 10px; border-radius: 8px; background: var(--terminal-panel); border-color: var(--terminal-line); box-shadow: none; }}
.terminal-filter-row {{ display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 10px; align-items: center; }}
.terminal-filter-row > span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.terminal-filter-panel .market-tab {{ gap: 6px; min-height: 30px; border-radius: 5px; background: var(--terminal-panel-2); color: var(--terminal-text); border: 1px solid var(--terminal-line); }}
.terminal-filter-panel .market-tab.active {{ background: var(--terminal-accent); color: #062f2b; border-color: transparent; }}
.terminal-filter-panel .market-tab span {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.terminal-filter-panel .market-tab em {{ min-width: 22px; min-height: 18px; display: inline-grid; place-items: center; padding: 0 5px; border-radius: 4px; background: rgba(255,255,255,.65); color: #14342f; font-size: 10px; font-style: normal; }}
.terminal-filter-panel .market-filter-form {{ grid-template-columns: minmax(160px, 1.4fr) minmax(150px, 1.2fr) minmax(88px, .7fr) minmax(88px, .7fr) minmax(120px, 1fr) minmax(110px, .8fr) minmax(88px, .7fr) auto auto auto auto; }}
.terminal-filter-panel .market-filter-form label span {{ color: var(--terminal-muted); }}
.terminal-filter-panel .market-filter-form input, .terminal-filter-panel .market-filter-form select {{ border-color: var(--terminal-line); background: var(--terminal-row); color: var(--terminal-text); }}
.terminal-filter-panel .market-check, .terminal-filter-panel .sheet-button {{ background: var(--terminal-panel-2); color: var(--terminal-text); border: 1px solid var(--terminal-line); }}
.terminal-filter-panel .sheet-button.primary {{ background: var(--terminal-accent); color: #062f2b; border-color: transparent; }}
.terminal-active-filters {{ display: flex; gap: 7px; align-items: center; flex-wrap: wrap; min-height: 30px; }}
.terminal-active-filters > span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.terminal-active-filters a, .terminal-active-filters em {{ min-height: 26px; display: inline-flex; align-items: center; gap: 5px; padding: 0 8px; border-radius: 5px; background: var(--terminal-panel-2); color: var(--terminal-text); border: 1px solid var(--terminal-line); font-size: 11px; font-style: normal; font-weight: 900; }}
.terminal-active-filters a.clear {{ background: transparent; color: var(--terminal-muted); }}
.terminal-layout {{ grid-template-columns: minmax(0, 1fr) 318px; }}
.terminal-table-title h2 {{ margin: 0 0 4px; font-size: 18px; }}
.terminal-table-title p {{ margin: 0; color: var(--terminal-muted); font-size: 12px; }}
.terminal-table {{ border-radius: 8px; background: var(--terminal-panel); border-color: var(--terminal-line); box-shadow: none; }}
.terminal-grid {{ grid-template-columns: 150px minmax(330px, 1fr) 78px 94px 128px 80px 70px 104px minmax(240px, .86fr); min-width: 1240px; }}
.terminal-table .market-terminal-head {{ color: var(--terminal-muted); border-bottom-color: var(--terminal-line); }}
.terminal-table .market-terminal-rows {{ gap: 5px; padding: 7px; }}
.terminal-table .market-row {{ min-height: 70px; border-radius: 6px; background: var(--terminal-row); color: var(--terminal-text); border: 1px solid var(--terminal-line); box-shadow: none; }}
.terminal-table .market-row:hover {{ background: var(--terminal-row-hover); }}
.terminal-table .market-row.stale {{ opacity: .82; background: var(--terminal-panel-2); }}
.market-token-cell > div {{ display: flex; align-items: center; gap: 7px; min-width: 0; }}
.market-token-head, .funding-token-head {{ display: flex; align-items: center; justify-content: space-between; gap: 7px; min-width: 0; }}
.market-row-link, .funding-token-head a {{ min-width: 0; display: inline-flex; align-items: center; gap: 6px; color: inherit; }}
.market-row-link:hover, .funding-token-head a:hover {{ color: var(--accent-ink); text-decoration: none; }}
.market-token-cell em {{ min-width: 22px; padding: 3px 5px; border-radius: 4px; background: var(--terminal-accent-soft); color: var(--accent-ink); font-size: 10px; font-style: normal; font-weight: 900; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.route-alert-btn {{ min-height: 32px; display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 0 9px; border: 1px solid var(--terminal-line); border-radius: 6px; background: var(--terminal-row); color: var(--terminal-text); cursor: pointer; font-size: 11px; font-weight: 900; white-space: nowrap; }}
.route-alert-btn:hover {{ border-color: var(--terminal-accent); background: var(--terminal-accent-soft); color: var(--accent-ink); }}
.route-alert-btn > span:first-child {{ width: 17px; height: 17px; display: inline-grid; place-items: center; border-radius: 50%; background: var(--terminal-accent); color: #062f2a; line-height: 1; }}
.route-alert-btn.compact {{ min-height: 25px; padding: 0 6px; font-size: 9px; }}
.route-alert-btn.compact > span:first-child {{ width: 14px; height: 14px; }}
.terminal-leg {{ display: grid; grid-template-columns: 16px minmax(74px, .35fr) 42px minmax(92px, .4fr) minmax(80px, .3fr); gap: 7px; align-items: center; min-height: 25px; border-bottom: 1px solid var(--terminal-line); }}
.terminal-leg:last-child {{ border-bottom: 0; }}
.terminal-leg b {{ width: 16px; height: 16px; display: grid; place-items: center; border-radius: 50%; background: var(--green); color: white; font-size: 11px; font-style: normal; }}
.terminal-leg.short b {{ background: var(--red); }}
.terminal-leg strong {{ color: var(--terminal-text); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.terminal-leg em {{ color: var(--terminal-muted); font-size: 11px; font-style: normal; font-weight: 900; text-transform: uppercase; }}
.terminal-leg i {{ color: var(--terminal-text); font-size: 12px; font-style: normal; font-weight: 900; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.terminal-leg small {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.market-number-cell, .market-age-cell {{ gap: 2px; }}
.profile-note {{ margin: 0 0 12px; padding: 10px 12px; border-radius: 8px; font-size: 0.92rem; line-height: 1.45; }}
@keyframes live-tick-flash {{ from {{ background: rgba(47,158,107,0.28); }} to {{ background: transparent; }} }}
.live-tick {{ animation: live-tick-flash 900ms ease-out; border-radius: 4px; }}
.member-alerts {{ margin: 0 0 18px; }}
.member-alert-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; }}
.member-alert-card {{ display: flex; flex-direction: column; gap: 8px; padding: 14px; border-radius: 12px; border: 1px solid #dedede; background: #fff; }}
.member-alert-card.paused {{ opacity: 0.66; }}
.member-alert-card.met {{ border-color: #2f9e6b; box-shadow: 0 0 0 2px rgba(47,158,107,0.16); }}
.member-alert-head {{ display: flex; justify-content: space-between; font-size: 0.76rem; text-transform: uppercase; letter-spacing: 0.04em; color: #6a6a6a; }}
.member-alert-state {{ font-weight: 700; }}
.member-alert-card.met .member-alert-state {{ color: #2f9e6b; }}
.member-alert-token {{ font-size: 1.18rem; }}
.member-alert-route {{ font-size: 0.82rem; color: #666; }}
.member-alert-now {{ font-size: 0.9rem; }}
.member-alert-card label {{ display: flex; flex-direction: column; gap: 3px; font-size: 0.8rem; color: #555; }}
.member-alert-card input, .member-alert-card select {{ padding: 6px 8px; border-radius: 6px; border: 1px solid #d5d5d5; }}
.member-alert-toggle {{ flex-direction: row; align-items: center; justify-content: space-between; }}
.member-alert-actions {{ display: flex; gap: 8px; }}
.member-alert-actions button {{ flex: 1; padding: 7px 10px; border-radius: 7px; border: 1px solid #d5d5d5; background: #f7f7f7; cursor: pointer; }}
.member-alert-actions button[data-alert-delete] {{ color: #a12; }}
.member-alert-card em {{ font-size: 0.78rem; color: #2f9e6b; min-height: 1em; }}
.profile-note.ok {{ background: #eefaf3; border: 1px solid #bfe6d2; color: #1d5c3c; }}
.profile-note.warn {{ background: #fff6e8; border: 1px solid #f0d3a1; color: #7a4c07; }}
.profile-state.ok {{ background: #eefaf3; color: #1d5c3c; }}
.profile-state.warn {{ background: #fff6e8; color: #7a4c07; }}
.market-number-cell strong, .market-age-cell strong {{ color: var(--terminal-text); font-size: 14px; }}
.market-number-cell span, .market-age-cell span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.market-funding-cell strong {{ color: var(--terminal-text); font-size: 13px; }}
.market-funding-cell span {{ color: var(--terminal-muted); }}
.market-dw-cell {{ gap: 3px; }}
.market-dw-cell span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.market-blocker-cell {{ gap: 4px; }}
.market-blocker-cell strong {{ color: var(--terminal-text); font-size: 12px; line-height: 1.2; }}
.market-blocker-cell em {{ color: var(--terminal-muted); font-size: 11px; font-style: normal; line-height: 1.2; overflow-wrap: anywhere; }}
.market-status {{ background: var(--terminal-panel-2); color: var(--terminal-muted); border: 1px solid var(--terminal-line); }}
.market-status.watch_only, .market-status.executor_ready {{ background: var(--terminal-accent-soft); color: var(--accent-ink); border-color: transparent; }}
.market-status.setup_needed {{ background: var(--terminal-warning-soft); color: var(--terminal-text); }}
.market-status.stale {{ background: var(--terminal-danger-soft); color: #d55d70; }}
.market-side-panel, .market-pagination {{ background: var(--terminal-panel); border-color: var(--terminal-line); box-shadow: none; color: var(--terminal-text); }}
.market-mini-row, .market-exchange-cloud a, .market-exchange-cloud span {{ background: var(--terminal-row); border-color: var(--terminal-line); color: var(--terminal-text); }}
.market-mini-row span, .market-coverage-group > span, .market-pagination, .market-pagination span {{ color: var(--terminal-muted); }}
.funding-terminal-panel {{ display: grid; gap: 10px; padding: 12px; border-radius: 8px; background: var(--terminal-panel); border: 1px solid var(--terminal-line); }}
.funding-terminal-table {{ overflow-x: auto; }}
.funding-terminal-grid {{ display: grid; grid-template-columns: 190px 112px 112px 108px 90px 90px 86px 72px 118px; gap: 0; min-width: 980px; align-items: center; }}
.funding-terminal-head {{ min-height: 34px; color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; border-bottom: 1px solid var(--terminal-line); }}
.funding-terminal-head > div {{ padding: 0 8px 7px; }}
.funding-terminal-rows {{ display: grid; gap: 5px; padding-top: 7px; }}
.funding-route-row {{ min-height: 58px; border-radius: 6px; background: var(--terminal-row); border: 1px solid var(--terminal-line); color: var(--terminal-text); font-size: 12px; font-weight: 900; }}
.funding-route-row:hover {{ background: var(--terminal-row-hover); }}
.funding-route-row.pays {{ background: var(--terminal-danger-soft); }}
.funding-route-row.stale {{ opacity: .82; }}
.funding-route-row > div {{ min-width: 0; padding: 8px; }}
.funding-token-cell {{ display: grid; gap: 3px; }}
.funding-token-cell strong {{ font-size: 18px; }}
.funding-token-cell span {{ color: var(--terminal-muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.funding-route-row b, .funding-route-row strong {{ color: var(--terminal-text); }}
:root[data-theme="dark"] .panel,
:root[data-theme="dark"] .data-card,
:root[data-theme="dark"] .leg-card,
:root[data-theme="dark"] .intel-section,
:root[data-theme="dark"] .side-card,
:root[data-theme="dark"] .source-card,
:root[data-theme="dark"] .chart-route-card,
:root[data-theme="dark"] .chart-summary-card,
:root[data-theme="dark"] .route-timeline,
:root[data-theme="dark"] .pair-checklist,
:root[data-theme="dark"] .spread-lens,
:root[data-theme="dark"] .pair-intel-strip,
:root[data-theme="dark"] .route-summary-card,
:root[data-theme="dark"] .pair-community {{ background: var(--terminal-panel); border-color: var(--terminal-line); color: var(--terminal-text); }}
:root[data-theme="dark"] .panel strong,
:root[data-theme="dark"] .data-card strong,
:root[data-theme="dark"] .leg-card strong,
:root[data-theme="dark"] .chart-route-card strong,
:root[data-theme="dark"] .chart-summary-card strong,
:root[data-theme="dark"] .route-summary-card strong,
:root[data-theme="dark"] .pair-intel-strip strong,
:root[data-theme="dark"] .pair-decision-strip strong,
:root[data-theme="dark"] .facts strong,
:root[data-theme="dark"] .metric strong,
:root[data-theme="dark"] .kv-row strong {{ color: var(--terminal-text); }}
:root[data-theme="dark"] .facts span,
:root[data-theme="dark"] .metric,
:root[data-theme="dark"] .kv-row,
:root[data-theme="dark"] .pair-intel-strip article,
:root[data-theme="dark"] .change-counts article,
:root[data-theme="dark"] .equation-leg,
:root[data-theme="dark"] .equation-result,
:root[data-theme="dark"] .check-item,
:root[data-theme="dark"] .context-card,
:root[data-theme="dark"] .spread-breakdown span {{ background: var(--terminal-row); border-color: var(--terminal-line); color: var(--terminal-muted); }}
:root[data-theme="dark"] .terminal-kpi.fresh,
:root[data-theme="dark"] .terminal-kpi.empty,
:root[data-theme="dark"] .terminal-kpi.unknown {{ background: var(--terminal-panel); color: var(--terminal-text); }}
:root[data-theme="dark"] .terminal-kpi.stale,
:root[data-theme="dark"] .terminal-kpi.missing,
:root[data-theme="dark"] .terminal-kpi.error {{ background: var(--terminal-danger-soft); color: var(--terminal-text); }}
:root[data-theme="dark"] .market-mini-row.stale {{ background: var(--terminal-danger-soft); }}
:root[data-theme="dark"] .market-mini-row strong,
:root[data-theme="dark"] .market-mini-row em {{ color: var(--terminal-text); }}
.arbitrage-page {{ display: grid; gap: 12px; }}
.arb-toolbar {{ display: grid; grid-template-columns: auto auto 1fr; align-items: center; gap: 14px; min-height: 42px; }}
.tab-selector {{ display: flex; align-items: flex-end; gap: 14px; min-width: 0; }}
.tab-button {{ position: relative; display: inline-flex; align-items: center; min-height: 36px; padding: 12px 0 8px; border-bottom: 2px solid transparent; color: var(--dark); font-size: 13px; font-weight: 500; white-space: nowrap; }}
.tab-button.active {{ border-bottom-color: var(--accent); color: #008b79; }}
.quick-tools, .live-tools {{ display: flex; align-items: center; gap: 10px; }}
.live-tools {{ justify-content: flex-end; }}
.filter-menu {{ position: relative; }}
.filter-menu summary {{ list-style: none; }}
.filter-menu summary::-webkit-details-marker {{ display: none; }}
.round-tool {{ width: 31px; height: 31px; border-radius: 50%; display: inline-grid; place-items: center; background: var(--accent); cursor: pointer; }}
.round-tool span {{ width: 15px; height: 11px; border-top: 3px solid var(--dark); border-bottom: 3px solid var(--dark); position: relative; }}
.round-tool span::after {{ content: ""; position: absolute; left: 4px; top: 4px; width: 7px; border-top: 3px solid var(--dark); }}
.filter-sheet {{ position: absolute; top: 39px; left: 0; z-index: 20; width: min(680px, calc(100vw - 48px)); display: grid; grid-template-columns: 1fr 1fr 110px 90px auto auto; gap: 8px; align-items: end; padding: 14px; background: #f7f7f7; border: 1px solid #d0d0d0; border-radius: 10px; box-shadow: var(--shadow); }}
.filter-sheet label {{ display: grid; gap: 4px; }}
.filter-sheet label span {{ color: #666; font-size: 10px; font-weight: 800; text-transform: uppercase; }}
.filter-sheet input {{ width: 100%; border: 1px solid #cfcfcf; border-radius: 6px; padding: 8px 9px; background: white; color: var(--dark); font-size: 13px; }}
.sheet-button {{ height: 34px; padding: 0 12px; border: 0; border-radius: 7px; display: inline-flex; align-items: center; justify-content: center; background: #e5e5e5; color: var(--dark); font-weight: 800; font-size: 12px; cursor: pointer; }}
.sheet-button.primary {{ background: var(--accent); }}
.select-tool {{ display: inline-flex; align-items: center; gap: 8px; min-height: 31px; padding: 0 13px; border-radius: 6px; background: #e5e5e5; color: var(--dark); font-size: 13px; font-weight: 700; }}
.clock-dot {{ width: 14px; height: 14px; border: 2px solid var(--dark); border-radius: 50%; position: relative; }}
.clock-dot::before {{ content: ""; position: absolute; left: 5px; top: 2px; height: 5px; border-left: 2px solid var(--dark); }}
.link-dot {{ width: 14px; height: 8px; border: 2px solid var(--dark); border-radius: 8px; }}
.select-tool::after {{ content: ""; width: 6px; height: 6px; border-right: 2px solid currentColor; border-bottom: 2px solid currentColor; transform: rotate(45deg) translateY(-2px); opacity: .65; }}
.board-meta {{ min-height: 18px; display: flex; align-items: center; gap: 12px; color: #75847f; font-size: 11px; font-weight: 700; }}
.board-meta a, .meta-link {{ color: #52635e; text-decoration: underline; text-underline-offset: 3px; }}
.arb-table-wrapper-wide {{ margin-top: 3px; overflow: hidden; }}
.arb-scroll, .table-wrap {{ overflow-x: auto; }}
.arb-grid-futures-main {{ display: grid; grid-template-columns: 136px minmax(430px, 1fr) 112px 64px 64px 64px 150px 104px 124px 114px; column-gap: 0; min-width: 1250px; }}
.arb-grid-head {{ align-items: end; min-height: 40px; padding: 0 8px 8px; color: var(--dark); font-size: 12px; font-weight: 500; border-bottom: 1px solid #dadada; }}
.arb-grid-head > div {{ padding: 0 8px; }}
.arb-grid-head span {{ color: #777; font-size: 13px; margin-right: 4px; }}
.arb-header-grid-futures-market-full {{ line-height: 1.08; }}
.arb-rows {{ padding-top: 8px; }}
.arb-result-row {{ min-height: 61px; margin-bottom: 8px; padding: 9px 8px; align-items: stretch; background: var(--row); color: var(--dark); border-radius: 10px; font-size: 14px; font-weight: 700; }}
.arb-result-row:hover {{ background: var(--row-hover); text-decoration: none; }}
.arb-result-row > div {{ padding: 0 8px; min-width: 0; }}
.token-column {{ display: grid; align-content: center; gap: 9px; }}
.token-topline {{ display: flex; align-items: center; gap: 8px; min-width: 0; }}
.token-topline strong {{ max-width: 82px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 16px; }}
.token-topline span {{ min-width: 31px; height: 20px; display: inline-flex; align-items: center; justify-content: center; border-radius: 7px; background: white; color: var(--dark); font-size: 12px; font-weight: 800; }}
.token-actions {{ display: flex; align-items: center; gap: 13px; }}
.token-actions i {{ width: 12px; height: 12px; border: 1.5px solid #8c8c8c; border-radius: 2px; position: relative; opacity: .75; }}
.token-actions i:first-child {{ clip-path: polygon(50% 0,62% 35%,100% 38%,70% 59%,82% 100%,50% 74%,18% 100%,30% 59%,0 38%,38% 35%); background: transparent; }}
.token-actions i:last-child {{ border-radius: 50% 50% 3px 3px; }}
.market-column {{ display: grid; align-content: stretch; }}
.market-leg {{ display: grid; grid-template-columns: 18px 18px minmax(80px, .7fr) 88px minmax(82px, 1fr); align-items: center; gap: 6px; min-height: 21px; }}
.market-leg + .market-leg {{ border-top: 1px solid #bcbcbc; }}
.direction-dot {{ width: 12px; height: 12px; border-radius: 50%; position: relative; }}
.direction-dot::before {{ content: ""; position: absolute; left: 4px; top: 3px; width: 4px; height: 4px; border-left: 2px solid white; border-top: 2px solid white; transform: rotate(45deg); }}
.market-leg.buy .direction-dot {{ background: var(--green); }}
.market-leg.sell .direction-dot {{ background: var(--red); }}
.market-leg.sell .direction-dot::before {{ top: 2px; transform: rotate(225deg); }}
.venue-dot {{ width: 12px; height: 12px; border-radius: 50%; background: #244e96; }}
.market-leg.sell .venue-dot {{ background: #252b36; }}
.market-leg strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; font-weight: 500; }}
.market-leg em {{ color: var(--dark); font-size: 12px; font-style: normal; font-weight: 800; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.market-leg b {{ justify-self: start; font-size: 14px; font-weight: 800; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.market-leg.buy b {{ color: var(--green); }}
.market-leg.sell b {{ color: var(--red); }}
.funding-column, .metric-column, .dw-status {{ display: grid; align-content: center; gap: 7px; }}
.funding-column span {{ white-space: nowrap; font-size: 13px; }}
.funding-column em {{ color: var(--dark); font-size: 12px; font-style: normal; font-weight: 500; }}
.metric-column {{ text-align: center; font-size: 14px; }}
.dw-status {{ grid-template-columns: 1fr auto; align-items: center; gap: 2px 10px; }}
.dw-row {{ display: flex; gap: 17px; align-items: center; }}
.dw-dot {{ width: 18px; height: 18px; display: inline-grid; place-items: center; border-radius: 50%; background: #f3f3f3; position: relative; }}
.dw-dot::before {{ content: ""; width: 6px; height: 2px; background: #bdbdbd; }}
.dw-dot.open {{ background: rgba(0,200,132,.2); border: 1px solid var(--green); }}
.dw-dot.open::before {{ width: 7px; height: 4px; border-left: 2px solid var(--green); border-bottom: 2px solid var(--green); background: transparent; transform: rotate(-45deg); }}
.dw-dot.closed {{ background: rgba(255,111,123,.18); border: 1px solid var(--red); }}
.dw-dot.closed::before {{ width: 8px; background: var(--red); }}
.age-line {{ grid-row: 1 / span 2; grid-column: 2; align-self: center; font-weight: 500; white-space: nowrap; }}
.value-chip, .spread-hot, .spread-good, .spread-watch, .spread-low, .spread-negative {{ display: inline-flex; min-width: 54px; justify-content: center; align-items: center; border-radius: 4px; padding: 4px 5px; font-size: 16px; font-weight: 900; color: var(--dark); }}
.value-chip.positive, .spread-hot, .spread-good {{ background: var(--green-soft); }}
.value-chip.negative, .spread-negative {{ background: var(--red-soft); }}
.value-chip.neutral, .spread-low {{ background: #ffffff; }}
.spread-watch {{ background: var(--yellow-chip); }}
/* In dark mode the chip backgrounds go very dark (--green-soft is #123f32)
   while the text stayed on var(--dark), so a percentage was dark-on-dark and
   barely legible. Brighten the text and keep the hue, so green still reads as
   green and red as red. */
:root[data-theme="dark"] .value-chip,
:root[data-theme="dark"] .spread-hot,
:root[data-theme="dark"] .spread-good,
:root[data-theme="dark"] .spread-watch,
:root[data-theme="dark"] .spread-low,
:root[data-theme="dark"] .spread-negative {{ color: #f4fbf9; }}
:root[data-theme="dark"] .value-chip.positive,
:root[data-theme="dark"] .spread-hot,
:root[data-theme="dark"] .spread-good {{ color: #6ee7b7; border: 1px solid rgba(110,231,183,0.35); }}
:root[data-theme="dark"] .value-chip.negative,
:root[data-theme="dark"] .spread-negative {{ color: #fca5a5; border: 1px solid rgba(252,165,165,0.35); }}
:root[data-theme="dark"] .spread-watch {{ color: #fcd34d; border: 1px solid rgba(252,211,77,0.35); }}
:root[data-theme="dark"] .value-chip.neutral,
:root[data-theme="dark"] .spread-low {{ background: #1d2b28; color: #d7e5e1; }}
.board-empty {{ min-width: 1250px; padding: 28px; background: var(--row); border-radius: 10px; color: #777; text-align: center; }}
.route-empty {{ display: grid; gap: 12px; text-align: left; border: 1px solid #d0d0d0; background: #f7f7f7; color: var(--dark); box-shadow: var(--shadow); }}
.route-empty.unavailable {{ border-color: rgba(242,109,125,.34); background: #fff8f9; }}
.route-empty.stale {{ border-color: rgba(200,233,111,.7); background: #fcfff2; }}
.route-empty-head {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; }}
.route-empty-head div {{ display: grid; gap: 5px; min-width: 0; }}
.route-empty-head span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.route-empty-head strong {{ color: var(--dark); font-size: 24px; line-height: 1.1; }}
.route-empty-head p {{ max-width: 820px; margin: 0; color: #52635e; font-size: 13px; line-height: 1.35; }}
.route-empty-head b {{ min-height: 30px; display: inline-flex; align-items: center; padding: 0 9px; border-radius: 6px; background: var(--dark); color: white; font-size: 12px; white-space: nowrap; }}
.route-empty.unavailable .route-empty-head b {{ background: #bf3149; }}
.route-empty.stale .route-empty-head b {{ background: #b18a00; }}
.route-empty-metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }}
.route-empty-metrics span {{ display: grid; gap: 4px; min-height: 54px; align-content: center; padding: 8px; border-radius: 7px; background: white; color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.route-empty-metrics strong {{ color: var(--dark); font-size: 15px; text-transform: none; overflow-wrap: anywhere; }}
.route-empty-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.route-empty-actions a {{ min-height: 32px; display: inline-flex; align-items: center; padding: 0 10px; border-radius: 6px; background: var(--accent); color: var(--accent-ink); font-size: 12px; font-weight: 900; }}
.route-empty-actions a + a {{ background: #e5e5e5; color: var(--dark); }}
.mobile-board-cards {{ display: none; }}
.mobile-board-card {{ display: grid; gap: 10px; padding: 12px; border-radius: 10px; background: var(--row); border: 1px solid rgba(21,32,31,.08); color: var(--dark); box-shadow: var(--shadow); }}
.mobile-board-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }}
.mobile-board-head div {{ display: grid; gap: 2px; min-width: 0; }}
.mobile-board-head span {{ color: #52635e; font-size: 11px; font-weight: 900; text-transform: uppercase; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.mobile-board-head strong {{ font-size: 24px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.mobile-board-head b {{ flex: 0 0 auto; font-size: 18px; }}
.mobile-leg-stack {{ display: grid; gap: 7px; }}
.mobile-leg {{ display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 4px 8px; align-items: center; padding: 9px; border-radius: 8px; background: rgba(255,255,255,.72); border: 1px solid #cfdad6; min-width: 0; }}
.mobile-leg.buy {{ border-color: rgba(0,184,132,.35); }}
.mobile-leg.sell {{ border-color: rgba(242,109,125,.35); }}
.mobile-leg span {{ grid-row: 1 / span 2; display: inline-flex; align-items: center; justify-content: center; min-height: 24px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.mobile-leg.sell span {{ background: #ffe1e6; color: #a1283d; }}
.mobile-leg strong {{ font-size: 16px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.mobile-leg em {{ color: #52635e; font-size: 12px; font-style: normal; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.mobile-leg b {{ justify-self: end; font-size: 14px; }}
.mobile-leg small {{ justify-self: end; color: #52635e; font-size: 11px; font-weight: 800; white-space: nowrap; }}
.mobile-metric-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; }}
.mobile-metric-grid span {{ min-height: 48px; display: grid; align-content: center; gap: 3px; padding: 7px; border-radius: 7px; background: white; color: #666; font-size: 10px; font-weight: 800; text-transform: uppercase; min-width: 0; }}
.mobile-metric-grid strong {{ color: var(--dark); font-size: 14px; text-transform: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.mobile-board-footer {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; color: #52635e; font-size: 11px; font-weight: 800; }}
.mobile-board-footer span, .mobile-board-footer em {{ min-height: 25px; display: inline-flex; align-items: center; padding: 0 8px; border-radius: 6px; background: rgba(255,255,255,.7); font-style: normal; }}
.mobile-board-footer em {{ margin-left: auto; background: var(--accent); color: var(--accent-ink); font-weight: 900; }}
.detail-frame, .intro, .pair-page {{ display: grid; gap: 14px; min-width: 0; }}
.detail-head, .panel-head {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; }}
.detail-head, .detail-title, .detail-actions, .detail-board, .leg-rail, .spread-console, .leg-card, .data-card {{ min-width: 0; max-width: 100%; }}
.detail-title h1, .intro h1 {{ margin: 0; font-size: 30px; line-height: 1.1; }}
.page-kicker, .route-subline, .plain, .small, .muted {{ color: #666; font-size: 13px; overflow-wrap: anywhere; }}
.back {{ color: #b18a00; font-weight: 800; }}
.trade-lock, .control-btn, .secondary, .primary {{ display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: 7px; padding: 8px 12px; background: var(--accent); color: var(--dark); font-weight: 800; }}
.secondary, .control-btn.ghost {{ background: #e5e5e5; }}
.pair-anchors {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.pair-anchors a {{ min-height: 34px; display: inline-flex; align-items: center; justify-content: center; padding: 0 11px; border-radius: 7px; background: #e5e5e5; color: var(--dark); font-size: 12px; font-weight: 900; }}
.pair-anchors a:hover {{ background: var(--accent-soft); color: var(--accent-ink); }}
.pair-hero {{ display: grid; grid-template-columns: minmax(220px, .7fr) minmax(360px, 1fr) 220px; gap: 14px; align-items: stretch; padding: 18px; border-radius: 10px; background: var(--dark); color: white; }}
.pair-hero .page-kicker, .pair-hero .route-subline {{ color: #b9c8c3; }}
.pair-hero .back {{ color: var(--accent); }}
.pair-diagram {{ display: grid; grid-template-columns: minmax(0, 1fr) 74px minmax(0, 1fr); gap: 8px; align-items: center; }}
.pair-leg-pill, .pair-score-card {{ display: grid; gap: 6px; padding: 13px; border: 1px solid rgba(255,255,255,.16); border-radius: 8px; background: rgba(255,255,255,.08); min-width: 0; }}
.pair-leg-pill span, .pair-score-card span {{ color: #b9c8c3; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.pair-leg-pill strong {{ font-size: 20px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.pair-leg-pill em {{ color: #dce8e5; font-style: normal; font-size: 12px; overflow-wrap: anywhere; }}
.token-community-pulse {{ display: grid; gap: 12px; }}
.token-pulse-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; }}
.token-pulse-grid article {{ display: grid; gap: 5px; min-height: 82px; align-content: center; padding: 10px; border-radius: 8px; background: var(--row); border: 1px solid #d5dfdc; min-width: 0; }}
.token-pulse-grid span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.token-pulse-grid strong {{ color: var(--dark); font-size: 17px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.token-pulse-grid em {{ color: #52635e; font-size: 12px; font-style: normal; overflow-wrap: anywhere; }}
.token-market-enrichment {{ display: grid; gap: 10px; }}
.token-market-enrichment.loading .table-wrap {{ opacity: .9; }}
.token-market-enrichment.unavailable {{ border-color: rgba(242,109,125,.28); background: #fff8f9; }}
.token-market-actions {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }}
.token-market-actions .mini-action {{ border: 0; cursor: pointer; min-height: 30px; }}
.enrichment-note {{ display: flex; align-items: center; gap: 8px; min-width: 0; padding: 8px 10px; border-radius: 7px; background: var(--row); color: #52635e; font-size: 12px; }}
.enrichment-note span {{ flex: 0 0 auto; padding: 4px 6px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.enrichment-note strong {{ color: var(--dark); font-size: 12px; overflow-wrap: anywhere; }}
.enrichment-state {{ display: grid; gap: 4px; min-height: 72px; align-content: center; padding: 10px; border-radius: 8px; background: var(--row); border: 1px solid #d5dfdc; }}
.enrichment-state strong {{ color: var(--dark); font-size: 14px; }}
.enrichment-state span {{ color: #52635e; font-size: 12px; }}
.market-loading-row td {{ color: #52635e; font-weight: 900; }}
.signal-lifecycle {{ display: grid; gap: 8px; padding: 10px; border-radius: 9px; background: #f7f7f7; border: 1px solid #d0d0d0; min-width: 0; }}
.lifecycle-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
.lifecycle-head h3 {{ margin: 0; font-size: 16px; }}
.lifecycle-head p {{ margin: 3px 0 0; color: #52635e; font-size: 12px; line-height: 1.35; }}
.lifecycle-head span {{ flex: 0 0 auto; padding: 5px 7px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 11px; font-weight: 900; white-space: nowrap; }}
.lifecycle-list {{ display: grid; gap: 7px; }}
.lifecycle-row {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, .9fr) auto; gap: 10px; align-items: center; padding: 9px; border-radius: 7px; background: white; border: 1px solid #dedede; min-width: 0; }}
.lifecycle-row.closed_or_faded {{ border-color: rgba(0,184,132,.35); background: #f4fffb; }}
.lifecycle-row.open_or_unresolved {{ border-color: rgba(191,125,0,.35); background: #fffaf0; }}
.lifecycle-row.close_without_recent_alert {{ border-color: rgba(242,109,125,.28); background: #fff8f9; }}
.lifecycle-row span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.lifecycle-row strong {{ display: block; margin: 2px 0; color: var(--dark); font-size: 15px; overflow-wrap: anywhere; }}
.lifecycle-row em {{ color: #52635e; font-size: 12px; font-style: normal; overflow-wrap: anywhere; }}
.lifecycle-metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; }}
.lifecycle-metrics span {{ display: grid; gap: 2px; padding: 6px; border-radius: 6px; background: var(--row); color: #666; font-size: 10px; min-width: 0; }}
.lifecycle-metrics strong {{ margin: 0; font-size: 13px; }}
.pair-leg-pill.buy {{ border-color: rgba(0,184,132,.45); }}
.pair-leg-pill.sell {{ border-color: rgba(242,109,125,.45); }}
.pair-connector {{ display: grid; gap: 5px; justify-items: center; color: #b9c8c3; font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.pair-connector span {{ width: 100%; border-top: 1px solid rgba(255,255,255,.28); }}
.pair-score-card strong {{ justify-content: flex-start; width: fit-content; font-size: 32px; }}
.score-meta {{ display: flex; gap: 7px; flex-wrap: wrap; }}
.score-meta em {{ padding: 4px 6px; border-radius: 5px; background: rgba(255,255,255,.12); color: #dce8e5; font-size: 11px; font-style: normal; }}
.pair-subnav {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
.auto-refresh-pill {{ position: fixed; right: 14px; bottom: 14px; z-index: 60; display: flex; align-items: center; gap: 7px; min-height: 32px; padding: 5px 6px 5px 10px; border: 1px solid rgba(21,32,31,.12); border-radius: 999px; background: rgba(247,247,247,.94); color: var(--dark); box-shadow: var(--shadow); backdrop-filter: blur(8px); font-size: 11px; font-weight: 900; }}
.auto-refresh-pill span {{ min-width: 64px; text-align: center; }}
.auto-refresh-pill button {{ min-height: 24px; border: 0; border-radius: 999px; padding: 0 8px; background: var(--dark); color: white; cursor: pointer; font-size: 10px; font-weight: 900; }}
.auto-refresh-pill.paused {{ background: #fff8f0; border-color: rgba(191,125,0,.28); }}
.auto-refresh-pill.paused button {{ background: var(--accent); color: var(--accent-ink); }}
.pair-snapshot-banner {{ display: flex; justify-content: space-between; gap: 14px; align-items: center; padding: 12px 14px; border-radius: 10px; border: 1px solid rgba(242,109,125,.42); background: #fff6f7; color: var(--dark); box-shadow: var(--shadow); }}
.pair-snapshot-banner div {{ display: grid; gap: 4px; min-width: 0; }}
.pair-snapshot-banner span {{ color: #a1283d; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.pair-snapshot-banner strong {{ font-size: 19px; line-height: 1.1; }}
.pair-snapshot-banner p {{ margin: 0; max-width: 820px; color: #52635e; font-size: 13px; line-height: 1.35; }}
.pair-snapshot-banner nav {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; flex: 0 0 auto; }}
.pair-snapshot-banner a {{ min-height: 32px; display: inline-flex; align-items: center; justify-content: center; padding: 0 10px; border-radius: 7px; background: var(--dark); color: white; font-size: 12px; font-weight: 900; }}
.pair-snapshot-banner a:first-child {{ background: var(--accent); color: var(--accent-ink); }}
.pair-cockpit {{ display: grid; gap: 14px; padding: 18px; border-radius: 10px; background: var(--dark); color: white; box-shadow: var(--shadow); }}
.pair-cockpit .page-kicker, .pair-cockpit .route-subline {{ color: #b9c8c3; }}
.pair-cockpit .back {{ color: var(--accent); }}
.pair-cockpit-head, .ticket-head, .pair-cockpit-foot {{ display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; }}
.pair-cockpit-grid {{ display: grid; grid-template-columns: minmax(0, 1fr) 315px; gap: 14px; align-items: stretch; }}
.pair-trade-ticket, .pair-edge-panel {{ display: grid; gap: 12px; min-width: 0; padding: 14px; border-radius: 10px; border: 1px solid rgba(255,255,255,.16); background: rgba(255,255,255,.08); }}
.ticket-head strong {{ display: block; margin-top: 4px; font-size: 22px; line-height: 1.08; }}
.ticket-legs {{ display: grid; grid-template-columns: minmax(0, 1fr) 92px minmax(0, 1fr); gap: 10px; align-items: stretch; }}
.ticket-leg {{ display: grid; gap: 5px; min-height: 138px; padding: 14px; border-radius: 9px; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.18); min-width: 0; }}
.ticket-leg.buy {{ border-color: rgba(0,184,132,.55); }}
.ticket-leg.sell {{ border-color: rgba(242,109,125,.55); }}
.ticket-leg span, .pair-edge-panel span, .cockpit-gate span {{ color: #b9c8c3; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.ticket-leg strong {{ font-size: 28px; line-height: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.ticket-leg em, .ticket-leg small, .pair-edge-panel p, .cockpit-gate em, .pair-cockpit-foot p {{ margin: 0; color: #dce8e5; font-size: 12px; font-style: normal; overflow-wrap: anywhere; }}
.ticket-leg b {{ align-self: end; color: white; font-size: 24px; }}
.ticket-bridge {{ display: grid; place-items: center; gap: 6px; color: #b9c8c3; text-align: center; font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.ticket-bridge span {{ width: 100%; border-top: 1px solid rgba(255,255,255,.28); }}
.ticket-bridge em {{ color: #dce8e5; font-size: 11px; font-style: normal; text-transform: none; }}
.pair-edge-panel {{ align-content: start; background: rgba(255,255,255,.11); }}
.pair-edge-panel > strong {{ justify-self: start; width: fit-content; font-size: 46px; line-height: 1; }}
.edge-story {{ display: grid; gap: 9px; min-width: 0; padding: 10px; border-radius: 8px; border: 1px solid rgba(255,255,255,.16); background: rgba(255,255,255,.08); }}
.edge-story div {{ display: grid; gap: 4px; min-width: 0; }}
.edge-story strong {{ color: white; font-size: 19px; line-height: 1.05; }}
.edge-story em {{ color: #dce8e5; font-size: 12px; font-style: normal; overflow-wrap: anywhere; }}
.edge-story .sparkline, .edge-story .spark-empty {{ min-height: 62px; border-radius: 7px; background: rgba(255,255,255,.92); }}
.edge-story .spark-empty {{ display: grid; place-items: center; padding: 10px; color: #41615b; font-size: 11px; font-weight: 900; text-align: center; text-transform: uppercase; }}
.edge-metrics {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 2px; }}
.edge-metrics span {{ display: grid; gap: 5px; min-height: 58px; align-content: center; padding: 8px; border-radius: 7px; background: rgba(255,255,255,.08); }}
.edge-metrics strong {{ color: white; font-size: 18px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.pair-proof-rail {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 7px; }}
.cockpit-gate {{ display: grid; gap: 4px; min-height: 73px; padding: 8px; border-radius: 7px; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.14); min-width: 0; }}
.cockpit-gate.ok {{ border-color: rgba(0,184,132,.45); background: rgba(0,184,132,.12); }}
.cockpit-gate.warn {{ border-color: rgba(200,233,111,.42); background: rgba(200,233,111,.10); }}
.cockpit-gate.missing {{ border-color: rgba(242,109,125,.50); background: rgba(242,109,125,.12); }}
.cockpit-gate strong {{ color: white; font-size: 13px; }}
.cockpit-gate em {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.pair-cockpit-foot {{ align-items: center; padding-top: 2px; }}
.pair-cockpit-foot .pair-anchors a {{ background: rgba(255,255,255,.12); color: white; }}
.pair-cockpit-foot .pair-anchors a:hover {{ background: var(--accent); color: var(--accent-ink); }}
.pair-intel-strip {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)) auto; gap: 8px; align-items: stretch; padding: 10px; border-radius: 10px; background: #f7f7f7; border: 1px solid #d0d0d0; box-shadow: var(--shadow); }}
.pair-intel-strip article {{ display: grid; gap: 4px; align-content: center; min-height: 74px; padding: 9px; border-radius: 7px; background: white; min-width: 0; }}
.pair-intel-strip span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.pair-intel-strip strong {{ color: var(--dark); font-size: 14px; line-height: 1.2; overflow-wrap: anywhere; }}
.pair-intel-strip em {{ color: #52635e; font-size: 12px; font-style: normal; line-height: 1.25; overflow-wrap: anywhere; }}
.pair-intel-links {{ display: grid; gap: 6px; align-content: center; min-width: 88px; }}
.pair-intel-links a {{ min-height: 28px; display: inline-flex; align-items: center; justify-content: center; padding: 0 8px; border-radius: 6px; background: var(--accent); color: var(--accent-ink); font-size: 12px; font-weight: 900; }}
.pair-intel-links a + a {{ background: #e5e5e5; color: var(--dark); }}
.pair-layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 12px; align-items: start; }}
.pair-main, .pair-side {{ display: grid; gap: 10px; min-width: 0; }}
.pair-decision-strip {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }}
.pair-decision-strip article {{ min-height: 82px; display: grid; gap: 5px; padding: 11px; border: 1px solid #d0d0d0; border-radius: 8px; background: #f7f7f7; box-shadow: var(--shadow); }}
.pair-decision-strip span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.pair-decision-strip strong {{ font-size: 17px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.pair-decision-strip em {{ color: #52635e; font-size: 12px; font-style: normal; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.spread-lens, .pair-community, .route-summary-card, .pair-checklist {{ background: #f7f7f7; border: 1px solid #d0d0d0; border-radius: var(--radius); box-shadow: var(--shadow); padding: 14px; }}
.spread-lens-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }}
.spread-lens h2 {{ margin: 2px 0 6px; font-size: 26px; }}
.spread-lens p {{ margin: 0; max-width: 760px; color: #52635e; }}
.read-only-pill {{ align-self: flex-start; padding: 5px 8px; border-radius: 6px; background: var(--accent-soft); color: var(--accent-ink); font-size: 12px; font-weight: 900; white-space: nowrap; }}
.spread-equation {{ display: grid; grid-template-columns: minmax(0, 1fr) 44px minmax(0, 1fr) 44px minmax(0, .8fr); gap: 8px; align-items: stretch; margin: 14px 0; }}
.equation-leg, .equation-result {{ display: grid; align-content: center; gap: 5px; min-height: 94px; padding: 12px; border-radius: 8px; background: white; border: 1px solid #d5dfdc; min-width: 0; }}
.equation-leg.buy {{ border-color: rgba(0,184,132,.45); }}
.equation-leg.sell {{ border-color: rgba(242,109,125,.45); }}
.equation-leg span, .equation-result span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.equation-leg strong {{ font-size: 24px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.equation-leg em, .equation-result em {{ color: #52635e; font-size: 12px; font-style: normal; overflow-wrap: anywhere; }}
.equation-result strong {{ justify-content: flex-start; width: fit-content; font-size: 24px; }}
.equation-operator {{ display: grid; place-items: center; color: #71807c; font-size: 12px; font-weight: 900; text-transform: uppercase; }}
.spread-track {{ height: 12px; margin: 15px 0; border-radius: 999px; background: #e5e5e5; overflow: hidden; }}
.spread-track span {{ display: block; width: var(--spread-width); height: 100%; min-width: 2px; border-radius: inherit; background: var(--green); }}
.spread-lens.negative .spread-track span, .pair-cockpit.negative .spread-track span {{ background: var(--red); }}
.spread-breakdown {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }}
.spread-breakdown span, .context-card, .route-summary-card {{ border-radius: 7px; background: white; padding: 10px; }}
.spread-breakdown b {{ display: block; font-size: 18px; }}
.spread-breakdown em, .context-card span, .context-card em, .route-summary-card span {{ display: block; color: #666; font-size: 12px; font-style: normal; }}
.pair-checklist .panel-head {{ align-items: flex-start; }}
.checklist-grid {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 8px; }}
.check-item {{ display: grid; gap: 4px; min-height: 78px; padding: 9px; border-radius: 7px; background: white; border: 1px solid #dedede; min-width: 0; }}
.check-item.ok {{ border-color: rgba(0,184,132,.35); background: #f4fffb; }}
.check-item.missing {{ border-color: rgba(242,109,125,.32); background: #fff7f8; }}
.check-item span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.check-item strong {{ font-size: 14px; }}
.check-item.ok strong {{ color: #007e61; }}
.check-item.missing strong {{ color: #bf3149; }}
.check-item em {{ color: #52635e; font-size: 11px; font-style: normal; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.pair-community .panel-head {{ align-items: flex-start; }}
.context-score {{ align-self: start; padding: 5px 8px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-weight: 900; font-size: 12px; }}
.context-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-bottom: 10px; }}
.context-card strong, .route-summary-card strong {{ display: block; margin: 3px 0; font-size: 18px; overflow-wrap: anywhere; }}
.route-summary-card {{ box-shadow: none; }}
.route-summary-card p {{ margin: 6px 0; color: #52635e; }}
.detail-board {{ display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 12px; align-items: start; }}
.leg-rail, .detail-grid {{ display: grid; gap: 10px; }}
.leg-card, .spread-console, .data-card, .panel {{ background: #f7f7f7; border: 1px solid #d0d0d0; border-radius: var(--radius); box-shadow: var(--shadow); }}
.leg-card, .data-card, .panel.text, .panel.inset {{ padding: 14px; }}
.leg-card-head, .spread-console-head, .kv-row {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
.leg-card-head span {{ color: #777; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.leg-card-head strong {{ font-size: 18px; }}
.facts, .metric-tape, .metric-grid {{ display: grid; gap: 8px; }}
.facts {{ grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 12px; }}
.facts span, .metric {{ border: 1px solid #d0d0d0; border-radius: 6px; padding: 8px; background: white; color: #666; font-size: 12px; }}
.facts strong, .metric strong {{ display: block; color: var(--dark); overflow-wrap: anywhere; }}
.spread-console {{ padding: 12px; }}
.spread-console-head {{ padding-bottom: 12px; border-bottom: 1px solid #d0d0d0; }}
.spread-console-head strong {{ font-size: 28px; }}
.price-pair {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
.price-pair span {{ border-radius: 5px; background: white; border: 1px solid #d0d0d0; padding: 7px 9px; font-size: 12px; font-weight: 800; }}
.metric-tape {{ grid-template-columns: repeat(5, minmax(0, 1fr)); padding: 12px 0; }}
.metric span, .metric small {{ display: block; color: #666; font-size: 11px; }}
.metric strong {{ margin: 5px 0 2px; font-size: 18px; }}
.detail-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.data-card h2 {{ margin: 0 0 10px; font-size: 16px; }}
.kv-row {{ padding: 7px 0; border-bottom: 1px solid #dedede; font-size: 13px; min-width: 0; }}
.kv-row span {{ color: #666; }}
.kv-row strong {{ min-width: 0; overflow-wrap: anywhere; text-align: right; }}
.plain-list {{ margin: 8px 0 0; padding-left: 18px; color: #666; }}
.plain-list li {{ overflow-wrap: anywhere; }}
.route-timeline {{ display: grid; gap: 12px; padding: 14px; border: 1px solid #d0d0d0; border-radius: var(--radius); background: #f7f7f7; box-shadow: var(--shadow); }}
.timeline-head {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }}
.timeline-head h2 {{ margin: 2px 0 6px; font-size: 24px; }}
.timeline-head p {{ margin: 0; max-width: 760px; color: #52635e; }}
.timeline-stats {{ display: grid; grid-template-columns: repeat(3, minmax(80px, 1fr)); gap: 8px; min-width: 300px; }}
.timeline-stats span, .timeline-dual > div {{ padding: 9px; border-radius: 7px; background: white; color: #666; font-size: 12px; }}
.timeline-stats strong, .timeline-dual span {{ display: block; color: var(--dark); font-weight: 900; }}
.timeline-dual {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
.sparkline {{ width: 100%; height: 72px; display: block; border-radius: 8px; background: linear-gradient(180deg, #fff, #edf4f1); border: 1px solid #d5dfdc; overflow: visible; }}
.sparkline.large {{ height: 124px; }}
.sparkline polyline {{ fill: none; stroke: #007e61; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; vector-effect: non-scaling-stroke; }}
.sparkline.negative polyline {{ stroke: #bf3149; }}
.sparkline circle {{ fill: white; stroke: #007e61; stroke-width: 2; vector-effect: non-scaling-stroke; }}
.sparkline.negative circle {{ stroke: #bf3149; }}
.spark-zero {{ stroke: #aebbb7; stroke-width: 1; stroke-dasharray: 4 4; vector-effect: non-scaling-stroke; }}
.spark-empty {{ min-height: 72px; display: grid; place-items: center; border-radius: 8px; border: 1px dashed #c5d1cd; background: white; color: #71807c; font-size: 12px; font-weight: 800; }}
.panel {{ margin-bottom: 14px; overflow: hidden; }}
.panel-head {{ padding: 14px; border-bottom: 1px solid #d0d0d0; }}
.panel-head.flat {{ padding: 0 0 10px; border: 0; }}
.intel-page {{ display: grid; gap: 16px; }}
.intel-hero {{ min-height: 128px; display: flex; justify-content: space-between; gap: 18px; align-items: flex-end; padding: 20px; background: var(--dark); color: white; border-radius: 10px; }}
.compact-hero {{ min-height: 112px; }}
.intel-hero h1 {{ margin: 4px 0 8px; max-width: 760px; font-size: 34px; line-height: 1.05; }}
.intel-hero p {{ margin: 0; color: #c7d5d1; max-width: 760px; }}
.intel-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
.intel-source-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 10px; }}
.source-card {{ min-height: 78px; display: grid; gap: 5px; padding: 12px; border: 1px solid #d0d0d0; border-radius: 8px; background: #f7f7f7; }}
.source-card span, .source-card em {{ color: #666; font-size: 12px; font-style: normal; }}
.source-card strong {{ font-size: 16px; }}
.source-card.stale strong, .source-card.missing strong, .source-card.error strong {{ color: #bf3149; }}
.source-card.fresh strong {{ color: #007e61; }}
.intel-layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 16px; align-items: start; }}
.intel-main, .intel-side {{ display: grid; gap: 16px; }}
.intel-section, .change-digest, .side-card, .hot-card, .reality-card, .feed-card {{ background: #f7f7f7; border: 1px solid #d0d0d0; border-radius: 10px; box-shadow: var(--shadow); }}
.intel-section {{ padding: 14px; }}
.change-digest {{ display: grid; gap: 10px; padding: 14px; }}
.change-digest .panel-head span {{ align-self: flex-start; padding: 5px 8px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 12px; font-weight: 900; text-transform: uppercase; }}
.change-counts {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }}
.change-counts article {{ display: grid; gap: 4px; min-height: 70px; align-content: center; padding: 9px; border-radius: 7px; background: white; border: 1px solid #d5dfdc; min-width: 0; }}
.change-counts span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.change-counts strong {{ color: var(--dark); font-size: 20px; overflow-wrap: anywhere; }}
.change-counts em {{ color: #52635e; font-size: 12px; font-style: normal; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.change-highlights {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }}
.change-highlight {{ display: grid; gap: 4px; min-height: 76px; padding: 10px; border-radius: 7px; background: var(--row); border: 1px solid transparent; min-width: 0; }}
.change-highlight:hover {{ background: var(--row-hover); border-color: var(--line); }}
.change-highlight span {{ color: #52635e; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.change-highlight strong {{ color: var(--dark); font-size: 18px; overflow-wrap: anywhere; }}
.change-highlight em {{ color: #52635e; font-size: 12px; font-style: normal; overflow-wrap: anywhere; }}
.action-queue-section {{ padding-bottom: 10px; }}
.action-queue {{ display: grid; gap: 8px; }}
.action-row {{ display: grid; grid-template-columns: 34px minmax(92px, .45fr) minmax(250px, 1.1fr) minmax(210px, .8fr) minmax(220px, .9fr); gap: 10px; align-items: stretch; min-height: 90px; padding: 9px; border: 1px solid #d7dedb; border-radius: 8px; background: white; }}
.action-row.inspect_now {{ border-color: rgba(0,184,132,.35); background: #f4fffb; }}
.action-row.setup_needed, .action-row.identity_needed {{ border-color: rgba(111,140,255,.35); background: #f6f7ff; }}
.action-row.stale_route {{ border-color: rgba(242,109,125,.28); background: #fff8f9; }}
.action-rank {{ display: grid; place-items: center; border-radius: 7px; background: var(--dark); color: white; font-size: 15px; font-weight: 900; }}
.action-symbol, .action-route, .action-metrics, .action-next {{ display: grid; align-content: center; gap: 6px; min-width: 0; }}
.action-symbol a {{ color: var(--dark); font-size: 22px; font-weight: 900; overflow-wrap: anywhere; }}
.action-symbol span {{ width: fit-content; padding: 4px 7px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.action-route strong {{ color: var(--dark); font-size: 14px; overflow-wrap: anywhere; }}
.action-route em, .action-next em {{ color: #52635e; font-size: 12px; font-style: normal; overflow-wrap: anywhere; }}
.tag-row.tight span {{ padding: 3px 6px; font-size: 10px; }}
.action-metrics {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
.action-metrics span {{ display: grid; gap: 4px; align-content: center; min-height: 54px; padding: 7px; border-radius: 7px; background: var(--row); color: #666; font-size: 11px; font-weight: 800; text-transform: uppercase; }}
.action-metrics strong {{ color: var(--dark); font-size: 14px; text-transform: none; overflow-wrap: anywhere; }}
.action-next strong {{ color: var(--dark); font-size: 13px; line-height: 1.25; overflow-wrap: anywhere; }}
.action-links {{ display: flex; gap: 7px; flex-wrap: wrap; }}
.action-links a {{ min-height: 27px; display: inline-flex; align-items: center; padding: 0 8px; border-radius: 6px; background: var(--accent); color: var(--accent-ink); font-size: 11px; font-weight: 900; }}
.action-links a + a {{ background: #e5e5e5; color: var(--dark); }}
.hot-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
.hot-card {{ display: grid; gap: 10px; padding: 12px; box-shadow: none; }}
.hot-head, .reality-head, .side-head {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
.hot-head strong {{ font-size: 22px; overflow-wrap: anywhere; }}
.hot-head span, .reality-head span, .side-head span {{ padding: 4px 7px; border-radius: 5px; background: var(--row); color: #52635e; font-size: 11px; font-weight: 800; }}
.hot-score {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }}
.hot-score b {{ font-size: 24px; }}
.hot-score em {{ color: #666; font-style: normal; font-weight: 800; }}
.hot-card p {{ min-height: 36px; margin: 0; color: #52635e; font-size: 13px; }}
.tag-row {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.tag-row span {{ padding: 4px 6px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 11px; font-weight: 800; }}
.mini-kv, .reality-meta {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }}
.mini-kv span, .reality-meta span, .profile-row {{ padding: 7px; border-radius: 6px; background: white; color: #666; font-size: 12px; }}
.mini-kv strong, .reality-meta strong {{ display: block; color: var(--dark); overflow-wrap: anywhere; }}
.reality-stack {{ display: grid; gap: 10px; }}
.reality-card {{ padding: 12px; box-shadow: none; }}
.reality-head a {{ font-size: 20px; font-weight: 900; }}
.reality-routes {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 10px 0; }}
.reality-route {{ display: grid; gap: 4px; padding: 9px; border-radius: 7px; background: var(--row); }}
.reality-route span, .reality-route em {{ color: #666; font-size: 11px; font-style: normal; }}
.reality-route strong {{ font-size: 18px; }}
.plain-list.compact {{ margin-top: 10px; }}
.side-card {{ padding: 14px; }}
.side-card h2, .feed-card h3 {{ margin: 0; font-size: 17px; }}
.side-head {{ margin-bottom: 9px; }}
.side-head .stale, .side-head .missing, .side-head .error {{ color: #bf3149; }}
.side-head .fresh {{ color: #007e61; }}
.brief-body {{ max-height: 210px; overflow: hidden; color: #52635e; font-size: 13px; }}
.brief-body p {{ margin: 0 0 7px; }}
.question-list {{ display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }}
.question-list li {{ display: grid; grid-template-columns: 1fr auto; gap: 4px 10px; padding-bottom: 8px; border-bottom: 1px solid #dedede; }}
.question-list em {{ grid-column: 1 / -1; color: #666; font-size: 12px; font-style: normal; }}
.alert-preview-list {{ display: grid; gap: 7px; }}
.alert-preview-row {{ display: flex; justify-content: space-between; gap: 10px; padding: 8px; border-radius: 6px; background: white; font-size: 12px; }}
.alert-preview-row.would_trigger strong {{ color: #007e61; }}
.alert-preview-row.review_only {{ background: #fff7e4; }}
.alert-preview-row.review_only strong {{ color: #9f6200; }}
.alert-preview-row.quiet strong {{ color: #777; }}
.alerts-page {{ display: grid; gap: 16px; }}
.alert-status-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
.alert-rule-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }}
.alert-rule-card {{ display: grid; gap: 10px; min-height: 230px; padding: 12px; border-radius: 10px; border: 1px solid #d0d0d0; background: #f7f7f7; box-shadow: var(--shadow); }}
.alert-rule-card.would_trigger {{ border-color: rgba(0,184,132,.45); }}
.alert-rule-card.review_only {{ border-color: rgba(191,125,0,.35); background: #fffaf0; }}
.alert-rule-card.quiet {{ opacity: .9; }}
.alert-rule-head {{ display: grid; gap: 4px; }}
.alert-rule-head span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.alert-rule-head strong {{ font-size: 18px; }}
.alert-rule-head em {{ width: fit-content; align-self: start; padding: 4px 7px; border-radius: 5px; background: var(--row); color: #52635e; font-size: 11px; font-style: normal; font-weight: 900; }}
.alert-rule-card.would_trigger .alert-rule-head em {{ background: var(--accent-soft); color: var(--accent-ink); }}
.alert-rule-card.review_only .alert-rule-head em {{ background: #ffe6ad; color: #8a5300; }}
.alert-review-note {{ min-height: 16px; }}
.alert-review-note span {{ color: #8a5300; font-size: 11px; font-weight: 900; }}
.alert-example-list {{ display: grid; gap: 7px; }}
.alert-example {{ display: grid; gap: 3px; padding: 8px; border-radius: 7px; background: white; border: 1px solid #dedede; min-width: 0; }}
.alert-example.stale {{ border-color: rgba(191,125,0,.28); background: #fffaf0; }}
.alert-example span {{ color: #666; font-size: 11px; font-weight: 900; overflow-wrap: anywhere; }}
.alert-example strong {{ font-size: 14px; overflow-wrap: anywhere; }}
.alert-example em {{ color: #52635e; font-size: 11px; font-style: normal; overflow-wrap: anywhere; }}
.alert-template-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }}
.alert-template {{ display: grid; gap: 7px; padding: 11px; border-radius: 8px; background: white; border: 1px solid #dedede; }}
.alert-template strong {{ font-size: 16px; }}
.alert-template p {{ margin: 0; color: #52635e; font-size: 12px; }}
.alert-template span {{ width: fit-content; padding: 4px 6px; border-radius: 5px; background: var(--row); color: #52635e; font-size: 11px; font-weight: 900; }}
.profile-page {{ display: grid; gap: 14px; }}
.profile-heading {{ display: grid; grid-template-columns: minmax(0, 1fr) 220px; gap: 14px; min-height: 112px; padding: 18px; border-radius: 8px; background: var(--terminal-shell); color: var(--terminal-shell-text); }}
.profile-heading h1 {{ margin: 3px 0 6px; font-size: 30px; }}
.profile-heading p {{ margin: 0; color: #adc2bc; font-size: 13px; }}
.profile-mode {{ display: grid; gap: 4px; align-content: center; padding: 12px; border: 1px solid rgba(255,255,255,.14); border-radius: 7px; background: rgba(255,255,255,.07); }}
.profile-mode span, .profile-mode em {{ color: #adc2bc; font-size: 10px; font-style: normal; font-weight: 900; text-transform: uppercase; }}
.profile-mode strong {{ font-size: 20px; }}
.profile-layout {{ display: grid; grid-template-columns: 230px minmax(0, 1fr); gap: 14px; align-items: start; }}
.profile-nav-panel {{ position: sticky; top: 10px; display: grid; gap: 6px; padding: 12px; border: 1px solid var(--terminal-line); border-radius: 8px; background: var(--terminal-panel); }}
.profile-nav-panel > span {{ padding: 4px 7px 8px; color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.profile-nav-item {{ min-height: 43px; display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 7px; align-items: center; padding: 0 9px; border-radius: 6px; color: var(--terminal-text); }}
.profile-nav-item span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; }}
.profile-nav-item strong {{ font-size: 13px; }}
.profile-nav-item.active {{ background: var(--terminal-accent); color: #062f2a; }}
.profile-nav-item.active span {{ color: #16463f; }}
.profile-local-note {{ display: grid; gap: 5px; margin-top: 8px; padding: 10px; border-top: 1px solid var(--terminal-line); }}
.profile-local-note strong {{ font-size: 12px; }}
.profile-local-note p {{ margin: 0; color: var(--terminal-muted); font-size: 11px; line-height: 1.4; }}
.profile-main, .profile-section, .profile-form {{ display: grid; gap: 14px; min-width: 0; }}
.profile-section-title {{ min-height: 68px; display: flex; justify-content: space-between; align-items: center; gap: 14px; }}
.profile-section-title h2 {{ margin: 3px 0 0; font-size: 25px; }}
.profile-section-title p {{ margin: 5px 0 0; color: var(--terminal-muted); font-size: 12px; }}
.profile-state {{ min-height: 28px; display: inline-flex; align-items: center; padding: 0 9px; border-radius: 5px; background: var(--terminal-panel-2); color: var(--terminal-muted); font-size: 11px; font-weight: 900; }}
.profile-state.good {{ background: var(--terminal-accent-soft); color: var(--accent-ink); }}
.profile-summary-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; }}
.profile-summary-grid article {{ min-height: 86px; display: grid; gap: 4px; align-content: center; padding: 11px; border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); }}
.profile-summary-grid span, .profile-status-list span, .profile-panel-head span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.profile-summary-grid strong {{ font-size: 20px; }}
.profile-summary-grid em {{ color: var(--terminal-muted); font-size: 11px; font-style: normal; }}
.profile-panel {{ display: grid; gap: 13px; padding: 14px; border: 1px solid var(--terminal-line); border-radius: 8px; background: var(--terminal-panel); min-width: 0; }}
.profile-panel-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }}
.profile-panel-head h3 {{ margin: 0; font-size: 18px; }}
.profile-panel-head p {{ margin: 4px 0 0; color: var(--terminal-muted); font-size: 12px; }}
.profile-status-list {{ display: grid; }}
.profile-status-list > div {{ display: grid; grid-template-columns: minmax(150px, 1fr) minmax(150px, 1fr) minmax(180px, 1.3fr); gap: 12px; align-items: center; min-height: 48px; border-top: 1px solid var(--terminal-line); }}
.profile-status-list > div:first-child {{ border-top: 0; }}
.profile-status-list strong {{ font-size: 13px; }}
.profile-status-list em {{ color: var(--terminal-muted); font-size: 11px; font-style: normal; }}
.profile-activity-table {{ display: grid; }}
.profile-activity-table > div {{ display: grid; grid-template-columns: 180px 220px minmax(0, 1fr); gap: 12px; min-height: 42px; align-items: center; border-top: 1px solid var(--terminal-line); font-size: 12px; }}
.profile-activity-table > div:first-child {{ border-top: 0; }}
.profile-activity-table span, .profile-activity-table em {{ color: var(--terminal-muted); font-style: normal; }}
.profile-empty {{ margin: 0; padding: 12px; color: var(--terminal-muted); font-size: 12px; }}
.profile-field-grid {{ display: grid; gap: 10px; }}
.profile-field-grid.two {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.profile-field-grid.three {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
.profile-field-grid label, .profile-wide-field, .profile-alert-filters label {{ display: grid; gap: 5px; min-width: 0; }}
.profile-field-grid label > span, .profile-wide-field > span, .profile-alert-filters label > span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.profile-field-grid input, .profile-field-grid select, .profile-wide-field textarea, .profile-alert-filters select {{ width: 100%; min-height: 38px; padding: 8px 10px; border: 1px solid var(--terminal-line); border-radius: 6px; background: var(--terminal-row); color: var(--terminal-text); font: inherit; }}
.profile-wide-field textarea {{ resize: vertical; }}
.profile-switch-field {{ grid-template-columns: minmax(0, 1fr) auto; align-items: center; min-height: 38px; padding: 7px 10px; border: 1px solid var(--terminal-line); border-radius: 6px; background: var(--terminal-row); }}
.profile-switch-field input {{ width: 34px; min-height: 18px; accent-color: var(--terminal-accent); }}
.profile-actions {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
.profile-actions > span {{ color: var(--terminal-muted); font-size: 11px; font-weight: 800; }}
.profile-chip-grid, .profile-exchange-grid, .profile-segmented {{ display: flex; gap: 7px; flex-wrap: wrap; }}
.profile-chip-grid label, .profile-exchange-grid label, .profile-segmented label {{ cursor: pointer; }}
.profile-chip-grid input, .profile-exchange-grid input, .profile-segmented input {{ position: absolute; opacity: 0; pointer-events: none; }}
.profile-chip-grid span, .profile-exchange-grid span, .profile-segmented span {{ min-height: 31px; display: inline-flex; align-items: center; padding: 0 9px; border: 1px solid var(--terminal-line); border-radius: 6px; background: var(--terminal-row); color: var(--terminal-text); font-size: 11px; font-weight: 900; }}
.profile-chip-grid input:checked + span, .profile-exchange-grid input:checked + span, .profile-segmented input:checked + span {{ border-color: var(--terminal-accent); background: var(--terminal-accent-soft); color: var(--accent-ink); }}
.profile-alert-filters {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }}
.profile-alert-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
.profile-alert-card {{ display: grid; gap: 10px; min-height: 260px; padding: 11px; border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-row); }}
.profile-alert-card.triggered {{ border-color: var(--terminal-accent); box-shadow: inset 3px 0 0 var(--terminal-accent); }}
.profile-alert-card.review {{ border-color: #d9a13c; }}
.profile-alert-card.inactive {{ opacity: .68; }}
.profile-alert-card header, .profile-alert-card footer {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
.profile-alert-card header > div {{ display: grid; gap: 3px; }}
.profile-alert-card header span {{ width: fit-content; color: var(--terminal-muted); font-size: 9px; font-weight: 900; text-transform: uppercase; }}
.profile-alert-card header strong {{ font-size: 14px; }}
.profile-alert-card header em {{ padding: 4px 6px; border-radius: 5px; background: var(--terminal-panel-2); font-size: 10px; font-style: normal; font-weight: 900; }}
.profile-alert-symbol, .profile-alert-live {{ display: grid; gap: 4px; }}
.profile-alert-symbol strong {{ font-size: 20px; }}
.profile-alert-symbol span, .profile-alert-live span, .profile-alert-live em, .profile-alert-meta {{ color: var(--terminal-muted); font-size: 10px; font-style: normal; }}
.profile-alert-live {{ padding: 9px; border-radius: 6px; background: var(--terminal-panel-2); }}
.profile-alert-live strong {{ font-size: 19px; }}
.profile-alert-meta {{ display: flex; justify-content: space-between; gap: 7px; }}
.profile-alert-card footer {{ margin-top: auto; border-top: 1px solid var(--terminal-line); padding-top: 9px; }}
.profile-alert-card footer label {{ display: inline-flex; align-items: center; gap: 5px; font-size: 10px; font-weight: 900; }}
.profile-alert-card footer input {{ accent-color: var(--terminal-accent); }}
.profile-alert-card footer button {{ min-height: 27px; padding: 0 7px; border: 1px solid var(--terminal-line); border-radius: 5px; background: var(--terminal-panel); color: var(--terminal-text); cursor: pointer; font-size: 10px; font-weight: 900; }}
.profile-alert-card footer .profile-alert-delete {{ color: #c44156; }}
.profile-alert-empty {{ grid-column: 1 / -1; min-height: 150px; display: grid; place-content: center; gap: 6px; text-align: center; color: var(--terminal-muted); }}
.profile-alert-empty strong {{ color: var(--terminal-text); font-size: 18px; }}
.profile-alert-empty p {{ margin: 0; font-size: 12px; }}
.alert-modal-backdrop {{ position: fixed; inset: 0; z-index: 1000; display: grid; place-items: center; padding: 18px; background: rgba(3,9,8,.72); }}
.alert-modal {{ width: min(660px, 100%); max-height: calc(100vh - 36px); overflow: auto; display: grid; gap: 13px; padding: 15px; border: 1px solid var(--terminal-line); border-radius: 8px; background: var(--terminal-panel); color: var(--terminal-text); box-shadow: 0 22px 60px rgba(0,0,0,.28); }}
.alert-modal > header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
.alert-modal > header span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.alert-modal > header h2 {{ margin: 3px 0 0; font-size: 22px; }}
.icon-close {{ width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid var(--terminal-line); border-radius: 6px; background: var(--terminal-row); color: var(--terminal-text); cursor: pointer; font-weight: 900; }}
.alert-modal-route {{ display: grid; gap: 4px; padding: 11px; border-radius: 6px; background: var(--terminal-panel-2); }}
.alert-modal-route strong {{ font-size: 22px; }}
.alert-modal-route span, .alert-modal-route em {{ color: var(--terminal-muted); font-size: 11px; font-style: normal; }}
.alert-modal form {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
.alert-modal form label {{ display: grid; gap: 5px; }}
.alert-modal form label > span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.alert-modal form input, .alert-modal form select {{ width: 100%; min-height: 38px; padding: 0 9px; border: 1px solid var(--terminal-line); border-radius: 6px; background: var(--terminal-row); color: var(--terminal-text); font: inherit; }}
.alert-modal-switch {{ grid-template-columns: minmax(0, 1fr) auto; align-items: center; padding: 7px 9px; border: 1px solid var(--terminal-line); border-radius: 6px; }}
.alert-modal-switch input {{ width: auto !important; min-height: auto !important; accent-color: var(--terminal-accent); }}
.alert-modal form p, .alert-modal form footer {{ grid-column: 1 / -1; }}
.alert-modal form p {{ margin: 0; color: var(--terminal-muted); font-size: 11px; }}
.alert-modal form footer {{ display: flex; justify-content: flex-end; gap: 8px; }}
.profile-toast {{ position: fixed; right: 16px; bottom: 16px; z-index: 1100; max-width: min(420px, calc(100vw - 32px)); padding: 10px 13px; border-radius: 6px; background: var(--terminal-shell); color: var(--terminal-shell-text); opacity: 0; transform: translateY(8px); pointer-events: none; transition: .16s ease; font-size: 12px; font-weight: 900; }}
.profile-toast.show {{ opacity: 1; transform: translateY(0); }}
body.alert-modal-open {{ overflow: hidden; }}
.triage-page {{ display: grid; gap: 16px; }}
.triage-summary-grid {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; }}
.triage-layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 390px; gap: 16px; align-items: start; }}
.triage-main, .triage-side {{ display: grid; gap: 16px; min-width: 0; }}
.triage-lane {{ display: grid; gap: 10px; min-width: 0; padding: 14px; border-radius: 10px; border: 1px solid #d0d0d0; background: #f7f7f7; box-shadow: var(--shadow); }}
.triage-lane .panel-head span {{ align-self: flex-start; padding: 5px 8px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 12px; font-weight: 900; }}
.triage-card-list {{ display: grid; gap: 8px; min-width: 0; }}
.triage-card {{ display: grid; gap: 9px; padding: 11px; border-radius: 8px; background: white; border: 1px solid #dedede; min-width: 0; }}
.triage-card.stale {{ border-color: rgba(242,109,125,.30); background: #fff8f9; }}
.triage-card.fresh {{ border-color: rgba(0,184,132,.22); }}
.triage-card.funding {{ border-color: rgba(0,184,132,.32); }}
.triage-card.community {{ border-color: rgba(111,140,255,.35); }}
.triage-card.source {{ border-color: rgba(242,109,125,.32); }}
.triage-card-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; min-width: 0; }}
.triage-card-head a, .triage-card-head strong {{ min-width: 0; font-size: 19px; font-weight: 900; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.triage-card-head span {{ flex: 0 1 auto; min-width: 0; max-width: 48%; padding: 4px 7px; border-radius: 5px; background: var(--row); color: #52635e; font-size: 11px; font-weight: 900; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.triage-card p {{ margin: 0; color: #52635e; font-size: 12px; overflow-wrap: anywhere; }}
.triage-freshness {{ display: flex; justify-content: space-between; gap: 8px; align-items: center; padding: 7px 8px; border-radius: 6px; background: #f2f5f3; color: #52635e; }}
.triage-freshness span {{ min-width: 0; font-size: 11px; font-weight: 900; text-transform: uppercase; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.triage-freshness strong {{ flex: 0 0 auto; color: var(--dark); font-size: 12px; }}
.triage-freshness.stale {{ background: #ffe9ed; color: #a1283d; }}
.triage-freshness.stale strong {{ color: #a1283d; }}
.triage-metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; }}
.triage-card.source .triage-metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.triage-metrics span {{ padding: 7px; border-radius: 6px; background: #f2f5f3; color: #666; font-size: 11px; min-width: 0; }}
.triage-metrics strong {{ display: block; color: var(--dark); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.triage-tags {{ display: flex; gap: 6px; flex-wrap: wrap; min-width: 0; }}
.triage-tags span {{ min-width: 0; max-width: 100%; padding: 4px 6px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 11px; font-weight: 800; line-height: 1.2; overflow-wrap: anywhere; }}
.watchlist-page {{ display: grid; gap: 16px; }}
.watch-status-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }}
.watchlist-layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 16px; align-items: start; }}
.watchlist-main, .watchlist-side {{ display: grid; gap: 16px; min-width: 0; }}
.watch-panel {{ display: grid; gap: 12px; padding: 14px; border-radius: 10px; border: 1px solid #d0d0d0; background: #f7f7f7; box-shadow: var(--shadow); min-width: 0; }}
.watch-control-row {{ display: grid; grid-template-columns: minmax(160px, 1fr) auto auto auto; gap: 8px; align-items: center; }}
.watch-input {{ width: 100%; min-height: 36px; border: 1px solid #cfcfcf; border-radius: 7px; padding: 0 10px; background: white; color: var(--dark); font-weight: 800; text-transform: uppercase; }}
.watch-items {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
.watch-token-card {{ position: relative; display: grid; gap: 9px; min-height: 148px; padding: 12px; border-radius: 9px; background: white; border: 1px solid #dedede; }}
.watch-token-card > div:first-child {{ display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }}
.watch-token-card strong {{ font-size: 22px; overflow-wrap: anywhere; }}
.watch-token-card span {{ color: #666; font-size: 12px; font-weight: 800; }}
.watch-token-card p {{ margin: 0; color: #52635e; font-size: 13px; overflow-wrap: anywhere; }}
.watch-token-metrics {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }}
.watch-token-metrics span {{ display: block; padding: 7px; border-radius: 6px; background: var(--row); color: #666; font-size: 11px; }}
.watch-token-metrics strong {{ display: block; color: var(--dark); font-size: 14px; }}
.watch-remove {{ justify-self: start; min-height: 30px; padding: 0 9px; border: 0; border-radius: 6px; background: #e5e5e5; color: var(--dark); font-size: 12px; font-weight: 900; cursor: pointer; }}
.suggestion-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
.suggestion-chip {{ min-height: 42px; display: flex; align-items: center; justify-content: space-between; gap: 8px; border: 1px solid #d6dfdc; border-radius: 8px; background: white; color: var(--dark); padding: 8px 9px; cursor: pointer; min-width: 0; }}
.suggestion-chip strong {{ min-width: 0; font-size: 17px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.suggestion-chip span {{ padding: 4px 6px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 11px; font-weight: 900; }}
.watch-route-list, .watch-alert-list, .profile-shell-list, .source-note-list {{ display: grid; gap: 8px; }}
.watch-route-card {{ display: grid; gap: 10px; padding: 12px; border-radius: 9px; background: white; border: 1px solid #dedede; }}
.watch-route-head {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
.watch-route-head a {{ font-size: 21px; font-weight: 900; }}
.watch-route-head span {{ padding: 4px 7px; border-radius: 5px; background: var(--row); color: #52635e; font-size: 11px; font-weight: 900; }}
.watch-route-links {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
.watch-route-link {{ display: grid; gap: 4px; padding: 9px; border-radius: 7px; background: var(--row); }}
.watch-route-link span, .watch-route-link em {{ color: #666; font-size: 11px; font-style: normal; }}
.watch-route-link strong {{ font-size: 18px; }}
.watch-route-meta {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }}
.watch-route-meta span {{ padding: 8px; border-radius: 6px; background: #f2f5f3; color: #666; font-size: 12px; }}
.watch-route-meta strong {{ display: block; color: var(--dark); overflow-wrap: anywhere; }}
.watch-route-card p {{ margin: 0; color: #52635e; font-size: 12px; overflow-wrap: anywhere; }}
.watch-alert-list {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
.watch-alert-card {{ display: grid; gap: 5px; padding: 10px; border-radius: 8px; background: white; border: 1px solid #dedede; }}
.watch-alert-card.on {{ border-color: rgba(0,184,132,.45); background: #f3fffb; }}
.watch-alert-card span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.watch-alert-card strong {{ font-size: 17px; overflow-wrap: anywhere; }}
.watch-alert-card em {{ color: #007e61; font-size: 13px; font-style: normal; font-weight: 900; }}
.watch-profile-row {{ display: grid; grid-template-columns: 1fr auto 28px; gap: 8px; align-items: center; padding: 8px; border-radius: 7px; background: white; border: 1px solid #dedede; }}
.watch-profile-row span {{ color: #666; font-size: 12px; }}
.watch-profile-row strong {{ font-size: 12px; }}
.watch-profile-row em {{ color: var(--dark); font-style: normal; font-weight: 900; text-align: right; }}
.source-note-list {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.source-note-list span {{ padding: 8px; border-radius: 7px; background: white; border: 1px solid #dedede; color: #52635e; font-size: 12px; font-weight: 800; overflow-wrap: anywhere; }}
.watch-empty {{ margin: 0; padding: 14px; border-radius: 8px; background: white; color: #71807c; text-align: center; font-size: 13px; font-weight: 800; }}
.watch-empty.compact {{ padding: 9px; text-align: left; }}
.profile-row {{ display: grid; grid-template-columns: 1fr auto 24px; gap: 7px; align-items: center; margin-top: 7px; }}
.profile-row strong {{ color: var(--dark); }}
.watchlist-line {{ padding: 8px; border-radius: 6px; background: var(--row); font-weight: 800; }}
.feed-columns {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
.feed-card {{ padding: 12px; box-shadow: none; }}
.feed-row {{ display: grid; grid-template-columns: 1fr auto; gap: 4px 8px; padding: 8px 0; border-bottom: 1px solid #dedede; font-size: 12px; }}
.feed-row span {{ font-size: 16px; font-weight: 900; }}
.feed-row em {{ color: #007e61; font-style: normal; font-weight: 900; }}
.feed-row small {{ color: #666; }}
.signals-page, .funding-page {{ display: grid; gap: 16px; }}
.signal-board {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
.signal-lane, .funding-card {{ background: #f7f7f7; border: 1px solid #d0d0d0; border-radius: 10px; box-shadow: var(--shadow); padding: 12px; }}
.signal-list {{ display: grid; gap: 8px; }}
.signal-event {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; padding: 10px; border-radius: 7px; background: white; border: 1px solid #dedede; }}
.signal-event span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.signal-event strong {{ display: block; margin: 2px 0; font-size: 18px; }}
.signal-event p {{ margin: 0; color: #52635e; font-size: 12px; overflow-wrap: anywhere; }}
.signal-event aside {{ display: grid; justify-items: end; align-content: center; gap: 4px; }}
.signal-event aside b {{ color: #007e61; font-size: 16px; }}
.signal-event aside em {{ color: #666; font-style: normal; font-size: 11px; }}
.signal-split {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; align-items: start; }}
.funding-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
.funding-card {{ display: grid; gap: 10px; }}
.funding-value {{ justify-content: flex-start; width: fit-content; font-size: 24px; }}
.charts-page {{ display: grid; gap: 16px; }}
.chart-kind-tabs {{ display: flex; gap: 14px; overflow-x: auto; padding: 0 2px; }}
.chart-summary-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }}
.chart-summary-card {{ min-height: 88px; display: grid; gap: 6px; padding: 12px; border-radius: 8px; background: #f7f7f7; border: 1px solid #d0d0d0; box-shadow: var(--shadow); }}
.chart-summary-card span, .chart-summary-card em {{ color: #666; font-size: 12px; font-style: normal; }}
.chart-summary-card strong {{ font-size: 19px; overflow-wrap: anywhere; }}
.chart-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
.chart-route-card {{ display: grid; gap: 10px; padding: 12px; border-radius: 10px; background: #f7f7f7; border: 1px solid #d0d0d0; box-shadow: var(--shadow); }}
.chart-card-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }}
.chart-card-head a {{ display: grid; gap: 2px; min-width: 0; }}
.chart-card-head strong {{ font-size: 22px; overflow-wrap: anywhere; }}
.chart-card-head span {{ color: #666; font-size: 12px; }}
.chart-card-head b {{ font-size: 18px; }}
.chart-route-card p {{ min-height: 36px; margin: 0; color: #52635e; font-size: 13px; }}
.chart-card-metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; }}
.chart-card-metrics span {{ padding: 7px; border-radius: 6px; background: white; color: #666; font-size: 11px; }}
.chart-card-metrics strong {{ display: block; color: var(--dark); font-size: 13px; overflow-wrap: anywhere; }}
.chart-card-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.chart-heading {{ align-items: end; }}
.chart-builder {{ border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); overflow: hidden; }}
.chart-builder-title {{ min-height: 58px; display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 10px 14px; border-bottom: 1px solid var(--terminal-line); }}
.chart-builder-title > div:first-child {{ display: grid; grid-template-columns: 34px auto; column-gap: 10px; align-items: center; }}
.chart-builder-title strong {{ color: var(--terminal-text); font-size: 15px; }}
.chart-builder-title em {{ grid-column: 2; color: var(--terminal-muted); font-size: 11px; font-style: normal; }}
.chart-builder-tools {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }}
.chart-builder-tools a {{ padding:6px 8px; border:1px solid var(--terminal-line); color:var(--terminal-text); font-size:11px; font-weight:900; text-decoration:none; }}
.chart-builder-tools a:hover {{ border-color:var(--accent); color:var(--accent); }}
.chart-builder-icon {{ grid-row: 1 / span 2; width: 34px; height: 34px; display: grid; place-items: center; border-radius: 6px; background: var(--terminal-accent-soft); color: var(--terminal-accent); font-size: 21px; font-weight: 900; }}
.chart-builder-state {{ padding: 5px 7px; border-radius: 5px; background: var(--terminal-panel-2); color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.chart-builder-form {{ display: grid; grid-template-columns: minmax(130px,.55fr) minmax(250px,1fr) 36px minmax(250px,1fr); gap: 10px; align-items: end; padding: 14px; }}
.chart-builder-form label, .chart-leg-picker {{ display: grid; gap: 6px; min-width: 0; }}
.chart-builder-form label > span, .chart-leg-picker > span {{ color: var(--terminal-muted); font-size: 9px; font-weight: 900; text-transform: uppercase; }}
.chart-builder-form select, .chart-builder-form input {{ width: 100%; min-height: 40px; padding: 0 10px; border: 1px solid var(--terminal-line); border-radius: 6px; background: var(--terminal-row); color: var(--terminal-text); font: inherit; font-size: 12px; font-weight: 800; }}
.chart-leg-picker.long > span {{ color: var(--terminal-accent); }}
.chart-leg-picker.short > span {{ color: var(--terminal-danger); }}
.chart-quote-preview {{ display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 5px; }}
.chart-quote-preview span {{ min-height: 34px; display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 0 8px; border: 1px solid var(--terminal-line); border-radius: 5px; color: var(--terminal-muted); font-size: 9px; text-transform: uppercase; }}
.chart-quote-preview strong {{ color: var(--terminal-text); font-size: 11px; }}
.chart-swap {{ width: 36px; height: 36px; margin-bottom: 17px; border: 1px solid var(--terminal-line); border-radius: 50%; background: var(--terminal-panel-2); color: var(--terminal-text); cursor: pointer; font-size: 17px; }}
.chart-create-button {{ grid-column: 1 / -1; min-height: 42px; border: 0; border-radius: 6px; background: var(--terminal-accent); color: var(--accent-ink); cursor: pointer; font-weight: 900; }}
.chart-create-button:disabled {{ cursor: not-allowed; opacity: .42; }}
.chart-blank-state {{ min-height: 390px; display: grid; place-content: center; justify-items: center; gap: 7px; border: 1px dashed var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); text-align: center; }}
.chart-blank-state div {{ width: 48px; height: 48px; display: grid; place-items: center; border: 1px solid var(--terminal-line); border-radius: 7px; color: var(--terminal-muted); font-size: 28px; }}
.chart-blank-state strong {{ color: var(--terminal-text); font-size: 18px; }}
.chart-blank-state p {{ max-width: 440px; margin: 0; color: var(--terminal-muted); font-size: 12px; }}
.selected-chart {{ border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); overflow: hidden; }}
.selected-chart-head {{ min-height: 62px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--terminal-line); }}
.selected-chart-head > div {{ display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; align-items: baseline; }}
.selected-chart-head span {{ color: var(--terminal-muted); font-size: 9px; font-weight: 900; text-transform: uppercase; }}
.selected-chart-head strong {{ color: var(--terminal-text); font-size: 24px; }}
.selected-chart-head em {{ grid-column: 1 / -1; color: var(--terminal-muted); font-size: 13px; font-style: normal; }}
.chart-window-tabs {{ display: flex; gap: 3px; }}
.chart-window-tabs a {{ min-width: 34px; min-height: 30px; display: grid; place-items: center; border-radius: 5px; color: var(--terminal-muted); font-size: 10px; font-weight: 900; }}
.chart-window-tabs a.active {{ background: var(--terminal-accent); color: var(--accent-ink); }}
.selected-chart-layout {{ display: grid; grid-template-columns: 250px minmax(0,1fr); min-height: 560px; }}
.chart-leg-stats {{ border-right: 1px solid var(--terminal-line); }}
.chart-leg-stats article {{ min-height: 50%; display: grid; align-content: start; gap: 0; padding: 12px; border-top: 1px solid var(--terminal-line); }}
.chart-leg-stats article:first-child {{ border-top: 0; }}
.chart-leg-stats header {{ display: grid; grid-template-columns: 1fr auto; gap: 3px 8px; padding-bottom: 10px; }}
.chart-leg-stats header span, .chart-leg-stats header em {{ color: var(--terminal-muted); font-size: 11px; font-style: normal; text-transform: uppercase; }}
.chart-leg-stats header strong {{ color: var(--terminal-text); font-size: 17px; }}
.chart-leg-stats header em {{ grid-column: 1 / -1; }}
.chart-leg-stats article > div {{ min-height: 38px; display: flex; align-items: center; justify-content: space-between; gap: 8px; border-top: 1px solid var(--terminal-line); color: var(--terminal-muted); font-size: 12px; }}
.chart-leg-stats article > div strong {{ color: var(--terminal-text); text-align: right; font-size: 13px; }}
.chart-plot-stack {{ display: grid; grid-template-rows: minmax(560px,1fr); min-width: 0; }}
.chart-plot-panel {{ min-width: 0; display: grid; grid-template-rows: auto minmax(0,1fr); padding: 10px 12px 6px; }}
.chart-plot-panel + .chart-plot-panel {{ border-top: 1px solid var(--terminal-line); }}
.chart-plot-title {{ display: flex; align-items: center; gap: 12px; min-height: 38px; color: var(--terminal-muted); font-size: 12px; }}
.chart-plot-title strong {{ color: var(--terminal-text); font-size: 15px; }}
.chart-plot-title em {{ margin-left: auto; font-style: normal; }}
.chart-plot-title em.stale {{ color: var(--terminal-danger); }}
.chart-plot-title button, .funding-history-open {{ margin-left: auto; min-height: 30px; padding: 0 9px; border: 1px solid var(--terminal-line); border-radius: 5px; background: var(--terminal-row); color: var(--terminal-text); cursor: pointer; font: inherit; font-size: 10px; font-weight: 900; }}
.live-spread-chart {{ position: relative; min-width: 0; display: grid; grid-template-rows: auto minmax(500px,1fr) auto; }}
.live-chart-legend {{ min-height: 34px; display: flex; justify-content: flex-end; align-items: center; gap: 14px; flex-wrap: wrap; color: var(--terminal-muted); font-size: 11px; font-weight: 900; }}
.live-chart-legend span, .live-chart-legend button {{ display: inline-flex; align-items: center; gap: 5px; }}
.live-chart-legend button {{ padding: 4px 5px; border: 0; border-radius: 4px; background: transparent; color: inherit; cursor: pointer; font: inherit; opacity: .45; }}
.live-chart-legend button.active {{ background: var(--terminal-panel-2); opacity: 1; }}
.live-chart-legend i {{ width: 15px; height: 3px; border-radius: 1px; background: currentColor; }}
.live-chart-legend .matched {{ color: var(--terminal-accent); }}
.live-chart-legend .entry {{ color: #4f8cff; }}
.live-chart-legend .exit {{ color: var(--terminal-danger); }}
.live-chart-legend .funding-a {{ color: #1ebf8f; }}
.live-chart-legend .funding-b {{ color: #ff7a82; }}
.live-chart-canvas {{ min-width: 0; min-height: 500px; cursor: crosshair; }}
.live-chart-tooltip {{ position: absolute; z-index: 8; min-width: 220px; padding: 10px 12px; border: 1px solid var(--terminal-line); border-radius: 5px; background: color-mix(in srgb,var(--terminal-panel) 94%,transparent); color: var(--terminal-text); box-shadow: 0 10px 30px rgba(0,0,0,.28); pointer-events: none; font-size: 12px; }}
.live-chart-tooltip time {{ display: block; margin-bottom: 6px; color: var(--terminal-muted); font-size: 11px; }}
.live-chart-tooltip span {{ display: flex; justify-content: space-between; gap: 16px; margin-top: 3px; }}
.live-chart-tooltip strong {{ color: var(--terminal-text); }}
.live-chart-svg {{ width: 100%; height: 100%; min-height: 280px; overflow: visible; }}
.live-chart-grid line {{ stroke: var(--terminal-line); stroke-width: 1; }}
.live-chart-grid text {{ fill: var(--terminal-muted); font-size: 10px; text-anchor: end; }}
.live-chart-grid text.x-label {{ text-anchor: middle; }}
.live-chart-lines polyline {{ fill: none; stroke-width: 1.7; vector-effect: non-scaling-stroke; }}
.live-chart-lines polyline.matched {{ stroke: var(--terminal-accent); stroke-width: 2.7; }}
.live-chart-lines polyline.entry {{ stroke: #4f8cff; stroke-dasharray: 5 4; }}
.live-chart-lines polyline.exit {{ stroke: var(--terminal-danger); stroke-width: 2.2; }}
.live-chart-svg .zero-line {{ stroke: var(--terminal-muted); stroke-width: 1; stroke-dasharray: 3 4; opacity: .65; }}
.live-chart-hits .chart-hit {{ fill: transparent; cursor: crosshair; }}
.live-chart-note {{ min-height: 30px; display: flex; justify-content: space-between; align-items: center; gap: 12px; color: var(--terminal-muted); font-size: 9px; }}
.live-chart-note strong {{ color: var(--terminal-text); white-space: nowrap; }}
.live-chart-note strong.stale {{ color: var(--terminal-danger); }}
.asset-chart-symbol {{ color: var(--terminal-text); font-size: inherit; font-weight: 900; text-decoration: none; }}
.asset-chart-symbol:hover {{ color: var(--terminal-accent); text-decoration: underline; text-underline-offset: 3px; }}
.dual-chart-wrap {{ min-width: 0; display: grid; grid-template-rows: auto minmax(0,1fr); }}
.dual-chart-legend {{ display: flex; justify-content: flex-end; gap: 12px; min-height: 24px; }}
.dual-chart-legend span {{ font-size: 10px; font-weight: 900; }}
.dual-chart-legend span.entry {{ color: var(--terminal-accent); }}
.dual-chart-legend span.exit {{ color: var(--terminal-danger); }}
.dual-chart {{ width: 100%; height: 100%; min-height: 280px; }}
.dual-chart.compact {{ min-height: 130px; }}
.dual-chart-grid line {{ stroke: var(--terminal-line); stroke-width: 1; }}
.dual-chart-grid text {{ fill: var(--terminal-muted); font-size: 10px; text-anchor: end; }}
.dual-chart-lines polyline {{ fill: none; stroke-width: 2; vector-effect: non-scaling-stroke; }}
.dual-chart-lines polyline.entry {{ stroke: var(--terminal-accent); }}
.dual-chart-lines polyline.exit {{ stroke: var(--terminal-danger); }}
.chart-data-empty {{ min-height: 150px; display: grid; place-items: center; border: 1px dashed var(--terminal-line); color: var(--terminal-muted); font-size: 11px; }}
.selected-chart-foot {{ min-height: 46px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 14px; border-top: 1px solid var(--terminal-line); color: var(--terminal-muted); font-size: 10px; }}
.selected-chart-foot a {{ color: var(--terminal-accent); font-weight: 900; }}
.funding-history-dialog {{ width: min(860px,calc(100vw - 30px)); max-height: calc(100vh - 40px); padding: 0; border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); color: var(--terminal-text); box-shadow: 0 22px 70px rgba(0,0,0,.35); }}
.funding-history-dialog::backdrop {{ background: rgba(3,9,8,.72); }}
.funding-history-head {{ min-height: 54px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 12px; border-bottom: 1px solid var(--terminal-line); }}
.funding-history-head > div {{ display: flex; align-items: baseline; gap: 9px; }}
.funding-history-head strong {{ font-size: 17px; }}
.funding-history-head span {{ color: var(--terminal-muted); font-size: 11px; }}
.funding-history-head button {{ width: 30px; height: 30px; border: 1px solid var(--terminal-line); border-radius: 5px; background: var(--terminal-row); color: var(--terminal-text); cursor: pointer; }}
.funding-history-scroll {{ max-height: calc(100vh - 110px); overflow: auto; }}
.funding-history-dialog table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
.funding-history-dialog th, .funding-history-dialog td {{ padding: 9px 11px; border-bottom: 1px solid var(--terminal-line); text-align: right; white-space: nowrap; }}
.funding-history-dialog th:first-child, .funding-history-dialog td:first-child {{ text-align: left; }}
.funding-history-dialog th {{ position: sticky; top: 0; background: var(--terminal-panel-2); color: var(--terminal-muted); font-size: 9px; text-transform: uppercase; }}
.funding-history-dialog td {{ color: var(--terminal-text); }}
.mini-action {{ display: inline-flex; align-items: center; min-height: 30px; padding: 0 9px; border-radius: 6px; background: #e5e5e5; font-size: 12px; font-weight: 900; }}
.mini-action.primary-link {{ background: var(--accent); color: var(--accent-ink); }}
.community-page {{ display: grid; gap: 16px; }}
.community-layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 16px; align-items: start; }}
.community-main, .community-side {{ display: grid; gap: 16px; min-width: 0; }}
.community-panel, .discussion-card, .score-lane {{ background: #f7f7f7; border: 1px solid #d0d0d0; border-radius: 10px; box-shadow: var(--shadow); }}
.community-panel {{ padding: 14px; }}
.status-pill {{ align-self: flex-start; padding: 5px 8px; border-radius: 5px; background: var(--row); color: #52635e; font-size: 12px; font-weight: 900; }}
.status-pill.fresh {{ background: var(--accent-soft); color: var(--accent-ink); }}
.status-pill.stale, .status-pill.missing, .status-pill.error {{ background: #ffe1e6; color: #a1283d; }}
.scoreboard-grid, .community-events {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
.score-lane {{ padding: 12px; box-shadow: none; }}
.score-lane h3 {{ margin: 0 0 10px; font-size: 16px; }}
.score-list {{ display: grid; gap: 7px; }}
.score-row {{ display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 8px; padding: 8px; border-radius: 7px; background: white; border: 1px solid #dedede; }}
.score-row strong {{ font-size: 18px; overflow-wrap: anywhere; }}
.score-row span {{ font-weight: 900; }}
.score-row.positive span {{ color: #007e61; }}
.score-row.negative span {{ color: #bf3149; }}
.score-row em {{ color: #666; font-style: normal; font-size: 11px; }}
.call-ledger-panel {{ display: grid; gap: 10px; }}
.call-ledger-list {{ display: grid; gap: 8px; }}
.call-ledger-row {{ display: grid; grid-template-columns: 140px minmax(0, 1.45fr) minmax(280px, .95fr) minmax(260px, .9fr); gap: 10px; align-items: stretch; padding: 10px; border-radius: 8px; background: white; border: 1px solid #dedede; min-width: 0; }}
.call-ledger-row.inspect_route {{ border-color: rgba(0,184,132,.45); background: #f4fffb; }}
.call-ledger-row.result_reported {{ border-color: rgba(53,199,186,.45); background: #f2fffd; }}
.call-ledger-row.closed_or_faded, .call-ledger-row.stale_board_match {{ border-color: rgba(242,109,125,.3); background: #fff7f8; }}
.call-ledger-symbol, .call-ledger-story, .call-ledger-route, .call-ledger-next {{ min-width: 0; }}
.call-ledger-symbol {{ display: grid; align-content: start; gap: 6px; }}
.call-ledger-symbol a {{ color: var(--dark); font-size: 21px; font-weight: 950; overflow-wrap: anywhere; }}
.call-ledger-symbol span {{ width: fit-content; padding: 4px 7px; border-radius: 5px; background: var(--accent-soft); color: var(--accent-ink); font-size: 11px; font-weight: 900; }}
.call-ledger-story {{ display: grid; gap: 5px; align-content: start; }}
.call-ledger-story strong {{ color: var(--dark); font-size: 13px; overflow-wrap: anywhere; }}
.call-ledger-story em, .call-ledger-next em {{ color: #52635e; font-size: 12px; font-style: normal; overflow-wrap: anywhere; }}
.call-ledger-route {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }}
.call-ledger-route span {{ display: grid; gap: 2px; padding: 7px; border-radius: 6px; background: var(--row); color: #666; font-size: 11px; font-weight: 800; }}
.call-ledger-route strong {{ color: var(--dark); overflow-wrap: anywhere; }}
.call-ledger-next {{ display: grid; gap: 6px; align-content: start; }}
.call-ledger-next > strong {{ color: var(--dark); font-size: 13px; overflow-wrap: anywhere; }}
.discussion-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
.discussion-card {{ display: grid; gap: 10px; padding: 12px; box-shadow: none; }}
.discussion-card p {{ min-height: 32px; margin: 0; color: #52635e; font-size: 13px; }}
.sources-page {{ display: grid; gap: 16px; }}
.source-summary-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
.sources-layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 16px; align-items: start; }}
.sources-main, .sources-side {{ display: grid; gap: 16px; min-width: 0; }}
.route-source-grid, .source-files-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
.source-status-card, .source-artifact-card {{ display: grid; gap: 10px; padding: 12px; border-radius: 9px; background: white; border: 1px solid #dedede; min-width: 0; }}
.source-status-card.ok, .source-status-card.fresh, .source-artifact-card.fresh {{ border-color: rgba(0,184,132,.35); background: #f4fffb; }}
.source-status-card.unavailable, .source-status-card.stale, .source-status-card.missing, .source-artifact-card.stale, .source-artifact-card.missing, .source-artifact-card.error {{ border-color: rgba(242,109,125,.3); background: #fff7f8; }}
.source-status-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }}
.source-status-head div, .source-artifact-card div {{ display: grid; gap: 3px; min-width: 0; }}
.source-status-head span, .source-artifact-card span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.source-status-head strong {{ font-size: 19px; overflow-wrap: anywhere; }}
.source-status-head b {{ padding: 4px 7px; border-radius: 5px; background: var(--row); color: #52635e; font-size: 11px; white-space: nowrap; }}
.source-status-card.ok .source-status-head b, .source-status-card.fresh .source-status-head b {{ background: var(--accent-soft); color: var(--accent-ink); }}
.source-status-card.unavailable .source-status-head b, .source-status-card.stale .source-status-head b, .source-status-card.missing .source-status-head b {{ background: #ffe1e6; color: #a1283d; }}
.source-status-metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; }}
.source-status-metrics span {{ display: grid; gap: 3px; padding: 7px; border-radius: 6px; background: var(--row); color: #666; font-size: 11px; }}
.source-status-metrics strong {{ color: var(--dark); overflow-wrap: anywhere; }}
.source-status-card p, .source-artifact-card p, .source-path-text {{ margin: 0; color: #52635e; font-size: 12px; overflow-wrap: anywhere; }}
.source-artifact-card {{ grid-template-columns: minmax(0, 1fr) auto; }}
.source-artifact-card p {{ grid-column: 1 / -1; }}
.source-artifact-card strong {{ font-size: 16px; }}
.source-artifact-card.fresh strong {{ color: #007e61; }}
.source-artifact-card.stale strong, .source-artifact-card.missing strong, .source-artifact-card.error strong {{ color: #bf3149; }}
.source-artifact-card em {{ color: #666; font-size: 12px; font-style: normal; font-weight: 900; }}
.source-config-grid {{ display: grid; gap: 7px; }}
.source-mode-row {{ display: flex; justify-content: space-between; gap: 10px; padding: 8px; border-radius: 7px; background: white; border: 1px solid #dedede; }}
.source-mode-row span {{ color: #666; font-size: 12px; }}
.source-mode-row strong {{ color: var(--dark); font-size: 12px; text-align: right; overflow-wrap: anywhere; }}
.playbook-page {{ display: grid; gap: 16px; }}
.playbook-status {{ display: grid; grid-template-columns: 180px 180px minmax(0, 1fr); gap: 10px; }}
.playbook-note {{ display: grid; gap: 5px; min-height: 78px; padding: 12px; border-radius: 8px; background: var(--dark); color: white; box-shadow: var(--shadow); }}
.playbook-note span {{ color: #b9c8c3; font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.playbook-note strong {{ font-size: 14px; line-height: 1.25; }}
.playbook-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; align-items: stretch; }}
.playbook-card {{ display: grid; gap: 11px; padding: 14px; border-radius: 10px; background: #f7f7f7; border: 1px solid #d0d0d0; box-shadow: var(--shadow); min-width: 0; }}
.playbook-card.active {{ border-color: rgba(111,140,255,.45); background: #f5f7ff; }}
.playbook-card-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
.playbook-card-head div {{ display: grid; gap: 4px; min-width: 0; }}
.playbook-card-head span {{ color: #666; font-size: 11px; font-weight: 900; text-transform: uppercase; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.playbook-card-head h2 {{ margin: 0; font-size: 20px; line-height: 1.08; }}
.playbook-card-head strong {{ min-width: 34px; height: 34px; display: grid; place-items: center; border-radius: 7px; background: var(--accent-soft); color: var(--accent-ink); font-size: 17px; }}
.playbook-card p {{ margin: 0; color: #52635e; font-size: 13px; }}
.playbook-answer {{ padding: 10px; border-radius: 8px; background: white; border: 1px solid #d5dfdc; font-size: 13px; font-weight: 800; line-height: 1.35; }}
.playbook-steps {{ display: grid; gap: 6px; margin: 0; padding-left: 18px; color: #52635e; font-size: 13px; }}
.playbook-links {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.playbook-links a {{ display: inline-flex; align-items: center; min-height: 30px; padding: 0 9px; border-radius: 6px; background: var(--accent); color: var(--accent-ink); font-size: 12px; font-weight: 900; }}
.playbook-examples {{ display: grid; gap: 7px; margin-top: auto; }}
.playbook-examples span {{ display: grid; gap: 3px; padding-top: 8px; border-top: 1px solid #dedede; color: #666; font-size: 12px; overflow-wrap: anywhere; }}
.playbook-examples strong {{ color: var(--dark); }}
.playbook-guard {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; padding: 16px; border-radius: 10px; background: var(--dark); color: white; box-shadow: var(--shadow); }}
.playbook-guard h2 {{ margin: 2px 0 6px; }}
.playbook-guard p {{ margin: 0; max-width: 740px; color: #dce8e5; }}
.playbook-guard-list {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
.playbook-guard-list span {{ min-height: 30px; display: inline-flex; align-items: center; padding: 0 9px; border-radius: 6px; background: rgba(255,255,255,.10); color: #dce8e5; font-size: 12px; font-weight: 900; }}
.route-list, .two-col, .learn-grid {{ display: grid; gap: 14px; }}
.route-list {{ grid-template-columns: repeat(3, minmax(0, 1fr)); padding: 14px; }}
.two-col, .learn-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.route-card {{ display: grid; gap: 5px; padding: 12px; background: var(--row); border-radius: 8px; }}
table {{ width: 100%; border-collapse: collapse; min-width: 980px; }}
th, td {{ padding: 10px; border-bottom: 1px solid #d0d0d0; text-align: left; }}
th {{ font-size: 12px; color: #555; }}
.empty {{ color: #777; text-align: center; padding: 24px; }}
.spread-list {{ margin: 12px 0 0; padding-left: 20px; }}
.callout {{ background: var(--yellow-chip); padding: 12px; border-radius: 8px; margin-top: 12px; }}
pre {{ background: var(--dark); color: white; padding: 14px; border-radius: 8px; overflow: auto; }}
.compact-kpis {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
.compact-kpis article {{ min-height: 68px; }}
.token-board-title {{ padding: 2px 2px 8px; }}
.token-group-list, .funding-group-list {{ display: grid; gap: 7px; }}
.token-route-group, .funding-token-group {{ min-width: 0; border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-row); overflow: hidden; }}
.token-route-group[open], .funding-token-group[open] {{ border-color: rgba(31,184,165,.58); }}
.token-route-summary, .funding-token-group > summary {{ list-style: none; cursor: pointer; }}
.token-route-summary::-webkit-details-marker, .funding-token-group > summary::-webkit-details-marker {{ display: none; }}
.token-route-summary {{ display: grid; grid-template-columns: minmax(180px,1.2fr) minmax(180px,1.25fr) 108px 108px 100px 82px; gap: 10px; min-height: 72px; align-items: center; padding: 9px 12px; }}
.token-route-summary:hover, .funding-token-group > summary:hover {{ background: var(--terminal-row-hover); }}
.asset-identity {{ display: flex; align-items: center; gap: 10px; min-width: 0; }}
.asset-monogram {{ flex: 0 0 38px; width: 38px; height: 38px; display: grid; place-items: center; border-radius: 7px; background: var(--terminal-accent-soft); color: var(--accent-ink); font-size: 12px; font-weight: 900; }}
.asset-identity > span:last-child, .best-route, .group-number, .group-routes {{ display: grid; gap: 3px; min-width: 0; }}
.asset-identity strong {{ font-size: 18px; line-height: 1; }}
.asset-identity em, .best-route span, .best-route em, .group-number span, .group-number em, .group-routes span, .group-routes em {{ color: var(--terminal-muted); font-size: 10px; font-style: normal; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.best-route strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }}
.best-route i {{ color: var(--terminal-muted); font-style: normal; }}
.group-number strong, .group-routes strong {{ font-size: 16px; }}
.group-age {{ display: flex; align-items: center; justify-content: flex-end; gap: 10px; color: var(--terminal-muted); font-size: 11px; }}
.group-age span, .funding-chevron {{ font-size: 20px; transition: transform .18s ease; }}
.token-route-group[open] .group-age span, .funding-token-group[open] .funding-chevron {{ transform: rotate(180deg); }}
.token-route-body {{ border-top: 1px solid var(--terminal-line); background: var(--terminal-panel-2); }}
.expanded-asset-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 38px; padding: 7px 10px; border-bottom: 1px solid var(--terminal-line); color: var(--terminal-muted); font-size: 11px; font-weight: 800; }}
.expanded-asset-bar div {{ display: flex; gap: 6px; }}
.expanded-asset-bar a, .route-actions a, .funding-pair-row .route-actions a {{ min-height: 26px; display: inline-flex; align-items: center; padding: 0 8px; border: 1px solid var(--terminal-line); border-radius: 5px; background: var(--terminal-row); color: var(--terminal-text); font-size: 10px; font-weight: 900; }}
.route-detail-table {{ min-width: 980px; }}
.route-detail-head, .route-detail-row {{ display: grid; grid-template-columns: minmax(105px,1fr) minmax(105px,1fr) 160px 105px 145px 140px 116px; gap: 8px; align-items: center; }}
.route-detail-head {{ min-height: 34px; padding: 0 10px; color: var(--terminal-muted); font-size: 9px; font-weight: 900; text-transform: uppercase; }}
.route-detail-row {{ min-height: 66px; padding: 7px 10px; border-top: 1px solid var(--terminal-line); background: var(--terminal-row); }}
.route-detail-row:hover {{ background: var(--terminal-row-hover); }}
.route-leg, .route-edge, .route-funding {{ display: grid; gap: 2px; min-width: 0; }}
.route-leg span, .route-edge span, .route-funding span, .route-funding em {{ color: var(--terminal-muted); font-size: 9px; font-style: normal; }}
.route-leg strong {{ font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.exchange-market-link {{ display: inline-flex; align-items: center; gap: 5px; width: fit-content; max-width: 100%; color: inherit; text-decoration: none; }}
.exchange-market-link strong {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.exchange-market-link > span {{ flex: 0 0 auto; color: var(--terminal-accent); font-size: 10px; font-weight: 900; line-height: 1; }}
.exchange-market-link:hover strong, .exchange-market-link:focus-visible strong {{ color: var(--terminal-accent); text-decoration: underline; text-underline-offset: 2px; }}
.exchange-market-link:focus-visible {{ border-radius: 3px; outline: 2px solid var(--terminal-accent); outline-offset: 2px; }}
.route-leg em {{ width: fit-content; padding: 2px 5px; border-radius: 4px; background: var(--terminal-panel-2); color: var(--terminal-muted); font-size: 9px; font-style: normal; }}
.route-prices {{ display: flex; gap: 6px; align-items: center; font-size: 11px; }}
.route-prices span {{ color: var(--terminal-muted); }}
.route-edge strong, .route-funding strong {{ font-size: 14px; }}
.route-rails {{ display: grid; gap: 3px; color: var(--terminal-muted); font-size: 9px; }}
.rail-na {{ color: var(--terminal-muted); }}
.route-actions {{ display: flex; justify-content: flex-end; gap: 5px; align-items: center; }}
.market-mini-row span {{ display: grid; gap: 2px; }}
.market-mini-row span small {{ color: var(--terminal-muted); font-size: 9px; }}
.side-chart-link {{ min-height: 42px; display: flex; justify-content: space-between; align-items: center; padding: 0 10px; border-radius: 6px; background: var(--terminal-accent); color: #062f2b; font-size: 12px; font-weight: 900; }}
.live-market-empty {{ display: grid; gap: 7px; padding: 28px; border: 1px dashed var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); text-align: center; }}
.live-market-empty strong {{ font-size: 18px; }}
.live-market-empty p {{ max-width: 620px; margin: 0 auto; color: var(--terminal-muted); font-size: 12px; }}
.live-market-empty span {{ color: var(--terminal-muted); font-size: 10px; }}
.market-reconnect {{ min-height: 430px; display: grid; place-items: center; padding: 28px 0 64px; }}
.market-reconnect-panel {{ width: min(820px, 100%); padding: 28px; border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); box-shadow: var(--shadow); }}
.market-reconnect-head {{ display: flex; align-items: center; gap: 12px; }}
.market-reconnect-head > div {{ display: grid; gap: 3px; }}
.market-reconnect-head span {{ color: var(--terminal-accent); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.market-reconnect-head h2 {{ margin: 0; color: var(--terminal-text); font-size: 24px; }}
.market-reconnect-dot {{ width: 12px; height: 12px; border-radius: 50%; background: var(--terminal-accent); box-shadow: 0 0 0 0 rgba(31,184,165,.38); animation: market-reconnect-pulse 1.6s ease-out infinite; }}
.market-reconnect-panel > p {{ max-width: 650px; margin: 16px 0 22px; color: var(--terminal-muted); font-size: 13px; line-height: 1.55; }}
.market-reconnect-stats {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid var(--terminal-line); border-radius: 6px; overflow: hidden; }}
.market-reconnect-stats article {{ min-height: 82px; display: grid; align-content: center; gap: 5px; padding: 12px 16px; border-left: 1px solid var(--terminal-line); background: var(--terminal-panel-2); }}
.market-reconnect-stats article:first-child {{ border-left: 0; }}
.market-reconnect-stats span {{ color: var(--terminal-muted); font-size: 9px; font-weight: 900; text-transform: uppercase; }}
.market-reconnect-stats strong {{ color: var(--terminal-text); font-size: 18px; }}
.market-reconnect-actions {{ display: flex; align-items: center; gap: 12px; margin-top: 18px; }}
.market-reconnect-actions span {{ color: var(--terminal-muted); font-size: 10px; }}
@keyframes market-reconnect-pulse {{
  0% {{ box-shadow: 0 0 0 0 rgba(31,184,165,.38); }}
  70%, 100% {{ box-shadow: 0 0 0 10px rgba(31,184,165,0); }}
}}
.token-alert-form {{ display: grid; grid-template-columns: minmax(0,1.6fr) repeat(4, minmax(0,1fr)) auto; gap: 10px; align-items: end; padding: 14px; border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); margin-bottom: 12px; }}
.token-alert-form > div {{ grid-column: 1 / -1; }}
.token-alert-form label {{ display: grid; gap: 4px; min-width: 0; }}
.token-alert-form label span {{ color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; letter-spacing: .06em; }}
.token-alert-form input, .token-alert-form select {{ min-height: 36px; padding: 0 10px; border: 1px solid var(--terminal-line); border-radius: 6px; background: var(--terminal-row); color: inherit; }}
.token-alert-status {{ grid-column: 1 / -1; color: var(--terminal-muted); font-size: 12px; }}
@media(max-width:820px) {{ .token-alert-form {{ grid-template-columns: 1fr 1fr; }} }}
.funding-farm-tabs {{ display: flex; gap: 7px; padding: 7px; border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); }}
.funding-farm-tabs a {{ min-height: 34px; display: inline-flex; align-items: center; padding: 0 12px; border-radius: 5px; color: var(--terminal-muted); font-size: 12px; font-weight: 900; }}
.funding-farm-tabs a.active {{ background: var(--terminal-accent); color: #062f2b; }}
.funding-token-group > summary {{ display: grid; grid-template-columns: minmax(180px,1.3fr) minmax(170px,1.2fr) 100px 100px 100px 62px 24px; gap: 10px; min-height: 70px; align-items: center; padding: 9px 12px; }}
.funding-token-group > summary > div:not(.asset-identity) {{ display: grid; gap: 3px; min-width: 0; }}
.funding-token-group > summary > div:not(.asset-identity) span {{ color: var(--terminal-muted); font-size: 9px; text-transform: uppercase; }}
.funding-token-group > summary > div:not(.asset-identity) strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }}
.funding-pair-list {{ border-top: 1px solid var(--terminal-line); }}
.funding-pair-row {{ display: grid; grid-template-columns: minmax(150px,1fr) minmax(150px,1fr) 110px 110px 80px 130px; gap: 10px; align-items: center; min-height: 66px; padding: 8px 12px; border-top: 1px solid var(--terminal-line); background: var(--terminal-panel-2); }}
.funding-pair-row:first-child {{ border-top: 0; }}
.funding-pair-row > div:not(.route-actions) {{ display: grid; gap: 3px; min-width: 0; }}
.funding-pair-row span, .funding-pair-row em {{ color: var(--terminal-muted); font-size: 9px; font-style: normal; }}
.funding-pair-row strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }}
.chart-token-search {{ display: flex; gap: 5px; }}
.chart-token-search input {{ width: 130px; min-height: 34px; padding: 0 9px; border: 1px solid rgba(255,255,255,.2); border-radius: 5px; background: rgba(255,255,255,.08); color: white; }}
.chart-token-search button {{ min-height: 34px; padding: 0 10px; border: 0; border-radius: 5px; background: var(--terminal-accent); color: #062f2b; font-weight: 900; }}
.chart-asset-index {{ display: flex; align-items: center; gap: 10px; padding: 9px; border: 1px solid var(--terminal-line); border-radius: 7px; background: var(--terminal-panel); }}
.chart-asset-index > span {{ flex: 0 0 auto; color: var(--terminal-muted); font-size: 10px; font-weight: 900; text-transform: uppercase; }}
.chart-asset-index > div {{ display: flex; gap: 5px; overflow-x: auto; }}
.chart-asset-index a {{ flex: 0 0 auto; min-height: 28px; display: inline-flex; align-items: center; gap: 5px; padding: 0 8px; border: 1px solid var(--terminal-line); border-radius: 5px; font-size: 10px; font-weight: 900; }}
.chart-asset-index em {{ font-style: normal; color: var(--terminal-muted); }}
.token-canonical-routes {{ margin-bottom: 14px; }}
.intro h1 small {{ color: var(--muted); font-size: 16px; font-weight: 700; }}
@media (max-width: 960px) {{
  .topbar {{ height: auto; min-height: 50px; padding: 8px 14px; align-items: flex-start; flex-wrap: wrap; }}
  .brand {{ font-size: 20px; }}
  .main-nav {{ display: none; }}
  .header-actions {{ margin-left: auto; }}
  .header-strip {{ height: 8px; }}
  .mobile-primary-nav {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; padding: 6px 8px 8px; background: var(--dark); }}
  .mobile-primary-nav a {{ min-height: 40px; display: grid; place-items: center; border-radius: 8px; color: #dce8e5; font-size: 11px; font-weight: 900; white-space: nowrap; }}
  .mobile-primary-nav a.active {{ background: var(--accent); color: var(--accent-ink); }}
  .mobile-secondary-nav {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 4px; padding: 0 8px 8px; background: var(--dark); border-top: 1px solid rgba(255,255,255,.08); }}
  .mobile-secondary-nav a {{ min-height: 34px; display: grid; place-items: center; border-radius: 7px; color: #b9c8c3; font-size: 10px; font-weight: 900; white-space: nowrap; }}
  .mobile-secondary-nav a.active {{ background: rgba(56,212,189,.18); color: #ffffff; }}
  main {{ padding: 24px 14px 0; }}
  .market-reconnect {{ min-height: 360px; padding: 18px 0 40px; }}
  .market-reconnect-panel {{ padding: 20px 16px; }}
  .market-reconnect-stats {{ grid-template-columns: 1fr; }}
  .market-reconnect-stats article {{ min-height: 64px; border-left: 0; border-top: 1px solid var(--terminal-line); }}
  .market-reconnect-stats article:first-child {{ border-top: 0; }}
  .market-reconnect-actions {{ align-items: flex-start; flex-direction: column; }}
  .chart-builder-form {{ grid-template-columns: 1fr; }}
  .chart-token-field, .chart-leg-picker, .chart-create-button {{ grid-column: 1; }}
  .chart-swap {{ justify-self: center; margin: 0; transform: rotate(90deg); }}
  .chart-builder-title, .selected-chart-head, .selected-chart-foot {{ align-items: flex-start; flex-direction: column; }}
  .selected-chart-layout {{ grid-template-columns: 1fr; min-height: 0; }}
  .chart-leg-stats {{ display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); border-right: 0; border-bottom: 1px solid var(--terminal-line); }}
  .chart-leg-stats article {{ min-height: 0; border-top: 0; border-left: 1px solid var(--terminal-line); }}
  .chart-leg-stats article:first-child {{ border-left: 0; }}
  .chart-plot-stack {{ grid-template-rows: auto; }}
  .dual-chart {{ min-height: 250px; }}
  .dual-chart.compact {{ min-height: 130px; }}
  .funding-history-dialog th, .funding-history-dialog td {{ padding: 8px; }}
  .market-hero, .market-layout {{ grid-template-columns: 1fr; }}
  .market-hero {{ align-items: stretch; padding: 14px; }}
  .market-hero h1 {{ font-size: 24px; }}
  .market-hero-actions {{ justify-content: flex-start; }}
  .market-source-strip, .market-tape {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .market-filter-form {{ grid-template-columns: 1fr; }}
  .market-check {{ justify-content: flex-start; }}
  .market-terminal-scroll {{ overflow-x: visible; background: transparent; border: 0; box-shadow: none; }}
  .market-terminal-head {{ display: none; }}
  .market-terminal-grid {{ min-width: 0; grid-template-columns: 1fr; }}
  .market-terminal-rows {{ padding: 0; gap: 8px; }}
  .market-row {{ min-height: 0; padding: 8px; }}
  .market-row > div {{ padding: 5px 4px; }}
  .market-token-cell strong {{ font-size: 20px; }}
  .market-mini-row {{ grid-template-columns: 1fr; align-items: start; }}
  .market-mini-row span, .market-mini-row em {{ white-space: normal; }}
  .market-empty {{ min-width: 0; }}
  .terminal-heading {{ grid-template-columns: 1fr; min-height: 0; padding: 12px; }}
  .terminal-heading h1 {{ font-size: 20px; }}
  .terminal-heading p {{ display: none; }}
  .terminal-live-box {{ min-width: 0; padding: 8px; justify-items: start; }}
  .terminal-live-box strong {{ font-size: 20px; }}
  .terminal-kpis, .terminal-tape, .funding-tape {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .terminal-kpi, .terminal-kpis article, .terminal-tape article {{ min-height: 64px; padding: 8px; }}
  .terminal-kpi strong, .terminal-kpis strong, .terminal-tape strong {{ font-size: 18px; }}
  .terminal-filter-row {{ grid-template-columns: 1fr; gap: 6px; }}
  .terminal-filter-row > span {{ padding-left: 2px; }}
  .terminal-filter-panel .market-tabs {{ flex-wrap: nowrap; overflow-x: auto; padding-bottom: 2px; }}
  .terminal-filter-panel .market-tab {{ flex: 0 0 auto; }}
  .terminal-filter-panel .market-filter-form {{ grid-template-columns: 1fr; }}
  .terminal-active-filters {{ overflow-x: auto; flex-wrap: nowrap; }}
  .terminal-active-filters a, .terminal-active-filters em {{ flex: 0 0 auto; }}
  .terminal-layout {{ grid-template-columns: 1fr; }}
  .compact-kpis {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .token-route-summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; min-height: 0; padding: 10px; }}
  .token-route-summary .asset-identity, .token-route-summary .best-route {{ grid-column: 1 / -1; }}
  .token-route-summary .best-route {{ padding-top: 7px; border-top: 1px solid var(--terminal-line); }}
  .token-route-summary .group-age {{ grid-column: 1 / -1; justify-content: space-between; padding-top: 6px; border-top: 1px solid var(--terminal-line); }}
  .expanded-asset-bar {{ align-items: flex-start; flex-direction: column; }}
  .expanded-asset-bar div {{ width: 100%; }}
  .expanded-asset-bar a {{ flex: 1; justify-content: center; }}
  .route-detail-table {{ min-width: 0; }}
  .route-detail-head {{ display: none; }}
  .route-detail-row {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; min-height: 0; padding: 10px; }}
  .route-prices, .route-edge, .route-funding, .route-rails, .route-actions {{ padding-top: 7px; border-top: 1px solid var(--terminal-line); }}
  .route-prices, .route-funding, .route-rails, .route-actions {{ grid-column: 1 / -1; }}
  .route-actions {{ justify-content: stretch; }}
  .route-actions > * {{ flex: 1; justify-content: center; }}
  .market-mini-row {{ grid-template-columns: 62px minmax(0, 1fr) auto; }}
  .funding-farm-tabs {{ overflow-x: auto; }}
  .funding-farm-tabs a {{ flex: 0 0 auto; }}
  .funding-token-group > summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); min-height: 0; }}
  .funding-token-group > summary .asset-identity {{ grid-column: 1 / -1; }}
  .funding-chevron {{ grid-column: 1 / -1; justify-self: end; }}
  .funding-pair-row {{ grid-template-columns: repeat(2, minmax(0, 1fr)); min-height: 0; }}
  .funding-pair-row .route-actions {{ grid-column: 1 / -1; }}
  .chart-token-search {{ width: 100%; }}
  .chart-token-search input {{ flex: 1; width: auto; }}
  .chart-asset-index {{ align-items: flex-start; flex-direction: column; }}
  .chart-asset-index > div {{ max-width: 100%; }}
  .terminal-grid {{ min-width: 0; grid-template-columns: 1fr; }}
  .terminal-table .market-row {{ padding: 10px; gap: 7px; }}
  .terminal-leg {{ grid-template-columns: 16px minmax(0, 1fr) 42px auto; }}
  .terminal-leg small {{ display: none; }}
  .market-number-cell, .market-age-cell, .market-dw-cell, .market-funding-cell {{ grid-template-columns: 1fr auto; align-items: center; border-top: 1px solid var(--terminal-line); }}
  .market-number-cell span, .market-age-cell span {{ justify-self: end; }}
  .market-blocker-cell {{ border-top: 1px solid var(--terminal-line); }}
  .funding-terminal-panel {{ padding: 10px; }}
  .funding-terminal-table {{ overflow-x: visible; }}
  .funding-terminal-grid {{ min-width: 0; grid-template-columns: 1fr; }}
  .funding-terminal-head {{ display: none; }}
  .funding-route-row {{ padding: 8px; gap: 5px; }}
  .funding-route-row > div {{ display: flex; justify-content: space-between; gap: 12px; min-height: 26px; padding: 3px; border-top: 1px solid var(--terminal-line); text-align: right; }}
  .funding-route-row > div:first-child {{ display: grid; text-align: left; border-top: 0; }}
  .profile-heading {{ grid-template-columns: 1fr; min-height: 0; padding: 13px; }}
  .profile-heading h1 {{ font-size: 23px; }}
  .profile-mode {{ min-height: 68px; }}
  .profile-layout {{ grid-template-columns: 1fr; }}
  .profile-nav-panel {{ position: static; grid-template-columns: repeat(3, minmax(0, 1fr)); }}
  .profile-nav-panel > span, .profile-local-note {{ grid-column: 1 / -1; }}
  .profile-nav-item {{ grid-template-columns: 1fr; justify-items: center; min-height: 48px; padding: 6px; text-align: center; }}
  .profile-nav-item span {{ display: none; }}
  .profile-summary-grid, .profile-field-grid.two, .profile-field-grid.three, .profile-alert-filters, .profile-alert-grid {{ grid-template-columns: 1fr; }}
  .profile-section-title, .profile-panel-head {{ align-items: flex-start; flex-direction: column; }}
  .profile-status-list > div, .profile-activity-table > div {{ grid-template-columns: 1fr; gap: 4px; padding: 9px 0; }}
  .profile-alert-card {{ min-height: 0; }}
  .alert-modal form {{ grid-template-columns: 1fr; }}
  .alert-modal form p, .alert-modal form footer {{ grid-column: 1; }}
  .alert-modal form footer {{ justify-content: stretch; }}
  .alert-modal form footer button {{ flex: 1; }}
  .arb-toolbar {{ grid-template-columns: 1fr; gap: 10px; }}
  .tab-selector {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; overflow: visible; padding-top: 9px; }}
  .tab-button {{ justify-content: center; min-height: 32px; padding: 0 6px; border: 0; border-radius: 7px; background: #e5e5e5; font-size: 11px; overflow: hidden; text-overflow: ellipsis; }}
  .tab-button.active {{ background: var(--accent-soft); color: var(--accent-ink); }}
  .live-tools {{ justify-content: flex-start; }}
  .filter-sheet {{ left: auto; right: 0; grid-template-columns: 1fr; width: calc(100vw - 28px); }}
  .board-meta {{ flex-wrap: wrap; }}
  .detail-head, .panel-head {{ align-items: stretch; flex-direction: column; }}
  .intel-hero {{ align-items: flex-start; flex-direction: column; }}
  .intel-source-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .intel-layout, .hot-grid, .reality-routes, .feed-columns, .signal-board, .signal-split, .funding-grid, .alert-status-grid, .alert-rule-grid, .alert-template-grid, .triage-summary-grid, .triage-layout, .triage-metrics, .watch-status-grid, .watchlist-layout, .watch-items, .watch-route-links, .watch-alert-list, .chart-summary-grid, .chart-grid, .community-layout, .community-events, .scoreboard-grid, .discussion-grid, .call-ledger-row, .playbook-status, .playbook-grid, .source-summary-grid, .sources-layout, .route-source-grid, .source-files-grid, .change-counts, .change-highlights, .action-row, .pair-hero, .pair-diagram, .pair-cockpit-grid, .ticket-legs, .pair-proof-rail, .pair-intel-strip, .pair-layout, .pair-decision-strip, .spread-equation, .spread-breakdown, .checklist-grid, .context-grid, .timeline-head, .timeline-stats, .timeline-dual, .token-pulse-grid, .lifecycle-row {{ grid-template-columns: 1fr; }}
  .triage-summary-grid, .triage-metrics, .triage-card.source .triage-metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .action-row {{ grid-template-columns: 30px minmax(0, 1fr); min-height: 0; gap: 7px 8px; padding: 8px; }}
  .action-rank {{ grid-row: 1 / span 4; min-height: 0; align-self: stretch; }}
  .action-symbol, .action-route, .action-metrics, .action-next {{ grid-column: 2; }}
  .action-symbol {{ grid-template-columns: 1fr auto; align-items: center; align-content: center; }}
  .action-symbol a {{ font-size: 20px; }}
  .action-symbol span {{ justify-self: start; }}
  .action-route em, .action-next strong {{ display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
  .action-route strong {{ font-size: 13px; }}
  .action-metrics {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
  .action-metrics span {{ min-height: 44px; padding: 6px; }}
  .action-next {{ gap: 5px; }}
  .action-next em {{ display: none; }}
  .watch-control-row {{ grid-template-columns: 1fr; }}
  .timeline-head {{ display: grid; }}
  .timeline-stats {{ min-width: 0; }}
  .detail-board, .metric-tape, .metric-grid, .detail-grid, .two-col, .learn-grid, .route-list {{ grid-template-columns: 1fr; }}
  .detail-head, .detail-title, .detail-actions, .detail-board, .leg-rail, .spread-console, .leg-card, .data-card {{ width: 100%; }}
  .token-exchange-wrap {{ overflow-x: visible; }}
  .token-exchange-table {{ min-width: 0; border-collapse: separate; border-spacing: 0 7px; }}
  .token-exchange-table thead {{ display: none; }}
  .token-exchange-table tbody, .token-exchange-table tr, .token-exchange-table td {{ display: block; width: 100%; }}
  .token-exchange-table tr {{ padding: 8px; border: 1px solid #d5dfdc; border-radius: 8px; background: var(--row); }}
  .token-exchange-table td {{ display: flex; justify-content: space-between; gap: 12px; min-height: 28px; padding: 5px 3px; border: 0; color: var(--dark); font-size: 12px; font-weight: 800; text-align: right; }}
  .token-exchange-table td::before {{ content: attr(data-label); flex: 0 0 auto; color: #52635e; font-size: 10px; font-weight: 900; text-align: left; text-transform: uppercase; }}
  .token-exchange-table td strong {{ font-size: 15px; }}
  .token-exchange-table .empty {{ display: block; min-height: 58px; padding: 12px; text-align: left; }}
  .token-exchange-table .empty::before {{ content: none; }}
  .arb-table-wrapper-wide {{ display: none; }}
  .mobile-board-cards {{ display: grid; gap: 10px; }}
  .mobile-empty {{ min-width: 0; }}
  .route-empty {{ padding: 14px; }}
  .route-empty-head {{ align-items: stretch; flex-direction: column; }}
  .route-empty-head strong {{ font-size: 21px; }}
  .route-empty-metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .mobile-metric-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .pair-hero, .pair-cockpit {{ padding: 14px; }}
  .pair-snapshot-banner {{ align-items: stretch; flex-direction: column; padding: 12px; }}
  .pair-snapshot-banner nav {{ justify-content: flex-start; }}
  .pair-cockpit-head, .ticket-head, .pair-cockpit-foot, .pair-subnav, .spread-lens-head {{ align-items: flex-start; flex-direction: column; }}
  .pair-intel-strip {{ padding: 8px; }}
  .pair-intel-strip article {{ min-height: 0; }}
  .pair-intel-links {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
  .lifecycle-head {{ display: grid; }}
  .lifecycle-head span {{ width: fit-content; white-space: normal; }}
  .lifecycle-metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .pair-edge-panel {{ order: -1; }}
  .pair-edge-panel > strong {{ font-size: 44px; }}
  .ticket-leg {{ min-height: 112px; }}
  .ticket-bridge {{ min-height: 35px; }}
  .edge-metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .playbook-guard {{ align-items: stretch; flex-direction: column; }}
  .playbook-guard-list {{ justify-content: flex-start; }}
  .pair-main {{ order: -1; }}
  .facts {{ grid-template-columns: 1fr; }}
  .pair-anchors {{ max-width: 100%; overflow-x: auto; flex-wrap: nowrap; }}
  .price-pair {{ justify-content: flex-start; }}
  .auto-refresh-pill {{ right: 10px; bottom: 10px; max-width: calc(100vw - 20px); }}
}}
@media (max-width: 560px) {{
  .chart-leg-stats {{ grid-template-columns: 1fr; }}
  .chart-leg-stats article,
  .chart-leg-stats article:first-child {{ border-left: 0; border-top: 1px solid var(--terminal-line); }}
  .chart-leg-stats article:first-child {{ border-top: 0; }}
  .chart-plot-title {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 7px 10px; padding: 8px 0; }}
  .chart-plot-title strong {{ text-align: right; }}
  .chart-plot-title button {{ margin-left: 0; }}
  .chart-plot-title em {{ grid-column: 1 / -1; margin-left: 0; }}
  .live-chart-note {{ align-items: flex-start; flex-direction: column; padding: 8px 0; }}
  .selected-chart-foot {{ min-height: 58px; }}
}}
.account-page {{ width:min(1480px,calc(100% - 36px)); margin:34px auto 70px; }}
.narrow-account {{ width:min(820px,calc(100% - 36px)); }}
.account-heading {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-start; }}
.account-membership {{ min-width:180px; border:1px solid var(--terminal-line); padding:13px 15px; display:grid; gap:4px; background:var(--terminal-panel); }}
.account-membership span,.account-membership em {{ color:var(--terminal-muted); font-size:12px; font-style:normal; }}
.account-membership strong {{ text-transform:capitalize; }}
.subscription-consent {{ display:flex; align-items:flex-start; gap:9px; max-width:720px; color:var(--terminal-muted); font-size:12px; line-height:1.45; }}
.subscription-consent input {{ width:auto; margin-top:2px; accent-color:var(--accent); }}
.subscription-consent a {{ color:var(--accent); text-decoration:underline; }}
.legal-page {{ width:min(900px,calc(100% - 32px)); margin:38px auto 72px; }}
.legal-page>header {{ padding:30px 0 24px; border-bottom:1px solid var(--terminal-line); }}
.legal-page h1 {{ margin:6px 0 10px; font-size:40px; }}
.legal-page>header p,.legal-page main p {{ color:var(--terminal-muted); line-height:1.65; }}
.legal-page main section {{ padding:22px 0; border-bottom:1px solid var(--terminal-line); }}
.legal-page main h2 {{ margin:0 0 8px; font-size:18px; }}
.legal-page main p {{ margin:0; }}
.legal-page nav {{ display:flex; flex-wrap:wrap; gap:16px; padding-top:20px; }}
.legal-page nav a {{ color:var(--accent); font-weight:800; }}
.account-kpis {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border:1px solid var(--terminal-line); margin:20px 0; background:var(--terminal-panel); }}
.account-kpis article {{ min-width:0; padding:16px; display:grid; gap:5px; border-right:1px solid var(--terminal-line); }}
.account-kpis article:last-child {{ border-right:0; }} .account-kpis span,.account-kpis em {{ color:var(--terminal-muted); font-size:12px; font-style:normal; }} .account-kpis strong {{ font-size:23px; }}
.account-tabs {{ display:flex; border-bottom:1px solid var(--terminal-line); margin-bottom:18px; overflow-x:auto; }}
.account-tabs button {{ border:0; border-bottom:3px solid transparent; background:transparent; color:var(--terminal-muted); padding:13px 18px; font:inherit; font-weight:800; cursor:pointer; white-space:nowrap; }}
.account-tabs button.active {{ color:var(--terminal-text); border-bottom-color:var(--accent); }} .account-tabs i {{ font-style:normal; padding:2px 6px; background:var(--accent); color:var(--accent-ink); }}
.account-panel-head {{ display:flex; align-items:center; justify-content:space-between; gap:20px; margin:16px 0; }} .account-panel-head h2 {{ margin:0 0 5px; }} .account-panel-head p {{ margin:0; color:var(--terminal-muted); }}
.position-list,.notification-list {{ display:grid; gap:10px; }} .position-card,.account-empty-panel,.account-settings,.notification-list article,.member-row {{ border:1px solid var(--terminal-line); background:var(--terminal-panel); padding:16px; }}
.position-card header,.position-card footer,.position-legs {{ display:flex; justify-content:space-between; align-items:center; gap:14px; }} .position-card header>div:first-child {{ display:grid; grid-template-columns:auto auto; gap:3px 12px; align-items:baseline; }} .position-card header em {{ grid-column:2; color:var(--terminal-muted); font-style:normal; font-size:12px; }}
.position-token {{ font-size:24px; font-weight:900; grid-row:1/3; }} .position-status {{ text-align:right; display:grid; }} .position-status span {{ text-transform:uppercase; font-size:10px; color:var(--terminal-muted); }} .position-status.live strong {{ color:var(--green); }}
.position-metrics {{ display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); border-block:1px solid var(--terminal-line); margin:14px -16px; }} .position-metrics span {{ padding:11px 16px; display:grid; gap:4px; color:var(--terminal-muted); font-size:11px; border-right:1px solid var(--terminal-line); }} .position-metrics strong {{ color:var(--terminal-text); font-size:16px; }}
.position-legs>div {{ flex:1; display:grid; grid-template-columns:auto 1fr auto; gap:10px; }} .position-legs span,.position-legs em {{ color:var(--terminal-muted); font-style:normal; }} .position-card footer {{ margin-top:14px; color:var(--terminal-muted); font-size:12px; }} .position-card footer div {{ display:flex; flex-wrap:wrap; gap:7px; align-items:center; }} .position-card footer button,.position-card footer a {{ border:1px solid var(--terminal-line); background:transparent; color:var(--terminal-text); padding:7px 10px; text-decoration:none; font:inherit; font-weight:700; cursor:pointer; }}
.account-dialog {{ width:min(760px,calc(100% - 28px)); max-height:90vh; overflow:auto; border:1px solid var(--terminal-line); background:var(--terminal-panel); color:var(--terminal-text); padding:0; }} .account-dialog::backdrop {{ background:rgba(0,0,0,.68); }} .account-dialog form>header,.account-dialog form>footer {{ padding:15px 18px; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--terminal-line); }} .account-dialog form>footer {{ border:0; border-top:1px solid var(--terminal-line); justify-content:flex-end; }} .account-dialog h2 {{ margin:2px 0 0; }} .account-dialog header span {{ color:var(--terminal-muted); font-size:11px; text-transform:uppercase; }} .account-dialog button {{ border:1px solid var(--terminal-line); background:transparent; color:var(--terminal-text); padding:8px 13px; cursor:pointer; }} .account-dialog button.primary {{ background:var(--accent); color:var(--accent-ink); border-color:var(--accent); }}
.position-form-grid,.account-dialog [data-action-fields],.account-settings form,.member-create-form {{ padding:18px; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:13px; }} .position-form-grid label,.account-dialog [data-action-fields] label,.account-settings label,.member-create-form label {{ display:grid; gap:6px; color:var(--terminal-muted); font-size:11px; text-transform:uppercase; }} .position-form-grid input,.position-form-grid select,.position-form-grid textarea,.account-dialog [data-action-fields] input,.account-dialog [data-action-fields] select,.account-settings input,.member-create-form input {{ min-height:40px; width:100%; border:1px solid var(--terminal-line); background:var(--terminal-row); color:var(--terminal-text); padding:8px; font:inherit; }} .position-form-grid .wide {{ grid-column:1/-1; }}
.notification-list article {{ display:grid; grid-template-columns:180px 220px 1fr; gap:14px; }} .notification-list p {{ margin:0; color:var(--terminal-muted); }} .member-row {{ display:grid; grid-template-columns:1fr auto auto; gap:20px; margin-top:8px; }} .member-row div {{ display:grid; }} .member-row span,.member-row em {{ color:var(--terminal-muted); font-style:normal; }}
.account-chip {{ display:grid; color:var(--terminal-shell-text); text-decoration:none; text-align:right; line-height:1.1; }} .account-chip em {{ color:var(--accent); font-size:10px; font-style:normal; text-transform:uppercase; }} .logout-button {{ width:38px; height:38px; border:1px solid rgba(255,255,255,.25); background:transparent; color:var(--terminal-shell-text); font-size:19px; cursor:pointer; }}
@media(max-width:900px) {{ .account-kpis {{ grid-template-columns:repeat(2,1fr); }} .position-metrics {{ grid-template-columns:repeat(3,1fr); }} .position-legs {{ align-items:stretch; flex-direction:column; }} .account-chip,.logout-button {{ display:none; }} }}
@media(max-width:600px) {{ .account-heading {{ flex-direction:column; }} .account-membership {{ width:100%; }} .account-kpis {{ grid-template-columns:1fr 1fr; }} .position-metrics {{ grid-template-columns:1fr 1fr; }} .position-card header,.position-card footer {{ align-items:flex-start; flex-direction:column; }} .position-form-grid,.account-dialog [data-action-fields],.account-settings form,.member-create-form {{ grid-template-columns:1fr; }} .position-form-grid .wide {{ grid-column:auto; }} .notification-list article {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
{render_alert_draft_script()}
<header class="site-header">
  <div class="topbar">
    <a class="brand" href="/"><span class="brand-mark" aria-hidden="true"></span><span>SpreadBoard</span></a>
    <nav class="main-nav">{render_primary_nav(active, signed_in=user is not None)}</nav>
    <div class="header-actions">
      {account_action}
      <button class="theme-toggle" id="themeToggle" type="button" aria-label="Toggle light and dark mode" aria-pressed="false">
        <span class="theme-swatch" aria-hidden="true"></span>
        <span data-theme-label>Theme</span>
      </button>
    </div>
  </div>
  <div class="header-strip"></div>
  <nav class="mobile-primary-nav" aria-label="Mobile primary navigation">{render_primary_nav(active, signed_in=user is not None)}</nav>
  {render_mobile_secondary_nav(active) if user else ''}
</header>
<main>{body}</main>
{render_theme_script()}
{render_auto_refresh_script()}
<script>document.querySelector('[data-logout]')?.addEventListener('click',async event=>{{await fetch('/api/logout',{{method:'POST',headers:{{'X-CSRF-Token':event.currentTarget.dataset.csrf}}}});location.assign('/login');}});</script>
</body>
</html>"""


def _decorate_board_row(row: board.BoardRow) -> dict[str, Any]:
    data = row.to_dict()
    data["kind_label"] = row.route_label
    data["kind_class"] = f"kind-{row.kind.lower().replace('-', '_')}"
    data["route_line"] = f"Buy on {row.long_venue or '?'} {row.long_market_type or '?'}, sell on {row.short_venue or '?'} {row.short_market_type or '?'}"
    data["pair_url"] = f"/pair/{board.route_key_url(row.route_key)}"
    return data


def _decorate_history_row(row: board.BoardRow) -> dict[str, Any]:
    open_spread = row.displayed_open_spread_pct if row.displayed_open_spread_pct is not None else row.spread_pct
    return {
        "symbol": row.symbol,
        "kind": row.kind,
        "route_key": row.route_key,
        "pair_url": f"/pair/{board.route_key_url(row.route_key)}",
        "route_line": f"{row.long_venue or '?'} {row.long_market_type or '?'} -> {row.short_venue or '?'} {row.short_market_type or '?'}",
        "open_spread_pct": open_spread,
        "executable_spread_pct": row.spread_pct,
        "funding_apr_pct": row.funding_apr_pct,
        "funding_spread_pct": row.funding_spread_pct,
        "depth_usd": row.depth_usd,
        "age_min": row.age_min,
        "ingested_at_us": row.ingested_at_us,
    }


def _find_board_symbol(symbol: str, board_path: Path) -> list[dict[str, Any]]:
    snapshot = board.load_board(board_path, include_stale=True, max_age_min=None, limit=None)
    return [_decorate_board_row(row) for row in snapshot.rows if row.symbol == symbol]


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[0] if values else None
    return value if value not in (None, "") else None


def _query_bool(query: dict[str, list[str]], key: str) -> bool:
    return str(_query_first(query, key) or "").casefold() in {"1", "true", "yes", "on"}


def _query_float(query: dict[str, list[str]], key: str, default: float | None = None) -> float | None:
    value = _query_first(query, key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _query_with(query: dict[str, list[str]], **updates: Any) -> dict[str, str]:
    output = {key: values[0] for key, values in query.items() if values and values[0] != ""}
    for key, value in updates.items():
        if value is None:
            output.pop(key, None)
        else:
            output[key] = str(value)
    return output


def _query_lists_with(query: dict[str, list[str]], **updates: Any) -> dict[str, list[str]]:
    output = {key: list(values) for key, values in query.items() if values}
    for key, value in updates.items():
        if value is None:
            output.pop(key, None)
        else:
            output[key] = [str(value)]
    return output


def _clean_symbol(value: str) -> str:
    return "".join(ch for ch in unquote(value).upper() if ch.isalnum() or ch in {"_", "-"}).strip("-_")[:24]


def active_class(active: str, name: str) -> str:
    return "active" if active == name else ""


def spread_class(value: Any) -> str:
    number = _float_or_none(value) or 0.0
    if number < 0:
        return "spread-negative"
    if number >= 12:
        return "spread-hot"
    if number >= 8:
        return "spread-good"
    if number >= 3:
        return "spread-watch"
    return "spread-low"


def status_char(value: bool | None) -> str:
    if value is True:
        return "open"
    if value is False:
        return "closed"
    return "?"


def fmt_pct(value: Any, *, digits: int = 1) -> str:
    number = _float_or_none(value)
    return "?" if number is None else f"{number:.{digits}f}%"


def fmt_signed_pct(value: Any, *, digits: int = 1) -> str:
    number = _float_or_none(value)
    return "?" if number is None else f"{number:+.{digits}f}%"


def fmt_money(value: Any) -> str:
    number = _float_or_none(value)
    return "?" if number is None else f"${number:,.0f}"


def fmt_signed_money(value: Any) -> str:
    number = _float_or_none(value)
    return "?" if number is None else f"{number:+,.2f} USD"


def fmt_signed_number(value: Any) -> str:
    number = _float_or_none(value)
    return "?" if number is None else f"{number:+,.0f}"


def fmt_price(value: Any) -> str:
    number = _float_or_none(value)
    if number is None:
        return "?"
    if number >= 1:
        return f"${number:,.4f}"
    return f"${number:.8f}".rstrip("0").rstrip(".")


def fmt_age(value: Any) -> str:
    number = _float_or_none(value)
    if number is None:
        return "?"
    if number < 60:
        return f"{number:.0f} min"
    return f"{number / 60:.1f} h"


def fmt_duration(value: Any) -> str:
    number = _float_or_none(value)
    if number is None:
        return "?"
    if number < 1:
        return "<1 min"
    if number < 60:
        return f"{number:.0f} min"
    return f"{number / 60:.1f} h"


def spread_width(value: Any) -> int:
    number = abs(_float_or_none(value) or 0.0)
    return max(4, min(100, int((number / 20.0) * 100)))


def h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def short_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        path = Path(raw)
        parent = path.parent.name
        return f"{parent}/{path.name}" if parent else path.name
    except Exception:  # noqa: BLE001
        return raw


def json_script_data(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str).replace("</", "<\\/")


def label_text(value: Any) -> str:
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    key = raw.casefold()
    if key in DISPLAY_LABELS:
        return h(DISPLAY_LABELS[key])
    if ":" in raw:
        head, tail = raw.split(":", 1)
        return f"{label_text(head)}: {h(tail.strip())}"
    label = " ".join(raw.replace("_", " ").replace("-", " ").split())
    return h(label[:1].upper() + label[1:])


def label_list(values: list[Any]) -> str:
    return ", ".join(label_text(value) for value in values if str(value or "").strip())


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8200, type=int, show_default=True)
@click.option(
    "--board-path",
    default=str(board.DEFAULT_BOARD_PATH),
    type=click.Path(path_type=Path),
    show_default=True,
)
def main(host: str, port: int, board_path: Path) -> None:
    config = alerts.load_config()
    server = SpreadBoardServer((host, port), SpreadBoardHandler, board_path=board_path, config=config)
    click.echo(f"SpreadBoard serving on http://{host}:{port}")
    crypto_stop = crypto_watcher.start_background(db_path=server.accounts_path)
    if crypto_stop is None:
        click.echo("Crypto billing watcher idle (receiving address or RPC URL not configured)")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if crypto_stop is not None:
            crypto_stop.set()
        if server.alert_watcher:
            server.alert_watcher.stop()
        server.server_close()


if __name__ == "__main__":
    main()
