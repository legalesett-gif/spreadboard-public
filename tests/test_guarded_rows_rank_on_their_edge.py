"""Guarded rows were un-hidden but still sorted to the bottom.

`load_spreads` carries an operator correction from 2026-08-01: guarded rows are
shown by default and badged, because "large spreads on this board are REAL -- he
has captured a 150% spread for real money -- so hiding them loses the very
opportunities the product exists to surface."

The ranking never followed. `_group_sort_value` returned -999999 for any group
whose best route was guarded, which on a paginated board is the same outcome as
hiding: BP's 18.78% Mexc->Gate spread sits behind every ordinary row on the
board. 243 of 1,576 board rows are guarded.

Guarded is a statement about IDENTITY EVIDENCE, not about size. It belongs on a
badge, which the row already carries, not in the sort key.
"""

from __future__ import annotations

from spreadboard import api_spreads


def _group(edge, *, guarded):
    return {
        "token": "BP",
        "best_edge_pct": edge,
        "best_route": {"mirage_guarded": guarded},
        "routes": [{"token": "BP"}],
    }


def test_a_guarded_group_ranks_on_its_real_edge() -> None:
    value = api_spreads._group_sort_value(_group(18.78, guarded=True), "edge")

    assert value == 18.78, (
        f"guarded group sorted at {value}; it is buried behind every ordinary row"
    )


def test_an_unguarded_group_is_unchanged() -> None:
    assert api_spreads._group_sort_value(_group(2.0, guarded=False), "edge") == 2.0


def test_a_wide_guarded_group_outranks_a_narrow_clean_one() -> None:
    """This is the whole point: an 18% lead the member can investigate should
    not sit behind a 0.2% one just because its identity is unproven."""

    wide = api_spreads._group_sort_value(_group(18.78, guarded=True), "edge")
    narrow = api_spreads._group_sort_value(_group(0.2, guarded=False), "edge")

    assert wide > narrow


def test_a_group_with_no_edge_still_sorts_last() -> None:
    """Unknown edge is not a big edge."""

    assert api_spreads._group_sort_value(_group(None, guarded=True), "edge") == 0.0


def test_an_empty_group_is_still_ranked_last() -> None:
    assert api_spreads._group_sort_value({"routes": []}, "edge") == -999999.0
