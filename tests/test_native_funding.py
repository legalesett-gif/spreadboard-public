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
        monkeypatch, payload, {"ZHIPU-USDT": {"symbol": "ZHIPU/USDT:USDT", "swap": True}}
    )

    rates = refresher._native_bulk_funding_rates("HTX")

    # The interval is always stated, so a rate can never sit on a stale one.
    assert rates == {
        "ZHIPU/USDT:USDT": {"current_funding_pct": 0.0, "funding_interval_hours": 8.0}
    }


def test_kraken_absolute_rate_is_normalised_by_mark_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kraken publishes an absolute rate; the fraction is it over mark price."""
    payload = {
        "tickers": [
            {"symbol": "PF_XRPUSD", "fundingRate": -7.84115846425e-06, "markPrice": 1.06696914757}
        ]
    }
    refresher = _refresher(monkeypatch, payload, {"PF_XRPUSD": {"symbol": "XRP/USD:USD", "swap": True}})

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
        monkeypatch, payload, {"ZHIPU-USDT": {"symbol": "ZHIPU/USDT:USDT", "swap": True}}
    )

    class _Empty(_Client):
        has = {"fetchFundingRates": True}

        def fetch_funding_rates(self):
            return {}

    monkeypatch.setattr(
        refresher, "_client", lambda *_a, **_k: _Empty({"ZHIPU-USDT": {"symbol": "ZHIPU/USDT:USDT", "swap": True}})
    )

    rates = refresher._bulk_funding_rates("HTX")

    assert rates["ZHIPU/USDT:USDT"]["current_funding_pct"] == pytest.approx(0.1)


def test_every_native_source_declares_the_fields_the_parser_reads() -> None:
    for venue, spec in NATIVE_FUNDING_SOURCES.items():
        assert spec.get("url"), venue
        assert "path" in spec, venue
        assert spec.get("symbol"), venue
        assert spec.get("rate"), venue


def test_kucoin_granularity_is_milliseconds_not_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """14400000 is four hours. Read as hours it made READY read 0.82%/day."""
    payload = {
        "data": [
            {"symbol": "READYUSDTM", "fundingFeeRate": 0.000931, "fundingRateGranularity": 14400000}
        ]
    }
    refresher = _refresher(monkeypatch, payload, {"READYUSDTM": {"symbol": "READY/USDT:USDT", "swap": True}})

    entry = refresher._native_bulk_funding_rates("Kucoin Futures")["READY/USDT:USDT"]

    assert entry["funding_interval_hours"] == pytest.approx(4.0)


def test_a_rate_without_an_interval_never_inherits_a_stale_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WhiteBIT publishes no interval.

    A fresh rate merged onto the 1h interval left from an old scan made DEXE
    read 4.27%/day against a real 0.02%. When the venue is silent the standard
    8h applies, so rate and interval always describe the same thing.
    """
    from spreadboard.fast_quotes import DEFAULT_FUNDING_INTERVAL_HOURS

    payload = {"data": [{"contract_code": "DEXE-USDT", "funding_rate": "-0.0015976"}]}
    refresher = _refresher(monkeypatch, payload, {"DEXE-USDT": {"symbol": "DEXE/USDT:USDT", "swap": True}})

    entry = refresher._native_bulk_funding_rates("HTX")["DEXE/USDT:USDT"]

    assert entry["funding_interval_hours"] == DEFAULT_FUNDING_INTERVAL_HOURS


def test_aster_bulk_rate_uses_its_published_hourly_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Aster(_Client):
        has = {"fetchFundingRates": True}

        def __init__(self) -> None:
            super().__init__({"BTWUSDT": {"symbol": "BTW/USDT:USDT", "swap": True}})
            self.markets = {
                "BTW/USDT:USDT": {
                    "id": "BTWUSDT",
                    "symbol": "BTW/USDT:USDT",
                    "swap": True,
                }
            }

        def fetch_funding_rates(self):
            return {
                "BTW/USDT:USDT": {
                    "symbol": "BTW/USDT:USDT",
                    "fundingRate": 0.00007305,
                    "nextFundingTimestamp": 1_786_561_200_000,
                }
            }

    refresher = FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda *_a, **_k: _Aster())
    monkeypatch.setattr(
        fast_quotes,
        "_json_url",
        lambda _url: [{"symbol": "BTWUSDT", "fundingIntervalHours": 1}],
    )

    entry = refresher._bulk_funding_rates("Aster")["BTW/USDT:USDT"]

    assert entry["current_funding_pct"] == pytest.approx(0.007305)
    assert entry["funding_interval_hours"] == 1.0


def test_an_id_shared_by_spot_and_perp_resolves_to_the_perp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XT's `night_usdt` is both NIGHT/USDT and its perpetual.

    Taking the first match filed the funding rate against the spot symbol,
    where nothing on the board ever looks it up.
    """
    payload = [{"symbol": "night_usdt", "funding_rate": "0.00005123"}]
    by_id = {
        "night_usdt": [
            {"symbol": "NIGHT/USDT", "swap": False},
            {"symbol": "NIGHT/USDT:USDT", "swap": True},
        ]
    }
    refresher = _refresher(monkeypatch, payload, by_id)

    rates = refresher._native_bulk_funding_rates("XT")

    assert "NIGHT/USDT:USDT" in rates
    assert "NIGHT/USDT" not in rates


def test_a_spot_only_id_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [{"symbol": "night_usdt", "funding_rate": "0.0001"}]
    refresher = _refresher(monkeypatch, payload, {"night_usdt": {"symbol": "NIGHT/USDT"}})

    assert refresher._native_bulk_funding_rates("XT") == {}


def test_ourbit_symbols_convert_without_a_ccxt_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ourbit is an MEXC white-label with no CCXT adapter.

    There are no markets to map ids through, so BASE_QUOTE converts directly.
    """
    payload = {
        "data": [
            {"symbol": "BTC_USDT", "fundingRate": 3.4e-05, "collectCycle": 8},
            {"symbol": "QNTX_USDT", "fundingRate": 0.0014, "collectCycle": 8},
        ]
    }
    refresher = FastQuoteRefresher()
    monkeypatch.setattr(fast_quotes, "_json_url", lambda _url: payload)

    rates = refresher._native_bulk_funding_rates("Ourbit")

    assert rates["BTC/USDT:USDT"]["current_funding_pct"] == pytest.approx(0.0034)
    assert rates["BTC/USDT:USDT"]["funding_interval_hours"] == 8.0
    assert "QNTX/USDT:USDT" in rates
