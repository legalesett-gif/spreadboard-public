#!/usr/bin/env python3
"""Fail when production Python sources differ from the local checkpoint.

The release marker is metadata, not proof that every hot-copied or rebuilt
container received every module.  This read-only verifier hashes the complete
``spreadboard`` Python package locally, on the persisted production host and in
the web and collector containers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SSH_HOST = "root@178.128.126.204"
DEFAULT_SSH_KEY = Path.home() / ".ssh" / "spreadboard_digitalocean"
DEFAULT_REMOTE_ROOT = "/opt/spreadboard/app"
DEFAULT_CONTAINERS = ("app-app-1", "app-collector-1")
_HASH_LINE = re.compile(r"^([0-9a-f]{64})\s+(.+)$")
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_SSH_DESTINATION = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+$")


def valid_container_name(value: str) -> bool:
    """Container names enter a remote shell command, so accept no metacharacters."""

    return bool(_CONTAINER_NAME.fullmatch(value))


def valid_ssh_host(value: str) -> bool:
    """Accept an ordinary OpenSSH destination, never an option-like value."""

    return bool(_SSH_DESTINATION.fullmatch(value)) and not value.startswith("-")


def local_manifest(root: Path = ROOT) -> dict[str, str]:
    """Hash every tracked-shape Python module in the product package."""

    package = root / "spreadboard"
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob("*.py"))
        if path.is_file()
    }


def parse_sha256_manifest(output: str) -> dict[str, str]:
    """Parse portable ``sha256sum`` output and reject unrelated paths."""

    manifest: dict[str, str] = {}
    for raw_line in output.splitlines():
        match = _HASH_LINE.fullmatch(raw_line.strip())
        if match is None:
            continue
        digest, raw_path = match.groups()
        path = raw_path.removeprefix("*").removeprefix("./")
        if not path.startswith("spreadboard/") or not path.endswith(".py"):
            continue
        if path in manifest:
            raise ValueError(f"duplicate_manifest_path:{path}")
        manifest[path] = digest
    return dict(sorted(manifest.items()))


def compare_manifests(expected: dict[str, str], actual: dict[str, str]) -> dict[str, list[str]]:
    """Return deterministic drift buckets for one production target."""

    expected_paths = set(expected)
    actual_paths = set(actual)
    return {
        "changed": sorted(
            path for path in expected_paths & actual_paths if expected[path] != actual[path]
        ),
        "missing": sorted(expected_paths - actual_paths),
        "unexpected": sorted(actual_paths - expected_paths),
    }


def _remote_manifest(
    *,
    ssh_host: str,
    ssh_key: Path,
    remote_root: str,
    container: str | None,
    timeout_seconds: float,
) -> dict[str, str]:
    if not valid_ssh_host(ssh_host):
        raise ValueError(f"invalid_ssh_host:{ssh_host}")
    manifest_command = (
        "find spreadboard -type f -name '*.py' -print0 "
        "| sort -z | xargs -0 sha256sum | sort -k2"
    )
    if container is None:
        remote_command = f"cd {shlex.quote(remote_root)} && {manifest_command}"
    else:
        if not valid_container_name(container):
            raise ValueError(f"invalid_container_name:{container}")
        container_command = f"cd /app && {manifest_command}"
        remote_command = f"docker exec {container} sh -c {shlex.quote(container_command)}"
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-i",
            str(ssh_key),
            ssh_host,
            remote_command,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    return parse_sha256_manifest(completed.stdout)


def verify(
    *,
    root: Path,
    ssh_host: str,
    ssh_key: Path,
    remote_root: str,
    containers: tuple[str, ...],
    timeout_seconds: float,
) -> dict[str, Any]:
    expected = local_manifest(root)
    targets: dict[str, Any] = {}
    manifests = {
        "persisted_host": _remote_manifest(
            ssh_host=ssh_host,
            ssh_key=ssh_key,
            remote_root=remote_root,
            container=None,
            timeout_seconds=timeout_seconds,
        ),
        **{
            container: _remote_manifest(
                ssh_host=ssh_host,
                ssh_key=ssh_key,
                remote_root=remote_root,
                container=container,
                timeout_seconds=timeout_seconds,
            )
            for container in containers
        },
    }
    for name, manifest in manifests.items():
        drift = compare_manifests(expected, manifest)
        targets[name] = {
            "ok": not any(drift.values()),
            "files": len(manifest),
            **drift,
        }
    return {
        "ok": bool(expected) and all(target["ok"] for target in targets.values()),
        "local_files": len(expected),
        "targets": targets,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--containers", nargs="+", default=list(DEFAULT_CONTAINERS))
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    try:
        report = verify(
            root=args.root.resolve(),
            ssh_host=args.ssh_host,
            ssh_key=args.ssh_key.expanduser(),
            remote_root=args.remote_root,
            containers=tuple(args.containers),
            timeout_seconds=max(1.0, args.timeout_seconds),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
