from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from spreadarb.api_discovery import sources
from spreadarb.api_discovery.identity import WatchAsset
from spreadarb.api_discovery.models import MarketQuote
from spreadarb.dex import velora_quotes
from spreadboard import fast_quotes


def _payload(*, source_usd: str, destination_usd: str, destination_amount: str) -> dict:
    return {
        "priceRoute": {
            "srcUSD": source_usd,
            "destUSD": destination_usd,
            "destAmount": destination_amount,
            "gasCostUSD": "0.01",
            "bestRoute": [
                {
                    "swaps": [
                        {"swapExchanges": [{"exchange": "uniswapv3"}]}
                    ]
                }
            ],
        }
    }


def test_velora_round_trip_preserves_exact_chain_identity_and_usdt_basis() -> None:
    def get(url: str, _headers: dict[str, str], _timeout: float) -> dict:
        query = parse_qs(urlparse(url).query)
        if query["srcToken"][0].casefold() == velora_quotes.USDT_BY_CHAIN["56"][0]:
            return _payload(
                source_usd="50",
                destination_usd="49.9",
                destination_amount=str(2_500 * 10**18),
            )
        return _payload(
            source_usd="49.9",
            destination_usd="49.5",
            destination_amount="49500000000000000000",
        )

    buy = velora_quotes.quote_usdt_to_token(
        chain="56",
        token_address="0xabc",
        token_decimals=18,
        notional_usdt=Decimal(50),
        http_get=get,
    )
    sell = velora_quotes.quote_token_to_usdt(
        chain="56",
        token_address="0xabc",
        token_decimals=18,
        token_quantity=Decimal(buy["out_qty"]),
        http_get=get,
    )

    assert buy["status"] == sell["status"] == "ok"
    assert buy["from_token_symbol"] == sell["to_token_symbol"] == "USDT"
    assert Decimal(str(buy["dex_buy_price_usd"])) == Decimal("0.02")
    assert Decimal(str(sell["dex_sell_price_usd"])) == Decimal("0.0198")
    assert buy["route_plan"] == ["uniswapv3"]


def test_velora_discovery_source_pairs_exact_evm_quote_with_futures() -> None:
    calls = 0

    def get(url: str, _headers: dict[str, str], _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        query = parse_qs(urlparse(url).query)
        if query["srcToken"][0].casefold() == velora_quotes.USDT_BY_CHAIN["56"][0]:
            return _payload(
                source_usd="50",
                destination_usd="50",
                destination_amount=str(2_500 * 10**18),
            )
        return _payload(
            source_usd="50",
            destination_usd="49",
            destination_amount=str(49 * 10**18),
        )

    reference = MarketQuote(
        token="TEST",
        venue="Gate",
        market_type="Futures",
        bid=0.021,
        ask=0.0211,
        bid_vwap=0.021,
        ask_vwap=0.0211,
        quote_ts_us=1,
        source_name="test",
        identity_key="eip155:56/erc20:0xabc",
        quote_asset="USDT",
    )
    context = sources.DiscoveryContext(
        tokens=("TEST",),
        watchlist={
            "TEST": WatchAsset(
                symbol="TEST",
                identity_key="eip155:56/erc20:0xabc",
                decimals=18,
                cex_enabled=True,
                dex_enabled=True,
                evm_contracts={56: "0xabc"},
            )
        },
        deadline_monotonic=None,
        reference_quotes=(reference,),
        min_spread_pct=1.0,
    )

    result = sources.VeloraQuoteSource(http_get_json=get).collect(context)

    assert calls == 2
    assert result.status.status == "ok"
    assert result.status.details["stable"] == "USDT"
    assert result.quotes[0].venue == "Velora DEX 56"
    assert result.quotes[0].token_address == "0xabc"
    assert result.rows


def test_fast_quote_worker_reprices_velora_leg_without_okx_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        velora_quotes,
        "quote_usdt_to_token",
        lambda **_kwargs: {
            "status": "ok",
            "out_qty": "2500",
            "dex_buy_price_usd": "0.02",
            "gas_estimate_usd": "0.01",
            "route_plan": ["uniswapv3"],
        },
    )
    monkeypatch.setattr(
        velora_quotes,
        "quote_token_to_usdt",
        lambda **_kwargs: {
            "status": "ok",
            "dex_sell_price_usd": "0.0198",
            "gas_estimate_usd": "0.01",
            "route_plan": ["uniswapv3"],
        },
    )
    row = {
        "token": "TEST",
        "long_venue": "Velora DEX 56",
        "long_market_type": "Spot",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "notes": {
            "identity": {
                "long": {
                    "chain_id": 56,
                    "token_address": "0xabc",
                    "decimals": 18,
                }
            }
        },
    }

    quote = fast_quotes._onchain_dex_leg_quote(
        row,
        "long",
        target_notional_usd=50,
        quote_both=False,
    )

    assert fast_quotes._fast_quote_lane(row) == "DEX-FUTURES"
    assert quote is not None
    assert quote["ask"] == pytest.approx(0.02)
    assert quote["quote_source"] == "velora_evm_quote"
