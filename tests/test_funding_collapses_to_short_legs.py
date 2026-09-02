"""Funding is paid by the short leg, so a route list repeats it endlessly.

Measured on production 2026-09-02: the funding catalogue held 144,409 routes to
convey 4,169 distinct (token, short leg) facts -- 34.6x duplication. TRX alone
held 1,512 routes across 43 short legs, each leg repeated 42 times against 42
different longs, every copy carrying the identical funding number.

That duplication is what squeezed the catalogue to a 500-token budget, which is
why LOBSTER -- whose Aster leg pays 116.8% APR, a 93.9th-percentile rate -- and
SKHYNIX were absent from Funding entirely while 308 tokens spent 9,741 board
rows between them.

Collapsing is lossless for funding: the discarded rows carry the same rate. What
a member loses is a choice of long, so the survivor is the one with the best
spread, which is also the one worth entering.
"""

from __future__ import annotations

from spreadboard import funding_catalog


def _route(short_venue, *, long_venue, spread, funding=1.0, short_type="Futures", short_symbol=None):
    return {
        "token": "TKN",
        "long_venue": long_venue,
        "long_market_type": "Spot",
        "short_venue": short_venue,
        "short_market_type": short_type,
        "short_market_symbol": short_symbol or f"TKN/USDT:USDT@{short_venue}",
        "displayed_open_spread_pct": spread,
        "funding_apr_pct": funding,
    }


def test_one_route_survives_per_short_leg() -> None:
    routes = [
        _route("Aster", long_venue="Gate", spread=0.10),
        _route("Aster", long_venue="Mexc", spread=0.90),
        _route("Aster", long_venue="HTX", spread=0.40),
        _route("XT", long_venue="Gate", spread=0.20),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 2, f"expected one row per short leg, got {len(kept)}"
    aster = [r for r in kept if r["short_venue"] == "Aster"]
    assert len(aster) == 1
    assert aster[0]["long_venue"] == "Mexc", (
        "kept the wrong long: the survivor should be the best spread, which is "
        "the one actually worth entering"
    )


def test_the_same_venue_on_different_contracts_is_not_collapsed() -> None:
    """A venue can list more than one contract, and they fund independently.

    Collapsing on venue alone would report one contract's rate for the other.
    """

    routes = [
        _route("Gate", long_venue="A", spread=0.1, short_symbol="TKN/USDT:USDT"),
        _route("Gate", long_venue="B", spread=0.2, short_symbol="TKN/USDC:USDC"),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 2, "two independently funded contracts were merged into one"


def test_spot_and_futures_shorts_stay_distinct() -> None:
    routes = [
        _route("HTX", long_venue="A", spread=0.1, short_type="Spot"),
        _route("HTX", long_venue="B", spread=0.2, short_type="Futures"),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 2


def test_collapsing_preserves_every_short_leg() -> None:
    """The point is removing repetition, never removing a venue.

    A member asking "who pays me to short this" must still see every venue that
    does; only the choice of long is reduced.
    """

    routes = [
        _route(v, long_venue=lv, spread=0.1 * i)
        for i, v in enumerate(["Aster", "XT", "Gate", "Bitget", "HTX", "Binance", "Bybit", "Mexc"])
        for lv in ("L1", "L2", "L3")
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert {r["short_venue"] for r in kept} == {
        "Aster", "XT", "Gate", "Bitget", "HTX", "Binance", "Bybit", "Mexc"
    }
    assert len(kept) == 8


def test_a_route_without_a_short_leg_is_dropped_not_merged() -> None:
    """Rows with no identifiable short leg would otherwise all collapse into
    one bucket and report a single arbitrary rate for all of them."""

    routes = [
        {"token": "TKN", "displayed_open_spread_pct": 0.5},
        {"token": "TKN", "displayed_open_spread_pct": 0.9},
        _route("Aster", long_venue="Gate", spread=0.1),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 1
    assert kept[0]["short_venue"] == "Aster"


def test_a_missing_spread_never_outranks_a_real_one() -> None:
    routes = [
        _route("Aster", long_venue="Good", spread=0.5),
        _route("Aster", long_venue="Unknown", spread=None),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 1
    assert kept[0]["long_venue"] == "Good"


def test_the_generation_build_actually_collapses(monkeypatch) -> None:
    """A collapse the build never calls saves nothing.

    Helper-only tests have passed against this exact class of mutant on this
    codebase before, so this drives the real build path.
    """

    built = {
        "TKN": {
            "token": "TKN",
            "route_count": 4,
            "displayed_route_count": 4,
            "routes": [
                _route("Aster", long_venue="Gate", spread=0.1),
                _route("Aster", long_venue="Mexc", spread=0.9),
                _route("XT", long_venue="Gate", spread=0.2),
                _route("XT", long_venue="HTX", spread=0.3),
            ],
        }
    }

    collapsed = funding_catalog.collapse_payloads_to_short_legs(built)
    payload = collapsed["TKN"]

    assert len(payload["routes"]) == 2
    assert payload["route_count"] == 2, "route_count still advertises the pre-collapse size"
    assert payload["displayed_route_count"] == 2


def test_a_payload_without_routes_is_left_alone() -> None:
    built = {"A": {"token": "A"}, "B": {"token": "B", "routes": "not-a-list"}, "C": None}

    collapsed = funding_catalog.collapse_payloads_to_short_legs(dict(built))

    assert collapsed["A"] == {"token": "A"}
    assert collapsed["B"]["routes"] == "not-a-list"


def test_the_ranked_list_keeps_the_seven_best_paying_short_legs() -> None:
    """Some tokens carried 14 short venues while others showed 3.

    Seven is enough to choose from, and capping the crowded tokens is what
    leaves room for the ones that were missing entirely.
    """

    candidates = [(float(i), _route(f"V{i}", long_venue="L", spread=0.1)) for i in range(12)]

    kept = funding_catalog.top_short_legs(candidates, budget=7)

    assert len(kept) == 7
    assert [value for value, _ in kept] == [11.0, 10.0, 9.0, 8.0, 7.0, 6.0, 5.0], (
        "kept legs other than the best paying seven"
    )


def test_a_token_with_fewer_legs_than_the_budget_is_untouched() -> None:
    candidates = [(1.0, _route("A", long_venue="L", spread=0.1)),
                  (2.0, _route("B", long_venue="L", spread=0.1))]

    kept = funding_catalog.top_short_legs(candidates, budget=7)

    assert len(kept) == 2


def test_an_unknown_rate_never_displaces_a_known_one() -> None:
    candidates = [(None, _route("Unknown", long_venue="L", spread=0.1))]
    candidates += [(float(i), _route(f"V{i}", long_venue="L", spread=0.1)) for i in range(7)]

    kept = funding_catalog.top_short_legs(candidates, budget=7)

    assert len(kept) == 7
    assert all(r["short_venue"] != "Unknown" for _, r in kept)


def test_complete_payloads_collapses_what_it_publishes(monkeypatch) -> None:
    """Drives the real generation build, not the helper.

    An earlier version of this file asserted only that the helper worked, and
    passed cleanly against the mutant that deletes the build's call to it --
    which would have shipped the entire duplication untouched.
    """

    uncollapsed = {
        "TKN": {
            "token": "TKN",
            "route_count": 3,
            "routes": [
                _route("Aster", long_venue="Gate", spread=0.1),
                _route("Aster", long_venue="Mexc", spread=0.9),
                _route("XT", long_venue="Gate", spread=0.2),
            ],
        }
    }

    monkeypatch.setattr(
        funding_catalog.chart_catalog,
        "load",
        lambda *a, **k: {"markets": [{"token": "TKN", "market_type": "Futures"}]},
    )
    monkeypatch.setattr(
        funding_catalog, "_tokens_worth_expanding", lambda tokens, **k: list(tokens)
    )
    monkeypatch.setattr(
        funding_catalog.catalog_pairs,
        "for_tokens",
        lambda *a, **k: {k: dict(v, routes=list(v["routes"])) for k, v in uncollapsed.items()},
    )
    monkeypatch.setattr(funding_catalog, "_persist_cache", lambda payloads: None)
    monkeypatch.setattr(funding_catalog, "_CACHE_PAYLOADS", {})
    monkeypatch.setattr(funding_catalog, "_CACHE_AT", 0.0)
    monkeypatch.setattr(funding_catalog, "_CACHE_BUILDING", False)

    published = funding_catalog._complete_payloads(force_refresh=True)

    routes = published["TKN"]["routes"]
    assert len(routes) == 2, (
        f"the published generation carries {len(routes)} routes; the build did "
        "not collapse them, so the duplication ships to production"
    )
    assert {r["short_venue"] for r in routes} == {"Aster", "XT"}


def _payload_with_legs(count: int) -> dict:
    """Routes shaped as `_current_value` and `_common_eligible` expect them."""

    return {
        "TKN": {
            "token": "TKN",
            "ok": True,
            "route_count": count,
            "routes": [
                dict(
                    _route(f"V{i}", long_venue="L", spread=0.5),
                    # `_current_value` ranks on this, not on funding_apr_pct.
                    funding_daily_pct=float(i + 1),
                    funding_24h_pct=float(i + 1),
                    route_kind="FUTURES",
                    long_quote="USDT",
                    short_quote="USDT",
                )
                for i in range(count)
            ],
        }
    }


def test_the_ranked_page_applies_the_cap(monkeypatch) -> None:
    """Drives page(), not the helper.

    The helper test passes against the mutant that deletes page()'s call to it,
    which would ship a token showing all fourteen of its short venues.
    """

    monkeypatch.setattr(
        funding_catalog, "_complete_payloads", lambda **k: _payload_with_legs(12)
    )
    monkeypatch.setattr(funding_catalog, "_resident_live_overlay", lambda rows: rows)

    result = funding_catalog.page(window="now", limit=50)
    routes = [r for g in (result.get("groups") or []) for r in (g.get("routes") or [])]

    assert routes, "page returned nothing; the fixture is wrong, not the cap"
    assert len(routes) <= funding_catalog.SHORT_LEG_BUDGET, (
        f"page returned {len(routes)} short legs against a budget of "
        f"{funding_catalog.SHORT_LEG_BUDGET}"
    )


def test_an_exact_symbol_search_keeps_every_leg(monkeypatch) -> None:
    """Someone auditing one token may hold a venue outside the best seven."""

    monkeypatch.setattr(
        funding_catalog, "_complete_payloads", lambda **k: _payload_with_legs(12)
    )
    monkeypatch.setattr(funding_catalog, "_resident_live_overlay", lambda rows: rows)

    result = funding_catalog.page(window="now", symbol="TKN", limit=50)
    routes = [r for g in (result.get("groups") or []) for r in (g.get("routes") or [])]

    assert len(routes) == 12, (
        f"exact-symbol view returned {len(routes)} legs; capping it can hide the "
        "venue the member actually holds"
    )


def _ff_route(short_venue, *, long_venue, long_funding, spread=0.5):
    """A futures-futures route: BOTH legs fund, so net carry depends on the long."""

    return {
        "token": "TKN",
        "long_venue": long_venue,
        "long_market_type": "Futures",
        "long_market_symbol": f"TKN/USDT:USDT@{long_venue}",
        "long_funding_pct": long_funding,
        "short_venue": short_venue,
        "short_market_type": "Futures",
        "short_market_symbol": f"TKN/USDT:USDT@{short_venue}",
        "displayed_open_spread_pct": spread,
    }


def test_futures_futures_routes_are_not_merged_on_the_short_alone() -> None:
    """Net carry is short minus long, so a funding long makes routes distinct.

    Production 龙虾 showed a Gate short at 91.76%, 86.29% and 80.81% against
    three different futures longs. Merging those reports one long's rate for
    the others -- the collapse would be inventing a number, not removing a
    repeat.
    """

    routes = [
        _ff_route("Gate", long_venue="Mexc", long_funding=0.0),
        _ff_route("Gate", long_venue="HTX", long_funding=0.05),
        _ff_route("Gate", long_venue="Bitget", long_funding=0.11),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 3, (
        "merged futures-futures routes whose net carry genuinely differs"
    )


def test_a_spot_long_pays_no_funding_so_those_do_collapse() -> None:
    """This is the case the duplication actually came from."""

    routes = [
        _route("Gate", long_venue="Mexc", spread=0.1),
        _route("Gate", long_venue="HTX", spread=0.9),
        _route("Gate", long_venue="Bitget", spread=0.3),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 1
    assert kept[0]["long_venue"] == "HTX"


def test_a_dex_long_also_collapses() -> None:
    routes = [
        dict(_route("Aster", long_venue="OKX DEX 56", spread=0.2), long_market_type="DEX"),
        dict(_route("Aster", long_venue="OKX DEX 1", spread=0.8), long_market_type="DEX"),
    ]

    kept = funding_catalog.collapse_to_short_legs(routes)

    assert len(kept) == 1
    assert kept[0]["long_venue"] == "OKX DEX 1"
