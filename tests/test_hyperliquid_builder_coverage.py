"""Exercise the installed adapter's real HIP-3 enumeration through our callers."""
from types import SimpleNamespace

import ccxt
import pytest
from spreadarb.public_clients import configure_public_market_client

from spreadboard import bulk_quotes, chart_catalog, fast_quotes


@pytest.fixture
def adapter(monkeypatch):
    client = ccxt.hyperliquid()
    requested = []
    names = [f"dex{i}" for i in range(1, 10)] + ["io"]

    def post(payload):
        if payload["type"] == "perpDexs":
            return [None, *({"name": name} for name in names)]
        name = payload["dex"]
        requested.append(name)
        return [{"collateralToken": 0, "universe": [{"name": f"{name}:GPRO"}]}, [{}]]

    def parse(rows):
        return [{"id": r["name"], "base": r["name"].upper().replace(":", "-"),
                 "symbol": r["name"].upper().replace(":", "-") + "/USDC:USDC",
                 "quote": "USDC", "settle": "USDC", "active": True,
                 "swap": True, "contractSize": 1} for r in rows]

    def load():
        client.markets = {m["symbol"]: m for m in client.fetch_hip3_markets()}
        return client.markets

    monkeypatch.setattr(client, "publicPostInfo", post)
    monkeypatch.setattr(client, "parse_markets", parse)
    monkeypatch.setattr(client, "load_markets", load)
    monkeypatch.setattr(ccxt, "hyperliquid", lambda *a, **kw: client)
    return client, requested, names


@pytest.mark.parametrize("caller", ["catalog", "bulk", "fast"])
def test_public_clients_include_tenth_builder_without_duplicate_requests(adapter, monkeypatch, caller):
    client, requested, names = adapter
    if caller == "catalog":
        rows = chart_catalog._load_venue("Hyperliquid", "Futures")
        assert any(r["symbol"] == "IO-GPRO/USDC:USDC" for r in rows)
    elif caller == "bulk":
        monkeypatch.setattr(bulk_quotes, "_CLIENTS", {})
        assert bulk_quotes._client("Hyperliquid", "Futures") is client
    else:
        assert fast_quotes.FastQuoteRefresher()._client("Hyperliquid", "Futures") is client
    assert requested == names
    assert "IO-GPRO/USDC:USDC" in client.markets


def test_explicit_dex_selection_and_other_venues_are_untouched(adapter):
    client, requested, _ = adapter
    client.options["fetchMarkets"]["hip3"]["dexes"] = ["io"]
    configure_public_market_client(client, "Hyperliquid")
    client.load_markets()
    assert requested == ["io"]
    other = SimpleNamespace(options={"fetchMarkets": {"types": ["swap"]}})
    configure_public_market_client(other, "Binance")
    assert other.options == {"fetchMarkets": {"types": ["swap"]}}
