#!/usr/bin/env python3
"""Fingerprint the Python source a deploy is supposed to ship.

`deploy_production.sh` builds on the server from `/opt/spreadboard/app`, which
nothing kept in step with the working tree. On 2026-09-03 it reported "deploy
OK" three times while the container kept running a `market_history.py` from
five days earlier: health was 200, the restart counts were flat, and none of
that says anything about which code came back up.

Comparing this digest on both sides does say it. The path prefix is excluded so
a working tree and `/app` inside the image can be compared directly.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

TREES = ("spreadboard", "src", "scripts")
#: Data the Dockerfile bakes into the image. Omitting them let a registry entry
#: pass a green deploy without ever reaching the container -- the same failure
#: this script exists to catch, one layer down. Named individually because the
#: container's data/ also holds files written at runtime.
FILES = (
    "data/api_discovery_watchlist.json",
    "data/api_discovery_identity_registry.json",
    "data/api_discovery_executor_attestations.json",
    "data/token_metadata_seed.json",
)


def digest(
    root: Path,
    trees: tuple[str, ...] = TREES,
    files: tuple[str, ...] = FILES,
) -> str:
    hasher = hashlib.sha256()
    for tree in trees:
        base = root / tree
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            _absorb(hasher, root, path)
    for name in files:
        path = root / name
        if path.exists():
            _absorb(hasher, root, path)
        else:
            # A file present on one side and absent on the other must not hash
            # the same as both sides having it.
            hasher.update(f"{name}\0<absent>\0".encode("utf-8"))
    return hasher.hexdigest()[:16]


def _absorb(hasher: "hashlib._Hash", root: Path, path: Path) -> None:
    hasher.update(str(path.relative_to(root)).encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(path.read_bytes())
    hasher.update(b"\0")


if __name__ == "__main__":
    print(digest(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
