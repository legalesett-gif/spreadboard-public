"""Methodology must describe the exact formulas used by live route pricing.

The named production ZIL HTX -> Gate route had buy VWAP 0.002281, sell VWAP
0.002702 and a displayed matched edge of 18.456817%.  Dividing by the midpoint
would instead say 16.897451%, so that apparently small copy error materially
misstates the product's primary number.
"""

from spreadarb.api_discovery.models import spread_pct
from spreadboard import server


def test_methodology_uses_the_live_buy_price_denominator_and_names_the_probe() -> None:
    buy_vwap = 0.002281
    sell_vwap = 0.002702

    assert spread_pct(buy_vwap, sell_vwap) == 18.456817185444976

    html = server.render_methodology_page()
    assert "(sell VWAP ÷ buy VWAP − 1) × 100" in html
    assert "midpoint" not in html
    assert f"standard {server.PROBE_LABEL} matched-size probe" in html
