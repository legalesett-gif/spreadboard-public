"""Operator-selected venue coverage for public research opportunities.

Adapters and historical account records are independent of this policy.
Excluding a venue must not change the identity or economics of another pair.
"""

from __future__ import annotations

from typing import Any

EXCLUDED_OPPORTUNITY_VENUES = frozenset({"ourbit"})


def opportunity_venue_enabled(venue: Any) -> bool:
    return str(venue or "").strip().casefold() not in EXCLUDED_OPPORTUNITY_VENUES


def opportunity_route_enabled(row: Any) -> bool:
    if isinstance(row, dict):
        return opportunity_venue_enabled(row.get("long_venue")) and opportunity_venue_enabled(row.get("short_venue"))
    return opportunity_venue_enabled(getattr(row, "long_venue", None)) and opportunity_venue_enabled(getattr(row, "short_venue", None))


def opportunity_payload_enabled(payload: dict[str, Any]) -> bool:
    """Reject an old ranked page so remaining routes can be ranked before slicing.

    Removing a disabled leader after pagination would leave a partial page and
    keep eligible alternatives hidden. The caller rebuilds from the complete
    current catalogue when an older persisted page fails this boundary.
    """
    for row in payload.get("rows") or []:
        if isinstance(row, dict) and not opportunity_route_enabled(row):
            return False
    for name in ("groups", "top_edges", "top_funding"):
        for group in payload.get(name) or []:
            if not isinstance(group, dict):
                continue
            for row in [group.get("best_route"), group.get("best_funding_route"), *(group.get("routes") or [])]:
                if isinstance(row, dict) and not opportunity_route_enabled(row):
                    return False
    return True
