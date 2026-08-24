"""One canonical matched-size probe shared by every spread data path."""

from __future__ import annotations

import os

TARGET_NOTIONAL_USD = float(
    os.environ.get("SPREADBOARD_LIVE_BOOK_NOTIONAL_USD", "500")
)
