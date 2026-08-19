"""Where a contract trades away from the venue's own fair price.

A different signal from everything else on this board. Every other lane compares
two venues; this compares one contract against the price its own exchange says
it should be -- the fair (mark) price the venue computes from the index and uses
for liquidations.

The reference product alerts on exactly this. Their notification reads:

    Fair Price Alert ARWRSTOCK 7.34% Long
    Last Price: 84.31    Fair Price: 90.99
    Volume: $145.2k      Limit: $46370.5     Leverage: 20x

Last is 7.34% below fair, so the side to take is Long. It is a mean-reversion
signal on a single venue, and it is most useful exactly where this board is
weakest: thin, newly listed contracts -- tokenised equities among them -- whose
last trade drifts from the index between fills.

It costs nothing to collect. `fetch_tickers` already returns `fairPrice` and
`indexPrice` in the venue payload for the venues that publish them, and the
bulk quote sweep already calls it once per venue to price the board.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(os.environ.get("SPREADBOARD_DATA_DIR", "data"))
CACHE_PATH = RUNTIME_DIR / "live_fair_price.json"

#: What the venues call it. Different exchanges spell the same number
#: differently, and some publish only an index.
FAIR_KEYS = ("fairPrice", "markPrice", "mark_price", "fair_price", "indexPrice", "index_price")

#: Below this a deviation is noise -- fees, tick size, a stale print.
MIN_DEVIATION_PCT = float(os.environ.get("SPREADBOARD_FAIR_PRICE_MIN_PCT", "1.0"))

#: A quote nobody can take is not an opportunity.
MIN_VOLUME_USD = float(os.environ.get("SPREADBOARD_FAIR_PRICE_MIN_VOLUME_USD", "25000"))


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _turnover_usd(ticker: dict[str, Any], last: float | None) -> float | None:
    """24h turnover in quote currency, however the venue chose to report it.

    Kraken Futures fills ``baseVolume`` and leaves ``quoteVolume`` empty, so
    reading only the latter made turnover unknown for every one of its
    contracts -- and an unknown used to skip the floor entirely.
    """
    quote_volume = _number(ticker.get("quoteVolume"))
    if quote_volume is not None:
        return quote_volume
    base_volume = _number(ticker.get("baseVolume"))
    if base_volume is not None and last is not None:
        return base_volume * last
    return None


def fair_price_of(ticker: dict[str, Any]) -> tuple[float | None, str | None]:
    """The venue's own fair price for this contract, and what it called it."""
    info = ticker.get("info")
    for source in (info if isinstance(info, dict) else {}, ticker):
        for key in FAIR_KEYS:
            value = _number(source.get(key))
            if value is not None:
                return value, key
    return None, None


def deviation(
    venue: str,
    symbol: str,
    ticker: dict[str, Any],
) -> dict[str, Any] | None:
    """One contract's distance from its own fair price, or None if it is close.

    The side is the trade the deviation implies: last below fair is Long,
    because the contract is cheap against what the venue marks it at.
    """
    last = _number(ticker.get("last")) or _number(ticker.get("close"))
    fair, basis = fair_price_of(ticker)
    if last is None or fair is None:
        return None
    deviation_pct = (fair - last) / fair * 100.0
    if abs(deviation_pct) < MIN_DEVIATION_PCT:
        return None
    volume = _turnover_usd(ticker, last)
    # A volume we cannot measure has not cleared the floor. Admitting it put the
    # thinnest contracts on the board at rank 1 -- DEGEN led at +13.18% on $71.
    if volume is None or volume < MIN_VOLUME_USD:
        return None
    return {
        "venue": venue,
        "symbol": symbol,
        "last_price": last,
        "fair_price": fair,
        "fair_basis": basis,
        "deviation_pct": round(deviation_pct, 4),
        # Last under fair means the contract is cheap: buy it.
        "side": "Long" if deviation_pct > 0 else "Short",
        "volume_24h_usd": volume,
    }


def write(rows: list[dict[str, Any]], *, cache_path: Path | str = CACHE_PATH) -> int:
    """Publish the deviations, widest first."""
    ordered = sorted(
        rows, key=lambda row: abs(float(row.get("deviation_pct") or 0.0)), reverse=True
    )
    payload = {
        "schema": "spreadboard.fair_price.v1",
        "updated_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
        "min_deviation_pct": MIN_DEVIATION_PCT,
        "min_volume_usd": MIN_VOLUME_USD,
        "rows": ordered,
    }
    path = Path(cache_path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
    return len(ordered)


_CACHE: dict[str, Any] = {"stamp": None, "payload": {}}


def load(*, cache_path: Path | str = CACHE_PATH) -> dict[str, Any]:
    """The published deviations, re-read only when the file changes."""
    path = Path(cache_path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {"rows": []}
    if _CACHE["stamp"] != stamp:
        try:
            _CACHE["payload"] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"rows": []}
        _CACHE["stamp"] = stamp
    return _CACHE["payload"] or {"rows": []}
