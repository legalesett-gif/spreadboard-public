"""What the websocket worker subscribes to must be what the board displays.

The worker used to rank the raw snapshot by spread and stream the top legs.
That set is close to the opposite of the board's: the widest raw numbers are
the dislocated rows the board filters out, so the worker held hundreds of
subscriptions while only a third of the routes on screen had a live price.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.websocket_book_worker import _board_leg_key, _desired_legs


def _row(token: str, *, spread: float, long_venue: str = "Binance") -> dict[str, Any]:
    return {
        "route_key": f"{token}|{long_venue}|Futures|Bybit|Futures",
        "token": token,
        "route_kind": "FUTURES",
        "long_venue": long_venue,
        "long_market_type": "Futures",
        "long_market_symbol": f"{token}/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": f"{token}/USDT:USDT",
        "depth_weighted_spread_pct": spread,
        "executable_spread_pct": spread,
    }
