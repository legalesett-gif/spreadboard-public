"""The stream must subscribe to the routes the page is showing.

Forwarding only kind/limit/sort left the funding page subscribed to the spread
board: events arrived for routes that were not on screen, so the funding lanes
and every filtered tab sat still while "All routes" moved.
"""

from __future__ import annotations

from spreadboard.server import render_board_stream_script


def test_the_funding_lane_subscribes_as_a_funding_lane() -> None:
    script = render_board_stream_script(
        {"funding_only": ["1"], "kind": ["FUTURES"], "sort": ["funding"], "direction": ["desc"]}
    )

    assert "funding_only=1" in script
    assert "kind=FUTURES" in script
    assert "direction=desc" in script


def test_a_filtered_tab_carries_its_filters() -> None:
    script = render_board_stream_script(
        {"kind": ["SPOT"], "exchange": ["gate"], "min_spread_pct": ["0.5"]}
    )

    assert "kind=SPOT" in script
    assert "exchange=gate" in script
    assert "min_spread_pct=0.5" in script


def test_an_unfiltered_board_subscribes_without_parameters() -> None:
    script = render_board_stream_script({})

    assert "/api/stream/board?" not in script
    assert "/api/stream/board" in script


def test_presentation_parameters_are_not_forwarded() -> None:
    """`farm` and `rank` change the order shown, not the set returned, and
    forwarding them fragments the stream cache for an identical payload."""
    script = render_board_stream_script(
        {"farm": ["futures-spot"], "rank": ["7d"], "kind": ["FUTURES-SPOT-PAIR"]}
    )

    assert "farm=" not in script
    assert "rank=" not in script
    assert "kind=FUTURES-SPOT-PAIR" in script
