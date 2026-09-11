"""Operator-selected venue coverage for public research opportunities.

Adapters and historical account records are independent of this policy.
Excluding a venue must not change the identity or economics of another pair.
"""

from __future__ import annotations

from typing import Any

EXCLUDED_OPPORTUNITY_VENUES = frozenset({"ourbit", "htx", "huobi"})


EXCLUDED_FUNDING_VENUES = frozenset({"coinex", "phemex"})


def funding_venue_enabled(venue: Any) -> bool:
    return opportunity_venue_enabled(venue) and str(venue or "").strip().casefold() not in EXCLUDED_FUNDING_VENUES


def funding_route_enabled(row: Any) -> bool:
    get = row.get if isinstance(row, dict) else lambda key: getattr(row, key, None)
    return opportunity_route_enabled(row) and all(funding_venue_enabled(get(f"{side}_venue")) for side in ("long", "short"))


def opportunity_venue_enabled(venue: Any) -> bool:
    return str(venue or "").strip().casefold() not in EXCLUDED_OPPORTUNITY_VENUES


def opportunity_route_enabled(row: Any) -> bool:
    get = row.get if isinstance(row, dict) else lambda key: getattr(row, key, None)
    return all(opportunity_market_enabled(get(f"{side}_venue"), get(f"{side}_market_type"),
                                         get(f"{side}_market_symbol")) for side in ("long", "short"))


def opportunity_market_enabled(venue: Any, market_type: Any, symbol: Any) -> bool:
    if not opportunity_venue_enabled(venue):
        return False
    if str(venue or "").casefold() == "bitmart" and str(market_type or "").casefold() == "futures":
        # Old cached definitions hardcoded USDT for every settlement currency,
        # including inverse BTCUSD. Never reinterpret such a cached route.
        pair, _, settle = str(symbol or "").upper().partition(":")
        base, _, quote = pair.partition("/")
        return bool(base) and quote in {"USDT", "USDC"} and settle == quote
    return True


def funding_leg_enabled(venue: Any, symbol: Any) -> bool:
    return funding_venue_enabled(venue) and opportunity_market_enabled(venue, "Futures", symbol)


def opportunity_payload_enabled(payload: dict[str, Any], *, funding_only: bool = False) -> bool:
    """Reject an old ranked page so remaining routes can be ranked before slicing.

    Removing a disabled leader after pagination would leave a partial page and
    keep eligible alternatives hidden. The caller rebuilds from the complete
    current catalogue when an older persisted page fails this boundary.
    """
    enabled = funding_route_enabled if funding_only else opportunity_route_enabled
    for row in payload.get("rows") or []:
        if isinstance(row, dict) and not enabled(row):
            return False
    for name in ("groups", "top_edges", "top_funding"):
        for group in payload.get(name) or []:
            if not isinstance(group, dict):
                continue
            for row in [group, group.get("best_route"), group.get("best_funding_route"), *(group.get("routes") or [])]:
                if isinstance(row, dict) and not enabled(row):
                    return False
    return True


def exchange_filter_matches(long_venue: Any, short_venue: Any, expression: Any) -> bool:
    """Comma-separated inclusions and !exact-venue exclusions, applied to both legs."""
    venues = {str(value or "").strip().casefold() for value in (long_venue, short_venue)}
    terms = [part.strip().casefold() for part in str(expression or "").split(",") if part.strip()]
    excluded = {part[1:] for part in terms if part.startswith("!")}
    included = [part for part in terms if not part.startswith("!")]
    if venues & excluded:
        return False
    return not included or any(term in venue for term in included for venue in venues)
