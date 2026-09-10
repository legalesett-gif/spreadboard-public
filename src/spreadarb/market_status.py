"""Native status vetoes missing from CCXT's generic active flag."""

from collections.abc import Mapping
from typing import Any


def market_open_for_opportunities(venue: str, market: Mapping[str, Any]) -> bool:
    """Reject explicitly closed markets; this is not private entry permission.

    Only current enumeration uses this check. A later definition refresh can
    reintroduce a reopened contract; historical settlements are not deleted.
    """
    info = market.get("info")
    if venue == "Bingx" and market.get("swap") and isinstance(info, Mapping) and "status" in info:
        # BingX documents 1 as active. CCXT checks only apiStateOpen/Close,
        # which remain true on status=25 contracts absent from premiumIndex.
        # The converse also occurs: CAP trades with status=1 while API order
        # opening/closing is disabled. That is not a public-market closure.
        return str(info["status"]) == "1"
    if market.get("active") is False:
        return False
    if not market.get("swap") or not isinstance(info, Mapping):
        return True
    if venue == "XT":
        # isOpenApi alone stays true after trading is switched off. A hidden
        # display flag, by itself, is not evidence that trading is disabled.
        return all(
            str(info[key]).casefold() == "true"
            for key in ("tradeSwitch", "openSwitch")
            if key in info
        )
    return True


def public_market_definition(venue: str, market: Mapping[str, Any]) -> dict[str, Any]:
    """Copy the public status without altering the execution client's metadata.

    Generic symbol selectors read ``active`` again after venue filtering. Keep
    them aligned with native public status; API order permissions remain in
    the original client and in the copied native info, and still need preflight.
    """
    return {**market, "active": market_open_for_opportunities(venue, market)}


def native_market_asset_class(venue: str, market: Mapping[str, Any]) -> str | None:
    """Positive native derivative evidence only; ticker spelling proves nothing."""
    if not market.get("swap"):
        return None
    info = market.get("info")
    if not isinstance(info, Mapping):
        return None
    if venue == "Bitget" and str(info.get("isRwa") or "").upper() == "YES":
        return "tokenized"
    if venue == "Binance" and str(info.get("underlyingType") or "").upper() == "EQUITY":
        return "tokenized"
    if venue == "Bybit" and str(info.get("symbolType") or "").casefold() == "stock":
        return "tokenized"
    return None
