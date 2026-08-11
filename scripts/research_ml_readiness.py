#!/usr/bin/env python3
"""Print the fail-closed readiness report for future research ML."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spreadboard import ml_readiness  # noqa: E402


def main() -> int:
    print(json.dumps(ml_readiness.assess(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
