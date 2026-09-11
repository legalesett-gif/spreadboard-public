#!/usr/bin/env python3
"""Single isolated owner of public exact-settlement refreshes."""
from __future__ import annotations

import fcntl
from pathlib import Path

from scripts import run_spreadboard_service as service
from spreadboard import venue_funding_history


def main() -> int:
    # Protect the JSON aggregate publication if an operator accidentally starts
    # another instance. SQLite already serializes event upserts independently.
    path = Path(venue_funding_history.DEFAULT_CACHE_PATH).with_suffix('.refresh.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return 0 if service._refresh_venue_funding_history() else 1


if __name__ == '__main__':
    raise SystemExit(main())
