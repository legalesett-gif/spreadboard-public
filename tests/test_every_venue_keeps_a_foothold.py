"""A venue a token trades on must survive the per-token cap.

The cap keeps a token's strongest routes and reserves diversity by ROUTE
FAMILY and by VENUE PAIR. A token trading on fifteen venues has over a hundred
possible pairs, so ranking by pair alone can spend every slot without ever
reaching a venue. On production CASHCAT had 18 buildable Hyperliquid routes and
the index carried none; GRIFFAIN had 28 and carried none. To a member that
reads as "this venue has no spread on this token", when the truth is that we
ranked it 29th and dropped it.

Raising the cap is the other way to fix this and it took the site down on
2026-08-29 (see the note at SPREADARB_MAX_ROWS_PER_TOKEN). This costs no extra
rows: it changes which of the same rows survive.
"""

from __future__ import annotations

from spreadarb.api_discovery import sources


def _row(long_venue: str, short_venue: str, spread: float, kind: str = "FUTURES"):
    return {
        "token": "T",
        "route_kind": kind,
        "long_venue": long_venue,
        "long_market_type": "Futures",
        "short_venue": short_venue,
        "short_market_type": "Futures",
        "depth_weighted_spread_pct": spread,
        "notes": {
            "route_inputs": {
                "long": {"bid": 1.0, "ask": 1.001},
                "short": {"bid": 1.0, "ask": 1.001},
            },
            "funding": {"net_apr_pct": 0.0},
        },
    }


def test_a_rare_venue_survives_a_cap_full_of_stronger_pairs() -> None:
    """The production shape: many strong pairs among a few popular venues."""

    strong = [
        _row(a, b, 10.0 - i * 0.01)
        for i, (a, b) in enumerate(
            (x, y)
            for x in ("Binance", "Bybit", "Gate", "Mexc", "OKX")
            for y in ("Bitget", "HTX", "Kucoin", "XT", "Bingx")
        )
    ]
    # One weak route, and the only way to reach Hyperliquid at all.
    rare = _row("Hyperliquid", "Gate", 0.01)

    kept = sources._keep_diverse_token_routes([*strong, rare], 12)

    assert len(kept) == 12
    venues = {r["long_venue"] for r in kept} | {r["short_venue"] for r in kept}
    assert "Hyperliquid" in venues, (
        "a venue reachable on this token was ranked out of existence"
    )


def test_the_cap_is_still_respected_exactly() -> None:
    rows = [_row(f"V{i}", f"W{i}", float(i)) for i in range(40)]
    assert len(sources._keep_diverse_token_routes(rows, 9)) == 9


def test_a_short_list_is_returned_untouched() -> None:
    rows = [_row("A", "B", 1.0), _row("C", "D", 2.0)]
    assert sources._keep_diverse_token_routes(rows, 28) == rows


def test_strength_still_decides_the_remaining_slots() -> None:
    """Once every venue has a foothold, the strongest routes fill the rest."""

    rows = [
        _row("A", "B", 9.0),
        _row("A", "B", 8.0),
        _row("A", "B", 7.0),
        _row("C", "D", 1.0),
    ]
    kept = sources._keep_diverse_token_routes(rows, 3)

    venues = {r["long_venue"] for r in kept} | {r["short_venue"] for r in kept}
    assert {"A", "B", "C", "D"} <= venues, "every venue keeps a foothold"
    assert any(r["depth_weighted_spread_pct"] == 9.0 for r in kept), (
        "the strongest route must still be kept"
    )
