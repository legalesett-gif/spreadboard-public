"""Do not discard the live schedule retained in CCXT's raw funding info."""

from types import SimpleNamespace

import ccxt
import pytest

from spreadboard import bulk_quotes, fast_quotes, funding_catalog


@pytest.mark.parametrize("reader", ["bulk", "point"])
@pytest.mark.parametrize(("token", "hours"), [("ONG", 1), ("ACE", 4), ("BTC", 8)])
def test_bingx_schedule_survives_real_ccxt_parse_into_pair_carry(monkeypatch, reader, token, hours):
    symbol = f"{token}/USDT:USDT"
    market = {"id": f"{token}-USDT", "symbol": symbol, "swap": True, "settle": "USDT",
              "info": {"fundingIntervalHours": 8}}
    raw = {"symbol": market["id"], "lastFundingRate": "-0.00096900",
           "fundingIntervalHours": hours, "nextFundingTime": 1788642000000,
           "indexPrice": "0.09559"}
    parser = ccxt.bingx()
    parser.markets_by_id = {market["id"]: [market]}
    parsed = parser.parse_funding_rate(raw, market)
    client = SimpleNamespace(
        has={"fetchFundingRates": True, "fetchFundingRate": True},
        markets={symbol: market},
        fetch_funding_rates=lambda: {symbol: parsed},
        fetch_funding_rate=lambda _symbol: parsed,
    )
    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda *a: client)
    fields = (refresher._bulk_funding_rates("Bingx")[symbol] if reader == "bulk"
              else fast_quotes._ccxt_current_funding(client, symbol, venue="Bingx"))
    assert fields["funding_interval_hours"] == hours
    assert fields["next_funding_ts_us"] == 1788642000000000
    if reader == "bulk":
        assert fields["funding_interval_assumed"] is False
    entry = bulk_quotes._funding_entry(fields)
    route = {"long_venue": "Bingx", "long_market_type": "Futures",
             "long_market_symbol": symbol, "short_venue": "Bybit",
             "short_market_type": "Futures", "short_market_symbol": symbol}
    carry = funding_catalog._live_current_value(route, {
        f"Bingx|{symbol}": entry,
        f"Bybit|{symbol}": {"rate_pct": -0.024649, "interval_hours": 1},
    })
    assert carry == pytest.approx(-0.024649 * 24 + 0.0969 * 24 / hours)
    if token == "ONG":
        assert carry > 0  # The old assumed 8h made this receiving pair negative.


@pytest.mark.parametrize("reader", ["bulk", "point"])
@pytest.mark.parametrize("minutes", [240, 480])
def test_whitebit_minute_schedule_survives_real_ccxt_parse(monkeypatch, reader, minutes):
    symbol = "LA/USDT:USDT"
    market = {"id": "LA_PERP", "symbol": symbol, "swap": True}
    raw = {"ticker_id": "LA_PERP", "funding_rate": "0.00005",
           "stock_currency": "LA", "money_currency": "USDT", "product_type": "Perpetual",
           "funding_interval_minutes": minutes,
           "next_funding_rate_timestamp": "1788652800000"}
    parser = ccxt.whitebit()
    parser.markets_by_id = {market["id"]: [market]}
    parsed = parser.parse_funding_rate(raw, market)
    client = SimpleNamespace(
        has={"fetchFundingRates": True, "fetchFundingRate": True},
        markets={symbol: market},
        fetch_funding_rates=lambda: {symbol: parsed},
        fetch_funding_rate=lambda _symbol: parsed,
    )
    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda *a: client)
    fields = (refresher._bulk_funding_rates("WhiteBIT")[symbol] if reader == "bulk"
              else fast_quotes._ccxt_current_funding(client, symbol, venue="WhiteBIT"))
    assert fields["funding_interval_hours"] == minutes / 60
    if reader == "bulk":
        assert fields["funding_interval_assumed"] is False
    entry = bulk_quotes._funding_entry(fields)
    assert entry["rate_pct"] * 24 / entry["interval_hours"] == pytest.approx(
        0.03 if minutes == 240 else 0.015
    )


@pytest.mark.parametrize("minutes", [0, 1440, "bad"])
def test_invalid_minute_schedule_does_not_become_published_carry(minutes):
    assert fast_quotes._market_interval_hours({"info": {"funding_interval_minutes": minutes}}) is None


@pytest.mark.parametrize("venue", ["Binance", "Aster"])
@pytest.mark.parametrize("hours", [1, 4, 8])
def test_live_funding_info_overrides_stale_market_schedule(monkeypatch, venue, hours):
    symbol = "ONG/USDT:USDT"
    client = SimpleNamespace(
        has={"fetchFundingRates": True},
        markets={symbol: {"id": "ONGUSDT", "info": {"fundingIntervalHours": 8}}},
        fetch_funding_rates=lambda: {symbol: {"symbol": symbol, "fundingRate": 0.0001}},
    )
    calls = []

    def response(url):
        calls.append(url)
        return [{"symbol": "ONGUSDT", "fundingIntervalHours": hours}]

    refresher = fast_quotes.FastQuoteRefresher()
    monkeypatch.setattr(refresher, "_client", lambda *a: client)
    monkeypatch.setattr(fast_quotes, "_json_url", response)
    fields = refresher._bulk_funding_rates(venue)[symbol]
    assert fields["funding_interval_hours"] == hours
    assert fields["funding_interval_assumed"] is False
    host = "fapi.binance.com" if venue == "Binance" else "fapi.asterdex.com"
    assert calls == [f"https://{host}/fapi/v1/fundingInfo"]
