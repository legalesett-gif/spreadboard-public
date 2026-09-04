"""A spot leg that disagrees with the perp's own index is not the same claim.

OPENAI's spot token trades at ~846 while every venue's OPENAI perp publishes an
index of ~1,390-1,461 and pays 0.005% funding per 8h against a 3% cap. The
perps are not rich to their deliverable; they settle against something else
entirely. Pairing the two produced 63%, then 72%, then 82% "spreads" that
nobody can take, and because the per-token route budget keeps the widest rows
they evicted the genuine Kucoin-to-Hyperliquid 5.69%.

The index is the venue's own statement of what the contract settles against, so
this needs no inference. It also arrives free: `fetch_funding_rates` already
returns `indexPrice` and `markPrice` for every perp on every venue, and
`_funding_fields` was discarding them.
"""

from __future__ import annotations

import time
from typing import ClassVar

from spreadboard import api_spreads, fast_quotes


def _route(**over) -> dict:
    now_us = int(time.time() * 1_000_000)
    row = {
        "token": "OPENAI",
        "route_key": "OPENAI-route",
        "route_kind": "SPOT-FUTURES",
        "long_venue": "Gate",
        "long_market_type": "Spot",
        "short_venue": "Hyperliquid",
        "short_market_type": "Futures",
        "long_market_symbol": "OPENAI/USDT",
        "short_market_symbol": "io:OAI",
        "long_quote": "USDT",
        "short_quote": "USDT",
        "long_price": 846.0,
        "short_price": 1461.35,
        "long_bid": 845.7,
        "long_ask": 846.5,
        "short_bid": 1461.0,
        "short_ask": 1461.7,
        "long_volume_24h_usd": 381_000.0,
        "short_volume_24h_usd": 5_296_809.0,
        "executable_spread_pct": 72.4,
        "displayed_open_spread_pct": 72.4,
        "depth_weighted_spread_pct": 72.4,
        "depth_usd": 500.0,
        "target_notional_usd": 500.0,
        "quote_ts_us": now_us,
        "blockers": [],
    }
    row.update(over)
    return row


def test_a_spot_far_from_the_perp_index_is_flagged() -> None:
    row = _route(short_index_price=1461.35)

    assert api_spreads.spot_disagrees_with_perp_index(row) is True


def test_a_spot_near_the_perp_index_is_not() -> None:
    """A real basis is nothing like this. Bitcoin spot sits pennies off index."""

    row = _route(long_price=1440.0, long_bid=1439.0, long_ask=1441.0,
                 short_index_price=1461.35, executable_spread_pct=1.5)

    assert api_spreads.spot_disagrees_with_perp_index(row) is False


def test_no_index_means_no_judgement() -> None:
    """Most venues publish one; a missing value must not exclude the row."""

    assert api_spreads.spot_disagrees_with_perp_index(_route()) is False


def test_a_futures_futures_route_is_untouched() -> None:
    """Kucoin to Hyperliquid is the route this exists to protect."""

    row = _route(
        route_kind="FUTURES",
        long_market_type="Futures",
        long_price=1382.68,
        long_bid=1382.0,
        long_ask=1383.0,
        short_index_price=1461.35,
        executable_spread_pct=5.69,
    )

    assert api_spreads.spot_disagrees_with_perp_index(row) is False


def test_the_evidence_classifier_excludes_it() -> None:
    """Flagging is only useful if it keeps the row off the board."""

    assert api_spreads.spread_evidence_state(_route(short_index_price=1461.35)) == "excluded"
    assert api_spreads.spread_evidence_state(_route()) != "excluded"


def test_the_index_price_survives_the_funding_fetch(monkeypatch) -> None:
    """It is already on the wire; the bulk fetch was dropping it on the floor."""

    class _Client:
        has: ClassVar[dict] = {"fetchFundingRates": True}
        markets: ClassVar[dict] = {
            "OPENAI/USDT:USDT": {"id": "OPENAI_USDT", "swap": True}
        }

        def fetch_funding_rates(self):
            return {
                "OPENAI/USDT:USDT": {
                    "symbol": "OPENAI/USDT:USDT",
                    "fundingRate": 0.00005,
                    "interval": "8h",
                    "indexPrice": 1461.35,
                    "markPrice": 1462.4,
                }
            }

    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda *_a, **_k: _Client())

    rates = refresher._bulk_funding_rates("Gate")

    assert rates["OPENAI/USDT:USDT"]["index_price"] == 1461.35


def test_a_spot_spot_route_is_never_judged_against_an_index() -> None:
    """Only a spot paired with a PERP is measured against that perp's index."""

    row = _route(
        route_kind="SPOT",
        short_market_type="Spot",
        short_index_price=1461.35,
        short_price=846.0,
        long_price=846.0,
    )

    assert api_spreads.spot_disagrees_with_perp_index(row) is False


def test_the_index_price_reaches_the_row() -> None:
    row = {"long_market_type": "Spot", "short_market_type": "Futures"}
    fast_quotes._sync_quoted_funding(
        row, {}, {"current_funding_pct": 0.005, "index_price": 1461.35}
    )

    assert row["short_index_price"] == 1461.35
