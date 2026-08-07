#!/usr/bin/env python3
"""Run SpreadBoard and its public-API refresh loop as one persistent service."""

from __future__ import annotations

# ruff: noqa: E402

import ctypes
import gc
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
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
    crypto_watcher,
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
        self.warm_thread: threading.Thread | None = None
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
        if self.warm_thread is not None and self.warm_thread.is_alive():
            self.warm_thread.join(timeout=5.0)
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
            # 22,309 markets means a loaded ccxt client per venue; that belongs
            # in a process that exits, not in the one serving pages.
            summary = _artifact_worker(
                "chart-catalog",
                "--workers",
                os.environ.get("SPREADBOARD_CHART_CATALOG_WORKERS", "4"),
            )
            if summary:
                _log(
                    f"chart catalog markets={summary.get('markets', 0)} "
                    f"tokens={summary.get('tokens', 0)}"
                )
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
        result = _run_worker(
            command,
            timeout=float(os.environ.get("SPREADBOARD_REFRESH_TIMEOUT_SECONDS", "900")),
        )
        if result.timed_out:
            _log("refresh timeout")
            return
        if result.returncode != 0:
            _log(f"refresh failed ({result.returncode}): {result.stderr[-500:]}")
            return
        # Every step below used to parse the 40MB snapshot here, in the web
        # server: the staging copy and the published one at the same time so the
        # merge could see both, then again for the identity registry. That is
        # roughly a gigabyte of Python objects per copy, and it took the process
        # to 4.31GB five minutes after every start until the kernel killed it --
        # taking the site down and losing the very scan it had just finished.
        # It all happens in a process that exits now.
        enriched = _finalize_snapshot("enrich")
        if enriched is None:
            return
        # The discovery worker writes to a staging snapshot so it cannot block
        # or overwrite fast quote cycles while the broad venue scan is running.
        # The publish stage is short, and the locks are held across it exactly
        # as they were when the merge and write happened inline.
        with self.quote_cycle_lock, self.snapshot_lock:
            published = _finalize_snapshot("publish")
        if published is None:
            return
        _log(
            "refresh complete "
            f"routes={published.get('routes')} "
            f"history_inserted={published.get('history_inserted')} "
            f"funding={enriched.get('funding')} status={published.get('refresh_status')}"
        )
        _refresh_enrichment_subprocess()
        if not lightweight_mode:
            self._refresh_verified_identity_registry(snapshot_path=SNAPSHOT_PATH)
        _return_freed_memory()

    def _refresh_verified_identity_registry(self, *, snapshot_path: Path) -> None:
        # Parsing the 40MB snapshot costs roughly a gigabyte, and this runs
        # before every scan -- inside the server it was most of what the process
        # held one minute after starting.
        summary = _artifact_worker(
            "identity-registry",
            "--snapshot-path",
            str(snapshot_path),
            "--output-path",
            str(GENERATED_IDENTITY_PATH),
            "--static-registry-path",
            str(ROOT / "data/api_discovery_identity_registry.json"),
            "--watchlist-path",
            str(ROOT / "data/api_discovery_watchlist.json"),
            "--rails-path",
            str(RUNTIME_DIR / "public_transfer_rails.json"),
        )
        if summary:
            _log(
                "verified identity registry "
                f"matches={summary.get('matches', 0)} "
                f"markets_added={summary.get('markets_added', 0)}"
            )

    def run_fast_quotes(self) -> None:
        interval = max(
            20.0,
            float(os.environ.get("SPREADBOARD_FAST_QUOTE_SECONDS", "30")),
        )
        while not self.stop_event.wait(5.0):
            cycle_started = time.monotonic()
            with self.quote_cycle_lock:
                _log("fast quote cycle starting")
                summary = self._refresh_fast_quotes()
                with self.snapshot_lock:
                    # This parsed the 40MB snapshot here, in the server, once a
                    # minute. That is roughly a gigabyte of Python objects per
                    # cycle; freeing them leaves the arenas fragmented, so RSS
                    # only climbed -- 1.8GB after one minute, 4.3GB by five, and
                    # then the kernel killed the whole container. It is the
                    # reason the site kept going down and no scan ever landed.
                    inserted = 0
                    if summary.get("updated_routes"):
                        recorded = _finalize_snapshot("record")
                        inserted = (recorded or {}).get("history_inserted") or 0
            _log(f"fast quotes {summary} history_inserted={inserted}")
            if summary.get("updated_routes"):
                _invalidate_market_price_caches()
            self._start_board_warm()
            # The interval is a start-to-start target. Sleeping a full interval
            # after a multi-minute quote pass and cache warm created a stale
            # gap on every cycle even though the configured cadence was 60s.
            self.stop_event.wait(max(0.0, interval - (time.monotonic() - cycle_started)))

    def _start_board_warm(self) -> None:
        """Warm request caches without delaying the next market-price pass."""
        if self.warm_thread is not None and self.warm_thread.is_alive():
            return
        self.warm_thread = threading.Thread(
            target=_warm_board_cache,
            name="spreadboard-board-warm",
            daemon=True,
        )
        self.warm_thread.start()

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
        result = _run_worker(command, timeout=_fast_quote_timeout())
        if result.timed_out:
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


def _artifact_worker(job: str, *arguments: str) -> dict[str, Any] | None:
    """Build one of the board's files in a process that exits."""
    result = _run_worker(
        [
            *_low_priority_prefix(),
            sys.executable,
            str(ROOT / "scripts/artifact_worker.py"),
            "--job",
            job,
            *arguments,
        ],
        timeout=float(os.environ.get("SPREADBOARD_ARTIFACT_TIMEOUT_SECONDS", "900")),
    )
    if result.timed_out:
        _log(f"{job} timeout")
        return None
    if result.returncode != 0:
        _log(f"{job} unavailable ({result.returncode}): {result.stderr[-300:]}")
        return None
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        _log(f"{job} produced no summary")
        return None


def _finalize_snapshot(stage: str) -> dict[str, Any] | None:
    """Run one stage of the post-scan pipeline and read back its summary."""
    command = [
        *_low_priority_prefix(),
        sys.executable,
        str(ROOT / "scripts/snapshot_finalize_worker.py"),
        "--stage",
        stage,
        "--staging-path",
        str(REFRESH_SNAPSHOT_PATH),
        "--published-path",
        str(SNAPSHOT_PATH),
        "--funding-workers",
        os.environ.get("SPREADBOARD_FUNDING_HISTORY_WORKERS", "4"),
    ]
    result = _run_worker(
        command,
        timeout=float(os.environ.get("SPREADBOARD_FINALIZE_TIMEOUT_SECONDS", "900")),
    )
    if result.timed_out:
        _log(f"snapshot {stage} timeout")
        return None
    if result.returncode != 0:
        _log(f"snapshot {stage} failed ({result.returncode}): {result.stderr[-400:]}")
        return None
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        _log(f"snapshot {stage} produced no summary")
        return None


def _refresh_enrichment_subprocess() -> None:
    command = [
        sys.executable,
        str(ROOT / "scripts/refresh_spreadboard_enrichment.py"),
        "--snapshot-path",
        str(SNAPSHOT_PATH),
        "--rail-workers",
        "1",
    ]
    result = _run_worker(
        command,
        timeout=float(os.environ.get("SPREADBOARD_ENRICHMENT_TIMEOUT_SECONDS", "240")),
    )
    if result.timed_out:
        _log("isolated enrichment timeout")
        return
    summary = (result.stdout or result.stderr).strip()[-500:]
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
    MemoryWatchdog(refresh_loop.stop_event).start()
    # Without this nothing watches the chain, so a member could send USDC and
    # the invoice would simply expire an hour later having credited nothing.
    # The watcher was only ever started by server.py's standalone CLI main(),
    # which production does not run -- production runs this file.
    crypto_stop = crypto_watcher.start_background(db_path=server.accounts_path)
    _log(
        "crypto watcher started"
        if crypto_stop is not None
        else "crypto watcher idle (receiving address or RPC URL not configured)"
    )
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
    # No `farm` here. The funding page strips farm and rank before building its
    # query -- they are presentation, not data -- so warming WITH farm builds a
    # key the page never reads. Measured live: /funding?farm=futures-dex took
    # 16.7s against 0.11s for the tabs whose key actually matched.
    {"funding_only": ["1"], "kind": ["FUTURES-SPOT-PAIR"], "sort": ["funding"], "direction": ["desc"], "limit": ["25"]},
    {"funding_only": ["1"], "kind": ["DEX-FUTURES"], "sort": ["funding"], "direction": ["desc"], "limit": ["25"]},
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

    #: A full pass measured 160s of quotes and 102s of funding, so it needs
    #: room to finish; a killed pass throws away everything it had gathered.
    TIMEOUT_SECONDS = max(
        120.0, float(os.environ.get("SPREADBOARD_BULK_QUOTE_TIMEOUT_SECONDS", "420"))
    )

    def run(self) -> None:
        from spreadboard import bulk_quotes

        while not self.stop_event.is_set():
            try:
                self._sweep_once()
            except Exception as exc:  # noqa: BLE001 - best effort beside everything else.
                _log(f"bulk quotes skipped: {type(exc).__name__}: {exc}")
            self.stop_event.wait(bulk_quotes.INTERVAL_SECONDS)

    def _sweep_once(self) -> None:
        """One pass in a process that exits, so its memory comes back.

        Run in-thread this held a loaded ccxt client per venue and grew by tens
        of megabytes a pass that no collection returned, until the service
        crossed its cgroup and was OOM-killed -- hourly, killing the discovery
        scan with it. It also held the GIL against every page load.
        """
        completed = _run_worker(
            [
                *_low_priority_prefix(),
                sys.executable,
                str(Path(__file__).with_name("bulk_quote_worker.py")),
                "--budget-seconds",
                str(self.TIMEOUT_SECONDS / 2),
                "--funding-budget-seconds",
                str(self.TIMEOUT_SECONDS / 2),
            ],
            timeout=self.TIMEOUT_SECONDS,
        )
        if completed.timed_out or completed.returncode != 0:
            _log(f"bulk quotes worker exit={completed.returncode} {completed.stderr[-300:]}")
            return
        try:
            summary = json.loads(completed.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            _log("bulk quotes worker produced no summary")
            return
        quotes = summary.get("quotes") or {}
        _log(
            f"bulk quotes: {quotes.get('quotes')} from {quotes.get('venues')} venues "
            f"in {quotes.get('seconds')}s"
        )
        funding = summary.get("funding") or {}
        if funding:
            _log(
                f"bulk funding: {funding.get('legs')} legs from {funding.get('venues')} "
                f"venues in {funding.get('seconds')}s"
            )
        if (quotes.get("quotes") or 0) > 0:
            _invalidate_market_price_caches()


def _invalidate_market_price_caches() -> None:
    """Drop grouped payloads after a worker writes newer books or deltas.

    Those caches are keyed on the large discovery snapshot because that keeps
    page loads fast. Price workers deliberately do not rewrite that snapshot,
    so without this explicit signal the server retained the old route ranking
    for 15-20 minutes after newer prices were already on disk.
    """

    from spreadboard import server as server_module

    with api_spreads._SNAPSHOT_CACHE_LOCK:
        api_spreads._RESULT_CACHE.clear()
    with server_module._MARKET_CACHE_LOCK:
        server_module._MARKET_CACHE.clear()
        server_module._MARKET_STALE_CACHE.clear()


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

#: Breathing room between warm builds, in seconds.
#:
#: Warming is CPU-bound pure Python in the same process as the HTTP server, so
#: it holds the GIL and the server cannot answer while it runs. Thirteen builds
#: back to back on two cores kept `/api/health` timing out for ten minutes after
#: every restart -- the site was not slow, it was unreachable. Sleeping between
#: builds releases the GIL and lets whatever is queued through; it costs a few
#: seconds on a pass that already takes minutes.
WARM_YIELD_SECONDS = max(0.0, float(os.environ.get("SPREADBOARD_WARM_YIELD_SECONDS", "1.0")))


def _yield_to_requests() -> None:
    if WARM_YIELD_SECONDS:
        time.sleep(WARM_YIELD_SECONDS)


def _return_freed_memory() -> None:
    """Hand freed arenas back to the kernel.

    Every snapshot write invalidates the caches and a fresh generation of
    payloads is built beside the one being dropped. Each generation is hundreds
    of megabytes of many medium-sized objects, which fragments the allocator, so
    `gc.collect()` frees the objects but glibc keeps the arenas and RSS only
    ever climbs. Measured components sum to under a gigabyte while the live
    process reached 4.2GB and was OOM-killed inside its 6GB cgroup.

    malloc_trim is glibc-only and advisory; anywhere else this is a no-op.
    """
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):  # noqa: S110 - musl, macOS: nothing to do.
        pass


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
    _log("board cache warm starting")
    for query in WARM_QUERIES:
        try:
            server.api_market_spreads(_board_path(), dict(query))
        except Exception as exc:  # noqa: BLE001 - warming is best effort.
            _log(f"board cache warm skipped {query}: {type(exc).__name__}: {exc}")
        _yield_to_requests()
    _log(f"warm queries done in {time.monotonic() - started:.1f}s")
    # Intel is derived from the same snapshot and costs about as much, so it is
    # warmed here rather than left to the first visitor.
    try:
        server.api_intel(_board_path())
    except Exception as exc:  # noqa: BLE001 - warming is best effort.
        _log(f"intel warm skipped: {type(exc).__name__}: {exc}")
    _yield_to_requests()
    try:
        # Opening a chart by route needs this index; building it on demand cost
        # 14.6s of the thirty a member waited.
        server._route_index(_board_path())
    except Exception as exc:  # noqa: BLE001 - warming is best effort.
        _log(f"route index warm skipped: {type(exc).__name__}: {exc}")
    _yield_to_requests()
    try:
        # The Telegram bot filters this one payload for every lookup. Unwarmed,
        # a bare "$" took 36s and Telegram's webhook gave up first.
        from spreadboard import telegram_queries

        telegram_queries._warm_payload(_board_path())
    except Exception as exc:  # noqa: BLE001 - warming is best effort.
        _log(f"telegram payload warm skipped: {type(exc).__name__}: {exc}")
    _yield_to_requests()
    try:
        # /api/health builds the board at limit=0, which is its own cache key
        # and was in nobody's warm set -- so the readiness probe was one of the
        # most expensive requests on the server and timed out against a cold
        # cache, reporting the container unhealthy while it was merely starting.
        server.api_source_health(_board_path(), {})
    except Exception as exc:  # noqa: BLE001 - warming is best effort.
        _log(f"health warm skipped: {type(exc).__name__}: {exc}")
    _yield_to_requests()
    _log(f"board cache warmed {len(WARM_QUERIES)} views in {time.monotonic() - started:.1f}s")
    _refresh_funding_windows()
    # The generation this pass replaced is now unreferenced; give it back.
    _return_freed_memory()


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


#: How much of a worker's output the parent keeps. Enough to diagnose a
#: failure, bounded so a chatty child cannot consume the server.
WORKER_OUTPUT_TAIL_BYTES = 64 * 1024


class WorkerResult:
    __slots__ = ("returncode", "stdout", "stderr", "timed_out")

    def __init__(self, returncode: int, stdout: str, stderr: str, timed_out: bool) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


def _run_worker(command: list[str], *, timeout: float, cwd: Path = ROOT) -> WorkerResult:
    """Run a worker without buffering everything it says in this process.

    `subprocess.run(capture_output=True)` accumulates the child's entire stdout
    and stderr in the parent's memory until it exits. The fast-quote worker hits
    its 240s deadline and writes venue errors the whole time, and the parent
    went from 0.51GB to 4.50GB inside a single call -- once a minute, which is
    what kept taking the container over its 6GB limit and killing the scan.

    The child writes to files; the parent reads back only the tail.
    """
    def tail(handle: Any) -> str:
        try:
            size = handle.tell()
            handle.seek(max(0, size - WORKER_OUTPUT_TAIL_BYTES))
            return handle.read().decode("utf-8", "replace")
        except OSError:
            return ""

    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        timed_out = False
        returncode = -1
        try:
            completed = subprocess.run(  # noqa: S603 - our own scripts, fixed argv.
                command,
                cwd=cwd,
                stdout=out,
                stderr=err,
                timeout=timeout,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        return WorkerResult(returncode, tail(out), tail(err), timed_out)


def _rss_gb() -> float:
    """This process's resident size. Zero where /proc is not available."""
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            return int(handle.read().split()[1]) * 4096 / 1024**3
    except (OSError, IndexError, ValueError):
        return 0.0


def _log(message: str) -> None:
    # Every line carries the process size. Finding what took this service to
    # 4.3GB inside a 6GB cgroup meant moving four subsystems out one at a time
    # and watching it climb anyway; a number on each line would have said which
    # one immediately.
    print(f"spreadboard-service: [{_rss_gb():.2f}GB] {message}", flush=True)


class MemoryWatchdog(threading.Thread):
    """Say what the process is holding, every so often.

    Four subsystems were moved out of this process one at a time while it kept
    climbing to 4GB, because the logs only spoke at the end of long operations
    and the growth happened between them.
    """

    def __init__(self, stop_event: threading.Event, interval_seconds: float = 20.0) -> None:
        super().__init__(name="memory-watchdog", daemon=True)
        self.stop_event = stop_event
        self.interval_seconds = interval_seconds

    def run(self) -> None:
        from spreadboard import server as server_module

        while not self.stop_event.wait(self.interval_seconds):
            try:
                _log(
                    "memory "
                    f"market={len(server_module._MARKET_CACHE)} "
                    f"stale={len(server_module._MARKET_STALE_CACHE)} "
                    f"result={len(api_spreads._RESULT_CACHE)} "
                    f"tick={len(server_module._LIVE_TICK)} "
                    f"threads={threading.active_count()}"
                )
            except Exception as exc:  # noqa: BLE001 - observation must not break serving.
                _log(f"memory watchdog: {type(exc).__name__}: {exc}")


def _seed_public_caches() -> None:
    seed_path = ROOT / "data/token_metadata_seed.json"
    target_path = token_metadata.DEFAULT_CACHE_PATH
    if target_path.exists() or not seed_path.exists():
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seed_path, target_path)


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


#: How many tokens per lane get REAL settled funding rather than a projection.
#:
#: Settled 24h funding is the sum of a venue's actual funding events, and it
#: needs one history call per futures leg -- there is no bulk endpoint for it on
#: most venues, so it cannot cover all 5,000 futures legs on the board. At 25 it
#: covered 181 of 15,754 rows, so 99% of the board showed a projection from the
#: current rate, which is exactly the number that misleads when funding flips.
#: Raising it trades scan time for accuracy on the routes people actually take.
FUNDING_TOKENS_PER_LANE = max(
    5, min(300, int(os.environ.get("SPREADBOARD_FUNDING_TOKENS_PER_LANE", "25")))
)


def _funding_refresh_route_keys(
    snapshot: dict[str, Any],
    *,
    tokens_per_lane: int | None = None,
) -> set[str]:
    rows = [
        row
        for bucket in ("api_discovered_rows", "dex_discovered_rows")
        for row in snapshot.get(bucket) or []
        if isinstance(row, dict)
    ]
    if tokens_per_lane is None:
        tokens_per_lane = FUNDING_TOKENS_PER_LANE
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
