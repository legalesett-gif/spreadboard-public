"""The public Guide must teach hedge mechanics without promising safety.

The previous tutorial called two legs automatically delta neutral, described
1x futures as "no borrowing", promised Futures/Spot convergence, and told a
reader to sell a DEX leg without saying that spot inventory is required.  Those
shortcuts are dangerous precisely for a beginner: contract multipliers,
separate venue collateral and adverse basis movement decide whether the hedge
survives long enough for either thesis to work.
"""

from __future__ import annotations

from spreadboard import server


def test_guide_teaches_normalised_exposure_and_independent_margin_risk() -> None:
    page = server.render_guide_page()
    lower = page.casefold()

    assert "matched underlying exposure" in lower
    assert "contract multipliers" in lower
    assert "each venue must carry its own collateral" in lower
    assert "1x can still be liquidated" in lower
    assert "convergence is not guaranteed" in lower

    assert "which they almost always do" not in lower
    assert "always converge" not in lower
    assert "use 1x leverage. no borrowing" not in lower
    assert "if the coin doubles or halves, you neither win nor lose" not in lower
    assert "the calmest trade on the board" not in lower
    assert "these are real and" not in lower


def test_guide_states_dex_inventory_transfer_and_probe_boundaries() -> None:
    page = server.render_guide_page()
    lower = page.casefold()

    assert "selling spot on a dex requires inventory" in lower
    assert "typical beginner route is long dex spot and short futures" in lower
    assert f"{server.PROBE_LABEL.casefold()} matched vwap" in lower
    assert "pre-positioned inventory" in lower

    assert "quoted price is for a small size" not in lower
    assert "if withdrawals are shut, you cannot do this trade at all" not in lower
    assert "fewer people can be bothered" not in lower
