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
    return all(funding_venue_enabled(get(f"{side}_venue")) for side in ("long", "short"))


def opportunity_venue_enabled(venue: Any) -> bool:
    return str(venue or "").strip().casefold() not in EXCLUDED_OPPORTUNITY_VENUES


def opportunity_route_enabled(row: Any) -> bool:
    if isinstance(row, dict):
        return opportunity_venue_enabled(row.get("long_venue")) and opportunity_venue_enabled(row.get("short_venue"))
    return opportunity_venue_enabled(getattr(row, "long_venue", None)) and opportunity_venue_enabled(getattr(row, "short_venue", None))


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
