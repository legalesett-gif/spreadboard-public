"""A zero funding total is only true when the market was proven to exist.

The ANSEM position displayed ``$0.00`` as an *exact* figure for days. Nothing
errored: ``fetch_funding_history`` filters the account ledger by coin, the
namespace resolved to the main dex instead of ``para:ANSEM``, every row was
filtered out, and an empty list is indistinguishable from a position that
genuinely never settled. The sync wrote ``status: ok, amount_usd: 0`` and the
read side faithfully reported a confident zero.

The mark path already refused this: ``_context`` raises
``hyperliquid_market_not_found``. The funding path had no such proof. So an
empty result now has to earn its zero -- on Hyperliquid and on every ccxt venue
-- and a market that cannot be found produces ``sync_error`` (rendered as
"unknown") instead of a number the operator would act on.

The probe runs *only* when the result is empty, so the normal path costs nothing.
"""

from __future__ import annotations

import pytest

from scripts import sync_portfolio_funding as sync

ADDRESS = "0x" + "ab" * 20


def _client(monkeypatch, *, universe: dict[str, list[str]], rows: list[dict]):
    client = sync.HyperliquidPublicAccountClient(ADDRESS)

    def fake_post(payload: dict):
        kind = payload.get("type")
        if kind == "perpDexs":
            return [None, {"name": "para"}, {"name": "xyz"}]
        if kind == "userFunding":
            return list(rows)
        if kind == "metaAndAssetCtxs":
            # Hyperliquid namespaces builder-dex universe names ("para:ANSEM"),
            # and leaves the main dex bare ("BTC"). Verified against the live
            # endpoint -- an unnamespaced fake would test the wrong contract.
            dex = str(payload.get("dex") or "")
            names = [f"{dex}:{name}" if dex else name for name in universe.get(dex, [])]
            return [
                {"universe": [{"name": name} for name in names]},
                [{"markPx": "1", "midPx": "1", "funding": "0.0001"} for _ in names],
            ]
        raise AssertionError(f"unexpected payload: {payload}")

    monkeypatch.setattr(client, "_post", fake_post)
    return client


def test_zero_funding_on_a_market_that_does_not_exist_is_refused(monkeypatch):
    """The exact ANSEM failure: right coin, wrong namespace, silent zero."""
    client = _client(monkeypatch, universe={"para": ["ANSEM"], "": ["BTC"]}, rows=[])
    with pytest.raises(RuntimeError, match="hyperliquid_market_not_found"):
        client.fetch_funding_history("GHOST-ANSEM/USDC:USDC", since=0, limit=500)


def test_zero_funding_on_a_real_market_stays_zero(monkeypatch):
    """A genuine zero must survive -- the guard must not invent errors."""
    client = _client(monkeypatch, universe={"para": ["ANSEM"]}, rows=[])
    assert client.fetch_funding_history("PARA-ANSEM/USDC:USDC", since=0, limit=500) == []


def test_builder_dex_funding_is_read_from_its_own_namespace(monkeypatch):
    """para:ANSEM rows must be found where ANSEM alone found nothing."""
    rows = [
        {"time": 1_700_000_000_000, "delta": {"coin": "para:ANSEM", "usdc": "1.25", "type": "funding"}},
        {"time": 1_700_003_600_000, "delta": {"coin": "ANSEM", "usdc": "99.0", "type": "funding"}},
    ]
    client = _client(monkeypatch, universe={"para": ["ANSEM"]}, rows=rows)
    got = client.fetch_funding_history("PARA-ANSEM/USDC:USDC", since=0, limit=500)
    assert [row["amount"] for row in got] == ["1.25"], "main-dex ANSEM must not leak in"


def test_market_lookup_refuses_an_unknown_coin(monkeypatch):
    """`market()` is the shared proof, so it must stop being a stub."""
    client = _client(monkeypatch, universe={"para": ["ANSEM"]}, rows=[])
    with pytest.raises(RuntimeError, match="hyperliquid_market_not_found"):
        client.market("PARA-NOTATOKEN/USDC:USDC")


def test_market_lookup_still_reports_the_quote(monkeypatch):
    client = _client(monkeypatch, universe={"para": ["ANSEM"]}, rows=[])
    assert client.market("PARA-ANSEM/USDC:USDC") == {"quote": "USDC"}


class _Exchange:
    """Minimal ccxt stand-in recording whether the market probe ran."""

    id = "gate"

    def __init__(self, rows, *, market_ok: bool):
        self._rows = rows
        self._market_ok = market_ok
        self.market_calls = 0

    def fetch_funding_history(self, symbol, since=None, limit=None):
        return list(self._rows)

    def market(self, symbol):
        self.market_calls += 1
        if not self._market_ok:
            raise ValueError(f"BadSymbol: {symbol}")
        return {"quote": "USDT"}


def test_empty_ccxt_funding_history_must_prove_the_market():
    """Call-site guard: the wrapper every ccxt venue shares."""
    exchange = _Exchange([], market_ok=False)
    with pytest.raises(ValueError, match="BadSymbol"):
        sync.fetch_private_funding(exchange, "GUA/USDT:USDT", 0)
    assert exchange.market_calls == 1


def test_empty_ccxt_funding_history_on_a_real_market_is_zero():
    exchange = _Exchange([], market_ok=True)
    assert sync.fetch_private_funding(exchange, "GUA/USDT:USDT", 0) == []
    assert exchange.market_calls == 1


def test_populated_funding_history_skips_the_market_probe():
    """The hot path must not pay for the guard."""
    rows = [{"timestamp": 1_700_000_000_000, "amount": "0.5", "code": "USDT"}]
    exchange = _Exchange(rows, market_ok=True)
    assert len(sync.fetch_private_funding(exchange, "GUA/USDT:USDT", 0)) == 1
    assert exchange.market_calls == 0
