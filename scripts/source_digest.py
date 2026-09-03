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


def digest(root: Path, trees: tuple[str, ...] = TREES) -> str:
    hasher = hashlib.sha256()
    for tree in trees:
        base = root / tree
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            hasher.update(str(path.relative_to(root)).encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(path.read_bytes())
            hasher.update(b"\0")
    return hasher.hexdigest()[:16]


if __name__ == "__main__":
    print(digest(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
