"""Bound temporary pairs without excluding a valid Funding page family."""

from spreadboard import funding_catalog


def row(key, *, long_type="Spot", short_type="Futures", spread=1.0, **flags):
    return {
        "token": "CATE", "route_key": key,
        "route_kind": "FUTURES" if long_type == short_type == "Futures" else "SPOT-FUTURES" if short_type == "Futures" else "SPOT",
        "long_venue": key, "long_market_type": long_type,
        "long_market_symbol": f"CATE/USDT{':USDT' if long_type == 'Futures' else ''}",
        "short_venue": "Bingx", "short_market_type": short_type,
        "short_market_symbol": "CATE/USDT:USDT",
        "funding_apr_pct": 10.0, "displayed_open_spread_pct": spread, **flags,
    }


def test_futures_long_cannot_displace_the_spot_funding_lane():
    spot = row("spot")
    future = row("future", long_type="Futures", spread=2.0)
    pure_spot = row("pure_spot", short_type="Spot", spread=3.0)
    result = funding_catalog._catalog_funding_routes([spot, future, pure_spot])
    assert {r["route_key"] for r in result} == {"spot", "future"}


def test_ineligible_widest_long_cannot_displace_a_valid_funding_route():
    good = row("good")
    bad = row("bad", spread=4.0, identity_mismatch=True)
    result = funding_catalog._catalog_funding_routes([good, bad])
    assert list(result) == [good]


def test_zero_budget_build_covers_positive_token_after_500(monkeypatch):
    tokens = [f"TOKEN{i:03}" for i in range(501)] + ["CATE"]
    markets = [{"token": t, "market_type": "Futures"} for t in tokens]
    monkeypatch.setattr(funding_catalog.chart_catalog, "load", lambda: {"markets": markets})
    monkeypatch.setattr(funding_catalog, "CATALOG_TOKEN_BUDGET", 0)
    monkeypatch.setattr(funding_catalog, "_CACHE_PAYLOADS", {})
    monkeypatch.setattr(funding_catalog, "_CACHE_RESTORE_ATTEMPTED", True)
    monkeypatch.setattr(funding_catalog, "_CACHE_BUILDING", False)
    monkeypatch.setattr(funding_catalog, "_persist_cache", lambda _: None)

    def build(selected, *, route_reducer, **_kwargs):
        assert set(selected) == set(tokens)
        return {t: {"token": t, "routes": route_reducer([row(t)])} for t in selected}

    monkeypatch.setattr(funding_catalog.catalog_pairs, "for_tokens", build)
    result = funding_catalog.refresh_cache()
    assert len(result) == 502
    assert result["CATE"]["routes"]
