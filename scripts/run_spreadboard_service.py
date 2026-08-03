#!/usr/bin/env python3
"""Run SpreadBoard and its public-API refresh loop as one persistent service."""

from __future__ import annotations

# ruff: noqa: E402

import gc
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from spreadboard import (
    alerts,
    api_spreads,
    board,
    chart_catalog,
    live,
    market_history,
    portfolio,
    public_rails,
    rail_watch,
    telegram_bot,
    token_metadata,
    verified_identity,
)  # noqa: E402
from spreadboard.server import SpreadBoardHandler, SpreadBoardServer  # noqa: E402

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
SNAPSHOT_PATH = RUNTIME_DIR / "api_discovery_latest.json"
REFRESH_SNAPSHOT_PATH = RUNTIME_DIR / "api_discovery_refresh.json"
GENERATED_IDENTITY_PATH = RUNTIME_DIR / "api_discovery_identity_registry.generated.json"


class RefreshLoop:
    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(30.0, interval_seconds)
        self.stop_event = threading.Event()
        self.snapshot_lock = threading.Lock()
        self.quote_cycle_lock = threading.Lock()
        self.thread = threading.Thread(
            target=self.run, name="spreadboard-public-refresh", daemon=True
        )
        self.fast_thread = threading.Thread(
            target=self.run_fast_quotes,
            name="spreadboard-fast-quotes",
            daemon=True,
        )
        self.catalog_thread = threading.Thread(
            target=self.run_chart_catalog,
            name="spreadboard-chart-catalog",
            daemon=True,
        )
        self.websocket_process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self._ensure_websocket_worker()
        self.thread.start()
        self.catalog_thread.start()
        if not _env_bool("SPREADBOARD_LIGHTWEIGHT_MODE"):
            self.fast_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5.0)
        if self.fast_thread.is_alive():
            self.fast_thread.join(timeout=5.0)
        if self.catalog_thread.is_alive():
            self.catalog_thread.join(timeout=5.0)
        if self.websocket_process is not None and self.websocket_process.poll() is None:
            self.websocket_process.terminate()
            try:
                self.websocket_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.websocket_process.kill()

    def run(self) -> None:
        while not self.stop_event.is_set():
            self._ensure_websocket_worker()
            started = time.monotonic()
            self.refresh_once()
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(15.0, self.interval_seconds - elapsed))

    def run_chart_catalog(self) -> None:
        while not self.stop_event.is_set():
            try:
                payload = chart_catalog.refresh(
                    workers=int(os.environ.get("SPREADBOARD_CHART_CATALOG_WORKERS", "4"))
                )
                _log(
                    f"chart catalog markets={payload.get('count', 0)} "
                    f"tokens={payload.get('token_count', 0)}"
                )
            except Exception as exc:  # noqa: BLE001 - board refresh remains independent.
                _log(f"chart catalog unavailable: {type(exc).__name__}: {exc}")
            self.stop_event.wait(
                max(900.0, float(os.environ.get("SPREADBOARD_CHART_CATALOG_SECONDS", "21600")))
            )

    def _ensure_websocket_worker(self) -> None:
        if _env_bool("SPREADBOARD_DISABLE_WEBSOCKETS"):
            return
        if self.websocket_process is not None and self.websocket_process.poll() is None:
            return
        self.websocket_process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts/websocket_book_worker.py"),
            ],
            cwd=ROOT,
            text=True,
        )
        _log(f"websocket book worker pid={self.websocket_process.pid}")

    def refresh_once(self) -> None:
        lightweight_mode = _env_bool("SPREADBOARD_LIGHTWEIGHT_MODE")
        with self.snapshot_lock:
            if SNAPSHOT_PATH.exists():
                shutil.copyfile(SNAPSHOT_PATH, REFRESH_SNAPSHOT_PATH)
            else:
                REFRESH_SNAPSHOT_PATH.unlink(missing_ok=True)
        self._refresh_verified_identity_registry(
            snapshot_path=SNAPSHOT_PATH if SNAPSHOT_PATH.exists() else REFRESH_SNAPSHOT_PATH
        )
        command = [
            *_low_priority_prefix(),
            sys.executable,
            str(ROOT / "scripts/api_discovery_worker.py"),
            "--db-path",
            str(ROOT / "data/spreadarb.db"),
            "--snapshot-path",
            str(REFRESH_SNAPSHOT_PATH),
            "--archive-dir",
            str(RUNTIME_DIR / "api_discovery_archive"),
            "--parts-dir",
            str(RUNTIME_DIR / "api_discovery_parts"),
            "--identity-registry-path",
            str(
                GENERATED_IDENTITY_PATH
                if GENERATED_IDENTITY_PATH.exists()
                else ROOT / "data/api_discovery_identity_registry.json"
            ),
            *(
                ["--skip-broad-dex-spot"]
                if os.environ.get("SPREADBOARD_SKIP_BROAD_DEX_SPOT", "").strip().lower()
                in {"1", "true", "yes", "on"}
                else [
                    # The repo's data/ dir is read-only in the container; only the
                    # runtime volume is writable. Without this the broad DEX scan
                    # dies with PermissionError and takes the whole refresh with it.
                    "--broad-dex-output-path",
                    str(RUNTIME_DIR / "api_discovery_broad_dex_latest.json"),
                ]
            ),
            "--dex-spot-timeout-s",
            os.environ.get("SPREADBOARD_DEX_SPOT_TIMEOUT_SECONDS", "240"),
            "--include-blacklisted",
            "--cex-max-orderbook-candidates",
            os.environ.get("SPREADBOARD_CEX_CANDIDATES", "100"),
            "--dex-derivative-max-orderbook-candidates",
            os.environ.get("SPREADBOARD_DEX_CANDIDATES", "40"),
            "--row-limit",
            os.environ.get("SPREADBOARD_DISCOVERY_ROWS", "800"),
            "--ttl-s",
            "900",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=float(os.environ.get("SPREADBOARD_REFRESH_TIMEOUT_SECONDS", "900")),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            _log(f"refresh timeout: {exc}")
            return
        if result.returncode != 0:
            _log(f"refresh failed ({result.returncode}): {(result.stderr or '')[-500:]}")
            return
        try:
            snapshot = json.loads(REFRESH_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log(f"snapshot unavailable after refresh: {exc}")
            return
        funding_summary = live.enrich_snapshot_funding_24h(
            snapshot,
            max_workers=int(os.environ.get("SPREADBOARD_FUNDING_HISTORY_WORKERS", "4")),
            route_keys=_funding_refresh_route_keys(snapshot),
        )
        # The discovery worker writes to a staging snapshot so it cannot block
        # or overwrite fast quote cycles while the broad venue scan is running.
        with self.quote_cycle_lock, self.snapshot_lock:
            try:
                current_snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current_snapshot = {}
            _merge_newer_fast_quotes(snapshot, current_snapshot)
            _atomic_write_snapshot(snapshot)
        inserted = market_history.record_snapshot(snapshot)
        refresh = snapshot.get("source_refresh") or {}
        _log(
            "refresh complete "
            f"routes={len(snapshot.get('api_discovered_rows') or []) + len(snapshot.get('dex_discovered_rows') or [])} "
            f"history_inserted={inserted} funding={funding_summary} status={refresh.get('status')}"
        )
        if lightweight_mode:
            _refresh_enrichment_subprocess()
        else:
            symbols = {
                str(row.get("token") or "").upper()
                for bucket in ("api_discovered_rows", "dex_discovered_rows")
                for row in snapshot.get(bucket) or []
                if isinstance(row, dict) and row.get("token")
            }
            try:
                token_metadata.refresh_token_metadata(symbols)
            except Exception as exc:  # noqa: BLE001 - metadata must not stop market refresh.
                _log(f"token-name refresh unavailable: {type(exc).__name__}: {exc}")
            try:
                public_rails.refresh_public_rails(snapshot)
            except Exception as exc:  # noqa: BLE001 - rail coverage can be partial.
                _log(f"transfer-rail refresh unavailable: {type(exc).__name__}: {exc}")
            self._refresh_verified_identity_registry(snapshot_path=SNAPSHOT_PATH)
        del snapshot
        gc.collect()

    def _refresh_verified_identity_registry(self, *, snapshot_path: Path) -> None:
        try:
            payload = verified_identity.build_verified_identity_registry(
                static_registry_path=ROOT / "data/api_discovery_identity_registry.json",
                watchlist_path=ROOT / "data/api_discovery_watchlist.json",
                rails_path=RUNTIME_DIR / "public_transfer_rails.json",
                snapshot_path=snapshot_path,
                output_path=GENERATED_IDENTITY_PATH,
            )
            generation = payload.get("generation") or {}
            _log(
                "verified identity registry "
                f"matches={generation.get('verified_matches', 0)} "
                f"markets_added={generation.get('markets_added', 0)}"
            )
        except Exception as exc:  # noqa: BLE001 - static registry remains available.
            _log(f"verified identity refresh unavailable: {type(exc).__name__}: {exc}")

    def run_fast_quotes(self) -> None:
        interval = max(
            20.0,
            float(os.environ.get("SPREADBOARD_FAST_QUOTE_SECONDS", "30")),
        )
        while not self.stop_event.wait(5.0):
            with self.quote_cycle_lock:
                _log("fast quote cycle starting")
                summary = self._refresh_fast_quotes()
                with self.snapshot_lock:
                    if summary.get("updated_routes"):
                        try:
                            snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
                            inserted = market_history.record_snapshot(snapshot)
                        except (OSError, json.JSONDecodeError):
                            inserted = 0
                    else:
                        inserted = 0
            _log(f"fast quotes {summary} history_inserted={inserted}")
            _warm_board_cache()
            self.stop_event.wait(interval)

    def _refresh_fast_quotes(self) -> dict[str, Any]:
        command = [
            *_low_priority_prefix(),
            sys.executable,
            str(ROOT / "scripts/fast_quote_worker.py"),
            "--snapshot-path",
            str(SNAPSHOT_PATH),
            "--route-limit",
            str(
                min(
                    400,
                    max(25, int(os.environ.get("SPREADBOARD_FAST_QUOTE_ROUTES", "50"))),
                )
            ),
            "--deadline-seconds",
            str(round(_fast_quote_timeout() * 0.8, 1)),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=_fast_quote_timeout(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "updated_routes": 0}
        try:
            return json.loads((result.stdout or "").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return {
                "status": "failed",
                "updated_routes": 0,
                "exit_code": result.returncode,
            }


def _fast_quote_timeout() -> float:
    """How long the parent waits before killing the fast-quote subprocess."""
    return max(
        90.0,
        float(os.environ.get("SPREADBOARD_FAST_QUOTE_TIMEOUT_SECONDS", "240")),
    )


def _merge_newer_fast_quotes(
    discovery_snapshot: dict[str, Any],
    current_snapshot: dict[str, Any],
) -> None:
    """Keep quotes produced while the staged discovery scan was running."""

    current_fast = current_snapshot.get("fast_quote_refresh")
    if isinstance(current_fast, dict) and current_fast.get("status") == "ok":
        discovery_snapshot["fast_quote_refresh"] = dict(current_fast)

    for bucket in ("api_discovered_rows", "dex_discovered_rows"):
        discovered = discovery_snapshot.get(bucket)
        current = current_snapshot.get(bucket)
        if not isinstance(discovered, list) or not isinstance(current, list):
            continue
        current_by_key = {
            _snapshot_route_key(row): row
            for row in current
            if isinstance(row, dict) and _snapshot_route_key(row)
        }
        for index, row in enumerate(discovered):
            if not isinstance(row, dict):
                continue
            newer = current_by_key.get(_snapshot_route_key(row))
            if not isinstance(newer, dict):
                continue
            if int(newer.get("quote_ts_us") or 0) > int(row.get("quote_ts_us") or 0):
                discovered[index] = newer


def _snapshot_route_key(row: dict[str, Any]) -> str:
    explicit = str(row.get("route_key") or "").strip()
    if explicit:
        return explicit
    return "|".join(
        str(row.get(field) or "").strip()
        for field in (
            "token",
            "long_venue",
            "long_market_type",
            "short_venue",
            "short_market_type",
        )
    )


def _refresh_enrichment_subprocess() -> None:
    command = [
        sys.executable,
        str(ROOT / "scripts/refresh_spreadboard_enrichment.py"),
        "--snapshot-path",
        str(SNAPSHOT_PATH),
        "--rail-workers",
        "1",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("SPREADBOARD_ENRICHMENT_TIMEOUT_SECONDS", "240")),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _log(f"isolated enrichment timeout: {exc}")
        return
    summary = (result.stdout or result.stderr or "").strip()[-500:]
    _log(f"isolated enrichment exit={result.returncode} {summary}")


def main() -> int:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8200"))
    interval = float(os.environ.get("SPREADBOARD_REFRESH_SECONDS", "300"))
    if _env_bool("SPREADBOARD_LIGHTWEIGHT_MODE"):
        interval = max(600.0, interval)
    board_path = Path(os.environ.get("SPREADBOARD_BOARD_PATH", str(board.DEFAULT_BOARD_PATH)))
    _seed_public_caches()
    refresh_loop = RefreshLoop(interval)
    server = SpreadBoardServer(
        (host, port),
        SpreadBoardHandler,
        board_path=board_path,
        config=alerts.load_config(),
    )
    position_alert_worker = portfolio.PositionAlertWorker(
        board_path=board_path,
        accounts_path=server.accounts_path,
        poll_seconds=float(os.environ.get("SPREADBOARD_POSITION_ALERT_SECONDS", "30")),
    )
    server.position_alert_worker = position_alert_worker
    membership_worker = telegram_bot.MembershipWorker(
        db_path=server.accounts_path,
        poll_seconds=float(os.environ.get("SPREADBOARD_TELEGRAM_MEMBERSHIP_SECONDS", "60")),
    )
    market_alert_worker = alerts.UserMarketAlertWorker(
        board_path=board_path,
        accounts_path=server.accounts_path,
        poll_seconds=float(os.environ.get("SPREADBOARD_MARKET_ALERT_SECONDS", "10")),
    )
    rail_reopen_worker = rail_watch.RailReopenWatcher(
        poll_seconds=float(os.environ.get("SPREADBOARD_RAIL_REOPEN_SECONDS", "300")),
    )

    def stop_service(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    refresh_loop.start()
    # Shares the refresh loop's stop event so shutdown stops it too.
    bulk_quote_loop = BulkQuoteLoop(refresh_loop.stop_event)
    bulk_quote_loop.start()
    position_alert_worker.start()
    membership_worker.start()
    market_alert_worker.start()
    rail_reopen_worker.start()
    _log(f"serving http://{host}:{port}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        position_alert_worker.stop()
        membership_worker.stop()
        market_alert_worker.stop()
        rail_reopen_worker.stop()
        refresh_loop.stop()
        server.server_close()
    return 0


#: Every view the navigation can reach, because each is a separate cache key
#: and each costs a full rebuild. Warming only the default left a member opening
#: Funding -> Futures-Spot waiting 59 seconds.
WARM_QUERIES: tuple[dict[str, list[str]], ...] = (
    {},
    # /charts builds its picker from 500 rows, which is its own cache key -- it
    # stayed at 27s while every other page came down.
    {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]},
    {"kind": ["FUTURES"]},
    {"kind": ["FUTURES-SPOT-PAIR"]},
    {"kind": ["SPOT"]},
    {"kind": ["DEX-FUTURES"]},
    {"kind": ["DEX-SPOT"]},
    # The funding page carries its `farm` parameter into the query, so warming
    # without it builds a different cache key and the tab stays cold -- which is
    # exactly what left /funding?farm=futures-spot at 27s while /funding was
    # 0.20s. Each tab is warmed as the page actually asks for it.
    {"funding_only": ["1"], "kind": ["FUTURES"], "sort": ["funding"], "direction": ["desc"], "limit": ["25"]},
    {"farm": ["futures-futures"], "funding_only": ["1"], "kind": ["FUTURES"], "sort": ["funding"], "direction": ["desc"], "limit": ["25"]},
    {"farm": ["futures-spot"], "funding_only": ["1"], "kind": ["FUTURES-SPOT-PAIR"], "sort": ["funding"], "direction": ["desc"], "limit": ["25"]},
    {"farm": ["futures-dex"], "funding_only": ["1"], "kind": ["DEX-FUTURES"], "sort": ["funding"], "direction": ["desc"], "limit": ["25"]},
)


def _low_priority_prefix() -> list[str]:
    """nice/ionice if the image has them, nothing if it does not.

    The scan is background work and a page load is not. Running it at the lowest
    priority the kernel offers does not make it finish sooner -- it still takes
    its 20-40 minutes -- it stops it taking them out of whoever is reading the
    board.
    """
    prefix: list[str] = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c3"]
    return prefix


class BulkQuoteLoop(threading.Thread):
    """Re-price the whole board from one bulk call per venue.

    Websockets cover a few hundred legs of the eight thousand the board carries
    and the scan re-quoted the rest every twenty-five minutes, so a route
    outside the streaming set could be twenty minutes stale and a token that
    turned positive in between did not appear until the next scan. One
    fetch_tickers per venue closes that in about fifteen seconds.
    """

    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="bulk-quotes", daemon=True)
        self.stop_event = stop_event

    def run(self) -> None:
        from spreadboard import bulk_quotes

        while not self.stop_event.is_set():
            try:
                summary = bulk_quotes.sweep()
                _log(
                    f"bulk quotes: {summary['quotes']} from {summary['venues']} venues "
                    f"in {summary['seconds']}s"
                )
                funding = bulk_quotes.sweep_funding()
                _log(
                    f"bulk funding: {funding['legs']} legs from {funding['venues']} "
                    f"venues in {funding['seconds']}s"
                )
            except Exception as exc:  # noqa: BLE001 - best effort beside everything else.
                _log(f"bulk quotes skipped: {type(exc).__name__}: {exc}")
            self.stop_event.wait(bulk_quotes.INTERVAL_SECONDS)


def _board_path() -> Path:
    return Path(os.environ.get("SPREADBOARD_BOARD_PATH", str(board.DEFAULT_BOARD_PATH)))


#: A full pass costs 150-200s on two cores, and it used to run after every 60s
#: quote cycle -- so it never finished before starting again and held both cores
#: permanently, making every page slow instead of fast. One pass per interval,
#: comfortably inside the 420s cache TTL so entries stay warm between passes.
WARM_INTERVAL_SECONDS = max(
    120.0, float(os.environ.get("SPREADBOARD_WARM_INTERVAL_SECONDS", "300"))
)
_LAST_WARM_AT = 0.0


def _warm_board_cache(*, force: bool = False) -> None:
    """Pay the grouping cost here, not in a member's page load.

    Every snapshot write invalidates the request cache, and rebuilding 12k rows
    into a public payload takes seconds. Doing it right after the write means the
    first real request finds it already done.

    It must warm the same cache the pages read -- the server's per-query market
    cache -- and every lane, not just the default. Measured cold: 40s for the
    board, 33s for Funding, 59s for its Futures-Spot tab.
    """
    global _LAST_WARM_AT

    now = time.monotonic()
    if not force and now - _LAST_WARM_AT < WARM_INTERVAL_SECONDS:
        return
    _LAST_WARM_AT = now

    from spreadboard import server

    started = time.monotonic()
    for query in WARM_QUERIES:
        try:
            server.api_market_spreads(_board_path(), dict(query))
        except Exception as exc:  # noqa: BLE001 - warming is best effort.
            _log(f"board cache warm skipped {query}: {type(exc).__name__}: {exc}")
    # Intel is derived from the same snapshot and costs about as much, so it is
    # warmed here rather than left to the first visitor.
    try:
        server.api_intel(_board_path())
    except Exception as exc:  # noqa: BLE001 - warming is best effort.
        _log(f"intel warm skipped: {type(exc).__name__}: {exc}")
    try:
        # Opening a chart by route needs this index; building it on demand cost
        # 14.6s of the thirty a member waited.
        server._route_index(_board_path())
    except Exception as exc:  # noqa: BLE001 - warming is best effort.
        _log(f"route index warm skipped: {type(exc).__name__}: {exc}")
    _log(f"board cache warmed {len(WARM_QUERIES)} views in {time.monotonic() - started:.1f}s")
    _refresh_funding_windows()


def _refresh_funding_windows() -> None:
    """Precompute realised 1d/7d/30d carry for the routes on the board.

    Integrating the rate costs roughly half a second per route-window, so it
    cannot happen while rendering. Doing it here, for the routes the warm pass
    just built, means the page only ever reads the answer.
    """
    from spreadboard import market_history, server

    try:
        route_keys: list[str] = []
        for query in WARM_QUERIES:
            if not query.get("funding_only"):
                continue
            payload = server.api_market_spreads(_board_path(), dict(query))
            for group in payload.get("groups") or []:
                for route in group.get("routes") or []:
                    key = route.get("route_key")
                    if key:
                        route_keys.append(str(key))
        if not route_keys:
            return
        started = time.monotonic()
        count = market_history.write_funding_windows(route_keys)
        _log(f"funding windows computed for {count} routes in {time.monotonic() - started:.1f}s")
        _refresh_venue_funding_history()
    except Exception as exc:  # noqa: BLE001 - a missing history file is not fatal.
        _log(f"funding windows skipped: {type(exc).__name__}: {exc}")


#: Settled history moves once per funding interval, so sweeping it every warm
#: pass would be waste. Hours, not minutes.
VENUE_HISTORY_INTERVAL_SECONDS = max(
    900.0, float(os.environ.get("SPREADBOARD_VENUE_HISTORY_SECONDS", "10800"))
)
_LAST_VENUE_HISTORY_AT = 0.0


def _refresh_venue_funding_history() -> None:
    """Pull each venue's settled funding for the legs the board is showing."""
    global _LAST_VENUE_HISTORY_AT

    now = time.monotonic()
    if now - _LAST_VENUE_HISTORY_AT < VENUE_HISTORY_INTERVAL_SECONDS:
        return
    _LAST_VENUE_HISTORY_AT = now

    from spreadboard import server, venue_funding_history

    try:
        legs: list[tuple[str, str]] = []
        for query in WARM_QUERIES:
            if not query.get("funding_only"):
                continue
            payload = server.api_market_spreads(_board_path(), dict(query))
            for group in payload.get("groups") or []:
                for route in group.get("routes") or []:
                    for side in ("long", "short"):
                        if route.get(f"{side}_market_type") != "Futures":
                            continue
                        venue = route.get(f"{side}_venue")
                        symbol = route.get(f"{side}_market_symbol")
                        if venue and symbol:
                            legs.append((str(venue), str(symbol)))
        if not legs:
            return
        started = time.monotonic()
        windows = venue_funding_history.build(legs)
        _log(
            f"venue funding history: {len(windows)} of {len(set(legs))} legs "
            f"in {time.monotonic() - started:.1f}s"
        )
    except Exception as exc:  # noqa: BLE001 - best effort beside everything else.
        _log(f"venue funding history skipped: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - a missing history file is not fatal.
        _log(f"funding windows skipped: {type(exc).__name__}: {exc}")


def _log(message: str) -> None:
    print(f"spreadboard-service: {message}", flush=True)


def _seed_public_caches() -> None:
    seed_path = ROOT / "data/token_metadata_seed.json"
    target_path = token_metadata.DEFAULT_CACHE_PATH
    if target_path.exists() or not seed_path.exists():
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seed_path, target_path)


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _funding_refresh_route_keys(
    snapshot: dict[str, Any],
    *,
    tokens_per_lane: int = 25,
) -> set[str]:
    rows = [
        row
        for bucket in ("api_discovered_rows", "dex_discovered_rows")
        for row in snapshot.get(bucket) or []
        if isinstance(row, dict)
    ]
    selected: set[str] = set()
    for lane in ("FUTURES", "FUTURES-SPOT", "DEX-FUTURES"):
        lane_rows = [row for row in rows if _funding_lane(row) == lane]
        for metric in ("depth_weighted_spread_pct", "funding_daily_pct"):
            seen_tokens: set[str] = set()
            ranked = sorted(
                lane_rows,
                key=lambda row: _rank_value(row.get(metric)),
                reverse=True,
            )
            for row in ranked:
                token = str(row.get("token") or "").upper()
                if not token or token in seen_tokens:
                    continue
                seen_tokens.add(token)
                selected.add(_snapshot_route_key(row))
                if len(seen_tokens) >= tokens_per_lane:
                    break
    return selected


def _funding_lane(row: dict[str, Any]) -> str | None:
    long_type = str(row.get("long_market_type") or "")
    short_type = str(row.get("short_market_type") or "")
    venue_text = " ".join(
        (
            str(row.get("long_venue") or ""),
            str(row.get("short_venue") or ""),
        )
    ).casefold()
    if "Futures" in {long_type, short_type} and (
        "dex" in venue_text or "dex" in f"{long_type} {short_type}".casefold()
    ):
        return "DEX-FUTURES"
    if long_type == short_type == "Futures":
        return "FUTURES"
    if {long_type, short_type} == {"Futures", "Spot"}:
        return "FUTURES-SPOT"
    return None


def _rank_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("-inf")
    return number


def _snapshot_route_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key) or "")
        for key in (
            "token",
            "long_venue",
            "long_market_type",
            "short_venue",
            "short_market_type",
        )
    )


def _atomic_write_snapshot(snapshot: dict[str, Any]) -> None:
    temporary = SNAPSHOT_PATH.with_suffix(".funding.tmp")
    temporary.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
    temporary.replace(SNAPSHOT_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
