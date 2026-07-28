#!/usr/bin/env python3
"""Run SpreadBoard and its public-API refresh loop as one persistent service."""

from __future__ import annotations

# ruff: noqa: E402

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
    board,
    live,
    market_history,
    public_rails,
    token_metadata,
)  # noqa: E402
from spreadboard.server import SpreadBoardHandler, SpreadBoardServer  # noqa: E402

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
SNAPSHOT_PATH = RUNTIME_DIR / "api_discovery_latest.json"


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

    def start(self) -> None:
        self.thread.start()
        self.fast_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5.0)
        self.fast_thread.join(timeout=5.0)

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            with self.quote_cycle_lock:
                self.refresh_once()
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(15.0, self.interval_seconds - elapsed))

    def refresh_once(self) -> None:
        lightweight_mode = _env_bool("SPREADBOARD_LIGHTWEIGHT_MODE")
        command = [
            sys.executable,
            str(ROOT / "scripts/api_discovery_worker.py"),
            "--db-path",
            str(ROOT / "data/spreadarb.db"),
            "--snapshot-path",
            str(SNAPSHOT_PATH),
            "--archive-dir",
            str(RUNTIME_DIR / "api_discovery_archive"),
            "--parts-dir",
            str(RUNTIME_DIR / "api_discovery_parts"),
            "--skip-broad-dex-spot",
            "--cex-max-orderbook-candidates",
            os.environ.get("SPREADBOARD_CEX_CANDIDATES", "150"),
            "--dex-derivative-max-orderbook-candidates",
            os.environ.get("SPREADBOARD_DEX_CANDIDATES", "100"),
            "--row-limit",
            "500",
            "--ttl-s",
            "900",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=float(os.environ.get("SPREADBOARD_REFRESH_TIMEOUT_SECONDS", "540")),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            _log(f"refresh timeout: {exc}")
            return
        if result.returncode != 0:
            _log(f"refresh failed ({result.returncode}): {(result.stderr or '')[-500:]}")
            return
        with self.snapshot_lock:
            try:
                snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _log(f"snapshot unavailable after refresh: {exc}")
                return
            funding_summary = live.enrich_snapshot_funding_24h(
                snapshot,
                max_workers=int(os.environ.get("SPREADBOARD_FUNDING_HISTORY_WORKERS", "12")),
            )
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

    def run_fast_quotes(self) -> None:
        interval = max(
            20.0,
            float(os.environ.get("SPREADBOARD_FAST_QUOTE_SECONDS", "30")),
        )
        while not self.stop_event.wait(5.0):
            started = time.monotonic()
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
            self.stop_event.wait(max(1.0, interval - (time.monotonic() - started)))

    def _refresh_fast_quotes(self) -> dict[str, Any]:
        command = [
            sys.executable,
            str(ROOT / "scripts/fast_quote_worker.py"),
            "--snapshot-path",
            str(SNAPSHOT_PATH),
            "--route-limit",
            "12",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=max(
                    60.0,
                    float(os.environ.get("SPREADBOARD_FAST_QUOTE_TIMEOUT_SECONDS", "60")),
                ),
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
    if _env_bool("SPREADBOARD_PUBLIC_MODE") or _env_bool("SPREADBOARD_LIGHTWEIGHT_MODE"):
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

    def stop_service(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    refresh_loop.start()
    _log(f"serving http://{host}:{port}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        refresh_loop.stop()
        server.server_close()
    return 0


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


def _atomic_write_snapshot(snapshot: dict[str, Any]) -> None:
    temporary = SNAPSHOT_PATH.with_suffix(".funding.tmp")
    temporary.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
    temporary.replace(SNAPSHOT_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
