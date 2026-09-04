"""A market with no book feed cannot be held to a 90-second freshness rule.

`_complete_current_catalogue_rows` admits a discovery row when its route_kind
starts with `DEX-` or its evidence state is verified/research. That state
requires `spread_quote_current`, and `SPREAD_LEADER_MAX_AGE_MIN` is 1.5 minutes
while a discovery scan takes 45-60. For CEX venues that is exactly right: the
chart catalogue supplies a current book for every market it covers, so a stale
discovery mirror of one is redundant.

A Hyperliquid HIP-3 builder market has no book feed at all. Discovery is its
only path, so the freshness rule closes that path permanently. Measured on
production 2026-09-04: all seven Hyperliquid OPENAI rows -- including
Kucoin:Futures to Hyperliquid:Futures at 5.69% off the live io:OAI market --
were excluded with `quote_current: false`, and the live route index contained
zero OPENAI entries and only 67 Hyperliquid ones, every one an OKX-DEX hedge.

Scoped to the sources whose markets exist nowhere else. Admitting every
`dex_discovered` row instead would have added 4,916 rows across 587 tokens, a
fifth of the index, almost all Aster mirrors of pairs the catalogue already
carries -- and the inflation this file's own comments warn about.
"""

from __future__ import annotations

import pytest
from tests.test_complete_spread_route_universe import _evidence_route

from spreadboard import api_spreads, catalog_pairs


@pytest.fixture
def catalogue(monkeypatch):
    monkeypatch.setattr(catalog_pairs.chart_catalog, "load", lambda: {"markets": []})
    monkeypatch.setattr(catalog_pairs, "for_tokens", lambda tokens, **kw: {})


def _stale(**overrides):
    return _evidence_route(quote_ts_us=1, **overrides)


def test_a_builder_market_route_survives_being_older_than_the_book_rule(
    catalogue,
) -> None:
    row = _stale(
        token="OPENAI",
        route_key="OPENAI|Kucoin Futures|Futures|Hyperliquid|Futures",
        long_venue="Kucoin Futures",
        short_venue="Hyperliquid",
        short_market_symbol="io:OAI",
        source_kind="dex_discovered",
        source_name="hyperliquid_builder_dex",
        executable_spread_pct=5.69,
    )

    rows, health = api_spreads._complete_current_catalogue_rows([row], metadata={})

    assert health["discovery_route_count"] == 1
    assert [r["route_key"] for r in rows] == [
        "OPENAI|Kucoin Futures|Futures|Hyperliquid|Futures"
    ]


def test_a_stale_cex_mirror_is_still_dropped(catalogue) -> None:
    """The catalogue already carries a current book for these."""

    row = _stale(token="OLD", route_key="OLD|Gate|Futures|Mexc|Futures")

    rows, health = api_spreads._complete_current_catalogue_rows([row], metadata={})

    assert health["discovery_route_count"] == 0
    assert rows == []


def test_a_stale_aster_mirror_is_still_dropped(catalogue) -> None:
    """Aster is a dex_discovered source too, but the catalogue covers it.

    Admitting every dex_discovered row was measured at 4,916 extra rows, mostly
    Gate/Binance/Bitget/Bybit against Aster -- pairs the catalogue already has.
    """

    row = _stale(
        token="GUA",
        route_key="GUA|Gate|Futures|Aster|Futures",
        short_venue="Aster",
        source_kind="dex_discovered",
        source_name="dex_derivatives_ccxt",
    )

    _, health = api_spreads._complete_current_catalogue_rows([row], metadata={})

    assert health["discovery_route_count"] == 0


def test_a_current_builder_route_is_unaffected(catalogue) -> None:
    """The carve-out adds a path; it must not remove the ordinary one."""

    row = _evidence_route(
        token="OPENAI",
        route_key="OPENAI|Gate|Futures|Hyperliquid|Futures",
        short_venue="Hyperliquid",
        source_kind="dex_discovered",
        source_name="hyperliquid_builder_dex",
    )

    _, health = api_spreads._complete_current_catalogue_rows([row], metadata={})

    assert health["discovery_route_count"] == 1
