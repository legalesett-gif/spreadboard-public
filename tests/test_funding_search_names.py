from spreadboard import funding_catalog, token_metadata


def test_english_name_resolves_without_changing_exact_market_identity(monkeypatch):
    route = {"token": "龙虾", "route_kind": "SPOT-FUTURES",
             "long_venue": "Mexc", "short_venue": "Aster",
             "long_market_type": "Spot", "short_market_type": "Futures",
             "long_market_symbol": "龙虾/USDT", "short_market_symbol": "龙虾/USDT:USDT"}
    payloads = {"龙虾": {"routes": [route]}}
    rows = list(funding_catalog._iter_routes(
        route_kind=None, symbol="Lobster", exchange=None, quote=None,
        include_retained=False, payloads=payloads,
    ))
    assert len(rows) == 1
    assert rows[0]["short_market_symbol"] == "龙虾/USDT:USDT"
    assert rows[0]["token"] == "龙虾"


def test_actual_ticker_wins_over_a_display_name_collision():
    assert token_metadata.resolve_search_symbol("lobster", {"LOBSTER", "龙虾"}) == "LOBSTER"
    assert token_metadata.resolve_search_symbol("lobster", {"龙虾"}) == "龙虾"
    assert token_metadata.resolve_search_symbol("lobster", {"BTC"}) == "LOBSTER"
