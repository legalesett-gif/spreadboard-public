"""Funding for venues CCXT cannot bulk-fetch.

Eight of eighteen futures venues returned nothing from fetch_funding_rates, so
their legs kept whatever the 20-40 minute discovery scan captured and were
never corrected. HTX's ZHIPU sat at -0.6677%/8h while the exchange had moved to
0.0, putting ZHIPU on the board at 2.43%/day against a real 0.41%.
"""

from __future__ import annotations

from typing import Any

import pytest

from spreadboard import fast_quotes
from spreadboard.fast_quotes import FastQuoteRefresher, NATIVE_FUNDING_SOURCES


class _Client:
    def __init__(self, by_id: dict[str, Any]) -> None:
        self.markets_by_id = by_id
        self.markets = {"x": {}}


def _refresher(monkeypatch: pytest.MonkeyPatch, payload: Any, by_id: dict[str, Any]):
    refresher = FastQuoteRefresher()
    monkeypatch.setattr(fast_quotes, "_json_url", lambda _url: payload)
    monkeypatch.setattr(refresher, "_client", lambda *_a, **_k: _Client(by_id))
    return refresher


def test_htx_zhipu_zero_rate_is_published_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine 0.0 is data. Treating it as missing is what froze ZHIPU."""
    payload = {"data": [{"contract_code": "ZHIPU-USDT", "funding_rate": "0E-18"}]}
    refresher = _refresher(
        monkeypatch, payload, {"ZHIPU-USDT": {"symbol": "ZHIPU/USDT:USDT"}}
    )

    rates = refresher._native_bulk_funding_rates("HTX")

    assert rates == {"ZHIPU/USDT:USDT": {"current_funding_pct": 0.0}}


def test_kraken_absolute_rate_is_normalised_by_mark_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kraken publishes an absolute rate; the fraction is it over mark price."""
    payload = {
        "tickers": [
            {"symbol": "PF_XRPUSD", "fundingRate": -7.84115846425e-06, "markPrice": 1.06696914757}
        ]
    }
    refresher = _refresher(monkeypatch, payload, {"PF_XRPUSD": {"symbol": "XRP/USD:USD"}})

    rates = refresher._native_bulk_funding_rates("Kraken Futures")

    entry = rates["XRP/USD:USD"]
    assert entry["current_funding_pct"] == pytest.approx(-0.000734, abs=1e-5)
    assert entry["funding_interval_hours"] == 1.0


def test_unmapped_native_symbols_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"data": [{"contract_code": "NOSUCH-USDT", "funding_rate": "0.001"}]}
    refresher = _refresher(monkeypatch, payload, {})

    assert refresher._native_bulk_funding_rates("HTX") == {}


def test_a_venue_without_a_native_source_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresher = _refresher(monkeypatch, {}, {})

    assert refresher._native_bulk_funding_rates("Binance") == {}


def test_ccxt_returning_empty_falls_back_to_the_native_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty answer and no answer both leave the legs frozen at scan time."""
    payload = {"data": [{"contract_code": "ZHIPU-USDT", "funding_rate": "0.001"}]}
    refresher = _refresher(
        monkeypatch, payload, {"ZHIPU-USDT": {"symbol": "ZHIPU/USDT:USDT"}}
    )

    class _Empty(_Client):
        has = {"fetchFundingRates": True}

        def fetch_funding_rates(self):
            return {}

    monkeypatch.setattr(
        refresher, "_client", lambda *_a, **_k: _Empty({"ZHIPU-USDT": {"symbol": "ZHIPU/USDT:USDT"}})
    )

    rates = refresher._bulk_funding_rates("HTX")

    assert rates["ZHIPU/USDT:USDT"]["current_funding_pct"] == pytest.approx(0.1)


def test_every_native_source_declares_the_fields_the_parser_reads() -> None:
    for venue, spec in NATIVE_FUNDING_SOURCES.items():
        assert spec.get("url"), venue
        assert spec.get("path"), venue
        assert spec.get("symbol"), venue
        assert spec.get("rate"), venue
