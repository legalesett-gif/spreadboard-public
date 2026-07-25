"""Official exchange market links for read-only leg navigation."""

from __future__ import annotations

from urllib.parse import quote


def exchange_market_url(
    *,
    venue: str | None,
    market_type: str | None,
    market_symbol: str | None,
    token: str | None = None,
) -> str | None:
    """Return an official market page for a normalized exchange leg."""

    venue_key = str(venue or "").casefold().replace(" ", "")
    kind = str(market_type or "").casefold()
    base, quote_asset = _market_parts(market_symbol, token)
    if not venue_key or not base:
        return None

    pair = f"{base}{quote_asset}"
    dashed = f"{base}-{quote_asset}"
    underscored = f"{base}_{quote_asset}"
    is_futures = kind in {"future", "futures", "swap", "perpetual", "perp"}

    if venue_key == "aster":
        return f"https://www.asterdex.com/en/trade/pro/futures/{pair}"
    if venue_key == "binance":
        if is_futures:
            return f"https://www.binance.com/en/futures/{pair}"
        return f"https://www.binance.com/en/trade/{underscored}?type=spot"
    if venue_key in {"bingx", "bingxpro"}:
        if is_futures:
            return f"https://bingx.com/en-us/perpetual/{dashed}"
        return f"https://bingx.com/en-us/spot/{pair}"
    if venue_key == "bitget":
        if is_futures:
            return f"https://www.bitget.com/futures/usdt/{pair}"
        return f"https://www.bitget.com/spot/{pair}"
    if venue_key in {"bybit", "bybitfi"}:
        if is_futures:
            return f"https://www.bybit.com/trade/usdt/{pair}"
        return f"https://www.bybit.com/trade/spot/{base}/{quote_asset}"
    if venue_key == "coinbase":
        return f"https://www.coinbase.com/advanced-trade/spot/{dashed}"
    if venue_key in {"gate", "gatefi", "gateio"}:
        if is_futures:
            return f"https://www.gate.com/futures/{quote_asset}/{underscored}"
        return f"https://www.gate.com/trade/{underscored}"
    if venue_key == "hyperliquid":
        hyperliquid_asset = str(market_symbol or base).split("/", 1)[0]
        return f"https://app.hyperliquid.xyz/trade/{quote(hyperliquid_asset, safe=':')}"
    if venue_key == "kraken":
        return f"https://pro.kraken.com/app/trade/{dashed.lower()}"
    if venue_key == "krakenfutures":
        return f"https://pro.kraken.com/app/trade/futures/{base.lower()}"
    if venue_key in {"kucoin", "kucoinspot"}:
        return f"https://www.kucoin.com/trade/{dashed}"
    if venue_key == "kucoinfutures":
        return f"https://www.kucoin.com/futures/trade/{pair}M"
    if venue_key in {"mexc", "mexcglobal"}:
        if is_futures:
            return f"https://futures.mexc.com/exchange/{underscored}"
        return f"https://www.mexc.com/exchange/{underscored}"
    if venue_key in {"okx", "okex"}:
        if kind == "dex":
            return "https://www.okx.com/web3/dex"
        if is_futures:
            return f"https://www.okx.com/trade-swap/{dashed.lower()}-swap"
        return f"https://www.okx.com/trade-spot/{dashed.lower()}"
    if venue_key in {"htx", "huobi"}:
        if is_futures:
            return f"https://www.htx.com/futures/linear_swap/exchange#{pair}"
        return f"https://www.htx.com/trade/{base.lower()}_{quote_asset.lower()}"
    if venue_key == "phemex":
        if is_futures:
            return f"https://phemex.com/trade/{pair}"
        return f"https://phemex.com/spot/trade/{pair}"
    if venue_key in {"okxdex", "okxdexaggregator"}:
        return "https://www.okx.com/web3/dex"
    return None


def _market_parts(market_symbol: str | None, token: str | None) -> tuple[str, str]:
    symbol = str(market_symbol or "").strip()
    if "/" in symbol:
        base, remainder = symbol.split("/", 1)
        quote_asset = remainder.split(":", 1)[0]
    else:
        base = str(token or symbol).strip()
        quote_asset = "USDT"
    base = base.upper().strip()
    quote_asset = quote_asset.upper().strip() or "USDT"
    return base, quote_asset
