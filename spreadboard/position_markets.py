"""Canonical market identity for user-owned positions.

The arbitrage board is deliberately selective: a valid market can disappear
from the ranked rows when its current spread or funding cools.  A saved
position cannot use that ranking decision as its market resolver.  This module
joins the exact venues, market types and symbols recorded at entry to the full
active chart catalogue, while keeping DEX contract identity intact.
"""

from __future__ import annotations

from typing import Any

from spreadboard import chart_catalog, route_taxonomy


def resolve_position_route(
    position: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    catalogue: dict[str, Any] | None = None,
    market_index: dict[tuple[str, str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve an exact saved position without substituting another market."""

    catalogue = catalogue if isinstance(catalogue, dict) else chart_catalog.load()
    market_index = market_index or catalogue_market_index(catalogue)
    token = str(position.get("token") or "").upper()
    long_leg = _catalogue_leg(
        position, "long", token=token, market_index=market_index
    )
    short_leg = _catalogue_leg(
        position, "short", token=token, market_index=market_index
    )
    listed_sides = [side for side, leg in (("long", long_leg), ("short", short_leg)) if leg]
    listing_status = (
        "listed"
        if len(listed_sides) == 2
        else "partial"
        if listed_sides
        else "unlisted"
    )

    canonical_route = None
    chart_route_key = None
    history_route_key = normalized_route_key(position)
    if long_leg is not None and short_leg is not None:
        try:
            chart_route_key = chart_catalog.custom_route_key(token, long_leg, short_leg)
            canonical_route = chart_catalog.route_from_key(chart_route_key)
        except (KeyError, TypeError, ValueError):
            canonical_route = None
            chart_route_key = None
        if canonical_route is not None:
            history_route_key = route_history_key(canonical_route)

    matched = _matching_row(position, rows, long_leg=long_leg, short_leg=short_leg)
    # A current canonical row is itself proof that both exact markets exist.
    # This matters during a temporary catalogue refresh failure; it does not
    # permit substitutions because _matching_row compares both saved symbols.
    if matched is not None and listing_status != "listed":
        listing_status = "listed"
        listed_sides = ["long", "short"]

    return {
        "listing_status": listing_status,
        "listed_sides": listed_sides,
        "long_leg": long_leg,
        "short_leg": short_leg,
        "canonical_route": canonical_route,
        "chart_route_key": chart_route_key,
        "history_route_key": history_route_key,
        "current_row": matched,
    }


def normalized_route_key(position: dict[str, Any]) -> str:
    return "|".join(
        [
            str(position.get("token") or "").upper() or "?",
            str(position.get("long_venue") or "?"),
            normalize_market_type(
                position.get("long_venue"), position.get("long_market_type")
            ),
            str(position.get("short_venue") or "?"),
            normalize_market_type(
                position.get("short_venue"), position.get("short_market_type")
            ),
        ]
    )


def route_history_key(route: dict[str, Any]) -> str:
    """Stable key shared by custom chart selections and canonical board rows."""

    return "|".join(
        [
            str(route.get("token") or "").upper() or "?",
            str(route.get("long_venue") or "?"),
            normalize_market_type(
                route.get("long_venue"), route.get("long_market_type")
            ),
            str(route.get("short_venue") or "?"),
            normalize_market_type(
                route.get("short_venue"), route.get("short_market_type")
            ),
        ]
    )


def normalize_market_type(venue: Any, market_type: Any) -> str:
    """Map the user-facing DEX label to the catalogue's exact Spot adapter."""

    venue_text = str(venue or "")
    value = str(market_type or "").strip().casefold()
    if value == "dex" or route_taxonomy.venue_is_onchain_spot(venue_text):
        return "Spot"
    if value in {"future", "futures", "perp", "perpetual", "swap"}:
        return "Futures"
    if value == "spot":
        return "Spot"
    return str(market_type or "")


def catalogue_market_index(
    catalogue: dict[str, Any],
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Index the 20k+ market catalogue once for a whole portfolio request."""

    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in catalogue.get("markets") or []:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("token") or "").upper(),
            str(item.get("venue") or "").casefold(),
            normalize_market_type(item.get("venue"), item.get("market_type")),
            str(item.get("symbol") or "").casefold(),
        )
        if all(key):
            result.setdefault(key, item)
    return result


def _catalogue_leg(
    position: dict[str, Any],
    side: str,
    *,
    token: str,
    market_index: dict[tuple[str, str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    requested_venue = str(position.get(f"{side}_venue") or "").strip()
    requested_type = normalize_market_type(
        requested_venue, position.get(f"{side}_market_type")
    )
    requested_symbol = str(position.get(f"{side}_symbol") or "").strip()
    if not token or not requested_venue or not requested_type or not requested_symbol:
        return None
    item = market_index.get(
        (
            token,
            requested_venue.casefold(),
            requested_type,
            requested_symbol.casefold(),
        )
    )
    return dict(item) if item is not None else None


def _matching_row(
    position: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    long_leg: dict[str, Any] | None,
    short_leg: dict[str, Any] | None,
) -> dict[str, Any] | None:
    token = str(position.get("token") or "").upper()
    expected = {
        "long": long_leg or _position_leg(position, "long"),
        "short": short_leg or _position_leg(position, "short"),
    }
    route_keys = {
        str(position.get("route_key") or ""),
        normalized_route_key(position),
    }
    for row in rows:
        if str(row.get("route_key") or "") not in route_keys:
            continue
        # Some compact/test rows carry the authoritative route key and prices
        # but omit repeated leg labels.  Full rows still pass exact-symbol
        # validation below, so a conflicting symbol can never hide behind the
        # coarser route key.
        if not any(row.get(f"{side}_venue") for side in ("long", "short")):
            return row
        if all(_same_leg(row, side, expected[side], token=token) for side in ("long", "short")):
            return row
    for row in rows:
        if str(row.get("token") or "").upper() != token:
            continue
        if all(_same_leg(row, side, expected[side], token=token) for side in ("long", "short")):
            return row
    return None


def _position_leg(position: dict[str, Any], side: str) -> dict[str, Any]:
    venue = str(position.get(f"{side}_venue") or "")
    return {
        "venue": venue,
        "market_type": normalize_market_type(
            venue, position.get(f"{side}_market_type")
        ),
        "symbol": str(position.get(f"{side}_symbol") or ""),
    }


def _same_leg(
    row: dict[str, Any],
    side: str,
    expected: dict[str, Any],
    *,
    token: str,
) -> bool:
    venue = str(row.get(f"{side}_venue") or "")
    if venue.casefold() != str(expected.get("venue") or "").casefold():
        return False
    if normalize_market_type(venue, row.get(f"{side}_market_type")) != str(
        expected.get("market_type") or ""
    ):
        return False
    expected_symbol = str(expected.get("symbol") or "")
    symbol = _row_symbol(row, side)
    # Legacy manual records created before the catalogue picker did not persist
    # symbols.  Their route key still fixes token, venue and market type; keep
    # those records markable instead of making an empty historical field a
    # permanent outage.  New records always take the exact-symbol branch.
    if not expected_symbol:
        return True
    if symbol:
        return symbol.casefold() == expected_symbol.casefold()
    # Canonical DEX discovery rows historically omitted *_market_symbol.  The
    # contract-verified catalogue symbol is the token itself in that adapter.
    return route_taxonomy.venue_is_onchain_spot(venue) and expected_symbol.upper() == token


def _row_symbol(row: dict[str, Any], side: str) -> str:
    notes = row.get("notes") if isinstance(row.get("notes"), dict) else {}
    route_inputs = (
        notes.get("route_inputs")
        if isinstance(notes.get("route_inputs"), dict)
        else {}
    )
    leg = route_inputs.get(side) if isinstance(route_inputs.get(side), dict) else {}
    return str(
        row.get(f"{side}_market_symbol")
        or row.get(f"{side}_symbol")
        or leg.get("symbol")
        or ""
    )
