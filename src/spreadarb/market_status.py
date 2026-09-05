"""Native status vetoes missing from CCXT's generic active flag."""

from collections.abc import Mapping
from typing import Any


def market_open_for_opportunities(venue: str, market: Mapping[str, Any]) -> bool:
    """Reject explicitly closed markets; this is not private entry permission.

    Only current enumeration uses this check. A later definition refresh can
    reintroduce a reopened contract; historical settlements are not deleted.
    """
    if market.get("active") is False:
        return False
    if not market.get("swap"):
        return True
    info = market.get("info")
    if not isinstance(info, Mapping):
        return True
    if venue == "Bingx" and "status" in info:
        # BingX documents 1 as active. CCXT checks only apiStateOpen/Close,
        # which remain true on status=25 contracts absent from premiumIndex.
        return str(info["status"]) == "1"
    if venue == "XT":
        # isOpenApi alone stays true after trading is switched off. A hidden
        # display flag, by itself, is not evidence that trading is disabled.
        return all(
            str(info[key]).casefold() == "true"
            for key in ("tradeSwitch", "openSwitch")
            if key in info
        )
    return True
