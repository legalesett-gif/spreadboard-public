"""Realised carry visible in the table, the way the reference product shows it.

The windows reached the API but the Pro Table still did not show them, so a
member judging a funding farm had to leave the row they were reading. Their
layout puts 1d, 7d and 30d directly beside the funding rate, and that adjacency
is the whole point: current rate next to what actually settled.
"""

from __future__ import annotations

from spreadboard import server

ROW = {
    "route_key": "AAA|FUTURES|Mexc|Futures|Ourbit|Futures",
    "token": "AAA",
    "long_venue": "Mexc",
    "short_venue": "Ourbit",
    "settled_funding_windows": {"1d": 0.5, "7d": 2.25, "30d": -8.0},
}


def test_the_header_carries_the_three_windows() -> None:
    table = server.render_pro_market_table([ROW])

    for label in ("1d", "7d", "30d"):
        assert f"<th>{label}</th>" in table


def test_a_row_shows_its_realised_carry() -> None:
    table = server.render_pro_market_table([ROW])

    assert "0.500" in table or "0.50" in table
    assert "2.250" in table or "2.25" in table


def test_a_negative_window_keeps_its_sign() -> None:
    """A farm that paid out is the single most important thing not to lose."""
    table = server.render_pro_market_table([ROW])

    assert "-8.0" in table or "−8.0" in table or "-8.000" in table


def test_a_missing_window_renders_a_dash_not_a_zero() -> None:
    """Unknown carry must never read as flat carry."""
    row = dict(ROW, settled_funding_windows={"1d": None, "7d": None, "30d": None})

    table = server.render_pro_market_table([row])

    assert "—" in table


def test_a_row_without_windows_still_renders() -> None:
    row = {k: v for k, v in ROW.items() if k != "settled_funding_windows"}

    table = server.render_pro_market_table([row])

    assert "AAA" in table


def test_every_row_has_one_cell_per_header() -> None:
    """A header/cell mismatch silently shifts every column in the table."""
    table = server.render_pro_market_table([ROW])
    headers = table.count("<th>")
    cells = table.count("<td ")

    assert headers == cells, f"{headers} headers but {cells} cells"


def test_pro_table_spread_and_basis_are_connected_to_the_live_stream() -> None:
    """The page says it streams order books, but Pro Table used to omit the
    hooks that its grouped view exposes, leaving every table edge frozen.
    """
    row = dict(
        ROW,
        age_min=0.1,
        depth_weighted_spread_pct=1.25,
        executable_spread_pct=1.4,
    )

    table = server.render_pro_market_table([row])

    assert "data-live-spread" in table
    assert "data-live-spread-basis" in table
