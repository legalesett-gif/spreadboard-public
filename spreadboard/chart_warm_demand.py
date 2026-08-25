"""Small in-process queue of chart routes members have actually revealed."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Iterable


_LOCK = threading.Lock()
_ROUTES: OrderedDict[str, float] = OrderedDict()
_MAX_ROUTES = 2_000
_TTL_SECONDS = 21_600.0


def enqueue(route_keys: Iterable[str]) -> int:
    """Prioritise self-contained route keys without making a provider call."""

    now = time.monotonic()
    added = 0
    with _LOCK:
        _expire(now)
        for value in route_keys:
            key = str(value or "")
            if not key:
                continue
            if key not in _ROUTES:
                added += 1
            _ROUTES[key] = now
            _ROUTES.move_to_end(key)
        while len(_ROUTES) > _MAX_ROUTES:
            _ROUTES.popitem(last=False)
    return added


def route_keys() -> list[str]:
    now = time.monotonic()
    with _LOCK:
        _expire(now)
        # Newest member intent first; persisted Funding routes follow it.
        return list(reversed(_ROUTES))


def _expire(now: float) -> None:
    expired = [key for key, seen in _ROUTES.items() if now - seen > _TTL_SECONDS]
    for key in expired:
        _ROUTES.pop(key, None)
