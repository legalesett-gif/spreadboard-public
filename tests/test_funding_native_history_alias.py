"""Native HIP-3 routes must reach the same exact settlement history as CCXT."""
from types import SimpleNamespace

import pytest

from spreadboard import venue_funding_history as history


def route(symbol="para:ANSEM"):
    return {"long_venue": "Mexc", "long_market_type": "Spot",
            "long_market_symbol": "ANSEM/USDT", "short_venue": "Hyperliquid",
            "short_market_type": "Futures", "short_market_symbol": symbol}


def test_native_alias_uses_canonical_history_instead_of_empty_native_record(monkeypatch):
    windows = {"1d": .33492917, "7d": 3.73368008, "30d": None}
    legs = {"Hyperliquid|PARA-ANSEM/USDC:USDC": windows,
            "Hyperliquid|para:ANSEM": dict.fromkeys(windows),
            "Hyperliquid|ANSEM/USDC:USDC": dict.fromkeys(windows, 99)}
    monkeypatch.setattr(history, "load", lambda: legs)
    canonical_status = {"status": "ok", "window_details": {
        key: {"complete": value is not None, "latest_event_at": 1789250400013}
        for key, value in windows.items()}}
    statuses = {"Hyperliquid|PARA-ANSEM/USDC:USDC": canonical_status,
                "Hyperliquid|para:ANSEM": {"status": "symbol_not_indexed"}}
    monkeypatch.setitem(history._CACHE, "leg_status", statuses)
    monkeypatch.setitem(history._CACHE, "legs", legs)
    monkeypatch.setattr(history, "_load_raw", lambda: {"legs": legs, "leg_status": statuses})
    assert history.route_windows(route()) == windows
    assert history.route_windows(route("PARA-ANSEM/USDC:USDC")) == windows
    assert history.route_history_status(route())["sides"]["short"]["status"] == "ok"
    assert history.route_windows_last_complete(route())["7d"]["net"] == windows["7d"]
    assert "30d" not in history.route_windows_last_complete(route())


@pytest.mark.parametrize("other", ["ANSEM/USDC:USDC", "XYZ-ANSEM/USDC:USDC"])
def test_native_alias_never_borrows_another_namespace(other):
    assert history.route_windows(route(), legs={"Hyperliquid|" + other: {"1d": 99}})["1d"] is None


def test_native_alias_does_not_guess_a_quote_when_two_contracts_match():
    assert history._history_symbol("Hyperliquid", "para:ANSEM", [
        "PARA-ANSEM/USDC:USDC", "PARA-ANSEM/USDT:USDT",
    ]) is None
    assert history.route_windows(route(), legs={
        "Hyperliquid|para:ANSEM": {"1d": 99},
        "Hyperliquid|PARA-ANSEM/USDC:USDC": {"1d": 1},
        "Hyperliquid|PARA-ANSEM/USDT:USDT": {"1d": 2},
    })["1d"] is None


def test_collector_resolves_native_name_before_ccxt_symbol_validation(monkeypatch):
    calls = []
    client = SimpleNamespace(
        has={"fetchFundingRateHistory": True}, symbols=["PARA-ANSEM/USDC:USDC"],
        fetch_funding_rate_history=lambda symbol, **kw: calls.append(symbol) or [],
    )
    out = history.leg_history_outcome("Hyperliquid", "para:ANSEM", client_factory=lambda _: client)
    assert calls == ["PARA-ANSEM/USDC:USDC"]
    assert out["status"] == "no_history_rows"


def test_canonical_symbol_is_never_rewritten():
    assert history._history_symbol("Hyperliquid", "PARA-ANSEM/USDT:USDT", [
        "PARA-ANSEM/USDC:USDC",
    ]) == "PARA-ANSEM/USDT:USDT"
