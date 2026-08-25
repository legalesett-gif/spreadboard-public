#!/usr/bin/env python3
"""Run grouped read-only API discovery and merge source snapshots."""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from spreadarb.api_discovery.models import clean_error, utc_now_iso  # noqa: E402
from spreadarb.api_discovery.worker import (  # noqa: E402
    default_groups,
    run_grouped_discovery,
)

DEFAULT_RUNTIME_DB = ROOT / "runtime/market-history.db"
DEFAULT_LOCK_PATH = Path("/tmp/spreadarb-api-discovery-worker.lock")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_RUNTIME_DB)
    parser.add_argument("--watchlist-path", type=Path, default=ROOT / "data/api_discovery_watchlist.json")
    parser.add_argument(
        "--identity-registry-path",
        type=Path,
        default=ROOT / "data/api_discovery_identity_registry.json",
    )
    parser.add_argument(
        "--attestation-path",
        type=Path,
        default=ROOT / "data/api_discovery_executor_attestations.json",
    )
    parser.add_argument("--snapshot-path", type=Path, default=ROOT / "data/api_discovery_latest.json")
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "data/api_discovery_archive")
    parser.add_argument("--parts-dir", type=Path, default=ROOT / "data/api_discovery_parts")
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--ttl-s", type=int, default=900)
    parser.add_argument("--token-limit", type=int, default=20)
    parser.add_argument("--row-limit", type=int, default=100)
    parser.add_argument("--cex-max-orderbook-candidates", type=int, default=100)
    parser.add_argument("--dex-derivative-max-orderbook-candidates", type=int, default=100)
    parser.add_argument("--dex-spot-timeout-s", type=float, default=240.0)
    parser.add_argument("--skip-dex-spot", action="store_true")
    parser.add_argument(
        "--skip-broad-dex-spot",
        action="store_true",
        help="Skip the slower broad Jupiter/0x DEX spot research scan.",
    )
    parser.add_argument("--broad-dex-sources", default="jupiter,0x")
    parser.add_argument("--broad-dex-jupiter-limit", type=int, default=30)
    parser.add_argument("--broad-dex-zerox-limit", type=int, default=40)
    # Jupiter's free keyed plan is one request per second. A candidate uses a
    # buy and a sell quote, so pacing only between candidates still bursts the
    # second request into a 429. Keep a little clock-skew headroom and let the
    # quote client apply this delay between each provider request.
    parser.add_argument("--broad-dex-rate-limit-s", type=float, default=1.05)
    parser.add_argument("--broad-dex-retry-429", type=int, default=1)
    parser.add_argument("--broad-dex-quote-timeout-s", type=float, default=4.0)
    parser.add_argument("--broad-dex-timeout-s", type=float, default=210.0)
    parser.add_argument(
        "--include-blacklisted",
        action="store_true",
        help="Keep demo-blacklisted tokens in the merged discovery snapshot for debugging.",
    )
    parser.add_argument(
        "--broad-dex-output-path",
        type=Path,
        default=ROOT / "data/api_discovery_broad_dex_latest.json",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with single_instance(args.lock_path) as acquired:
        if not acquired:
            payload = {"status": "skipped_locked", "snapshot_path": str(args.snapshot_path)}
            _print_result(payload, json_output=args.json)
            return 0
        groups = default_groups(
            cex_max_orderbook_candidates=args.cex_max_orderbook_candidates,
            dex_derivative_max_orderbook_candidates=args.dex_derivative_max_orderbook_candidates,
            dex_spot_timeout_seconds=args.dex_spot_timeout_s,
            row_limit=args.row_limit,
        )
        if args.skip_dex_spot:
            groups = [
                replace(group, sources=set(group.sources) - {"dex-spot"})
                for group in groups
                if set(group.sources) - {"dex-spot"}
            ]
        broad_dex_payload = None
        if not args.skip_broad_dex_spot:
            broad_dex_payload = run_broad_dex_scan(args)
        snapshot = run_grouped_discovery(
            db_path=args.db_path,
            watchlist_path=args.watchlist_path,
            snapshot_path=args.snapshot_path,
            archive_dir=args.archive_dir,
            parts_dir=args.parts_dir,
            identity_registry_path=args.identity_registry_path,
            attestation_path=args.attestation_path,
            ttl_seconds=args.ttl_s,
            token_limit=args.token_limit,
            row_limit=args.row_limit,
            groups=groups,
            broad_dex_payload=broad_dex_payload,
            blacklist_filter_enabled=not args.include_blacklisted,
        )
    _print_result(snapshot, json_output=args.json)
    return 0


def run_broad_dex_scan(args: argparse.Namespace) -> dict[str, Any]:
    output_path = Path(args.broad_dex_output_path)
    selected_sources = _broad_dex_sources(args.broad_dex_sources)
    if len(selected_sources) > 1:
        return _run_broad_dex_sources_parallel(args, selected_sources, output_path)
    return _run_broad_dex_source(args, selected_sources[0], output_path)


def _run_broad_dex_source(args: argparse.Namespace, source: str, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.unlink()
    except FileNotFoundError:
        pass
    command = _broad_dex_command(args, source=source, output_path=output_path)
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(args.broad_dex_timeout_s)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _broad_dex_partial_or_error_payload(
            status="timeout",
            error=f"TimeoutExpired: {exc}",
            output_path=output_path,
            selected_sources=[source],
        )
    if result.returncode != 0:
        return _broad_dex_partial_or_error_payload(
            status="failed",
            error=result.stderr.replace("\n", " ")[:500] or f"returncode={result.returncode}",
            output_path=output_path,
            selected_sources=[source],
        )
    try:
        return json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _broad_dex_error_payload(
            "invalid",
            clean_error(exc),
            output_path,
            sources=[source],
        )


def _run_broad_dex_sources_parallel(
    args: argparse.Namespace,
    sources: list[str],
    output_path: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_runs: list[tuple[str, Path, subprocess.Popen[str]]] = []
    for source in sources:
        source_path = _source_output_path(output_path, source)
        try:
            source_path.unlink()
        except FileNotFoundError:
            pass
        proc = subprocess.Popen(
            _broad_dex_command(args, source=source, output_path=source_path),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        source_runs.append((source, source_path, proc))

    payloads: list[dict[str, Any]] = []
    per_source_timeout = max(1.0, float(args.broad_dex_timeout_s))
    for source, source_path, proc in source_runs:
        try:
            remaining = max(0.1, per_source_timeout - (time.monotonic() - started))
            _stdout, stderr = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()
            payloads.append(
                _broad_dex_partial_or_error_payload(
                    status="timeout",
                    error=f"TimeoutExpired: {exc}",
                    output_path=source_path,
                    selected_sources=[source],
                )
            )
            continue
        if proc.returncode != 0:
            payloads.append(
                _broad_dex_partial_or_error_payload(
                    status="failed",
                    error=(stderr or "").replace("\n", " ")[:500] or f"returncode={proc.returncode}",
                    output_path=source_path,
                    selected_sources=[source],
                )
            )
            continue
        try:
            payloads.append(json.loads(source_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            payloads.append(
                _broad_dex_error_payload("invalid", clean_error(exc), source_path, sources=[source])
            )

    return _merge_broad_dex_payloads(
        payloads,
        output_path=output_path,
        started=started,
        target_usd=args.target_usd if hasattr(args, "target_usd") else None,
        slippage_bps=args.slippage_bps if hasattr(args, "slippage_bps") else None,
    )


def _broad_dex_command(args: argparse.Namespace, *, source: str, output_path: Path) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "scripts/dex_spot_broad_scan.py"),
        "--sources",
        source,
        "--jupiter-limit",
        str(args.broad_dex_jupiter_limit),
        "--zerox-limit",
        str(args.broad_dex_zerox_limit),
        "--rate-limit-s",
        str(args.broad_dex_rate_limit_s),
        "--retry-429",
        str(args.broad_dex_retry_429),
        "--quote-timeout-s",
        str(args.broad_dex_quote_timeout_s),
        "--output-path",
        str(output_path),
        "--json",
    ]


def _source_output_path(output_path: Path, source: str) -> Path:
    safe_source = source.replace("/", "_").replace(" ", "_")
    return output_path.with_name(f"{output_path.stem}.{safe_source}{output_path.suffix}")


def _merge_broad_dex_payloads(
    payloads: list[dict[str, Any]],
    *,
    output_path: Path,
    started: float,
    target_usd: float | None = None,
    slippage_bps: int | None = None,
) -> dict[str, Any]:
    scans: list[dict[str, Any]] = []
    cex: dict[str, Any] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if not cex and isinstance(payload.get("cex"), dict):
            cex = dict(payload["cex"])
        for scan in payload.get("scans") or []:
            if isinstance(scan, dict):
                scans.append(scan)
    merged = {
        "schema": "spreadarb.dex_spot_broad_scan.v1",
        "updated_at": utc_now_iso(),
        "output_path": str(output_path),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "target_usd": target_usd,
        "slippage_bps": slippage_bps,
        "cex": cex,
        "scans": scans,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
    return merged


def _broad_dex_sources(raw: str) -> list[str]:
    sources: list[str] = []
    for item in str(raw or "").split(","):
        source = item.strip().lower()
        if source == "zerox":
            source = "0x"
        if source and source not in sources:
            sources.append(source)
    return sources or ["broad_dex"]


def _broad_dex_partial_or_error_payload(
    *,
    status: str,
    error: str,
    output_path: Path,
    selected_sources: list[str],
) -> dict[str, Any]:
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _broad_dex_error_payload(status, error, output_path, sources=selected_sources)
    if not isinstance(payload, dict):
        return _broad_dex_error_payload(status, error, output_path, sources=selected_sources)
    _append_missing_broad_source_errors(payload, selected_sources, status, error)
    return payload


def _append_missing_broad_source_errors(
    payload: dict[str, Any],
    selected_sources: list[str],
    status: str,
    error: str,
) -> None:
    scans = payload.setdefault("scans", [])
    if not isinstance(scans, list):
        payload["scans"] = scans = []
    present = {
        str(scan.get("source") or "").strip().lower()
        for scan in scans
        if isinstance(scan, dict)
    }
    for source in selected_sources:
        if source not in present:
            scans.append(_broad_dex_error_scan(source, status, error))


def _broad_dex_error_payload(
    status: str,
    error: str,
    output_path: Path,
    *,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "spreadarb.dex_spot_broad_scan.v1",
        "updated_at": "",
        "output_path": str(output_path),
        "elapsed_seconds": 0,
        "scans": [_broad_dex_error_scan(source, status, error) for source in (sources or ["broad_dex"])],
    }


def _broad_dex_error_scan(source: str, status: str, error: str) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "rows": [],
        "row_count": 0,
        "positive_row_count": 0,
        "research_row_count_ge_1pct_lte_90pct": 0,
        "quote_attempted_tokens": 0,
        "quote_success_tokens": 0,
        "quote_error_tokens": 0,
        "errors": [error],
    }


@contextmanager
def single_instance(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _print_result(snapshot: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return
    refresh = snapshot.get("source_refresh") or {}
    worker = snapshot.get("worker") or {}
    print(
        "api_discovery_worker "
        f"status={refresh.get('status') or snapshot.get('status')} "
        f"api={refresh.get('api_discovered_count')} "
        f"dex={refresh.get('dex_discovered_count')} "
        f"executor_ready={refresh.get('executor_ready_count')} "
        f"elapsed={worker.get('elapsed_seconds')}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
