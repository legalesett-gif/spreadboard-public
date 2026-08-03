"""What the websocket worker subscribes to must be what the board displays.

The worker used to rank the raw snapshot by spread and stream the top legs.
That set is close to the opposite of the board's: the widest raw numbers are
the dislocated rows the board filters out, so the worker held hundreds of
subscriptions while only a third of the routes on screen had a live price.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.websocket_book_worker import _board_leg_key, _desired_legs


def _row(token: str, *, spread: float, long_venue: str = "Binance") -> dict[str, Any]:
    return {
        "route_key": f"{token}|{long_venue}|Futures|Bybit|Futures",
        "token": token,
        "route_kind": "FUTURES",
        "long_venue": long_venue,
        "long_market_type": "Futures",
        "long_market_symbol": f"{token}/USDT:USDT",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "short_market_symbol": f"{token}/USDT:USDT",
        "depth_weighted_spread_pct": spread,
        "executable_spread_pct": spread,
    }


def test_board_leg_key_matches_the_field_the_board_looks_books_up_by() -> None:
    route = _row("BTC", spread=1.0)
    # A route_inputs symbol must not win: live_prices_for keys on market_symbol,
    # so a book stored under anything else is invisible to the board.
    route["notes"] = {"route_inputs": {"long": {"symbol": "BTC-PERP-DIFFERENT"}}}

    assert _board_leg_key(route, "long") == ("Binance", "Futures", "BTC/USDT:USDT")


def test_board_leg_key_rejects_unsupported_venues_and_types() -> None:
    assert _board_leg_key({**_row("BTC", spread=1.0), "long_venue": "NoSuchVenue"}, "long") is None
    assert _board_leg_key({**_row("BTC", spread=1.0), "long_market_type": "Option"}, "long") is None
    assert _board_leg_key({**_row("BTC", spread=1.0), "long_market_symbol": ""}, "long") is None


def test_subscriptions_follow_the_board_not_the_widest_raw_spread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _row("SHOWN", spread=2.0)
    # A far wider row that the board filters out and never renders.
    hidden = _row("HIDDEN", spread=900.0, long_venue="Mexc")

    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text(
        json.dumps({"api_discovered_rows": [shown, hidden], "dex_discovered_rows": []}),
        encoding="utf-8",
    )

    from spreadboard import api_spreads

    monkeypatch.setattr(
        api_spreads,
        "load_spreads",
        lambda **_kwargs: {"groups": [{"routes": [shown]}], "rows": [shown]},
    )

    legs = _desired_legs(snapshot, limit=2)

    assert legs == {
        ("Binance", "Futures", "SHOWN/USDT:USDT"),
        ("Bybit", "Futures", "SHOWN/USDT:USDT"),
    }
    assert ("Mexc", "Futures", "HIDDEN/USDT:USDT") not in legs


def test_leftover_budget_still_covers_the_widest_unshown_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = _row("SHOWN", spread=2.0)
    other = _row("OTHER", spread=50.0, long_venue="Mexc")

    snapshot = tmp_path / "api_discovery_latest.json"
    snapshot.write_text(
        json.dumps({"api_discovered_rows": [shown, other], "dex_discovered_rows": []}),
        encoding="utf-8",
    )

    from spreadboard import api_spreads

    monkeypatch.setattr(
        api_spreads,
        "load_spreads",
        lambda **_kwargs: {"groups": [{"routes": [shown]}], "rows": [shown]},
    )

    legs = _desired_legs(snapshot, limit=8)

    # The board's legs come first, then the budget spills onto the next widest.
    assert ("Binance", "Futures", "SHOWN/USDT:USDT") in legs
    assert ("Mexc", "Futures", "OTHER/USDT:USDT") in legs
