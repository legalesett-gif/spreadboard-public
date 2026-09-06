"""CCXT active=True must not override a venue's explicit trading disable."""

import json
import time

import ccxt
import pytest

from spreadarb.api_discovery import sources
from spreadboard import bulk_quotes, chart_catalog, fast_quotes


def _client(venue, enabled):
    if venue == "Bingx":
        client = ccxt.bingx()
        raw = {"symbol": "BTC-USDT", "currency": "USDT", "asset": "BTC",
               "apiStateOpen": "true", "apiStateClose": "true", "status": 1 if enabled else 25}
    else:
        client = ccxt.xt()
        raw = {"symbol": "btc_usdt", "pair": "btc_usdt", "contractType": "perpetual",
               "productType": "perpetual", "underlyingType": "U_BASED", "baseCoin": "btc",
               "quoteCoin": "usdt", "isOpenApi": True, "tradeSwitch": enabled,
               "openSwitch": True, "isDisplay": False, "contractSize": "1"}
    market = client.parse_market(raw)
    assert market["active"] is True  # Reproduce the installed adapter omission.
    assert market["swap"] is True
    client.set_markets([market])
    return client


@pytest.mark.parametrize("venue", ["Bingx", "XT"])
def test_catalog_reintroduces_reopened_contracts(monkeypatch, venue):
    client = _client(venue, False)
    monkeypatch.setattr(ccxt, chart_catalog.VENUE_IDS[venue], lambda *a: client)
    assert chart_catalog._load_venue(venue, "Futures") == []
    reopened = _client(venue, True)
    monkeypatch.setattr(ccxt, chart_catalog.VENUE_IDS[venue], lambda *a: reopened)
    assert [r["symbol"] for r in chart_catalog._load_venue(venue, "Futures")] == ["BTC/USDT:USDT"]


@pytest.mark.parametrize("venue", ["Bingx", "XT"])
def test_discovery_cannot_reintroduce_disabled_contracts(monkeypatch, venue):
    client = _client(venue, False)
    monkeypatch.setattr(sources, "_build_ccxt_exchange", lambda *a: client)
    calls = []
    monkeypatch.setattr(sources, "_ticker_quotes_for_symbols", lambda **kw: calls.append(kw["symbol_map"]) or [])
    context = sources.DiscoveryContext(tokens=(), watchlist={}, deadline_monotonic=None, all_platform_tokens=True)
    source = sources.CexCcxtSource(venues={venue: client.id}, market_type="Futures")
    source.collect(context)
    assert calls == [{}]
    client = _client(venue, True)
    source.collect(context)
    assert calls[-1] == {"BTC": "BTC/USDT:USDT"}


@pytest.mark.parametrize("venue", ["Bingx", "XT"])
@pytest.mark.parametrize("enabled", [False, True])
def test_bulk_rate_does_not_make_a_disabled_market_active(monkeypatch, venue, enabled):
    client = _client(venue, enabled)
    monkeypatch.setattr(client, "fetch_funding_rates", lambda *a, **kw: {
        "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001, "interval": "4h"}})
    client.has["fetchFundingRates"] = True
    monkeypatch.setattr(fast_quotes.FastQuoteRefresher, "_client", lambda *a: client)
    # If the unified path rejects the market, the native fallback must agree.
    monkeypatch.setattr(fast_quotes, "_json_url", lambda *a: [
        {"symbol": "btc_usdt", "funding_rate": "0.0001"}])
    rates = fast_quotes.FastQuoteRefresher()._bulk_funding_rates(venue)
    assert set(rates) == ({"BTC/USDT:USDT"} if enabled else set())


def test_bitget_bulk_requires_real_perpetual_metadata(monkeypatch):
    client = ccxt.bitget()
    client.set_markets([
        {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "base": "BTC", "quote": "USDT",
         "type": "swap", "swap": True, "spot": False, "settle": "USDT", "active": True},
        {"id": "GAIBUSDT", "symbol": "GAIB/USDT", "base": "GAIB", "quote": "USDT",
         "type": "spot", "swap": False, "spot": True, "active": True},
    ])
    monkeypatch.setattr(client, "publicMixGetV2MixMarketCurrentFundRate", lambda params: {
        "code": "00000", "data": [{"symbol": s, "fundingRate": "0.0001", "fundingRateInterval": "4"}
                                  for s in ("BTCUSDT", "GAIBUSDT", "BGTESTMEUSDT")]
        if params["productType"] == "USDT-FUTURES" else []})
    monkeypatch.setattr(fast_quotes.FastQuoteRefresher, "_client", lambda *a: client)
    assert set(fast_quotes.FastQuoteRefresher()._bulk_funding_rates("Bitget")) == {"BTC/USDT:USDT"}


@pytest.mark.parametrize("reader", ["load", "merge"])
def test_bitget_retention_prunes_old_raw_and_spot_keys(tmp_path, reader):
    path = tmp_path / "funding.json"
    keys = ["Bitget|BGTESTMEUSDT", "Bitget|GAIB/USDT", "Bitget|BTC/USDT:USDT", "Bitget|BTC/USDC:USDC"]
    path.write_text(json.dumps({"legs": {k: {"rate_pct": 0.01, "interval_hours": 4} for k in keys},
                                "leg_updated_at": dict.fromkeys(keys, time.time())}))
    if reader == "merge":
        bulk_quotes.sweep_funding([], cache_path=path, merge_existing=True)
        actual = json.loads(path.read_text())["legs"]
    else:
        actual = bulk_quotes.load_funding(cache_path=path)
    assert set(actual) == set(keys[2:])


def _bingx_api_disabled_client():
    client = ccxt.bingx()
    market = client.parse_market({
        'symbol': 'CAP-USDT', 'currency': 'USDT', 'asset': 'CAP',
        'status': 1, 'apiStateOpen': 'false', 'apiStateClose': 'false',
    })
    assert market['active'] is False
    client.set_markets([market])
    return client


def test_catalog_keeps_publicly_active_bingx_without_enabling_api_orders(monkeypatch):
    client = _bingx_api_disabled_client()
    monkeypatch.setattr(ccxt, 'bingx', lambda *args: client)
    rows = chart_catalog._load_venue('Bingx', 'Futures')
    assert [r['symbol'] for r in rows] == ['CAP/USDT:USDT']
    assert client.markets['CAP/USDT:USDT']['active'] is False
    assert client.markets['CAP/USDT:USDT']['info']['apiStateOpen'] == 'false'


def test_discovery_keeps_active_bingx_public_symbol_with_api_orders_disabled(monkeypatch):
    client = _bingx_api_disabled_client()
    monkeypatch.setattr(sources, '_build_ccxt_exchange', lambda *args: client)
    calls = []
    monkeypatch.setattr(sources, '_ticker_quotes_for_symbols', lambda **kw: calls.append(kw['symbol_map']) or [])
    context = sources.DiscoveryContext(tokens=(), watchlist={}, deadline_monotonic=None, all_platform_tokens=True)
    sources.CexCcxtSource(venues={'Bingx': 'bingx'}, market_type='Futures').collect(context)
    assert calls == [{'CAP': 'CAP/USDT:USDT'}]
    assert client.markets['CAP/USDT:USDT']['active'] is False


def test_current_public_funding_is_not_private_entry_permission(monkeypatch):
    client = _bingx_api_disabled_client()
    client.has['fetchFundingRates'] = True
    monkeypatch.setattr(client, 'fetch_funding_rates', lambda *args, **kwargs: {
        'CAP/USDT:USDT': {'symbol': 'CAP/USDT:USDT', 'fundingRate': -0.001256, 'interval': '4h'},
    })
    monkeypatch.setattr(fast_quotes.FastQuoteRefresher, '_client', lambda *args: client)
    rates = fast_quotes.FastQuoteRefresher()._bulk_funding_rates('Bingx')
    assert rates['CAP/USDT:USDT']['current_funding_pct'] == pytest.approx(-0.1256)
    assert rates['CAP/USDT:USDT']['funding_interval_hours'] == 4
    assert client.markets['CAP/USDT:USDT']['active'] is False


def test_unknown_native_status_cannot_override_inactive_unified_market():
    from spreadarb.market_status import market_open_for_opportunities
    assert not market_open_for_opportunities('Bingx', {'swap': True, 'active': False, 'info': {}})
    assert not market_open_for_opportunities('Bingx', {'swap': True, 'active': False, 'info': {'status': 25}})
