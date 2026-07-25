#!/usr/bin/env python3
"""Run SpreadBoard and its public-API refresh loop as one persistent service."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spreadboard import alerts, board, market_history, public_rails, token_metadata  # noqa: E402
from spreadboard.server import SpreadBoardHandler, SpreadBoardServer  # noqa: E402

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", str(ROOT / "data")))
SNAPSHOT_PATH = RUNTIME_DIR / "api_discovery_latest.json"


class RefreshLoop:
    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(30.0, interval_seconds)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="spreadboard-public-refresh", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5.0)

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.refresh_once()
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(15.0, self.interval_seconds - elapsed))

    def refresh_once(self) -> None:
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
        try:
            snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log(f"snapshot unavailable after refresh: {exc}")
            return
        inserted = market_history.record_snapshot(snapshot)
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
        refresh = snapshot.get("source_refresh") or {}
        _log(
            "refresh complete "
            f"routes={len(snapshot.get('api_discovered_rows') or []) + len(snapshot.get('dex_discovered_rows') or [])} "
            f"history_inserted={inserted} status={refresh.get('status')}"
        )


def main() -> int:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8200"))
    interval = float(os.environ.get("SPREADBOARD_REFRESH_SECONDS", "300"))
    board_path = Path(os.environ.get("SPREADBOARD_BOARD_PATH", str(board.DEFAULT_BOARD_PATH)))
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


if __name__ == "__main__":
    raise SystemExit(main())
