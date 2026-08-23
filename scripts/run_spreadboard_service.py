#!/usr/bin/env python3
"""Run SpreadBoard and its public-API refresh loop as one persistent service."""

from __future__ import annotations

# ruff: noqa: E402
import ctypes
import gc
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT / "src", ROOT):
    while str(import_path) in sys.path:
        sys.path.remove(str(import_path))
    sys.path.insert(0, str(import_path))

from spreadboard import (
    accounts,
    alerts,
    api_spreads,
    board,
    crypto_watcher,
    market_history,
    portfolio,
    rail_watch,
    route_taxonomy,
    subscription_lifecycle,
    telegram_bot,
    telegram_checkout,
    token_metadata,
    web_push,
)  # noqa: E402
from spreadboard.server import SpreadBoardHandler, SpreadBoardServer  # noqa: E402

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
SNAPSHOT_PATH = RUNTIME_DIR / "api_discovery_latest.json"
#: The compact per-cycle quote delta, beside the discovery snapshot.
FAST_QUOTE_PATH = RUNTIME_DIR / "api_discovery_fast_quotes.json"
REFRESH_SNAPSHOT_PATH = RUNTIME_DIR / "api_discovery_refresh.json"
GENERATED_IDENTITY_PATH = RUNTIME_DIR / "api_discovery_identity_registry.generated.json"
MARKET_GENERATION_PATH = RUNTIME_DIR / "market_generation.json"


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
                *_live_worker_prefix(),
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
        staging_seed_signature = _artifact_signature(REFRESH_SNAPSHOT_PATH)
        result = _run_worker(
            command,
            timeout=float(os.environ.get("SPREADBOARD_REFRESH_TIMEOUT_SECONDS", "900")),
            env={**os.environ, "SPREADBOARD_OKX_DEX_BACKGROUND": "1"},
        )
        partial_after_timeout = False
        if result.timed_out:
            # The discovery runner atomically checkpoints after every completed
            # source and retains the previous rows for sources it has not yet
            # reached. A slow or unavailable venue must therefore not discard
            # 45-60 minutes of fresh work and leave the public catalogue aging
            # forever. Publish only when at least one checkpoint replaced the
            # seed copy; an early timeout still leaves the last good snapshot.
            partial_after_timeout = (
                _artifact_signature(REFRESH_SNAPSHOT_PATH)
                != staging_seed_signature
            )
            if not partial_after_timeout:
                _log("refresh timeout before any source completed")
                return
            _log("refresh timeout; publishing completed-source partial")
        if result.returncode != 0 and not partial_after_timeout:
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
            ("refresh partial complete " if partial_after_timeout else "refresh complete ")
            + f"routes={published.get('routes')} "
            f"history_inserted={published.get('history_inserted')} "
            f"funding={enriched.get('funding')} status={published.get('refresh_status')}"
        )
        _publish_shared_market_generation("discovery")
        _refresh_enrichment_subprocess()
        if not lightweight_mode and not _env_bool("SPREADBOARD_DISABLE_LOCAL_CACHE_WARM"):
            self._refresh_verified_identity_registry(snapshot_path=SNAPSHOT_PATH)
            # A broad discovery publishes a new structural universe, so this is
            # the useful point to rebuild every navigable view.  Fast quote
            # deltas only replace prices in that universe and arrive roughly
            # once a minute; warming all eleven grouped views after those deltas
            # held the GIL for minutes and made the supposedly warm site slower.
            self._start_board_warm()
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
            # Recording the entire 25k-row snapshot is deliberately not on the
            # current-quote path. It took another 45-60 seconds and prevented a
            # 14-contract DEX half from rotating before the previous half
            # expired. The scheduled discovery/funding-history pipeline still
            # records broad history; the compact delta publishes immediately.
            #
            # The delta itself is a different matter, and leaving it unrecorded
            # is what made the charts jagged: points were written only when the
            # discovery snapshot published, so samples on a charted route sat a
            # median of 17.7 minutes apart and a one-hour window came back
            # empty. A couple of hundred repriced routes is cheap to store and
            # is exactly the set anyone charts.
            inserted = market_history.record_fast_quotes(FAST_QUOTE_PATH)
            _log(f"fast quotes {summary} history_inserted={inserted}")
            if summary.get("updated_routes"):
                _publish_shared_market_generation("fast_quotes")
                _invalidate_market_price_caches()
                # Fast DEX quotes are part of the token page and Telegram
                # fallback artefact. Publish rankings without holding the quote
                # lock: the old synchronous 30-60s ranking build delayed the
                # next rolling DEX half until the first half had expired.
                _schedule_token_rankings()
            # The interval is a start-to-start target. Sleeping a full interval
            # after a multi-minute quote pass created a stale gap on every cycle
            # even though the configured cadence was 60s. Structural view warms
            # belong to the much less frequent broad-discovery publication.
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


def _publish_shared_market_generation(kind: str) -> None:
    """Tell a separate web process that shared market artifacts advanced.

    The combined service can invalidate its in-memory caches directly. Once
    collectors run in another container, that direct call only clears the
    collector's unused caches. This tiny atomic file is the process boundary:
    it contains no market rows or credentials, only a generation and reason.
    """

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "spreadboard.market_generation.v1",
        "kind": str(kind or "market"),
        "updated_at_unix": time.time(),
        "generation_ns": time.time_ns(),
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{MARKET_GENERATION_PATH.name}.",
        dir=RUNTIME_DIR,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, MARKET_GENERATION_PATH)
    except Exception:
        with suppress(OSError):
            os.unlink(temporary)
        raise


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


def _warm_telegram_payload_at_startup(board_path: Path) -> None:
    """Make bot queries available without waiting for the first quote cycle.

    A production restart already has a complete canonical snapshot on the
    runtime volume. The first exchange refresh can take several minutes, so
    tying bot readiness to that cycle made every deployment look like a broken
    bot even while the website was healthy.
    """
    try:
        from spreadboard import server, telegram_queries

        started = time.monotonic()
        # Markets is the first authenticated navigation view. Warm its exact
        # default key before the larger Telegram catalogue so the first member
        # after a deploy never pays a 15-20 second grouped-board build.
        server.api_market_spreads(board_path, {})
        _yield_to_requests()
        payload = server.api_market_spreads(
            board_path,
            {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]},
        )
        _yield_to_requests()
        telegram_queries.replace_payload(payload)
        funding_payloads = []
        for query in WARM_QUERIES:
            if not query.get("funding_only"):
                continue
            funding_payloads.append(
                server.api_market_spreads(board_path, dict(query))
            )
            _yield_to_requests()
        telegram_queries.replace_funding_payloads(funding_payloads)
        _log(
            "telegram startup payload ready "
            f"in {time.monotonic() - started:.1f}s"
        )
        if _service_role() != "web":
            _refresh_token_rankings(force=True)
    except Exception as exc:  # noqa: BLE001 - the regular warmer retries later.
        _log(f"telegram startup payload skipped: {type(exc).__name__}: {exc}")


def _service_role() -> str:
    """Return the explicit process role, retaining combined mode for local use."""

    role = os.environ.get("SPREADBOARD_SERVICE_ROLE", "combined").strip().casefold()
    if role not in {"combined", "web", "collector"}:
        raise ValueError(
            "SPREADBOARD_SERVICE_ROLE must be combined, web, or collector"
        )
    return role


def _artifact_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class SharedArtifactWatcher(threading.Thread):
    """Bridge collector file generations into the web process's memory caches."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        poll_seconds: float = 1.0,
        initial_warm_delay_seconds: float = 30.0,
        invalidation_interval_seconds: float | None = None,
    ) -> None:
        super().__init__(name="shared-market-artifact-watcher", daemon=True)
        self.stop_event = stop_event
        self.poll_seconds = max(0.05, poll_seconds)
        self.initial_warm_at = time.monotonic() + max(
            0.0, initial_warm_delay_seconds
        )
        self.initial_warm_requested = False
        self.invalidation_interval_seconds = max(
            1.0,
            float(
                invalidation_interval_seconds
                if invalidation_interval_seconds is not None
                else os.environ.get(
                    "SPREADBOARD_WEB_CACHE_INVALIDATION_SECONDS", "120"
                )
            ),
        )
        self.last_invalidation_at = 0.0
        self.invalidation_pending = False
        self.generation_signature = _artifact_signature(MARKET_GENERATION_PATH)
        self.snapshot_signature = _artifact_signature(SNAPSHOT_PATH)
        self.warm_lock = threading.Lock()
        self.warm_pending = False
        self.warm_thread: threading.Thread | None = None

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 - stale data is safer than a dead site.
                _log(f"shared artifact watcher: {type(exc).__name__}: {exc}")
            self.stop_event.wait(self.poll_seconds)

    def check_once(self) -> None:
        generation = _artifact_signature(MARKET_GENERATION_PATH)
        if generation != self.generation_signature:
            self.generation_signature = generation
            self.invalidation_pending = True
        self._invalidate_if_due()

        snapshot = _artifact_signature(SNAPSHOT_PATH)
        if snapshot != self.snapshot_signature:
            self.snapshot_signature = snapshot
            self.request_warm()

        if (
            not self.initial_warm_requested
            and time.monotonic() >= self.initial_warm_at
        ):
            self.initial_warm_requested = True
            self.request_warm()

    def _invalidate_if_due(self) -> None:
        """Coalesce price/funding generations while live overlays stay current."""

        now = time.monotonic()
        if not self.invalidation_pending or (
            self.last_invalidation_at
            and now - self.last_invalidation_at < self.invalidation_interval_seconds
        ):
            return
        _invalidate_market_price_caches()
        self.invalidation_pending = False
        self.last_invalidation_at = now

    def request_warm(self) -> None:
        """Coalesce structural changes while never losing the newest one."""

        with self.warm_lock:
            self.warm_pending = True
            if self.warm_thread is not None and self.warm_thread.is_alive():
                return
            self.warm_thread = threading.Thread(
                target=self._drain_warms,
                name="shared-market-cache-warm",
                daemon=True,
            )
            self.warm_thread.start()

    def _drain_warms(self) -> None:
        while not self.stop_event.is_set():
            with self.warm_lock:
                if not self.warm_pending:
                    return
                self.warm_pending = False
            _warm_board_cache(force=True)

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=5.0)
        if self.warm_thread is not None and self.warm_thread.is_alive():
            self.warm_thread.join(timeout=5.0)


def _run_collector_service() -> int:
    """Own exchange I/O and artifact publication without accepting HTTP."""

    os.environ["SPREADBOARD_DISABLE_LOCAL_CACHE_WARM"] = "1"
    interval = float(os.environ.get("SPREADBOARD_REFRESH_SECONDS", "300"))
    if _env_bool("SPREADBOARD_LIGHTWEIGHT_MODE"):
        interval = max(600.0, interval)
    market_history.initialize()
    _seed_public_caches()
    refresh_loop = RefreshLoop(interval)
    bulk_quote_loop = BulkQuoteLoop(refresh_loop.stop_event)
    bulk_funding_loop = BulkFundingLoop(refresh_loop.stop_event)
    market_evidence_loop = MarketEvidenceLoop(refresh_loop.stop_event)

    def stop_collector(_signum: int, _frame: Any) -> None:
        refresh_loop.stop_event.set()

    signal.signal(signal.SIGTERM, stop_collector)
    signal.signal(signal.SIGINT, stop_collector)
    refresh_loop.start()
    bulk_quote_loop.start()
    bulk_funding_loop.start()
    market_evidence_loop.start()
    MemoryWatchdog(refresh_loop.stop_event).start()
    _log("collector role started")
    try:
        while not refresh_loop.stop_event.wait(0.5):
            pass
    finally:
        refresh_loop.stop()
        bulk_quote_loop.join(timeout=5.0)
        bulk_funding_loop.join(timeout=5.0)
        market_evidence_loop.join(timeout=5.0)
    return 0


def main() -> int:
    from spreadboard import server as server_module

    role = _service_role()
    if role == "collector":
        return _run_collector_service()

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8200"))
    interval = float(os.environ.get("SPREADBOARD_REFRESH_SECONDS", "300"))
    if _env_bool("SPREADBOARD_LIGHTWEIGHT_MODE"):
        interval = max(600.0, interval)
    board_path = Path(os.environ.get("SPREADBOARD_BOARD_PATH", str(board.DEFAULT_BOARD_PATH)))
    # Read-only pages select the complete history schema. Apply additive
    # columns before any warm thread or HTTP request can race the first writer.
    market_history.initialize()
    _seed_public_caches()
    refresh_loop = RefreshLoop(interval) if role == "combined" else None
    service_stop_event = (
        refresh_loop.stop_event if refresh_loop is not None else threading.Event()
    )
    artifact_watcher = (
        SharedArtifactWatcher(service_stop_event) if role == "web" else None
    )
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
        quote_scheduler=server_module._schedule_chart_route_refresh,
    )
    server.position_alert_worker = position_alert_worker
    membership_worker = telegram_bot.MembershipWorker(
        db_path=server.accounts_path,
        poll_seconds=float(os.environ.get("SPREADBOARD_TELEGRAM_MEMBERSHIP_SECONDS", "60")),
    )
    subscription_worker = subscription_lifecycle.Worker(
        db_path=server.accounts_path,
        poll_seconds=float(
            os.environ.get("SPREADBOARD_SUBSCRIPTION_LIFECYCLE_SECONDS", "900")
        ),
    )
    server.subscription_lifecycle_worker = subscription_worker
    public_feed_worker = telegram_bot.PublicFeedWorker(
        board_path=board_path,
        poll_seconds=float(os.environ.get("SPREADBOARD_TELEGRAM_PUBLIC_FEED_SECONDS", "900")),
    )
    market_alert_worker = alerts.UserMarketAlertWorker(
        board_path=board_path,
        accounts_path=server.accounts_path,
        poll_seconds=float(os.environ.get("SPREADBOARD_MARKET_ALERT_SECONDS", "10")),
    )
    web_push_worker = web_push.Worker(
        accounts_path=server.accounts_path,
        poll_seconds=float(os.environ.get("SPREADBOARD_WEB_PUSH_SECONDS", "5")),
    )
    checkout_notifier = telegram_checkout.Notifier(
        accounts_path=server.accounts_path,
        poll_seconds=float(os.environ.get("SPREADBOARD_TELEGRAM_CHECKOUT_SECONDS", "10")),
    )
    server.web_push_worker = web_push_worker
    rail_reopen_worker = rail_watch.RailReopenWatcher(
        poll_seconds=float(os.environ.get("SPREADBOARD_RAIL_REOPEN_SECONDS", "300")),
    )
    page_view_worker = accounts.PageViewWorker(db_path=server.accounts_path)

    # Route links are a primary navigation path. Building their index on the
    # first request cost 14-15 seconds and made the first chart after every
    # deploy look broken. Pay that one-time cost inside Docker's startup grace
    # period, before the service announces that it is serving traffic.
    _warm_route_index()
    # Build the member's default market view before the background Telegram and
    # funding warm can occupy the only memory-safe grouping slot. Otherwise an
    # early browser sees a false empty scanner even though the mounted snapshot
    # and live books are already available.
    try:
        from spreadboard import server as server_module

        server_module.api_market_spreads(board_path, {})
    except Exception as exc:  # noqa: BLE001 - readiness still reports the failure.
        _log(f"default market warm skipped: {type(exc).__name__}: {exc}")

    def stop_service(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    threading.Thread(
        target=_warm_telegram_payload_at_startup,
        args=(board_path,),
        name="spreadboard-telegram-startup-warm",
        daemon=True,
    ).start()
    if refresh_loop is not None:
        refresh_loop.start()
    elif artifact_watcher is not None:
        artifact_watcher.start()
    MemoryWatchdog(service_stop_event).start()
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
    # Combined mode remains useful for a one-process local checkout. Production
    # gives these workers to the collector role so exchange load cannot starve
    # the subscriber-facing HTTP process.
    bulk_quote_loop = (
        BulkQuoteLoop(service_stop_event) if role == "combined" else None
    )
    bulk_funding_loop = (
        BulkFundingLoop(service_stop_event) if role == "combined" else None
    )
    if bulk_quote_loop is not None and bulk_funding_loop is not None:
        bulk_quote_loop.start()
        bulk_funding_loop.start()
    position_alert_worker.start()
    membership_worker.start()
    subscription_worker.start()
    public_feed_worker.start()
    market_alert_worker.start()
    web_push_worker.start()
    checkout_notifier.start()
    rail_reopen_worker.start()
    page_view_worker.start()
    _log(f"serving http://{host}:{port}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        position_alert_worker.stop()
        membership_worker.stop()
        subscription_worker.stop()
        public_feed_worker.stop()
        market_alert_worker.stop()
        web_push_worker.stop()
        checkout_notifier.stop()
        rail_reopen_worker.stop()
        page_view_worker.stop()
        if crypto_stop is not None:
            crypto_stop.set()
        service_stop_event.set()
        if refresh_loop is not None:
            refresh_loop.stop()
        if artifact_watcher is not None:
            artifact_watcher.stop()
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
    # Telegram needs the whole current funding universe, not only the 25 rows
    # currently leading each page tab. Otherwise a retained radar token could
    # still lose its current low rate after it cooled below rank 25. One
    # all-lane 500-row snapshot covers the live catalog without a per-message
    # rebuild or any relaxed filtering.
    {"funding_only": ["1"], "sort": ["funding"], "direction": ["desc"], "limit": ["500"]},
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


def _live_worker_prefix() -> list[str]:
    """Modestly deprioritise continuous collectors behind member requests.

    Discovery, rankings and funding use nice 19 because they can finish later.
    WebSocket and bulk-price collectors have a strict freshness budget, so
    they use a smaller configurable value. A page request or structural cache
    build therefore wins a saturated CPU slice without disabling collectors.
    """
    if not shutil.which("nice"):
        return []
    try:
        value = int(os.environ.get("SPREADBOARD_LIVE_WORKER_NICE", "8"))
    except ValueError:
        value = 8
    return ["nice", "-n", str(max(0, min(19, value)))]


class BulkQuoteLoop(threading.Thread):
    """Re-price the whole board from one bulk call per venue.

    Websockets cover a few hundred legs of the eight thousand the board carries
    and the scan re-quoted the rest every twenty-five minutes, so a route
    outside the streaming set could be twenty minutes stale and a token that
    turned positive in between did not appear until the next scan. One
    fetch_tickers per venue closes that in about fifteen seconds.
    """

    #: The bounded-concurrent quote pass measures about 43s. A killed pass
    #: throws away its summary and skips cache invalidation, so retain a
    #: generous hard ceiling without coupling it to slower funding providers.
    TIMEOUT_SECONDS = max(
        120.0, float(os.environ.get("SPREADBOARD_BULK_QUOTE_TIMEOUT_SECONDS", "420"))
    )

    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="bulk-quotes", daemon=True)
        self.stop_event = stop_event

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
                # Current prices are the board's truth boundary. Funding,
                # catalogue and ranking workers remain lowest-priority. Give
                # this continuous collector a modest background priority too:
                # it retains the measured 90-second freshness budget while
                # HTTP is protected from a collector using a whole core.
                *_live_worker_prefix(),
                sys.executable,
                str(Path(__file__).with_name("bulk_quote_worker.py")),
                "--budget-seconds",
                str(self.TIMEOUT_SECONDS / 2),
                "--funding-budget-seconds",
                "0",
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
        if (quotes.get("quotes") or 0) > 0:
            _publish_shared_market_generation("bulk_quotes")
            _invalidate_market_price_caches()
            # The rankings read the complete shared catalogue, so publish a new
            # atomic generation immediately after its prices move. This worker
            # is isolated and low-priority; HTTP readers keep serving the last
            # complete generation while it runs.
            _schedule_token_rankings()


class BulkFundingLoop(threading.Thread):
    """Refresh current funding independently of price freshness.

    Funding providers are materially slower and more failure-prone than bulk
    tickers. Keeping them in the quote worker made a 43-second price pass wait
    another minute or two before the next pass could begin, recreating stale
    spreads despite faster quote collection.
    """

    TIMEOUT_SECONDS = max(
        120.0, float(os.environ.get("SPREADBOARD_BULK_FUNDING_TIMEOUT_SECONDS", "240"))
    )
    INTERVAL_SECONDS = max(
        15.0, float(os.environ.get("SPREADBOARD_BULK_FUNDING_SECONDS", "15"))
    )
    VENUES_PER_PASS = max(
        1, int(os.environ.get("SPREADBOARD_FUNDING_VENUES_PER_PASS", "4"))
    )

    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="bulk-funding", daemon=True)
        self.stop_event = stop_event
        self.funding_cursor = 0

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._sweep_once()
            except Exception as exc:  # noqa: BLE001 - one slice is best effort.
                _log(f"bulk funding skipped: {type(exc).__name__}: {exc}")
            self.stop_event.wait(self.INTERVAL_SECONDS)

    def _sweep_once(self) -> None:
        from spreadboard.fast_quotes import NATIVE_FUNDING_SOURCES, VENUE_IDS

        funding_venues = sorted(set(VENUE_IDS) | set(NATIVE_FUNDING_SOURCES))
        count = min(self.VENUES_PER_PASS, len(funding_venues))
        selected = [
            funding_venues[(self.funding_cursor + index) % len(funding_venues)]
            for index in range(count)
        ] if funding_venues else []
        self.funding_cursor = (
            (self.funding_cursor + count) % len(funding_venues)
            if funding_venues
            else 0
        )
        if not selected:
            return
        completed = _run_worker(
            [
                *_low_priority_prefix(),
                sys.executable,
                str(Path(__file__).with_name("bulk_quote_worker.py")),
                "--skip-quotes",
                "--funding-budget-seconds",
                str(max(30.0, self.TIMEOUT_SECONDS * 0.7)),
                "--funding-venues",
                ",".join(selected),
            ],
            timeout=self.TIMEOUT_SECONDS,
        )
        if completed.timed_out or completed.returncode != 0:
            _log(f"bulk funding worker exit={completed.returncode} {completed.stderr[-300:]}")
            return
        try:
            funding = json.loads(completed.stdout.strip().splitlines()[-1]).get("funding") or {}
        except (ValueError, IndexError, AttributeError):
            _log("bulk funding worker produced no summary")
            return
        _log(
            f"bulk funding: {funding.get('legs')} legs from {funding.get('venues')} "
            f"venues in {funding.get('seconds')}s"
        )
        if (funding.get("legs") or 0) > 0:
            _publish_shared_market_generation("bulk_funding")


class MarketEvidenceLoop(threading.Thread):
    """Publish slow funding windows outside the web and collector parents."""

    TIMEOUT_SECONDS = max(
        900.0,
        float(os.environ.get("SPREADBOARD_MARKET_EVIDENCE_TIMEOUT_SECONDS", "3600")),
    )
    INTERVAL_SECONDS = max(
        900.0,
        float(os.environ.get("SPREADBOARD_MARKET_EVIDENCE_SECONDS", "10800")),
    )
    INITIAL_DELAY_SECONDS = max(
        0.0,
        float(
            os.environ.get(
                "SPREADBOARD_MARKET_EVIDENCE_INITIAL_DELAY_SECONDS", "900"
            )
        ),
    )

    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="market-evidence", daemon=True)
        self.stop_event = stop_event

    def run(self) -> None:
        if self.stop_event.wait(self.INITIAL_DELAY_SECONDS):
            return
        while not self.stop_event.is_set():
            self._sweep_once()
            self.stop_event.wait(self.INTERVAL_SECONDS)

    def _sweep_once(self) -> None:
        result = _run_worker(
            [
                *_low_priority_prefix(),
                sys.executable,
                str(Path(__file__).with_name("market_evidence_worker.py")),
            ],
            timeout=self.TIMEOUT_SECONDS,
        )
        if result.timed_out or result.returncode != 0:
            _log(
                "market evidence unavailable "
                f"exit={result.returncode} {result.stderr[-300:]}"
            )
            return
        summary = (result.stdout or result.stderr).strip().splitlines()
        _log(summary[-1] if summary else "market evidence completed")


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


def _board_path() -> Path:
    return Path(os.environ.get("SPREADBOARD_BOARD_PATH", str(board.DEFAULT_BOARD_PATH)))


#: A full pass costs 150-200s on two cores, and it used to run after every 60s
#: quote cycle -- so it never finished before starting again and held both cores
#: permanently, making every page slow instead of fast. One pass per interval,
#: comfortably inside the 420s cache TTL so entries stay warm between passes.
WARM_INTERVAL_SECONDS = max(
    300.0, float(os.environ.get("SPREADBOARD_WARM_INTERVAL_SECONDS", "900"))
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
    # A new discovery snapshot invalidates route links before it invalidates a
    # member's need to click them. Rebuild this small lookup first; the larger
    # navigation views can continue warming afterwards.
    _warm_route_index()
    _yield_to_requests()
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
        # The Telegram bot installs this exact website payload. Building a
        # separate direct-load generation let the two sets drift even when the
        # counts happened to match.
        from spreadboard import telegram_queries

        payload = server.api_market_spreads(
            _board_path(),
            {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]},
        )
        telegram_queries.replace_payload(payload)
        telegram_queries.replace_funding_payloads([
            server.api_market_spreads(_board_path(), dict(query))
            for query in WARM_QUERIES
            if query.get("funding_only")
        ])
    except Exception as exc:  # noqa: BLE001 - warming is best effort.
        _log(f"telegram payload warm skipped: {type(exc).__name__}: {exc}")
    _yield_to_requests()
    try:
        # /api/health builds the board at limit=0, which is its own cache key
        # and was in nobody's warm set -- so the readiness probe was one of the
        # most expensive requests on the server and timed out against a cold
        # cache, reporting the container unhealthy while it was merely starting.
        # Public readiness probes are deliberately O(1) and keep returning the
        # last complete answer.  Only this background pass is allowed to pay
        # for a new grouped health generation.
        server.api_source_health(_board_path(), {"force": True})
    except Exception as exc:  # noqa: BLE001 - warming is best effort.
        _log(f"health warm skipped: {type(exc).__name__}: {exc}")
    _yield_to_requests()
    _log(f"board cache warmed {len(WARM_QUERIES)} views in {time.monotonic() - started:.1f}s")
    if _service_role() != "web":
        _refresh_token_rankings()
        _refresh_funding_windows()
    # The generation this pass replaced is now unreferenced; give it back.
    _return_freed_memory()


_TOKEN_RANKING_REFRESH_LOCK = threading.Lock()
_LAST_TOKEN_RANKING_AT = 0.0
TOKEN_RANKING_INTERVAL_SECONDS = max(
    30.0, float(os.environ.get("SPREADBOARD_TOKEN_RANKING_SECONDS", "120"))
)


def _schedule_token_rankings() -> None:
    """Do not let ranking publication delay the next current-price pass."""

    threading.Thread(
        target=_refresh_token_rankings,
        name="token-ranking-publish",
        daemon=True,
    ).start()


def _refresh_token_rankings(*, force: bool = False) -> None:
    """Publish the individual-token leaderboard outside the web process."""

    global _LAST_TOKEN_RANKING_AT

    if not _TOKEN_RANKING_REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        if (
            not force
            and time.monotonic() - _LAST_TOKEN_RANKING_AT
            < TOKEN_RANKING_INTERVAL_SECONDS
        ):
            return
        result = _run_worker(
            [
                *_low_priority_prefix(),
                sys.executable,
                str(ROOT / "scripts/token_ranking_worker.py"),
                "--board-path",
                str(_board_path()),
            ],
            timeout=float(os.environ.get("SPREADBOARD_TOKEN_RANKING_TIMEOUT_SECONDS", "240")),
        )
        if result.timed_out or result.returncode != 0:
            _log(
                "token rankings unavailable "
                f"exit={result.returncode} {result.stderr[-300:]}"
            )
            return
        try:
            summary = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            _log("token rankings produced no summary")
            return
        _log(
            "token rankings ready "
            f"tokens={summary.get('tokens', 0)} live={summary.get('live', 0)} "
            f"cooled={summary.get('cooled', 0)}"
        )
        _LAST_TOKEN_RANKING_AT = time.monotonic()
    finally:
        _TOKEN_RANKING_REFRESH_LOCK.release()


def _warm_route_index() -> None:
    """Build chart-route lookup state before a browser can be charged for it."""
    from spreadboard import server as server_module

    started = time.monotonic()
    try:
        rows = server_module._route_index(_board_path())
    except Exception as exc:  # noqa: BLE001 - service can still run without it.
        _log(f"route index warm skipped: {type(exc).__name__}: {exc}")
        return
    _log(f"route index warmed rows={len(rows)} in {time.monotonic() - started:.1f}s")


def _refresh_funding_windows() -> None:
    """Precompute realised 1d/7d/30d carry for the routes on the board.

    Integrating the rate costs roughly half a second per route-window, so it
    cannot happen while rendering. Doing it here, for the routes the warm pass
    just built, means the page only ever reads the answer.
    """
    from spreadboard import funding_radar, market_history, research_calibration, server

    try:
        route_keys: list[str] = []
        leaders: list[dict[str, Any]] = []
        for query in WARM_QUERIES:
            if not query.get("funding_only"):
                continue
            payload = server.api_market_spreads(_board_path(), dict(query))
            for group in payload.get("groups") or []:
                leader = group.get("best_funding_route")
                if isinstance(leader, dict):
                    leaders.append(leader)
                for route in group.get("routes") or []:
                    key = route.get("route_key")
                    if key:
                        route_keys.append(str(key))
        if not route_keys:
            return
        started = time.monotonic()
        count = market_history.write_funding_windows(route_keys)
        _log(f"funding windows computed for {count} routes in {time.monotonic() - started:.1f}s")
        # Capture every warm generation, not merely the three-hour venue sweep.
        # A rate can lead for thirty minutes and cool before the next settlement
        # history refresh; that brief leader still belongs on the historical
        # radar, explicitly marked as no longer live.
        radar_count = funding_radar.refresh(leaders)
        _log(f"funding radar retained {radar_count} leader routes")
        calibration_capture = research_calibration.capture_routes(leaders)
        calibration_labels = research_calibration.label_matured()
        _log(
            "research calibration shadow "
            f"captured={calibration_capture['inserted']} "
            f"labeled={calibration_labels['labeled']}"
        )
        _refresh_venue_funding_history(leaders=leaders)
    except Exception as exc:  # noqa: BLE001 - a missing history file is not fatal.
        _log(f"funding windows skipped: {type(exc).__name__}: {exc}")


#: Once every catalog leg has been attempted, settled history moves slowly and
#: a three-hour maintenance cadence is sufficient. A new/expanded catalog uses
#: a bounded ten-minute catch-up cadence instead; otherwise a first full pass
#: over thousands of markets can take a day and leave legitimate 1d/7d/30d
#: research cells blank merely because their legs have never been queried.
VENUE_HISTORY_INTERVAL_SECONDS = max(
    900.0, float(os.environ.get("SPREADBOARD_VENUE_HISTORY_SECONDS", "10800"))
)
VENUE_HISTORY_CATCH_UP_SECONDS = max(
    300.0,
    min(
        VENUE_HISTORY_INTERVAL_SECONDS,
        float(os.environ.get("SPREADBOARD_VENUE_HISTORY_CATCH_UP_SECONDS", "600")),
    ),
)
_LAST_VENUE_HISTORY_AT = 0.0


def _refresh_venue_funding_history(*, leaders: list[dict[str, Any]] | None = None) -> None:
    """Pull each venue's settled funding for the legs the board is showing."""
    global _LAST_VENUE_HISTORY_AT

    from spreadboard import accounts, chart_catalog, funding_radar, server, venue_funding_history

    try:
        catalog = chart_catalog.load()
        catalog_legs = [
            (str(item.get("venue")), str(item.get("symbol")))
            for item in catalog.get("markets") or []
            if isinstance(item, dict)
            and item.get("market_type") == "Futures"
            and item.get("venue")
            and item.get("symbol")
        ]
        catalog_legs = list(dict.fromkeys(catalog_legs))
        before = venue_funding_history.coverage_summary(catalog_legs)
        interval = (
            VENUE_HISTORY_INTERVAL_SECONDS
            if before["catch_up_complete"]
            and int(before.get("retryable_error_leg_count") or 0) == 0
            else VENUE_HISTORY_CATCH_UP_SECONDS
        )
        now = time.monotonic()
        if now - _LAST_VENUE_HISTORY_AT < interval:
            return
        _LAST_VENUE_HISTORY_AT = now

        priority_legs: list[tuple[str, str]] = []
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
                            priority_legs.append((str(venue), str(symbol)))
        priority_legs.extend(
            accounts.all_open_position_futures_legs(db_path=accounts.DEFAULT_DB_PATH)
        )
        tracked = set(accounts.all_watchlist_symbols(db_path=accounts.DEFAULT_DB_PATH))
        priority_legs.extend(
            (str(item.get("venue")), str(item.get("symbol")))
            for item in catalog.get("markets") or []
            if isinstance(item, dict)
            and str(item.get("token") or "").upper() in tracked
            and item.get("market_type") == "Futures"
            and item.get("venue")
            and item.get("symbol")
        )
        for retained in funding_radar.routes_for():
            for side in ("long", "short"):
                if retained.get(f"{side}_market_type") == "Futures":
                    venue = retained.get(f"{side}_venue")
                    symbol = retained.get(f"{side}_market_symbol")
                    if venue and symbol:
                        priority_legs.append((str(venue), str(symbol)))
        priority_legs = list(dict.fromkeys(priority_legs))[:120]
        if not catalog_legs and not priority_legs:
            return
        started = time.monotonic()
        windows = venue_funding_history.build(
            list(dict.fromkeys(catalog_legs)), priority_legs=priority_legs
        )
        # Replace the just-captured settlement snapshots with the fresher venue
        # history while these routes are still known live.
        if leaders:
            funding_radar.refresh(leaders)
        after = venue_funding_history.coverage_summary(catalog_legs)
        _log(
            f"venue funding history: {len(windows)} legs with windows; "
            f"checked={after['attempted_leg_count']}/{after['catalog_leg_count']} "
            f"classified={after['classified_leg_count']} "
            f"pending={after['pending_leg_count']} verified={after['coverage_pct']}% "
            f"retryable={after['retryable_error_leg_count']} "
            f"mode={
                'maintenance'
                if after['catch_up_complete']
                and int(after.get('retryable_error_leg_count') or 0) == 0
                else 'catch_up'
            } "
            f"in {time.monotonic() - started:.1f}s"
        )
    except Exception as exc:  # noqa: BLE001 - best effort beside everything else.
        _log(f"venue funding history skipped: {type(exc).__name__}: {exc}")


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


def _run_worker(
    command: list[str],
    *,
    timeout: float,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> WorkerResult:
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
                env=env,
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
    kind = route_taxonomy.route_kind(
        long_venue=row.get("long_venue"),
        long_market_type=row.get("long_market_type"),
        short_venue=row.get("short_venue"),
        short_market_type=row.get("short_market_type"),
        source_kind=row.get("source_kind"),
    )
    if kind == "DEX-FUTURES":
        return kind
    if kind == "FUTURES":
        return "FUTURES"
    if kind in {"FUTURES-SPOT", "SPOT-FUTURES"}:
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
