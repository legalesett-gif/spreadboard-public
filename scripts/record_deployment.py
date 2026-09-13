#!/usr/bin/env python3
"""Record only source/health-verified services; preserve partial-release truth.

Called by deploy_production.sh after every requested service passes. The JSON
receipt is authoritative. The legacy .deployed_revision means WEB APP revision,
not a claim that an untouched collector/accounting service runs the same commit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def atomic_text(path: Path, value: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def record(root: Path, revision: str, digest: str, dirty: bool, services: list[str]) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"[0-9a-f]{16}", digest):
        raise ValueError("invalid_release_identity")
    if not services or any(s not in {"app", "collector", "accounting-worker"} for s in services):
        raise ValueError("unsupported_release_service")
    receipt = root / ".deployment_receipt.json"
    previous = json.loads(receipt.read_text()) if receipt.exists() else {"schema": 1, "services": {}}
    if previous.get("schema") != 1 or not isinstance(previous.get("services"), dict):
        raise ValueError("invalid_previous_deployment_receipt")
    evidence = {"revision": revision, "source_digest": digest, "dirty_source": dirty,
                "verified_at": datetime.now(UTC).isoformat()}
    current = {**previous, "last_verified_services": sorted(set(services)),
               "services": {**previous["services"], **{s: evidence for s in services}}}
    atomic_text(receipt, json.dumps(current, sort_keys=True, indent=2) + "\n")
    if "app" in services:
        atomic_text(root / ".deployed_revision", revision + ("-dirty" if dirty else "") + "\n")
    return current


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--dirty", choices=("0", "1"), required=True)
    parser.add_argument("services", nargs="+")
    args = parser.parse_args()
    record(args.root, args.revision, args.digest, args.dirty == "1", args.services)
