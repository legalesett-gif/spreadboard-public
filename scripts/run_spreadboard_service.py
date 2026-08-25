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
    chart_warm_demand,
    crypto_watcher,
    funding_catalog,
    funding_history_demand,
    historical_spreads,
    market_history,
    materialized_views,
    portfolio,
    rail_watch,
    route_taxonomy,
    subscription_lifecycle,
    telegram_bot,
    telegram_checkout,
    token_metadata,
    tracked_route_warmer,
    warm_query_projection,
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

# Discovery, exact funding evidence, and materialized navigation each expand
# tens of thousands of markets into large Python object graphs.  The collector
# has enough memory for any one of them, but not for two at once.  Keep current
# quote workers independent while serializing only these heavy publications.
_COLLECTOR_HEAVY_LOCK = threading.Lock()


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
        self.websocket_lock = threading.Lock()
        self.websocket_paused = threading.Event()
        self.startup_evidence_ready: threading.Event | None = None

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
        self.pause_websocket_worker()

    def run(self) -> None:
        # A deploy must not put deep venue discovery in front of the warm
        # member views. If structural artifacts advanced while the collector
        # was down, first compact the already-published last-complete snapshot;
        # discovery can then run without making Funding cold in the meantime.
        if not _materialized_sources_current():
            self.pause_websocket_worker()
            try:
                with _COLLECTOR_HEAVY_LOCK:
                    _refresh_materialized_views(force=True)
            finally:
                self.resume_websocket_worker()
        self._wait_for_startup_evidence()
        initial_delay = _remaining_discovery_delay_seconds(
            SNAPSHOT_PATH,
            interval_seconds=self.interval_seconds,
        )
        if initial_delay and self.stop_event.wait(initial_delay):
            return
        while not self.stop_event.is_set():
            self._ensure_websocket_worker()
            started = time.monotonic()
            with _COLLECTOR_HEAVY_LOCK:
                self.refresh_once()
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(15.0, self.interval_seconds - elapsed))

    def _wait_for_startup_evidence(self) -> None:
        """Repair exact settlements before a due deep discovery starts.

        A persisted snapshot already serves the complete structural catalogue,
        while current book and funding workers start independently. Starting a
        ten-minute deep scan first therefore bought no visible availability but
        held the heavy-work lock as rolling settlement windows expired. The
        wait is bounded; the same lock still prevents overlap if evidence runs
        longer than the gate.
        """

        ready = self.startup_evidence_ready
        if ready is None or ready.is_set() or not SNAPSHOT_PATH.exists():
            return
        timeout = max(
            0.0,
            float(
                os.environ.get(
                    "SPREADBOARD_STARTUP_EVIDENCE_WAIT_SECONDS", "600"
                )
            ),
        )
        if timeout <= 0:
            return
        _log("structural discovery waiting for initial exact funding evidence")
        deadline = time.monotonic() + timeout
        while not ready.is_set() and not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log("initial funding evidence wait elapsed; discovery remains lock-safe")
                return
            ready.wait(min(1.0, remaining))
        if ready.is_set():
            _log("initial exact funding evidence complete; structural discovery may run")

    def run_chart_catalog(self) -> None:
        interval = max(
            900.0,
            float(os.environ.get("SPREADBOARD_CHART_CATALOG_SECONDS", "21600")),
        )
        initial_delay = _remaining_discovery_delay_seconds(
            RUNTIME_DIR / "chart_market_catalog.json",
            interval_seconds=interval,
        )
        if initial_delay and self.stop_event.wait(initial_delay):
            return
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
            self.stop_event.wait(interval)

    def _ensure_websocket_worker(self) -> None:
        if _env_bool("SPREADBOARD_DISABLE_WEBSOCKETS"):
            return
        with self.websocket_lock:
            if self.websocket_paused.is_set() or self.stop_event.is_set():
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

    def pause_websocket_worker(self) -> None:
        """Release the optional fast-lane process during high-memory analytics."""

        self.websocket_paused.set()
        with self.websocket_lock:
            process = self.websocket_process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.websocket_process = None

    def resume_websocket_worker(self) -> None:
        """Restore the fast lane after bulk quotes kept prices current."""

        if self.stop_event.is_set():
            return
        self.websocket_paused.clear()
        self._ensure_websocket_worker()

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
        # Heavy structural products are published by the collector. The web
        # watcher atomically installs them from the shared runtime volume and
        # never competes with subscriber requests for CPU.
        _refresh_live_route_index(install=False)
        _refresh_complete_funding_catalog(force=True)
        _refresh_enrichment_subprocess()
        # Publish the compact navigation generation immediately after its
        # structural universe and complete funding catalogue agree. Waiting
        # for the later evidence sweep left a restarted site serving the old
        # 100MB-per-window views for up to fifteen minutes.
        _refresh_materialized_views(force=True)
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
        # In production the collector publishes atomic materialized views and
        # the web process installs them.  Running the legacy in-process warm in
        # the collector retained the full 200MB funding catalogue in its parent
        # and overlapped the next child generation until the 4GB cgroup OOMed.
        if _service_role() == "collector":
            return
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
    """Restore bot queries without rebuilding any market view in HTTP."""
    try:
        from spreadboard import server, telegram_queries

        started = time.monotonic()
        restored = telegram_queries.restore_persisted_payloads()
        if restored["spread"] and restored["funding"]:
            _log(
                "telegram startup payload already restored "
                f"in {time.monotonic() - started:.1f}s"
            )
            return
        payload = server._MATERIALIZED_VIEW_STORE.payload_for(
            {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]},
        )
        if not restored["spread"] and payload:
            telegram_queries.replace_payload(payload)
        funding_payloads = [
            server._MATERIALIZED_VIEW_STORE.payload_for(query)
            for query in WARM_QUERIES
            if query.get("funding_only") and not query.get("funding_window")
        ]
        if not restored["funding"] and funding_payloads and all(funding_payloads):
            telegram_queries.replace_funding_payloads(
                [item for item in funding_payloads if item]
            )
        _log(
            "telegram startup payload restored "
            f"in {time.monotonic() - started:.1f}s"
        )
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


def _remaining_discovery_delay_seconds(
    snapshot_path: Path,
    *,
    interval_seconds: float,
    now: float | None = None,
) -> float:
    """Keep a recent persisted catalogue warm across collector restarts.

    Bulk books and funding start independently, so rerunning the most expensive
    structural discovery immediately after every deploy only steals CPU from
    member requests. A missing or expired snapshot still refreshes at once.
    """

    try:
        modified_at = snapshot_path.stat().st_mtime
    except OSError:
        return 0.0
    current = time.time() if now is None else float(now)
    age = max(0.0, current - modified_at)
    return max(0.0, float(interval_seconds) - age)


def _materialized_sources_current() -> bool:
    """Whether the durable generation already covers every structural input.

    Live books and current funding deliberately are not structural inputs: they
    are overlaid on every response.  Avoiding a blind startup rebuild matters
    because a complete generation is the warm state we are trying to preserve
    across restarts.  The comparison also coalesces duplicate watcher events
    that arrived while the just-completed generation was being built.
    """

    status = materialized_views.default_store().status()
    source = status.get("source_signature")
    if not status.get("ready") or not isinstance(source, dict):
        return False

    def encoded_signature(path: Path) -> list[int] | None:
        signature = _artifact_signature(path)
        return list(signature) if signature is not None else None

    board_path = _board_path()
    expected = {
        "board_path": str(board_path.resolve()),
        "board": encoded_signature(board_path),
        "discovery": encoded_signature(SNAPSHOT_PATH),
        "chart_catalog": encoded_signature(RUNTIME_DIR / "chart_market_catalog.json"),
        "metadata": encoded_signature(api_spreads.token_metadata.DEFAULT_CACHE_PATH),
        "rails": encoded_signature(api_spreads.public_rails.DEFAULT_CACHE_PATH),
    }
    return all(source.get(key) == value for key, value in expected.items())


def _materialized_generation_ready() -> bool:
    """Whether a complete durable fallback exists, regardless of live prices.

    The resident universe reprices current routes and handles every arbitrary
    query. Rebuilding all HTML/API views whenever a 60-90 second quote snapshot
    changes is therefore waste: it kept a multi-minute Python child running
    almost continuously and stole CPU from requests. The durable generation is
    only the restart fallback; it needs replacement when absent/incomplete, not
    for every price tick.
    """

    status = materialized_views.default_store().status()
    source = status.get("source_signature")
    expected_board = str(_board_path().resolve())
    return bool(
        status.get("ready")
        and isinstance(source, dict)
        and str(source.get("board_path") or expected_board) == expected_board
        and int(status.get("view_count") or 0) >= len(_materialized_view_queries())
        and int(status.get("route_count") or 0) > 0
    )


def _cleanup_abandoned_discovery_temps(
    *, max_age_seconds: float = 21_600.0, now: float | None = None
) -> dict[str, int]:
    """Delete only timed-out atomic discovery files older than six hours."""

    moment = time.time() if now is None else float(now)
    removed = 0
    bytes_removed = 0
    try:
        entries = list(RUNTIME_DIR.iterdir())
    except OSError:
        return {"removed": 0, "bytes": 0}
    for entry in entries:
        if (
            not entry.is_file()
            or entry.is_symlink()
            or not entry.name.startswith(".api_discovery_refresh.json.")
            or not entry.name.endswith(".tmp")
        ):
            continue
        try:
            stat = entry.stat()
            if moment - stat.st_mtime < max(60.0, max_age_seconds):
                continue
            entry.unlink()
        except OSError:
            continue
        removed += 1
        bytes_removed += stat.st_size
    return {"removed": removed, "bytes": bytes_removed}


def _artifact_generation_kind(path: Path) -> str:
    """Best-effort reason attached to an atomic shared generation marker."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return "market"
    return str(payload.get("kind") or "market").strip().casefold()


def _live_route_pointer_path() -> Path:
    store = materialized_views.default_store()
    return Path(
        getattr(
            store,
            "live_route_pointer_path",
            materialized_views.DEFAULT_ROOT / "live-route-index-current.json",
        )
    )


class SharedArtifactWatcher(threading.Thread):
    """Bridge collector file generations into the web process's memory caches."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        poll_seconds: float = 1.0,
        initial_warm_delay_seconds: float = 30.0,
        invalidation_interval_seconds: float | None = None,
        telegram_recovery_interval_seconds: float | None = None,
    ) -> None:
        super().__init__(name="shared-market-artifact-watcher", daemon=True)
        self.stop_event = stop_event
        self.poll_seconds = max(0.05, poll_seconds)
        self.initial_warm_at = time.monotonic() + max(
            0.0, initial_warm_delay_seconds
        )
        # A restart is not a reason to rebuild. If every structural signature
        # still matches, the last complete on-disk generation is already warm.
        self.initial_warm_requested = _materialized_sources_current()
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
        self.pending_generation_kinds: set[str] = set()
        self.telegram_recovery_interval_seconds = max(
            30.0,
            float(
                telegram_recovery_interval_seconds
                if telegram_recovery_interval_seconds is not None
                else os.environ.get(
                    "SPREADBOARD_TELEGRAM_RECOVERY_SECONDS", "60"
                )
            ),
        )
        self.next_telegram_recovery_at = (
            time.monotonic() + self.telegram_recovery_interval_seconds
        )
        self.generation_signature = _artifact_signature(MARKET_GENERATION_PATH)
        self.snapshot_signature = _artifact_signature(SNAPSHOT_PATH)
        self.materialized_signature = _artifact_signature(
            materialized_views.default_store().pointer_path
        )
        self.live_route_signature = _artifact_signature(
            _live_route_pointer_path()
        )
        self.funding_catalog_signature = _artifact_signature(
            funding_catalog.DEFAULT_CACHE_PATH
        )
        self.warm_lock = threading.Lock()
        self.warm_pending = False
        self.funding_warm_pending = False
        self.warm_thread: threading.Thread | None = None

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 - stale data is safer than a dead site.
                _log(f"shared artifact watcher: {type(exc).__name__}: {exc}")
            self.stop_event.wait(self.poll_seconds)

    def check_once(self) -> None:
        live_route_signature = _artifact_signature(
            _live_route_pointer_path()
        )
        if live_route_signature != self.live_route_signature:
            self.live_route_signature = live_route_signature
            from spreadboard import server as server_module

            route_count = server_module.restore_materialized_route_index(_board_path())
            if route_count:
                warm_query_projection.LIVE_UNIVERSE.refresh()
            _log(f"live route index installed routes={route_count}")
        materialized_signature = _artifact_signature(
            materialized_views.default_store().pointer_path
        )
        if materialized_signature != self.materialized_signature:
            self.materialized_signature = materialized_signature
            from spreadboard import server as server_module

            server_module._MATERIALIZED_VIEW_STORE.invalidate()
            route_count = server_module.restore_materialized_route_index(_board_path())
            server_module.restore_materialized_intel(_board_path())
            if route_count:
                server_module.mark_historical_dex_archive_ready()
                warm_query_projection.LIVE_UNIVERSE.refresh()
            _log(
                "materialized navigation generation installed "
                f"routes={route_count} live_query={warm_query_projection.LIVE_UNIVERSE.status()}"
            )
        funding_catalog_signature = _artifact_signature(
            funding_catalog.DEFAULT_CACHE_PATH
        )
        if (
            funding_catalog_signature != self.funding_catalog_signature
            and _service_role() != "web"
        ):
            self.funding_catalog_signature = funding_catalog_signature
            installed = funding_catalog.reload_persisted_cache()
            _log(
                "complete funding catalogue installed "
                f"tokens={installed.get('token_count')}"
            )
        generation = _artifact_signature(MARKET_GENERATION_PATH)
        if generation != self.generation_signature:
            self.generation_signature = generation
            self.invalidation_pending = True
            self.pending_generation_kinds.add(
                _artifact_generation_kind(MARKET_GENERATION_PATH)
            )
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
        self._recover_telegram_snapshot_if_due()

    def _recover_telegram_snapshot_if_due(self) -> None:
        """Retry a missing resident bot snapshot without waiting for discovery.

        The collector's broad structural generation can be hours away. A
        process-local Telegram snapshot is an availability cache, so a lost or
        failed startup generation gets its own bounded recovery cadence. The
        existing warm lock coalesces this with any structural/funding warm
        already in progress.
        """

        now = time.monotonic()
        if now < self.next_telegram_recovery_at:
            return
        self.next_telegram_recovery_at = (
            now + self.telegram_recovery_interval_seconds
        )
        from spreadboard import telegram_queries

        restored = telegram_queries.restore_persisted_payloads()
        if restored["spread"] or restored["funding"]:
            _log(
                "telegram persisted snapshots recovered "
                f"spread={restored['spread']} funding={restored['funding']}"
            )
        snapshot = telegram_queries.payload_status()
        if snapshot["ready"] and snapshot.get("funding_ready"):
            return
        # The navigation generation already contains the complete principal
        # spread/funding payloads. Rehydrate the bot from those atomic files in
        # milliseconds before considering a multi-minute regeneration. This is
        # also the correct restart path when a process-local snapshot was lost
        # but the structural source did not change.
        if _restore_telegram_from_materialized_generation():
            _log("telegram snapshots reconstructed from materialized generation")
            return
        with self.warm_lock:
            if self.warm_pending or (
                self.warm_thread is not None and self.warm_thread.is_alive()
            ):
                return
        if not snapshot["ready"]:
            _log("telegram snapshot missing; requesting automatic recovery warm")
            self.request_warm()
            return
        _log("telegram funding snapshot missing; requesting recovery warm")
        self.request_funding_warm()

    def _invalidate_if_due(self) -> None:
        """Coalesce price/funding generations while live overlays stay current."""

        now = time.monotonic()
        if not self.invalidation_pending or (
            self.last_invalidation_at
            and now - self.last_invalidation_at < self.invalidation_interval_seconds
        ):
            return
        kinds = set(self.pending_generation_kinds)
        _invalidate_market_price_caches()
        self.invalidation_pending = False
        self.pending_generation_kinds.clear()
        self.last_invalidation_at = now
        if "bulk_funding" in kinds:
            self.request_funding_warm()

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

    def request_funding_warm(self) -> None:
        """Refresh only funding views after a live-rate generation advances."""

        with self.warm_lock:
            self.funding_warm_pending = True
            if self.warm_thread is not None and self.warm_thread.is_alive():
                return
            self.warm_thread = threading.Thread(
                target=self._drain_warms,
                name="shared-market-funding-warm",
                daemon=True,
            )
            self.warm_thread.start()

    def _drain_warms(self) -> None:
        while not self.stop_event.is_set():
            with self.warm_lock:
                if not self.warm_pending and not self.funding_warm_pending:
                    return
                full_warm = self.warm_pending
                funding_warm = self.funding_warm_pending
                self.warm_pending = False
                self.funding_warm_pending = False
            built_full_generation = False
            if full_warm:
                _refresh_live_route_index()
                if _materialized_generation_ready():
                    _log(
                        "materialized fallback retained; resident route universe "
                        "covers current structural generation"
                    )
                else:
                    _refresh_materialized_views(force=True)
                    built_full_generation = True
            # Current books and rates are already overlaid by the ten-second
            # resident universe, while exact historical windows are read from
            # the settlement archive. A bulk-funding handoff therefore must
            # not launch the old whole-site materializer. Refresh only the
            # persisted structural funding catalogue, at its bounded cadence;
            # the first-ever full generation already includes it.
            if (full_warm or funding_warm) and not built_full_generation:
                _refresh_complete_funding_catalog(force=False)

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=5.0)
        if self.warm_thread is not None and self.warm_thread.is_alive():
            self.warm_thread.join(timeout=5.0)


def _restore_telegram_from_materialized_generation() -> bool:
    """Restore both Telegram universes without parsing public discovery."""

    from spreadboard import telegram_queries

    store = materialized_views.default_store()
    spread = store.payload_for(
        {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]}
    )
    funding_payloads = [
        store.payload_for(query)
        for query in WARM_QUERIES
        if query.get("funding_only") and not query.get("funding_window")
    ]
    if not spread or not funding_payloads or not all(funding_payloads):
        return False
    telegram_queries.replace_payload(spread)
    telegram_queries.replace_funding_payloads(
        [payload for payload in funding_payloads if payload]
    )
    status = telegram_queries.payload_status()
    return bool(status.get("ready") and status.get("funding_ready"))


class ChartHistoryWarmLoop(threading.Thread):
    """Fill member-visible chart proxies in the isolated collector process."""

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        board_path: Path,
        interval_seconds: float | None = None,
        batch: int | None = None,
    ) -> None:
        super().__init__(name="chart-history-warm", daemon=True)
        self.stop_event = stop_event
        self.board_path = board_path
        self.interval_seconds = max(
            5.0,
            float(
                interval_seconds
                if interval_seconds is not None
                else os.environ.get("SPREADBOARD_CHART_HISTORY_WARM_SECONDS", "10")
            ),
        )
        self.batch = max(
            1,
            int(
                batch
                if batch is not None
                else os.environ.get("SPREADBOARD_CHART_HISTORY_WARM_BATCH", "4")
            ),
        )
        self.next_at: dict[tuple[str, float], float] = {}
        self.last_run: dict[str, Any] = {
            "ready": False,
            "requested": 0,
            "priority": 0,
            "started": 0,
            "error": None,
        }

    def run(self) -> None:
        if self.stop_event.wait(5.0):
            return
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.last_run = self.check_once()
            except Exception as exc:  # noqa: BLE001 - charts retain live books.
                self.last_run = {
                    **self.last_run,
                    "ready": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                }
            self.stop_event.wait(
                max(0.0, self.interval_seconds - (time.monotonic() - started))
            )

    def check_once(self) -> dict[str, Any]:
        from spreadboard import server as server_module

        requested = chart_warm_demand.requests()
        priority = [
            (key, 24.0)
            for key in _priority_funding_chart_route_keys()
        ]
        candidates: list[tuple[str, float]] = []
        seen: set[tuple[str, float]] = set()
        for key, hours in (*requested, *priority):
            horizon = historical_spreads.cache_horizon_for(hours)
            identity = (key, horizon)
            if not key or identity in seen:
                continue
            seen.add(identity)
            candidates.append((key, hours))

        now = time.monotonic()
        attempted = 0
        started_count = 0
        for key, hours in candidates:
            identity = (key, historical_spreads.cache_horizon_for(hours))
            if now < self.next_at.get(identity, 0.0):
                continue
            row = server_module._find_canonical_route(key, self.board_path)
            if row is None:
                self.next_at[identity] = now + 300.0
                continue
            result = historical_spreads.load_or_fetch(
                row,
                hours=hours,
                max_points=1,
                blocking=False,
            )
            attempted += 1
            if result.get("status") == "warming" and not result.get("started"):
                self.next_at[identity] = now + self.interval_seconds
                break
            if result.get("status") == "warming":
                started_count += 1
                self.next_at[identity] = now + 120.0
            else:
                self.next_at[identity] = now + historical_spreads.CACHE_SECONDS
            if attempted >= self.batch:
                break
        return {
            "ready": True,
            "requested": len(requested),
            "priority": len(priority),
            "attempted": attempted,
            "started": started_count,
            "error": None,
        }


def _run_collector_service() -> int:
    """Own exchange I/O and artifact publication without accepting HTTP."""

    os.environ["SPREADBOARD_DISABLE_LOCAL_CACHE_WARM"] = "1"
    interval = float(os.environ.get("SPREADBOARD_REFRESH_SECONDS", "300"))
    if _env_bool("SPREADBOARD_LIGHTWEIGHT_MODE"):
        interval = max(600.0, interval)
    market_history.initialize()
    _seed_public_caches()
    _seed_funding_history_demand()
    refresh_loop = RefreshLoop(interval)
    bulk_quote_loop = BulkQuoteLoop(refresh_loop.stop_event)
    bulk_funding_loop = BulkFundingLoop(refresh_loop.stop_event)
    market_evidence_loop = MarketEvidenceLoop(
        refresh_loop.stop_event,
        refresh_loop=refresh_loop,
    )
    refresh_loop.startup_evidence_ready = market_evidence_loop.first_sweep_done
    chart_history_loop = ChartHistoryWarmLoop(
        refresh_loop.stop_event,
        board_path=_board_path(),
    )

    def stop_collector(_signum: int, _frame: Any) -> None:
        refresh_loop.stop_event.set()

    signal.signal(signal.SIGTERM, stop_collector)
    signal.signal(signal.SIGINT, stop_collector)
    refresh_loop.start()
    bulk_quote_loop.start()
    bulk_funding_loop.start()
    market_evidence_loop.start()
    chart_history_loop.start()
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
        chart_history_loop.join(timeout=5.0)
    return 0


def _seed_funding_history_demand() -> int:
    """Carry the last complete visible leaders through restart/deploy warming."""

    store = materialized_views.default_store()
    legs: list[tuple[str, str]] = []
    for query in _materialized_view_queries():
        if not query.get("funding_only"):
            continue
        payload = store.payload_for(query, board_path=_board_path())
        if payload:
            legs.extend(funding_history_demand.payload_legs(payload))
    if legs:
        funding_history_demand.enqueue(legs)
    return len(set(legs))


def main() -> int:
    from spreadboard import server as server_module
    from spreadboard import telegram_queries

    role = _service_role()
    cleanup = _cleanup_abandoned_discovery_temps()
    if cleanup["removed"]:
        _log(
            "abandoned discovery temps removed "
            f"files={cleanup['removed']} bytes={cleanup['bytes']}"
        )
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
    if role == "combined":
        funding_cache = funding_catalog.restore_persisted_cache()
        _log(f"complete funding catalogue restore {funding_cache}")
    else:
        _log("complete funding catalogue restore skipped in web role")
    restored_telegram = telegram_queries.restore_persisted_payloads()
    if restored_telegram["spread"] or restored_telegram["funding"]:
        _log(
            "telegram persisted snapshots restored "
            f"spread={restored_telegram['spread']} "
            f"funding={restored_telegram['funding']}"
        )
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

    # Install the last complete route/index and Intel generation before the
    # socket opens. This is disk decoding, not market reconstruction, and keeps
    # every chart/detail link available immediately after a restart. On the
    # very first deployment the background materializer creates generation 1;
    # no request is made responsible for doing so.
    restored_routes = server_module.restore_materialized_route_index(board_path)
    restored_intel = server_module.restore_materialized_intel(board_path)
    if restored_routes:
        server_module.mark_historical_dex_archive_ready()
        live_status = warm_query_projection.LIVE_UNIVERSE.refresh()
    else:
        live_status = warm_query_projection.LIVE_UNIVERSE.status()
    _log(
        "materialized startup restore "
        f"routes={restored_routes} intel={restored_intel} "
        f"status={server_module._MATERIALIZED_VIEW_STORE.status()} "
        f"live_query={live_status}"
    )
    live_route_worker = warm_query_projection.Worker(
        service_stop_event,
        interval_seconds=max(
            30.0,
            float(os.environ.get("SPREADBOARD_LIVE_QUERY_REFRESH_SECONDS", "30")),
        ),
    )
    tracked_route_worker = tracked_route_warmer.Worker(
        service_stop_event,
        accounts_path=server.accounts_path,
        route_resolver=lambda route_key: server_module._find_canonical_route(
            route_key, board_path
        ),
        quote_scheduler=server_module._schedule_chart_route_refresh,
        # Historical candles are fetched by the isolated collector via the
        # shared demand lane. The web process only schedules current books.
        proxy_route_keys_provider=None,
        warm_history_proxies=False,
        interval_seconds=float(
            os.environ.get("SPREADBOARD_TRACKED_ROUTE_WARM_SECONDS", "10")
        ),
    )
    server.tracked_route_worker = tracked_route_worker

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
    threading.Thread(
        target=_refresh_complete_funding_catalog,
        kwargs={"force": False},
        name="spreadboard-funding-catalog-startup",
        daemon=True,
    ).start()
    if refresh_loop is not None:
        refresh_loop.start()
    elif artifact_watcher is not None:
        artifact_watcher.start()
    live_route_worker.start()
    tracked_route_worker.start()
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
        live_route_worker.join(timeout=5.0)
        tracked_route_worker.join(timeout=5.0)
        server.server_close()
    return 0


#: Every view the navigation can reach, because each is a separate cache key
#: and each costs a full rebuild. Warming only the default left a member opening
#: Funding -> Futures-Spot waiting 59 seconds.
WARM_QUERIES: tuple[dict[str, list[str]], ...] = (
    # /charts builds its picker from 500 rows, which is its own cache key -- it
    # stayed at 27s while every other page came down.
    {"limit": ["500"], "sort": ["edge"], "direction": ["desc"]},
    {"kind": ["FUTURES"], "limit": ["500"]},
    {"kind": ["FUTURES-SPOT-PAIR"], "limit": ["500"]},
    {"kind": ["SPOT"], "limit": ["500"]},
    {"kind": ["DEX-FUTURES"], "limit": ["500"]},
    {"kind": ["DEX-SPOT"], "limit": ["500"]},
    # The funding page carries its `farm` parameter into the query, so warming
    # without it builds a different cache key and the tab stays cold -- which is
    # exactly what left /funding?farm=futures-spot at 27s while /funding was
    # 0.20s. Each tab is warmed as the page actually asks for it.
    {"funding_only": ["1"], "kind": ["FUTURES"], "sort": ["funding"], "direction": ["desc"], "limit": ["500"]},
    # No `farm` here. The funding page strips farm and rank before building its
    # query -- they are presentation, not data -- so warming WITH farm builds a
    # key the page never reads. Measured live: /funding?farm=futures-dex took
    # 16.7s against 0.11s for the tabs whose key actually matched.
    {"funding_only": ["1"], "kind": ["FUTURES-SPOT-PAIR"], "sort": ["funding"], "direction": ["desc"], "limit": ["500"]},
    {"funding_only": ["1"], "kind": ["DEX-FUTURES"], "sort": ["funding"], "direction": ["desc"], "limit": ["500"]},
    # Telegram needs the whole current funding universe, not only the 25 rows
    # currently leading each page tab. Otherwise a retained radar token could
    # still lose its current low rate after it cooled below rank 25. One
    # all-lane 500-row snapshot covers the live catalog without a per-message
    # rebuild or any relaxed filtering.
    {"funding_only": ["1"], "sort": ["funding"], "direction": ["desc"], "limit": ["500"]},
)

# Every historical Funding screen gets a full pre-ranked lane. Readers slice
# this exact materialization for page 1, page 2, and Export JSON, so pagination
# never creates another expensive cache key. Now remains independent and is
# represented by WARM_QUERIES above.
HISTORICAL_FUNDING_QUERIES: tuple[dict[str, list[str]], ...] = tuple(
    {
        "funding_only": ["1"],
        "kind": [kind],
        "funding_window": [window],
        "sort": ["funding"],
        "direction": ["desc"],
        "limit": ["500"],
        "offset": ["0"],
    }
    for kind in ("FUTURES", "FUTURES-SPOT-PAIR", "DEX-FUTURES")
    for window in ("1d", "7d", "30d")
)

# Historical DEX ranking must archive more than the 25 routes visible in Now.
# Keep this separate from WARM_QUERIES: Telegram intentionally consumes the
# ordinary funding lanes only, while the collector uses this broad DEX lane to
# retain and roll every currently eligible OKX DEX route.
FUNDING_ARCHIVE_QUERIES: tuple[dict[str, list[str]], ...] = (
    {
        "funding_only": ["1"],
        "kind": ["DEX-FUTURES"],
        "sort": ["funding"],
        "direction": ["desc"],
        "limit": ["500"],
        "offset": ["0"],
    },
)


def _materialized_view_queries() -> tuple[dict[str, list[str]], ...]:
    """All principal navigation queries, deduplicated by semantic identity."""

    result: list[dict[str, list[str]]] = []
    seen: set[str] = set()
    for query in (*FUNDING_ARCHIVE_QUERIES, *WARM_QUERIES, *HISTORICAL_FUNDING_QUERIES):
        identity = materialized_views.query_identity(query)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(dict(query))
    return tuple(result)


_PRIORITY_CHART_KEYS_LOCK = threading.Lock()
_PRIORITY_CHART_KEYS_AT = 0.0
_PRIORITY_CHART_KEYS: list[str] = []


def _priority_funding_chart_route_keys() -> list[str]:
    """Best routes on every Funding screen, from the persisted warm views.

    This never rebuilds a market view and never calls an exchange. The route
    warmer uses the keys one at a time, so the charts a subscriber can reach
    from the first page are prepared continuously instead of making the first
    click own an OHLCV download.
    """

    global _PRIORITY_CHART_KEYS_AT, _PRIORITY_CHART_KEYS

    now = time.monotonic()
    with _PRIORITY_CHART_KEYS_LOCK:
        if _PRIORITY_CHART_KEYS and now - _PRIORITY_CHART_KEYS_AT < 300.0:
            return list(_PRIORITY_CHART_KEYS)
        from spreadboard import server as server_module

        keys: list[str] = []
        store = materialized_views.default_store()
        for query in _materialized_view_queries():
            if not query.get("funding_only"):
                continue
            payload = store.payload_for(query)
            if not isinstance(payload, dict):
                continue
            for group in list(payload.get("groups") or [])[:25]:
                routes = list(group.get("routes") or [])
                best = group.get("best_funding_route") or group.get("best_route") or {}
                # Put the route a member sees in the collapsed row first, then
                # every exact pair revealed by that token.  Warming only the
                # best route left the remaining pair links with one point.
                for route in (best, *routes):
                    key = server_module._chart_link_route_key(route)
                    if key:
                        keys.append(key)
        maximum = max(
            256,
            int(os.environ.get("SPREADBOARD_PRIORITY_CHART_ROUTE_LIMIT", "2400")),
        )
        _PRIORITY_CHART_KEYS = list(dict.fromkeys(keys))[:maximum]
        _PRIORITY_CHART_KEYS_AT = now
        return list(_PRIORITY_CHART_KEYS)


def _complete_telegram_funding_payloads(
    board_path: Path,
    *,
    attempts: int | None = None,
    retry_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Return every completed funding lane or no installable generation.

    Funding views share a deliberately bounded build slot with the rest of the
    website.  A concurrent page or startup warm can therefore return the
    explicit ``status=warming`` sentinel for one lane while the other lanes
    finish.  Installing that mixed batch would either erase Telegram funding
    entirely or publish a partial universe.  Retain completed lanes and retry
    only the contended ones for a finite number of background attempts.
    """

    from spreadboard import server

    queries = [dict(query) for query in WARM_QUERIES if query.get("funding_only")]
    completed: list[dict[str, Any] | None] = [None] * len(queries)
    max_attempts = max(
        1,
        int(
            attempts
            if attempts is not None
            else os.environ.get("SPREADBOARD_TELEGRAM_FUNDING_WARM_ATTEMPTS", "4")
        ),
    )
    pause = max(
        0.0,
        float(
            retry_seconds
            if retry_seconds is not None
            else os.environ.get("SPREADBOARD_TELEGRAM_FUNDING_WARM_RETRY_SECONDS", "2")
        ),
    )
    for attempt in range(max_attempts):
        for index, query in enumerate(queries):
            if completed[index] is not None:
                continue
            try:
                payload = server.api_market_spreads(board_path, dict(query))
            except Exception as exc:  # noqa: BLE001 - bounded retry preserves the last complete snapshot.
                _log(
                    "telegram funding lane retry "
                    f"kind={query.get('kind', ['all'])[0]} "
                    f"attempt={attempt + 1}/{max_attempts} "
                    f"error={type(exc).__name__}"
                )
                continue
            if payload.get("status") == "warming" or not isinstance(
                payload.get("groups"), list
            ):
                continue
            completed[index] = payload
            _yield_to_requests()
        pending = [index for index, payload in enumerate(completed) if payload is None]
        if not pending:
            return [payload for payload in completed if payload is not None]
        if attempt + 1 < max_attempts and pause:
            time.sleep(pause)
    pending_kinds = [
        str((queries[index].get("kind") or ["all"])[0])
        for index, payload in enumerate(completed)
        if payload is None
    ]
    _log(
        "telegram funding generation incomplete; preserving last complete snapshot "
        f"pending={','.join(pending_kinds)} attempts={max_attempts}"
    )
    return []


def _warm_funding_cache() -> None:
    """Rebuild only funding views while members keep the last complete page.

    Live rate files advance far more often than the structural discovery
    snapshot. Rebuilding all eleven navigation views on every rate slice was
    too expensive, but never rebuilding the four funding views made their
    membership drift until the next broad scan. This bounded warm closes that
    gap and refreshes Telegram from the exact same completed payloads.
    """

    from spreadboard import funding_catalog, telegram_queries

    started = time.monotonic()
    funding_catalog.clear_cache()
    funding_catalog.refresh_cache()
    payloads = _complete_telegram_funding_payloads(_board_path())
    if payloads:
        telegram_queries.replace_funding_payloads(payloads)
    _log(
        f"funding cache warm views={len(payloads)} "
        f"in {time.monotonic() - started:.1f}s"
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
        300.0,
        float(os.environ.get("SPREADBOARD_MARKET_EVIDENCE_SECONDS", "600")),
    )
    INITIAL_DELAY_SECONDS = max(
        0.0,
        float(
            os.environ.get(
                "SPREADBOARD_MARKET_EVIDENCE_INITIAL_DELAY_SECONDS", "900"
            )
        ),
    )

    def __init__(
        self,
        stop_event: threading.Event,
        *,
        refresh_loop: RefreshLoop | None = None,
    ) -> None:
        super().__init__(name="market-evidence", daemon=True)
        self.stop_event = stop_event
        self.refresh_loop = refresh_loop
        self.first_sweep_done = threading.Event()

    def run(self) -> None:
        if self.stop_event.wait(self.INITIAL_DELAY_SECONDS):
            return
        first_sweep = True
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self._sweep_once()
            finally:
                if first_sweep:
                    self.first_sweep_done.set()
                    first_sweep = False
            # Start-to-start cadence: the bounded history work itself can use
            # four minutes. Sleeping a further ten minutes made the advertised
            # catch-up interval fourteen minutes and prolonged initial archive
            # coverage by many hours. Still yield briefly after an overrun so a
            # due structural publication already waiting on the same heavy lock
            # cannot be starved by immediate evidence re-acquisition.
            self.stop_event.wait(
                max(5.0, self.INTERVAL_SECONDS - (time.monotonic() - started))
            )

    def _sweep_once(self) -> None:
        # Evidence and token rankings each expand the all-token catalogue into
        # hundreds of megabytes. Running them together pushed the 4 GiB
        # collector to 95% before current quote/funding workers even started.
        # They are both deferred analytics, so serialize only these two while
        # live market collection continues independently.
        if not _BACKGROUND_ANALYTICS_LOCK.acquire(timeout=300.0):
            _log("market evidence deferred; token ranking still active")
            return
        try:
            if not _COLLECTOR_HEAVY_LOCK.acquire(timeout=300.0):
                _log("market evidence deferred; structural publication active")
                return
            try:
                self._run_isolated_sweep()
            finally:
                _COLLECTOR_HEAVY_LOCK.release()
        finally:
            _BACKGROUND_ANALYTICS_LOCK.release()

    def _run_isolated_sweep(self) -> None:
        # The WebSocket worker is an optional low-latency layer over the bulk
        # quote generation, but both it and the evidence catalogue retain more
        # than a gigabyte of Python objects. Their overlap exceeded the 4 GiB
        # collector cgroup and the kernel killed the WebSocket child. Pause the
        # fast lane while exact historical work runs; venue-wide bulk quotes
        # continue publishing current books, so pages retain correct prices.
        if self.refresh_loop is not None:
            self.refresh_loop.pause_websocket_worker()
        try:
            result = _run_worker(
                [
                    *_low_priority_prefix(),
                    sys.executable,
                    str(Path(__file__).with_name("market_evidence_worker.py")),
                ],
                timeout=self.TIMEOUT_SECONDS,
            )
        finally:
            if self.refresh_loop is not None:
                self.refresh_loop.resume_websocket_worker()
        if result.timed_out or result.returncode != 0:
            _log(
                "market evidence unavailable "
                f"exit={result.returncode} {result.stderr[-300:]}"
            )
            return
        summary = (result.stdout or result.stderr).strip().splitlines()
        _log(summary[-1] if summary else "market evidence completed")
        # Historical CEX readers rank the persisted complete catalogue against
        # the exact settlement file, while DEX readers rank the durable radar.
        # Neither needs the 19-view navigation generation rebuilt after every
        # five-minute evidence slice. Keeping that multi-minute build here
        # blocked the next provider catch-up cycle and prolonged blank windows.


def _invalidate_market_price_caches() -> None:
    """Expire source builds while retaining completed grouped pages for SWR.

    Those caches are keyed on the large discovery snapshot because that keeps
    page loads fast. Price workers deliberately do not rewrite that snapshot,
    so without this explicit signal the server retained the old route ranking
    for 15-20 minutes after newer prices were already on disk.
    """

    with api_spreads._SNAPSHOT_CACHE_LOCK:
        api_spreads._RESULT_CACHE.clear()
    # Do not clear server._MARKET_CACHE here. Its key contains every dynamic
    # file signature, and foreground requests use the last structurally
    # compatible generation with live books/funding overlaid while the
    # background warmer builds the new key. Clearing it created a false empty
    # Funding page for several seconds at every collector handoff.


def _board_path() -> Path:
    return Path(os.environ.get("SPREADBOARD_BOARD_PATH", str(board.DEFAULT_BOARD_PATH)))


MATERIALIZED_VIEW_MIN_INTERVAL_SECONDS = max(
    300.0,
    float(os.environ.get("SPREADBOARD_MATERIALIZED_VIEW_MIN_SECONDS", "900")),
)
MATERIALIZED_VIEW_FAILURE_RETRY_SECONDS = max(
    60.0,
    float(
        os.environ.get("SPREADBOARD_MATERIALIZED_VIEW_FAILURE_RETRY_SECONDS", "300")
    ),
)
_LAST_MATERIALIZED_VIEW_AT = 0.0
_MATERIALIZED_VIEW_RETRY_AFTER = 0.0
_MATERIALIZED_VIEW_BUILD_LOCK = threading.Lock()
_LIVE_ROUTE_INDEX_BUILD_LOCK = threading.Lock()
_FUNDING_CATALOG_BUILD_LOCK = threading.Lock()
FUNDING_CATALOG_REFRESH_SECONDS = max(
    900.0,
    float(os.environ.get("SPREADBOARD_FUNDING_CATALOG_REFRESH_SECONDS", "900")),
)
FUNDING_CATALOG_FAILURE_RETRY_SECONDS = max(
    60.0,
    float(
        os.environ.get(
            "SPREADBOARD_FUNDING_CATALOG_FAILURE_RETRY_SECONDS", "300"
        )
    ),
)
_FUNDING_CATALOG_RETRY_AFTER = 0.0


def _refresh_live_route_index(*, install: bool = True) -> bool:
    """Publish and install the latest complete query index in an isolated child."""

    if _service_role() == "web":
        return False

    if not _LIVE_ROUTE_INDEX_BUILD_LOCK.acquire(blocking=False):
        return False
    try:
        started = time.monotonic()
        result = _run_worker(
            [
                *_low_priority_prefix(),
                sys.executable,
                str(ROOT / "scripts/live_route_index_worker.py"),
                "--board-path",
                str(_board_path()),
                "--output-root",
                str(materialized_views.DEFAULT_ROOT),
            ],
            timeout=float(
                os.environ.get("SPREADBOARD_LIVE_ROUTE_INDEX_TIMEOUT_SECONDS", "180")
            ),
        )
        if result.timed_out or result.returncode != 0:
            _log(
                "live route index retained previous generation "
                f"timeout={result.timed_out} exit={result.returncode} "
                f"detail={(result.stdout or result.stderr)[-300:]}"
            )
            return False
        try:
            summary = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            _log("live route index produced no summary")
            return False
        routes = int(summary.get("routes") or 0)
        if install:
            from spreadboard import server as server_module

            routes = server_module.restore_materialized_route_index(_board_path())
            if routes:
                warm_query_projection.LIVE_UNIVERSE.refresh()
        _log(
            f"live route index {'ready' if install else 'published'} "
            f"routes={routes} child={summary.get('seconds')}s "
            f"elapsed={time.monotonic() - started:.1f}s"
        )
        return bool(routes)
    finally:
        _LIVE_ROUTE_INDEX_BUILD_LOCK.release()


def _refresh_complete_funding_catalog(*, force: bool = False) -> bool:
    """Publish the all-pair funding structure without rebuilding every page."""

    global _FUNDING_CATALOG_RETRY_AFTER

    if _service_role() == "web":
        return False

    now = time.monotonic()
    if now < _FUNDING_CATALOG_RETRY_AFTER:
        return False
    state = funding_catalog.status(restore=True)
    if not state.get("ready") and Path(str(state.get("path") or "")).exists():
        state = funding_catalog.reload_persisted_cache()
    age = state.get("age_seconds")
    if (
        not force
        and state.get("ready")
        and age is not None
        and float(age) < FUNDING_CATALOG_REFRESH_SECONDS
    ):
        return False
    if not _FUNDING_CATALOG_BUILD_LOCK.acquire(blocking=False):
        return False
    try:
        started = time.monotonic()
        result = _run_worker(
            [
                *_low_priority_prefix(),
                sys.executable,
                str(ROOT / "scripts/complete_funding_catalog_worker.py"),
            ],
            timeout=float(
                os.environ.get(
                    "SPREADBOARD_FUNDING_CATALOG_TIMEOUT_SECONDS", "1200"
                )
            ),
        )
        if result.timed_out or result.returncode != 0:
            _FUNDING_CATALOG_RETRY_AFTER = (
                time.monotonic() + FUNDING_CATALOG_FAILURE_RETRY_SECONDS
            )
            _log(
                "complete funding catalogue retained previous generation "
                f"timeout={result.timed_out} exit={result.returncode} "
                f"detail={(result.stdout or result.stderr)[-300:]}"
            )
            return False
        try:
            summary = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            _FUNDING_CATALOG_RETRY_AFTER = (
                time.monotonic() + FUNDING_CATALOG_FAILURE_RETRY_SECONDS
            )
            _log("complete funding catalogue worker produced no summary")
            return False
        installed = funding_catalog.reload_persisted_cache()
        if not installed.get("ready"):
            _FUNDING_CATALOG_RETRY_AFTER = (
                time.monotonic() + FUNDING_CATALOG_FAILURE_RETRY_SECONDS
            )
            _log(f"complete funding catalogue install failed {installed}")
            return False
        _FUNDING_CATALOG_RETRY_AFTER = 0.0
        _log(
            "complete funding catalogue ready "
            f"tokens={installed.get('token_count')} bytes={summary.get('bytes')} "
            f"child={summary.get('seconds')}s rss={summary.get('max_rss_mb')}MB "
            f"elapsed={time.monotonic() - started:.1f}s"
        )
        return True
    finally:
        _FUNDING_CATALOG_BUILD_LOCK.release()


def _refresh_materialized_views(*, force: bool) -> bool:
    """Build a whole navigation generation outside the HTTP interpreter.

    The child is nice/ionice constrained and writes every screen to a staging
    directory. Only a complete, source-coherent generation becomes current;
    until then the website and bot retain the previous one. Funding files can
    advance every few minutes, so those requests are coalesced behind a bounded
    cadence instead of keeping the host in a permanent warm cycle.
    """

    global _LAST_MATERIALIZED_VIEW_AT, _MATERIALIZED_VIEW_RETRY_AFTER

    now = time.monotonic()
    # A failed source-coherent build keeps the last complete generation. A
    # collector event queued during that attempt must not immediately start the
    # same multi-minute work again; wait for the transient handoff to settle.
    if now < _MATERIALIZED_VIEW_RETRY_AFTER:
        return False
    if (
        not force
        and _LAST_MATERIALIZED_VIEW_AT
        and now - _LAST_MATERIALIZED_VIEW_AT < MATERIALIZED_VIEW_MIN_INTERVAL_SECONDS
    ):
        return False
    if not _MATERIALIZED_VIEW_BUILD_LOCK.acquire(blocking=False):
        return False
    try:
        started = time.monotonic()
        _log("materialized navigation build starting")
        result = _run_worker(
            [
                *_low_priority_prefix(),
                sys.executable,
                str(ROOT / "scripts/materialized_view_worker.py"),
                "--board-path",
                str(_board_path()),
                "--output-root",
                str(materialized_views.DEFAULT_ROOT),
            ],
            timeout=float(
                os.environ.get("SPREADBOARD_MATERIALIZED_VIEW_TIMEOUT_SECONDS", "1800")
            ),
        )
        if result.timed_out or result.returncode != 0:
            _MATERIALIZED_VIEW_RETRY_AFTER = (
                time.monotonic() + MATERIALIZED_VIEW_FAILURE_RETRY_SECONDS
            )
            _log(
                "materialized navigation build retained previous generation "
                f"timeout={result.timed_out} exit={result.returncode} "
                f"detail={(result.stdout or result.stderr)[-400:]}"
            )
            return False
        try:
            summary = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            _MATERIALIZED_VIEW_RETRY_AFTER = (
                time.monotonic() + MATERIALIZED_VIEW_FAILURE_RETRY_SECONDS
            )
            _log("materialized navigation build produced no summary")
            return False
        from spreadboard import server as server_module

        server_module._MATERIALIZED_VIEW_STORE.invalidate()
        routes = server_module.restore_materialized_route_index(_board_path())
        server_module.restore_materialized_intel(_board_path())
        server_module.mark_historical_dex_archive_ready()
        funding_catalog.reload_persisted_cache()
        _LAST_MATERIALIZED_VIEW_AT = time.monotonic()
        _MATERIALIZED_VIEW_RETRY_AFTER = 0.0
        _log(
            "materialized navigation ready "
            f"generation={summary.get('generation')} views={summary.get('views')} "
            f"routes={routes} child={summary.get('seconds')}s "
            f"elapsed={time.monotonic() - started:.1f}s"
        )
        return True
    finally:
        _MATERIALIZED_VIEW_BUILD_LOCK.release()


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

    from spreadboard import funding_catalog, server

    started = time.monotonic()
    _log("board cache warm starting")
    funding_catalog.clear_cache()
    funding_catalog.refresh_cache()
    # A new discovery snapshot invalidates route links before it invalidates a
    # member's need to click them. Rebuild this small lookup first; the larger
    # navigation views can continue warming afterwards.
    _warm_route_index()
    _yield_to_requests()
    # Publish the one broad DEX generation first. Historical page 1, page 2,
    # and Export JSON all reuse it; until it is complete their readers return
    # an immediate honest warming payload instead of owning this build.
    warm_queries = (*FUNDING_ARCHIVE_QUERIES, *WARM_QUERIES)
    for query in warm_queries:
        try:
            payload = server.api_market_spreads(_board_path(), dict(query))
            if query in FUNDING_ARCHIVE_QUERIES and payload.get("status") != "warming":
                server.mark_historical_dex_archive_ready()
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
        funding_payloads = _complete_telegram_funding_payloads(_board_path())
        if funding_payloads:
            telegram_queries.replace_funding_payloads(funding_payloads)
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
    _log(f"board cache warmed {len(warm_queries)} views in {time.monotonic() - started:.1f}s")
    if _service_role() != "web":
        _refresh_token_rankings()
        _refresh_funding_windows()
    # The generation this pass replaced is now unreferenced; give it back.
    _return_freed_memory()


_TOKEN_RANKING_REFRESH_LOCK = threading.Lock()
_BACKGROUND_ANALYTICS_LOCK = threading.Lock()
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
    analytics_acquired = False
    try:
        if (
            not force
            and time.monotonic() - _LAST_TOKEN_RANKING_AT
            < TOKEN_RANKING_INTERVAL_SECONDS
        ):
            return
        analytics_acquired = _BACKGROUND_ANALYTICS_LOCK.acquire(blocking=False)
        if not analytics_acquired:
            _log("token rankings deferred; market evidence active")
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
        if analytics_acquired:
            _BACKGROUND_ANALYTICS_LOCK.release()
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
    from spreadboard import (
        catalog_pairs,
        funding_catalog,
        funding_radar,
        market_history,
        research_calibration,
        server,
    )

    try:
        _refresh_complete_funding_catalog(force=False)
        route_keys: list[str] = []
        leaders: list[dict[str, Any]] = []
        warm_routes: list[dict[str, Any]] = []
        priority_routes: list[dict[str, Any]] = []
        for query in (*WARM_QUERIES, *FUNDING_ARCHIVE_QUERIES):
            if not query.get("funding_only"):
                continue
            payload = server.api_market_spreads(_board_path(), dict(query))
            for group in payload.get("groups") or []:
                leader = group.get("best_funding_route")
                if isinstance(leader, dict):
                    leaders.append(leader)
                    priority_routes.append(leader)
                priority_routes.extend(
                    route
                    for route in (group.get("routes") or [])
                    if isinstance(route, dict)
                )
                for route in group.get("routes") or []:
                    if isinstance(route, dict):
                        warm_routes.append(route)
                    key = route.get("route_key")
                    if key:
                        route_keys.append(str(key))
        if not route_keys:
            return
        # Refresh exact settlement evidence before the heavier radar/calibration
        # work. Member-visible totals therefore publish as soon as their next
        # settlement is due instead of waiting behind a multi-minute archive.
        _refresh_venue_funding_history(priority_routes=priority_routes)
        started = time.monotonic()
        count = market_history.write_funding_windows(route_keys)
        _log(f"funding windows computed for {count} routes in {time.monotonic() - started:.1f}s")
        # Capture every warm generation, not merely the three-hour venue sweep.
        # A rate can lead for thirty minutes and cool before the next settlement
        # history refresh; that brief leader still belongs on the historical
        # radar, explicitly marked as no longer live.
        radar_routes_by_identity = {
            catalog_pairs.route_identity(route): route
            for route in [*funding_catalog.archive_routes(), *warm_routes, *leaders]
            if route.get("route_key")
        }
        radar_routes = list(radar_routes_by_identity.values())
        radar_count = funding_radar.refresh(radar_routes)
        _log(
            f"funding radar retained {radar_count} complete-catalogue routes "
            f"from {len(radar_routes)} current candidates"
        )
        calibration_capture = research_calibration.capture_routes(leaders)
        calibration_labels = research_calibration.label_matured()
        _log(
            "research calibration shadow "
            f"captured={calibration_capture['inserted']} "
            f"labeled={calibration_labels['labeled']}"
        )
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
VENUE_HISTORY_PRIORITY_SECONDS = max(
    60.0, float(os.environ.get("SPREADBOARD_VENUE_HISTORY_PRIORITY_SECONDS", "300"))
)
_LAST_VENUE_HISTORY_PRIORITY_AT = 0.0
_LAST_VENUE_HISTORY_CATALOG_AT = 0.0


def _refresh_venue_funding_history(
    *,
    priority_routes: list[dict[str, Any]] | None = None,
) -> None:
    """Pull each venue's settled funding for the legs the board is showing."""
    global _LAST_VENUE_HISTORY_PRIORITY_AT, _LAST_VENUE_HISTORY_CATALOG_AT

    from spreadboard import (
        accounts,
        chart_catalog,
        venue_funding_history,
    )

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
            and before["history_catch_up_complete"]
            and int(before.get("retryable_error_leg_count") or 0) == 0
            else VENUE_HISTORY_CATCH_UP_SECONDS
        )
        now = time.monotonic()
        priority_due = now - _LAST_VENUE_HISTORY_PRIORITY_AT >= VENUE_HISTORY_PRIORITY_SECONDS
        catalog_due = now - _LAST_VENUE_HISTORY_CATALOG_AT >= interval
        if not priority_due and not catalog_due:
            return

        demanded_legs = funding_history_demand.legs()
        priority_legs: list[tuple[str, str]] = [
            (str(route.get(f"{side}_venue")), str(route.get(f"{side}_market_symbol")))
            for route in priority_routes or []
            for side in ("long", "short")
            if route.get(f"{side}_market_type") == "Futures"
            and route.get(f"{side}_venue")
            and route.get(f"{side}_market_symbol")
        ]
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
        # Explicitly visible queries, open positions and member watchlists are
        # the demand-driven fast lane. Retained radar routes already remain in
        # the rotating full catalogue; adding that entire archive here made a
        # static first-120 slice starve later symbols forever.
        priority_legs = list(dict.fromkeys(priority_legs))
        if not catalog_legs and not priority_legs:
            return
        if priority_due:
            _LAST_VENUE_HISTORY_PRIORITY_AT = now
        if catalog_due:
            _LAST_VENUE_HISTORY_CATALOG_AT = now
        started = time.monotonic()
        windows: dict[str, dict[str, float | None]] = {}
        modes: list[str] = []
        # Keep the total historical-work budget unchanged while reserving half
        # for subscriber-visible leaders and half for the unattempted catalog.
        # Putting all priority legs at the head of every catch-up generation
        # could otherwise consume the entire 240 seconds and leave the same
        # thousands of background legs pending forever.
        if priority_due and (priority_legs or demanded_legs):
            if demanded_legs:
                windows = venue_funding_history.build(
                    list(dict.fromkeys(catalog_legs)),
                    priority_legs=demanded_legs,
                    priority_only=True,
                    budget_seconds=30.0,
                )
                modes.append("demand")
            demanded_set = set(demanded_legs)
            ordinary_priorities = [
                leg for leg in priority_legs if leg not in demanded_set
            ]
            if ordinary_priorities:
                windows = venue_funding_history.build(
                    list(dict.fromkeys(catalog_legs)),
                    priority_legs=ordinary_priorities,
                    priority_only=True,
                    budget_seconds=90.0 if demanded_legs else 120.0,
                )
                modes.append("priority")
        if catalog_due:
            windows = venue_funding_history.build(
                list(dict.fromkeys(catalog_legs)),
                priority_legs=[],
                priority_only=False,
                budget_seconds=120.0,
            )
            modes.append(
                "maintenance"
                if before["catch_up_complete"]
                and before["history_catch_up_complete"]
                and int(before.get("retryable_error_leg_count") or 0) == 0
                else "catch_up"
            )
        after = venue_funding_history.coverage_summary(catalog_legs)
        _log(
            f"venue funding history: {len(windows)} legs with windows; "
            f"checked={after['attempted_leg_count']}/{after['catalog_leg_count']} "
            f"classified={after['classified_leg_count']} "
            f"pending={after['pending_leg_count']} verified={after['coverage_pct']}% "
            f"windows=24h:{after['window_leg_counts']['1d']},"
            f"7d:{after['window_leg_counts']['7d']},"
            f"30d:{after['window_leg_counts']['30d']} "
            f"deep_pending={after['deep_history_pending_leg_count']} "
            f"retryable={after['retryable_error_leg_count']} "
            f"mode={'+'.join(modes) or 'idle'} "
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
