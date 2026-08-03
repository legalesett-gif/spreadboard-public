"""Every lane a token appears in must keep a foothold under the per-token cap.

_row_strength scores a carry trade above a 0.2% spot spread every time, so the
funding-bearing lanes took the whole allowance: ASR, CFG, CHZ, LA and PTB each
held 25 or more Spot-Futures rows and not one Spot-Spot pair, while the
reference product lists all five in its Spot lane.
"""

from __future__ import annotations

import collections

from src.spreadarb.api_discovery.runner import _keep_across_lanes


def _row(kind: str, index: int) -> dict:
    return {"route_kind": kind, "id": f"{kind}-{index}"}


def test_a_crowded_token_still_keeps_spot_routes() -> None:
    rows = [_row("SPOT-FUTURES", i) for i in range(25)] + [_row("SPOT", i) for i in range(3)]

    kept = _keep_across_lanes(rows, limit=10)

    kinds = collections.Counter(r["route_kind"] for r in kept)
    assert kinds["SPOT"] >= 3, kinds
    assert len(kept) == 10


def test_every_lane_present_gets_a_foothold() -> None:
    rows = (
        [_row("FUTURES", i) for i in range(20)]
        + [_row("SPOT", i) for i in range(5)]
        + [_row("DEX-SPOT", i) for i in range(5)]
        + [_row("DEX-FUTURES", i) for i in range(5)]
    )

    kept = _keep_across_lanes(rows, limit=16)

    kinds = collections.Counter(r["route_kind"] for r in kept)
    for lane in ("FUTURES", "SPOT", "DEX-SPOT", "DEX-FUTURES"):
        assert kinds[lane] >= 3, kinds


def test_a_token_within_its_allowance_is_untouched() -> None:
    rows = [_row("FUTURES", i) for i in range(4)]

    assert _keep_across_lanes(rows, limit=10) == rows


def test_the_remainder_still_goes_to_the_strongest() -> None:
    """Order is preserved after each lane's reserve, so strength still decides."""
    rows = [_row("FUTURES", i) for i in range(10)] + [_row("SPOT", i) for i in range(10)]

    kept = _keep_across_lanes(rows, limit=12)

    assert len(kept) == 12
    # The first futures rows are the strongest and must survive.
    assert {r["id"] for r in kept} >= {"FUTURES-0", "FUTURES-1", "FUTURES-2"}
