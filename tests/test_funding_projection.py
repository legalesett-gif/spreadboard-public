"""A live funding rate must produce a carry figure.

1,498 futures routes showed no funding at all while holding a live rate on
both legs, because only the enrichment step -- which covers a few dozen tokens
a lane -- ever wrote `projected_24h_pct`, and a route needs it on both sides.
"""

from __future__ import annotations

import pytest

from spreadboard import api_spreads


def _row(long_rate, long_interval, short_rate, short_interval):
    return {
        "token": "AAA",
        "long_venue": "Gate", "long_market_type": "Futures",
        "short_venue": "Mexc", "short_market_type": "Futures",
        "notes": {"route_inputs": {
            "long": {"symbol": "AAA/USDT:USDT"},
            "short": {"symbol": "AAA/USDT:USDT"},
        }},
    }, {
        "Gate|AAA/USDT:USDT": {"rate_pct": long_rate, "interval_hours": long_interval},
        "Mexc|AAA/USDT:USDT": {"rate_pct": short_rate, "interval_hours": short_interval},
    }


def test_a_live_rate_and_interval_produce_a_projection() -> None:
    raw, legs = _row(0.01, 8.0, 0.02, 4.0)

    out = api_spreads._apply_live_funding(raw, legs)
    inputs = out["notes"]["route_inputs"]

    # 0.01% every 8h is 0.03%/day; 0.02% every 4h is 0.12%/day.
    assert inputs["long"]["projected_24h_pct"] == pytest.approx(0.03)
    assert inputs["short"]["projected_24h_pct"] == pytest.approx(0.12)


def test_a_zero_rate_is_a_projection_of_zero_not_a_missing_one() -> None:
    """Both legs at 0.0 is a real answer: this route pays nothing."""
    raw, legs = _row(0.0, 4.0, 0.0, 8.0)

    inputs = api_spreads._apply_live_funding(raw, legs)["notes"]["route_inputs"]

    assert inputs["long"]["projected_24h_pct"] == 0.0
    assert inputs["short"]["projected_24h_pct"] == 0.0


def test_a_measured_value_is_never_overwritten_by_the_estimate() -> None:
    """Settled and enriched values are measured; this one is arithmetic."""
    raw, legs = _row(0.05, 8.0, 0.05, 8.0)
    raw["notes"]["route_inputs"]["long"]["projected_24h_pct"] = 9.99

    inputs = api_spreads._apply_live_funding(raw, legs)["notes"]["route_inputs"]

    assert inputs["long"]["projected_24h_pct"] == 9.99
    assert inputs["short"]["projected_24h_pct"] == pytest.approx(0.15)


def test_no_interval_means_no_invented_projection() -> None:
    raw, legs = _row(0.01, None, 0.02, None)

    inputs = api_spreads._apply_live_funding(raw, legs)["notes"]["route_inputs"]

    assert inputs["long"].get("projected_24h_pct") is None
    assert inputs["short"].get("projected_24h_pct") is None


def test_the_settled_cap_is_configurable_and_raised() -> None:
    """At 25 tokens a lane only 181 of 15,754 rows carried settled funding."""
    import re
    from pathlib import Path

    from scripts.run_spreadboard_service import FUNDING_TOKENS_PER_LANE

    assert FUNDING_TOKENS_PER_LANE >= 25
    configured = int(
        re.search(
            r'SPREADBOARD_FUNDING_TOKENS_PER_LANE:\s*"(\d+)"',
            Path("compose.production.yml").read_text(encoding="utf-8"),
        ).group(1)
    )
    assert configured > 25


def test_a_quote_records_which_path_produced_it() -> None:
    """Needed to tell a walked ladder from a ticker when the VWAP matches."""
    import inspect

    from spreadarb.api_discovery import sources
    from spreadarb.api_discovery.models import MarketQuote

    assert "quote_source" in MarketQuote.__dataclass_fields__
    body = inspect.getsource(sources)
    assert 'quote_source="ticker"' in body
    assert 'quote_source="orderbook"' in body
    assert '"quote_source": quote.quote_source' in body
