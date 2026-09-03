"""A builder market must be identifiable by its price, not only by its ticker.

`HyperliquidBuilderDexSource` admitted a builder market only when its ticker was
already a token the CEX side quoted. Hyperliquid's live OpenAI perp is
`io:OAI` -- mark 1462.4, open interest 2,302, $5.58M of 24h volume -- while every
CEX calls the same asset OPENAI, so it was dropped before reaching the price
gate that exists to decide exactly this question. The delisted `vntl:OPENAI`
matched by name and had no volume at all.

Price is the evidence the source already trusts for identity (`xyz:MU`, `km:MU`
and `mkts:MU` are separated this way). It is now allowed to work in the other
direction too, and only when exactly one token corroborates: an ambiguous match
is not evidence, and a silent drop is not a reason.
"""

from __future__ import annotations

import time

from spreadarb.api_discovery import sources
from spreadarb.api_discovery.models import MarketQuote


def _anchor(token: str, price: float) -> MarketQuote:
    ts = int(time.time() * 1_000_000)
    return MarketQuote(
        token=token,
        venue="Gate",
        market_type="Futures",
        bid=price,
        ask=price,
        bid_vwap=price,
        ask_vwap=price,
        quote_ts_us=ts,
        source_name="cex",
        symbol=f"{token}/USDT:USDT",
    )


def _source(monkeypatch, symbol: str, mid: str):
    source = sources.HyperliquidBuilderDexSource()
    monkeypatch.setattr(
        source,
        "_info",
        lambda payload, _timeout: (
            [{"name": symbol.split(":")[0]}]
            if payload["type"] == "perpDexs"
            else [
                {"universe": [{"name": symbol}]},
                [{"midPx": mid, "funding": "0.00001", "dayNtlVlm": "5580000"}],
            ]
        ),
    )
    return source


def _collect(source, *anchors: MarketQuote):
    ctx = sources.DiscoveryContext(
        tokens=(),
        watchlist={},
        deadline_monotonic=None,
        reference_quotes=anchors,
        min_spread_pct=0.05,
        min_funding_apr_pct=0.01,
    )
    return source.collect(ctx)


def test_a_differently_tickered_market_is_admitted_on_price(monkeypatch) -> None:
    """`io:OAI` is what Hyperliquid calls the asset every CEX calls OPENAI."""

    source = _source(monkeypatch, "io:OAI", "1400.0")

    result = _collect(source, _anchor("OPENAI", 1390.21))

    assert {q.symbol for q in result.quotes} == {"io:OAI"}
    assert {q.token for q in result.quotes} == {"OPENAI"}


def test_an_ambiguous_price_identifies_nothing(monkeypatch) -> None:
    """Two tokens within tolerance is not evidence about which one this is."""

    source = _source(monkeypatch, "io:OAI", "1400.0")

    result = _collect(source, _anchor("OPENAI", 1390.21), _anchor("CLOSEAI", 1395.0))

    assert result.quotes == [] or {q.symbol for q in result.quotes} == set()


def test_a_price_that_matches_nothing_is_recorded_not_silently_dropped(
    monkeypatch,
) -> None:
    """The silent `continue` is why this gap left no trace to find."""

    source = _source(monkeypatch, "io:OAI", "17.0")

    result = _collect(source, _anchor("OPENAI", 1390.21))

    assert not result.quotes
    assert any("io:OAI" in str(error) for error in result.status.errors)


def test_a_ticker_that_does_match_still_wins(monkeypatch) -> None:
    """Name matching stays the first answer; price only fills the gap."""

    source = _source(monkeypatch, "xyz:OPENAI", "1400.0")

    result = _collect(source, _anchor("OPENAI", 1390.21))

    assert {q.token for q in result.quotes} == {"OPENAI"}


def test_a_named_match_that_disagrees_on_price_is_still_rejected(monkeypatch) -> None:
    """The original guard must survive: same ticker, different instrument."""

    source = _source(monkeypatch, "xyz:OPENAI", "848.0")

    result = _collect(source, _anchor("OPENAI", 1390.21))

    assert not result.quotes
