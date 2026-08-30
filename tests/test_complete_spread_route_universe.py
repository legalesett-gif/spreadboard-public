"""The public pair browser must not inherit the scanner's per-token quota."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from spreadboard import api_spreads, catalog_pairs


def _evidence_route(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "token": "GUA",
        "route_key": "GUA-route",
        "route_kind": "FUTURES",
        "long_venue": "Gate",
        "short_venue": "Mexc",
        "long_market_type": "Futures",
        "short_market_type": "Futures",
        "long_market_symbol": "GUA/USDT:USDT",
        "short_market_symbol": "GUA/USDT:USDT",
        "long_quote": "USDT",
        "short_quote": "USDT",
        "long_price": 1.0,
        "short_price": 1.02,
        "long_bid": 0.999,
        "long_ask": 1.0,
        "short_bid": 1.02,
        "short_ask": 1.021,
        "long_volume_24h_usd": 1_000_000.0,
        "short_volume_24h_usd": 1_000_000.0,
        "executable_spread_pct": 2.0,
        "displayed_open_spread_pct": 2.0,
        "depth_weighted_spread_pct": 1.9,
        "depth_usd": 500.0,
        "target_notional_usd": 500.0,
        "catalog_pair": True,
        "quote_ts_us": int(time.time() * 1_000_000),
        "blockers": [],
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "verified"),
        ({"depth_weighted_spread_pct": None, "depth_unverified": True}, "research"),
        ({"short_price": 4.0}, "excluded"),
        # A dollar is a dollar (owner ruling 2026-08-30): a USD leg against a
        # USDT leg is the same trade, and refusing it removed whole venues --
        # Hyperliquid quotes every perpetual in USDC.
        ({"short_quote": "USD"}, "verified"),
        # A genuinely different currency is still a second, unmodelled basis.
        ({"short_quote": "BTC"}, "excluded"),
        ({"token": "ETH3L"}, "excluded"),
        ({"short_market_symbol": "GUA/USD:GUA"}, "excluded"),
        ({"depth_weighted_spread_pct": -0.1}, "excluded"),
        (
            {
                "token": "AMZNSTOCK",
                "short_market_symbol": "xyz:AMZN",
                "depth_weighted_spread_pct": None,
                "depth_unverified": True,
            },
            "research",
        ),
        (
            {
                "long_bid": 1.0,
                "long_ask": 1.0,
                "short_bid": 1.03,
                "short_ask": 1.03,
                "depth_weighted_spread_pct": None,
                "depth_unverified": True,
                "executable_spread_pct": 3.0,
                "displayed_open_spread_pct": 3.0,
            },
            "excluded",
        ),
    ],
)
def test_spread_evidence_is_identical_before_and_after_serialization(
    overrides: dict[str, object], expected: str
) -> None:
    mapping = _evidence_route(**overrides)
    obj = SimpleNamespace(**mapping)

    assert api_spreads.spread_evidence_state(mapping) == expected
    assert api_spreads.spread_evidence_state(obj) == expected


def test_short_spot_inventory_route_is_conditional_not_silently_deleted() -> None:
    route = _evidence_route(
        route_kind="FUTURES-SPOT",
        long_market_type="Futures",
        short_market_type="Spot",
        deliverable=False,
        depth_weighted_spread_pct=None,
        depth_unverified=True,
    )

    assert api_spreads.requires_existing_spot_inventory(route) is True
    assert api_spreads.spread_evidence_state(route) == "research"


def test_closed_transfer_route_remains_excluded() -> None:
    route = _evidence_route(
        route_kind="SPOT",
        long_market_type="Spot",
        short_market_type="Spot",
        deliverable=False,
    )

    assert api_spreads.spread_evidence_state(route) == "excluded"


def test_complete_route_index_merges_all_catalogue_pairs_and_reverse_spot_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_dex = _evidence_route(
        token="DEXONLY",
        route_key="DEXONLY|OKX DEX|Spot|Gate|Futures",
        route_kind="DEX-FUTURES",
        long_venue="OKX DEX 56",
        long_market_type="Spot",
        long_market_symbol="0x123",
    )
    stale_cex_mirror = _evidence_route(
        token="OLD",
        route_key="OLD|Gate|Futures|Mexc|Futures",
        quote_ts_us=1,
    )
    spot_future = _evidence_route(
        route_key="CUSTOM:spot-future",
        route_kind="SPOT-FUTURES",
        long_venue="Gate",
        long_market_type="Spot",
        long_market_symbol="GUA/USDT",
        short_venue="Mexc",
        short_market_type="Futures",
        short_market_symbol="GUA/USDT:USDT",
    )
    future_spot = _evidence_route(
        route_key="CUSTOM:future-spot",
        route_kind="FUTURES-SPOT",
        long_venue="Mexc",
        long_market_type="Futures",
        long_market_symbol="GUA/USDT:USDT",
        short_venue="Gate",
        short_market_type="Spot",
        short_market_symbol="GUA/USDT",
        depth_weighted_spread_pct=None,
        depth_unverified=True,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        catalog_pairs.chart_catalog,
        "load",
        lambda: {
            "markets": [
                {
                    "token": "GUA",
                    "venue": "Gate",
                    "market_type": "Spot",
                    "symbol": "GUA/USDT",
                },
                {
                    "token": "GUA",
                    "venue": "Mexc",
                    "market_type": "Futures",
                    "symbol": "GUA/USDT:USDT",
                },
            ]
        },
    )

    def fake_for_tokens(tokens, **kwargs):
        captured["tokens"] = tokens
        captured.update(kwargs)
        return {
            "GUA": {
                "routes": [spot_future, future_spot],
                "catalog_market_count": 3,
                "fresh_market_count": 2,
                "missing_book_count": 1,
            }
        }

    monkeypatch.setattr(catalog_pairs, "for_tokens", fake_for_tokens)

    rows, health = api_spreads._complete_current_catalogue_rows(
        [discovery_dex, stale_cex_mirror],
        metadata={"GUA": {"name": "GUA Token"}},
    )

    assert captured["include_short_spot"] is True
    assert captured["include_history"] is False
    assert captured["admissible_spreads_only"] is True
    assert captured["retain_reverse_pairs"] is True
    assert captured["max_age_seconds"] == catalog_pairs.MAX_BOOK_AGE_SECONDS
    assert {row["route_kind"] for row in rows} == {
        "SPOT-FUTURES",
        "FUTURES-SPOT",
        "DEX-FUTURES",
    }
    assert health["catalogue_route_count"] == 2
    assert health["merged_route_count"] == 3
    assert health["discovery_input_route_count"] == 2
    assert health["discovery_route_count"] == 1
    assert health["discovery_pruned_route_count"] == 1
    assert health["catalog_market_count"] == 3
    assert health["fresh_market_count"] == 2
    assert health["missing_book_count"] == 1
    assert health["book_coverage_pct"] == 66.67


def test_public_route_index_expands_dex_against_every_current_futures_leg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DEX discovery must happen before ranking, not after choosing its tab.

    Otherwise the default Markets universe can never discover a fleeting
    OKX-DEX -> Aster route: only a request that already knows to filter the DEX
    lane performs the fan-out.
    """

    dex_payload = _evidence_route(
        token="TRX",
        route_key="TRX|OKX DEX 56|DEX|Gate|Futures",
        route_kind="DEX-FUTURES",
        long_venue="OKX DEX 56",
        long_market_type="DEX",
        long_market_symbol="0xtrx",
        short_venue="Gate",
        short_market_type="Futures",
        short_market_symbol="TRX/USDT:USDT",
    )
    dex = SimpleNamespace(**dex_payload)
    dex.to_dict = lambda: dict(dex_payload)
    expanded_payload = _evidence_route(
        token="TRX",
        route_key="TRX|OKX DEX 56|DEX|Aster|Futures",
        route_kind="DEX-FUTURES",
        long_venue="OKX DEX 56",
        long_market_type="DEX",
        long_market_symbol="0xtrx",
        short_venue="Aster",
        short_market_type="Futures",
        short_market_symbol="TRX/USDT:USDT",
    )
    expanded = SimpleNamespace(**expanded_payload)
    expanded.to_dict = lambda: dict(expanded_payload)
    monkeypatch.setattr(
        api_spreads,
        "_load_api_discovery_rows",
        lambda *_args, **_kwargs: ([dex], {"status": "fresh"}),
    )
    monkeypatch.setattr(api_spreads, "_live_books", lambda: {"book": object()})
    monkeypatch.setattr(api_spreads, "apply_live_books", lambda rows, *_a, **_k: rows)
    monkeypatch.setattr(api_spreads, "_dedupe_rows", lambda rows: rows)
    monkeypatch.setattr(
        api_spreads,
        "_expand_current_dex_futures_pairs",
        lambda rows, **_kwargs: [*rows, expanded],
    )
    monkeypatch.setattr(
        api_spreads,
        "_complete_current_catalogue_rows",
        lambda rows, **_kwargs: (rows, {"status": "ok"}),
    )
    monkeypatch.setattr(api_spreads.token_metadata, "load_token_metadata", dict)
    monkeypatch.setattr(api_spreads.public_rails, "load_public_rails", dict)

    rows, _health = api_spreads.load_public_route_index(
        api_path=tmp_path / "discovery.json",
        now=time.time(),
    )

    assert "TRX|OKX DEX 56|DEX|Aster|Futures" in rows


def test_unfiltered_spread_query_expands_dex_before_kind_filtering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dex = SimpleNamespace(**_evidence_route(
        token="TRX",
        route_key="TRX|OKX DEX 56|DEX|Gate|Futures",
        route_kind="DEX-FUTURES",
        long_venue="OKX DEX 56",
        long_market_type="DEX",
        long_market_symbol="0xtrx",
    ))
    calls: list[int] = []

    monkeypatch.setattr(
        api_spreads,
        "_load_api_discovery_rows",
        lambda *_args, **_kwargs: ([dex], {"status": "fresh"}),
    )
    monkeypatch.setattr(
        api_spreads,
        "_load_board_rows",
        lambda *_args, **_kwargs: ([], {"status": "unused"}),
    )
    monkeypatch.setattr(api_spreads, "_live_books", lambda: {"book": object()})
    monkeypatch.setattr(api_spreads, "apply_live_books", lambda rows, *_a, **_k: rows)
    monkeypatch.setattr(api_spreads, "_dedupe_rows", lambda _rows: [])
    monkeypatch.setattr(
        api_spreads,
        "_expand_current_dex_futures_pairs",
        lambda rows, **_kwargs: calls.append(len(rows)) or rows,
    )
    monkeypatch.setattr(api_spreads.token_metadata, "load_token_metadata", dict)
    monkeypatch.setattr(api_spreads.public_rails, "load_public_rails", dict)
    monkeypatch.setattr(api_spreads, "_RESULT_CACHE", {})

    api_spreads.load_spreads(
        api_path=tmp_path / "discovery.json",
        board_path=tmp_path / "board.json",
        kind=None,
        include_stale=True,
        limit=10,
        now=time.time(),
    )

    assert calls == [1]


def test_bulk_catalogue_discards_negative_mirrors_before_global_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    book_path = tmp_path / "books.sqlite3"
    book_path.touch()
    monkeypatch.setattr(catalog_pairs.live_book_cache, "DEFAULT_PATH", book_path)
    monkeypatch.setattr(
        catalog_pairs.chart_catalog,
        "load",
        lambda: {
            "markets": [
                {
                    "token": "GUA",
                    "venue": "Gate",
                    "market_type": "Futures",
                    "symbol": "GUA/USDT:USDT",
                }
            ]
        },
    )

    class Store:
        def load_all(self, **_kwargs):
            return {}

        def close(self):
            return None

    monkeypatch.setattr(catalog_pairs.live_book_cache, "LiveBookStore", Store)
    positive = _evidence_route(route_key="positive")
    indicative = _evidence_route(
        route_key="indicative",
        depth_weighted_spread_pct=None,
        depth_unverified=True,
    )
    negative = _evidence_route(
        route_key="negative",
        executable_spread_pct=-0.2,
        displayed_open_spread_pct=-0.2,
        depth_weighted_spread_pct=-0.3,
    )
    monkeypatch.setattr(
        catalog_pairs,
        "_payload_from_legs",
        lambda *_args, **_kwargs: {
            "routes": [positive, indicative, negative],
            "route_count": 3,
            "displayed_route_count": 3,
        },
    )

    payload = catalog_pairs.for_tokens(
        {"GUA"},
        admissible_spreads_only=True,
        include_short_spot=True,
    )["GUA"]

    assert {row["route_key"] for row in payload["routes"]} == {
        "positive",
        "indicative",
    }
    assert payload["route_count"] == 2


def test_route_index_retains_reverse_of_each_admitted_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    book_path = tmp_path / "books.sqlite3"
    book_path.touch()
    monkeypatch.setattr(catalog_pairs.live_book_cache, "DEFAULT_PATH", book_path)
    monkeypatch.setattr(
        catalog_pairs.chart_catalog,
        "load",
        lambda: {
            "markets": [
                {
                    "token": "GUA",
                    "venue": "Gate",
                    "market_type": "Futures",
                    "symbol": "GUA/USDT:USDT",
                }
            ]
        },
    )

    class Store:
        def load_all(self, **_kwargs):
            return {}

        def close(self):
            return None

    monkeypatch.setattr(catalog_pairs.live_book_cache, "LiveBookStore", Store)
    forward = _evidence_route(route_key="forward")
    reverse = _evidence_route(
        route_key="reverse",
        long_venue="Mexc",
        short_venue="Gate",
        executable_spread_pct=-2.2,
        displayed_open_spread_pct=-2.2,
        depth_weighted_spread_pct=-2.1,
    )
    unrelated = _evidence_route(
        token="OTHER",
        route_key="unrelated",
        long_market_symbol="OTHER/USDT:USDT",
        short_market_symbol="OTHER/USDT:USDT",
        executable_spread_pct=-1.0,
        displayed_open_spread_pct=-1.0,
        depth_weighted_spread_pct=-1.0,
    )
    monkeypatch.setattr(
        catalog_pairs,
        "_payload_from_legs",
        lambda *_args, **_kwargs: {
            "routes": [forward, reverse, unrelated],
            "route_count": 3,
            "displayed_route_count": 3,
        },
    )

    payload = catalog_pairs.for_tokens(
        {"GUA"},
        admissible_spreads_only=True,
        include_short_spot=True,
        retain_reverse_pairs=True,
    )["GUA"]

    assert {row["route_key"] for row in payload["routes"]} == {
        "forward",
        "reverse",
    }
