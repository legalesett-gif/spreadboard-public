"""Realised funding history on the board itself, not one tab away.

The reference product shows 1d, 7d and 30d realised carry beside every pair, so
a subscriber judges a funding farm without leaving the table. We computed the
same windows and showed them only on Rankings and the per-token view.

The cost model is the reason this was not simply switched on everywhere:
windows are attached to the paginated slice a member can actually see, never to
the hundreds of hidden alternatives behind it, exactly as catalog_pairs already
does.
"""

from __future__ import annotations

from spreadboard import api_spreads


def test_windows_are_attached_to_the_visible_best_route() -> None:
    groups = [{
        "token": "AAA",
        "best_route": {"long_venue": "Mexc", "long_market_type": "Spot",
                       "short_venue": "Ourbit", "short_market_type": "Futures"},
        "routes": [],
    }]

    api_spreads.attach_funding_history(
        groups, lookup=lambda route: {"1d": 0.5, "7d": 2.0, "30d": 8.0}
    )

    assert groups[0]["best_route"]["settled_funding_windows"] == {
        "1d": 0.5, "7d": 2.0, "30d": 8.0
    }


def test_the_group_carries_the_windows_for_the_table_header() -> None:
    groups = [{"token": "AAA", "best_route": {"long_venue": "Mexc"}, "routes": []}]

    api_spreads.attach_funding_history(
        groups, lookup=lambda route: {"1d": 1.0, "7d": None, "30d": 3.0}
    )

    assert groups[0]["settled_funding_windows"] == {"1d": 1.0, "7d": None, "30d": 3.0}


def test_a_group_without_a_best_route_is_left_alone() -> None:
    groups = [{"token": "AAA", "best_route": None, "routes": []}]

    api_spreads.attach_funding_history(groups, lookup=lambda route: {"1d": 1.0})

    assert groups[0].get("settled_funding_windows") is None


def test_a_failing_lookup_never_breaks_the_board() -> None:
    """History is context. The board must render without it."""
    def boom(route):
        raise RuntimeError("window cache unreadable")

    groups = [{"token": "AAA", "best_route": {"long_venue": "Mexc"}, "routes": []}]
    api_spreads.attach_funding_history(groups, lookup=boom)

    assert groups[0]["best_route"].get("settled_funding_windows") is None


def test_each_route_is_looked_up_once_per_call() -> None:
    """One lookup per visible row; hidden alternatives cost nothing."""
    calls = []

    def counting(route):
        calls.append(route)
        return {"1d": 1.0}

    groups = [
        {"token": "AAA", "best_route": {"long_venue": "Mexc"},
         "routes": [{"route_key": "hidden-1"}, {"route_key": "hidden-2"}]},
        {"token": "BBB", "best_route": {"long_venue": "Gate"}, "routes": []},
    ]
    api_spreads.attach_funding_history(groups, lookup=counting)

    assert len(calls) == 2, "hidden routes must not be priced"
