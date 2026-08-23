#!/usr/bin/env python3
# ruff: noqa: BLE001, S110, S112
"""Broad read-only DEX spot quote scan against CEX spot references.

The always-on worker runs this in bounded per-source research mode, while manual
deep sweeps can raise the limits. Rows remain symbol-only research candidates
until identity, route feasibility, gas, and executor attestation are proven.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from spreadarb.api_discovery.models import (
    as_float,
    clean_error,
    now_us,
    spread_pct,
    utc_now_iso,
)
from spreadarb.api_discovery.sources import (
    USDC_ETHEREUM,
    USDC_SOLANA,
    _all_market_symbols,
    _build_ccxt_exchange,
)
from spreadarb.public_runtime import keychain

DEFAULT_OUTPUT = Path("/tmp/spreadarb_dex_spot_broad_scan.json")
DEFAULT_EVM_TOKEN_LIST_URL = (
    "https://cdn.jsdelivr.net/gh/Uniswap/default-token-list@main/src/tokens/mainnet.json"
)
FALLBACK_EVM_TOKEN_LIST_URLS = (
    DEFAULT_EVM_TOKEN_LIST_URL,
    "https://raw.githubusercontent.com/Uniswap/default-token-list/main/src/tokens/mainnet.json",
    "https://tokens.coingecko.com/uniswap/all.json",
)
JUPITER_TOKEN_URL = "https://api.jup.ag/tokens/v2/tag"
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
ZEROX_PRICE_URL = "https://api.0x.org/swap/allowance-holder/price"
ZEROX_CHAIN_ID = 1
STABLE_SYMBOLS = {
    "USDC",
    "USDT",
    "USD",
    "DAI",
    "FDUSD",
    "TUSD",
    "USDP",
    "PYUSD",
    "USDE",
    "USDS",
    "EUR",
    "EURC",
}
VENUES = {
    "Binance": "binance",
    "Bybit": "bybit",
    "Bitget": "bitget",
    "OKX": "okx",
    "Gate": "gateio",
    "Mexc": "mexc",
    "Kucoin": "kucoin",
    "Bingx": "bingx",
    "Coinbase": "coinbaseexchange",
    "Kraken": "kraken",
}


@dataclass(frozen=True, slots=True)
class TokenInfo:
    symbol: str
    name: str
    address: str
    decimals: int
    universe: str
    usd_price: float | None = None
    liquidity_usd: float | None = None


@dataclass(frozen=True, slots=True)
class CexQuote:
    venue: str
    symbol: str
    bid: float
    ask: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default="jupiter,0x", help="Comma-separated: jupiter,0x")
    parser.add_argument("--target-usd", type=float, default=500.0)
    parser.add_argument("--slippage-bps", type=int, default=50)
    parser.add_argument("--jupiter-limit", type=int, default=300)
    parser.add_argument("--zerox-limit", type=int, default=150)
    parser.add_argument("--min-jupiter-liquidity-usd", type=float, default=10_000.0)
    parser.add_argument("--max-screen-spread-pct", type=float, default=90.0)
    parser.add_argument("--include-extreme-symbol-matches", action="store_true")
    parser.add_argument("--rate-limit-s", type=float, default=1.05)
    parser.add_argument("--quote-timeout-s", type=float, default=8.0)
    parser.add_argument("--retry-429", type=int, default=1)
    parser.add_argument("--evm-token-list-url", default=DEFAULT_EVM_TOKEN_LIST_URL)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = monotonic()
    selected = {item.strip().lower() for item in args.sources.split(",") if item.strip()}
    cex = load_cex_quotes()
    cex_symbols = set(cex["quotes"])
    scans: list[dict[str, Any]] = []
    if "0x" in selected or "zerox" in selected:
        scans.append(scan_zerox(args, cex["quotes"], cex_symbols))
        payload = write_payload(args=args, started=started, cex=cex, cex_symbols=cex_symbols, scans=scans)
    if "jupiter" in selected:
        scans.append(scan_jupiter(args, cex["quotes"], cex_symbols))
        payload = write_payload(args=args, started=started, cex=cex, cex_symbols=cex_symbols, scans=scans)
    if not scans:
        payload = write_payload(args=args, started=started, cex=cex, cex_symbols=cex_symbols, scans=scans)
    summary = summarize(payload)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            "dex_spot_broad_scan "
            f"cex_symbols={summary['cex_unique_symbols_with_quotes']} "
            f"jupiter_attempted={summary.get('jupiter_quote_attempted_tokens', 0)} "
            f"zerox_attempted={summary.get('zerox_quote_attempted_tokens', 0)} "
            f"output={args.output_path}"
        )
    return 0


def write_payload(
    *,
    args: argparse.Namespace,
    started: float,
    cex: dict[str, Any],
    cex_symbols: set[str],
    scans: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "schema": "spreadarb.dex_spot_broad_scan.v1",
        "updated_at": utc_now_iso(),
        "output_path": str(args.output_path),
        "elapsed_seconds": round(monotonic() - started, 3),
        "target_usd": args.target_usd,
        "slippage_bps": args.slippage_bps,
        "cex": {
            "venue_market_counts": cex["venue_market_counts"],
            "venue_ticker_counts": cex["venue_ticker_counts"],
            "total_venue_token_listings": sum(cex["venue_market_counts"].values()),
            "total_usable_ticker_listings": sum(cex["venue_ticker_counts"].values()),
            "unique_symbols_with_quotes": len(cex_symbols),
            "errors": cex["errors"],
        },
        "scans": scans,
    }
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def load_cex_quotes() -> dict[str, Any]:
    quotes: dict[str, list[CexQuote]] = {}
    venue_market_counts: dict[str, int] = {}
    venue_ticker_counts: dict[str, int] = {}
    errors: list[str] = []
    for venue, exchange_id in VENUES.items():
        try:
            exchange = _build_ccxt_exchange(exchange_id, "Spot", 20.0)
            markets = exchange.load_markets()
            symbol_map = _all_market_symbols(markets, "Spot")
            venue_market_counts[venue] = len(symbol_map)
            tickers = fetch_exchange_tickers(exchange, list(symbol_map.values()))
            reverse = {symbol: token for token, symbol in symbol_map.items()}
            usable = 0
            for symbol, ticker in tickers.items():
                token = reverse.get(str(symbol))
                if not token:
                    continue
                bid, ask = ticker_bid_ask(ticker)
                if bid is None or ask is None:
                    continue
                quotes.setdefault(token.upper(), []).append(
                    CexQuote(venue=venue, symbol=str(symbol), bid=bid, ask=ask)
                )
                usable += 1
            venue_ticker_counts[venue] = usable
        except Exception as exc:
            errors.append(f"{venue}:cex_quote:{clean_error(exc)}")
    return {
        "quotes": quotes,
        "venue_market_counts": venue_market_counts,
        "venue_ticker_counts": venue_ticker_counts,
        "errors": errors[:20],
    }


def fetch_exchange_tickers(exchange: Any, symbols: list[str]) -> dict[str, Any]:
    try:
        if getattr(exchange, "has", {}).get("fetchTickers"):
            try:
                return dict(exchange.fetch_tickers(symbols))
            except TypeError:
                return dict(exchange.fetch_tickers())
            except Exception:
                return dict(exchange.fetch_tickers())
    except Exception:
        pass
    tickers: dict[str, Any] = {}
    for symbol in symbols:
        try:
            tickers[symbol] = exchange.fetch_ticker(symbol)
        except Exception:
            continue
    return tickers


def ticker_bid_ask(ticker: Any) -> tuple[float | None, float | None]:
    if not isinstance(ticker, dict):
        return None, None
    bid = as_float(ticker.get("bid"))
    ask = as_float(ticker.get("ask"))
    if bid is None or ask is None:
        midpoint = as_float(ticker.get("markPrice")) or as_float(ticker.get("last")) or as_float(
            ticker.get("close")
        )
        bid = bid if bid is not None else midpoint
        ask = ask if ask is not None else midpoint
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None, None
    return bid, ask


def scan_jupiter(args: argparse.Namespace, cex_quotes: dict[str, list[CexQuote]], cex_symbols: set[str]) -> dict[str, Any]:
    api_key = keychain("SPREADARB/jupiter/api_key") or ""
    headers = jupiter_headers(api_key)
    try:
        tokens_payload = fetch_json_with_retries(
            f"{JUPITER_TOKEN_URL}?{urlencode({'query': 'verified'})}",
            headers,
            timeout=60.0,
            retry_429=args.retry_429,
            rate_limit_s=args.rate_limit_s,
        )
    except Exception as exc:
        return {
            "source": "jupiter",
            "status": "failed",
            "verified_tokens": 0,
            "unique_symbols": 0,
            "duplicate_symbol_groups": 0,
            "crosslisted_unique_symbols_before_filters": 0,
            "candidate_tokens_after_filters": 0,
            "quote_attempted_tokens": 0,
            "quote_success_tokens": 0,
            "quote_error_tokens": 0,
            "rows": [],
            "row_count": 0,
            "positive_row_count": 0,
            "research_row_count_ge_1pct_lte_90pct": 0,
            "errors": [f"jupiter_token_list:{clean_error(exc)}"],
        }
    tokens = parse_jupiter_tokens(tokens_payload)
    unique, duplicate_symbols = unique_by_symbol(tokens)
    candidates = []
    for token in unique.values():
        if token.symbol not in cex_symbols or token.symbol in STABLE_SYMBOLS:
            continue
        if token.usd_price is None or token.usd_price <= 0:
            continue
        if token.liquidity_usd is not None and token.liquidity_usd < args.min_jupiter_liquidity_usd:
            continue
        screen = price_screen(token, cex_quotes[token.symbol], token.usd_price)
        if not args.include_extreme_symbol_matches and screen["max_abs_screen_spread_pct"] > args.max_screen_spread_pct:
            continue
        candidates.append((token, screen))
    candidates.sort(key=lambda item: item[1]["max_abs_screen_spread_pct"], reverse=True)
    # The verified-token catalogue uses the same keyed free quota as Quote.
    # Without a pause here, the first buy request immediately followed the
    # catalogue request and reliably consumed a 429 even though every later
    # buy/sell request was paced correctly.
    if candidates and args.rate_limit_s > 0:
        sleep(args.rate_limit_s)
    return quote_token_candidates(
        source="jupiter",
        candidates=candidates[: max(0, args.jupiter_limit)],
        cex_quotes=cex_quotes,
        quote_func=lambda token: quote_jupiter(token, api_key, args),
        rate_limit_s=args.rate_limit_s,
        retry_429=args.retry_429,
        universe_counts={
            "verified_tokens": len(tokens),
            "unique_symbols": len(unique),
            "duplicate_symbol_groups": len(duplicate_symbols),
            "crosslisted_unique_symbols_before_filters": sum(
                1 for token in unique.values() if token.symbol in cex_symbols and token.symbol not in STABLE_SYMBOLS
            ),
            "candidate_tokens_after_filters": len(candidates),
        },
    )


def scan_zerox(args: argparse.Namespace, cex_quotes: dict[str, list[CexQuote]], cex_symbols: set[str]) -> dict[str, Any]:
    api_key = keychain("SPREADARB/0x/api_key") or ""
    if not api_key:
        return {
            "source": "0x",
            "status": "not_configured",
            "evm_tokens": 0,
            "unique_symbols": 0,
            "duplicate_symbol_groups": 0,
            "crosslisted_unique_symbols_before_filters": 0,
            "candidate_tokens_after_filters": 0,
            "quote_attempted_tokens": 0,
            "quote_success_tokens": 0,
            "quote_error_tokens": 0,
            "rows": [],
            "row_count": 0,
            "positive_row_count": 0,
            "research_row_count_ge_1pct_lte_90pct": 0,
            "errors": ["0x_api_key_not_configured"],
        }
    try:
        token_list, token_list_url = fetch_evm_token_list(args.evm_token_list_url)
    except Exception as exc:
        return {
            "source": "0x",
            "status": "failed",
            "evm_tokens": 0,
            "unique_symbols": 0,
            "duplicate_symbol_groups": 0,
            "crosslisted_unique_symbols_before_filters": 0,
            "candidate_tokens_after_filters": 0,
            "quote_attempted_tokens": 0,
            "quote_success_tokens": 0,
            "quote_error_tokens": 0,
            "rows": [],
            "row_count": 0,
            "positive_row_count": 0,
            "research_row_count_ge_1pct_lte_90pct": 0,
            "errors": [f"evm_token_list:{clean_error(exc)}"],
        }
    tokens = parse_evm_tokens(token_list)
    unique, duplicate_symbols = unique_by_symbol(tokens)
    candidates = []
    for token in unique.values():
        if token.symbol not in cex_symbols or token.symbol in STABLE_SYMBOLS:
            continue
        screen = cex_internal_screen(cex_quotes[token.symbol])
        if not args.include_extreme_symbol_matches and screen["max_abs_screen_spread_pct"] > args.max_screen_spread_pct:
            continue
        candidates.append((token, screen))
    candidates.sort(key=lambda item: item[1]["max_abs_screen_spread_pct"], reverse=True)
    return quote_token_candidates(
        source="0x",
        candidates=candidates[: max(0, args.zerox_limit)],
        cex_quotes=cex_quotes,
        quote_func=lambda token: quote_zerox(token, api_key, args),
        rate_limit_s=args.rate_limit_s,
        retry_429=args.retry_429,
        universe_counts={
            "evm_tokens": len(tokens),
            "unique_symbols": len(unique),
            "duplicate_symbol_groups": len(duplicate_symbols),
            "crosslisted_unique_symbols_before_filters": sum(
                1 for token in unique.values() if token.symbol in cex_symbols and token.symbol not in STABLE_SYMBOLS
            ),
            "candidate_tokens_after_filters": len(candidates),
            "token_list_url": token_list_url,
        },
    )


def quote_token_candidates(
    *,
    source: str,
    candidates: list[tuple[TokenInfo, dict[str, Any]]],
    cex_quotes: dict[str, list[CexQuote]],
    quote_func: Any,
    rate_limit_s: float,
    retry_429: int,
    universe_counts: dict[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    attempted = 0
    succeeded = 0
    for token, screen in candidates:
        attempted += 1
        quote = None
        for attempt in range(max(1, retry_429 + 1)):
            try:
                quote = quote_func(token)
                break
            except HTTPError as exc:
                body = safe_error_body(exc)
                errors.append(f"{token.symbol}:http_{exc.code}:{body}")
                if exc.code != 429 or attempt >= retry_429:
                    break
                sleep(max(rate_limit_s * 5, 1.5))
            except (URLError, TimeoutError) as exc:
                errors.append(f"{token.symbol}:url:{str(exc)[:160]}")
                break
            except Exception as exc:
                errors.append(f"{token.symbol}:{clean_error(exc)}")
                break
        sleep(rate_limit_s)
        if quote is None:
            continue
        succeeded += 1
        rows.extend(compare_dex_to_cex(source, token, quote, cex_quotes[token.symbol], screen))
    positive_rows = [row for row in rows if as_float(row.get("executable_spread_pct")) and as_float(row["executable_spread_pct"]) > 0]
    actionable_research_rows = [
        row
        for row in rows
        if (as_float(row.get("executable_spread_pct")) or -999.0) >= 1.0
        and (as_float(row.get("executable_spread_pct")) or 999.0) <= 90.0
    ]
    rows.sort(key=lambda row: as_float(row.get("executable_spread_pct")) or -999.0, reverse=True)
    return {
        "source": source,
        **universe_counts,
        "quote_attempted_tokens": attempted,
        "quote_success_tokens": succeeded,
        "quote_error_tokens": attempted - succeeded,
        "rows": rows[:200],
        "row_count": len(rows),
        "positive_row_count": len(positive_rows),
        "research_row_count_ge_1pct_lte_90pct": len(actionable_research_rows),
        "errors": errors[:50],
        "status": "partial" if errors else "ok",
    }


def compare_dex_to_cex(
    source: str,
    token: TokenInfo,
    quote: dict[str, Any],
    refs: list[CexQuote],
    screen: dict[str, Any],
) -> list[dict[str, Any]]:
    best_bid = max(refs, key=lambda item: item.bid)
    best_ask = min(refs, key=lambda item: item.ask)
    dex_ask = quote["ask"]
    dex_bid = quote["bid"]
    now = now_us()
    rows = [
        build_row(
            token=token,
            source=source,
            direction="dex_long_sell_cex",
            long_venue=quote["venue"],
            short_venue=best_bid.venue,
            long_ask=dex_ask,
            short_bid=best_bid.bid,
            cex_symbol=best_bid.symbol,
            quote_ts_us=now,
            quote=quote,
            screen=screen,
        ),
        build_row(
            token=token,
            source=source,
            direction="cex_long_sell_dex",
            long_venue=best_ask.venue,
            short_venue=quote["venue"],
            long_ask=best_ask.ask,
            short_bid=dex_bid,
            cex_symbol=best_ask.symbol,
            quote_ts_us=now,
            quote=quote,
            screen=screen,
        ),
    ]
    return [row for row in rows if row["executable_spread_pct"] is not None]


def build_row(
    *,
    token: TokenInfo,
    source: str,
    direction: str,
    long_venue: str,
    short_venue: str,
    long_ask: float,
    short_bid: float,
    cex_symbol: str,
    quote_ts_us: int,
    quote: dict[str, Any],
    screen: dict[str, Any],
) -> dict[str, Any]:
    spread = spread_pct(long_ask, short_bid)
    return {
        "token": token.symbol,
        "token_name": token.name,
        "source_name": f"broad_{source}_quote",
        "source_kind": "dex_discovered",
        "direction": direction,
        "long_venue": long_venue,
        "long_market_type": "Spot",
        "short_venue": short_venue,
        "short_market_type": "Spot",
        "cex_symbol": cex_symbol,
        "dex_address": token.address,
        "executable_spread_pct": round(spread, 8) if spread is not None else None,
        "depth_weighted_spread_pct": round(spread, 8) if spread is not None else None,
        "validation_state": "quote_verified",
        "executor_status": "not_ready",
        "quote_ts_us": quote_ts_us,
        "blockers": [
            "symbol_match_only",
            "identity_unverified",
            "route_feasibility_unproven",
            "executor_attestation_missing",
            "gas_estimate_missing",
            "dex_spot_route_feasibility_unproven",
        ],
        "notes": {
            "dex_quote": quote,
            "screen": screen,
            "universe_source": token.universe,
        },
    }


def jupiter_headers(api_key: str) -> dict[str, str]:
    """Use Jupiter's public lane without sending a blank credential header."""

    headers = {
        "accept": "application/json",
        "user-agent": "spreadarb-broad-dex/1.0",
    }
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def quote_jupiter(token: TokenInfo, api_key: str, args: argparse.Namespace) -> dict[str, Any]:
    headers = jupiter_headers(api_key)
    buy = fetch_json(
        f"{JUPITER_QUOTE_URL}?{urlencode({'inputMint': USDC_SOLANA, 'outputMint': token.address, 'amount': str(int(args.target_usd * 1_000_000)), 'slippageBps': str(args.slippage_bps), 'restrictIntermediateTokens': 'true'})}",
        headers,
        timeout=args.quote_timeout_s,
    )
    buy_amount = int(str((buy or {}).get("outAmount") or "0"))
    if buy_amount <= 0:
        raise RuntimeError("jupiter_zero_buy_amount")
    # Free keyed Jupiter access is one request per second. The buy and sell are
    # separate requests; waiting only after the candidate is too late and made
    # almost every sell quote hit 429.
    sleep(max(0.0, float(args.rate_limit_s)))
    sell = fetch_json(
        f"{JUPITER_QUOTE_URL}?{urlencode({'inputMint': token.address, 'outputMint': USDC_SOLANA, 'amount': str(buy_amount), 'slippageBps': str(args.slippage_bps), 'restrictIntermediateTokens': 'true'})}",
        headers,
        timeout=args.quote_timeout_s,
    )
    sell_amount = int(str((sell or {}).get("outAmount") or "0"))
    token_qty = buy_amount / (10 ** max(token.decimals, 0))
    if sell_amount <= 0 or token_qty <= 0:
        raise RuntimeError("jupiter_zero_sell_amount")
    return {
        "venue": "Jupiter Solana",
        "bid": sell_amount / 1_000_000 / token_qty,
        "ask": args.target_usd / token_qty,
        "route_plan": jupiter_route_plan(buy),
        "price_impact_pct": as_float(buy.get("priceImpactPct")),
    }


def quote_zerox(token: TokenInfo, api_key: str, args: argparse.Namespace) -> dict[str, Any]:
    headers = {
        "0x-api-key": api_key,
        "0x-version": "v2",
        "accept": "application/json",
        "user-agent": "spreadarb-broad-dex/1.0",
    }
    buy = fetch_json(
        f"{ZEROX_PRICE_URL}?{urlencode({'chainId': str(ZEROX_CHAIN_ID), 'sellToken': USDC_ETHEREUM, 'buyToken': token.address, 'sellAmount': str(int(args.target_usd * 1_000_000)), 'slippageBps': str(args.slippage_bps)})}",
        headers,
        timeout=args.quote_timeout_s,
    )
    buy_amount = int(str((buy or {}).get("buyAmount") or "0"))
    if buy_amount <= 0:
        raise RuntimeError("zerox_zero_buy_amount")
    sleep(max(0.0, float(args.rate_limit_s)))
    sell = fetch_json(
        f"{ZEROX_PRICE_URL}?{urlencode({'chainId': str(ZEROX_CHAIN_ID), 'sellToken': token.address, 'buyToken': USDC_ETHEREUM, 'sellAmount': str(buy_amount), 'slippageBps': str(args.slippage_bps)})}",
        headers,
        timeout=args.quote_timeout_s,
    )
    sell_amount = int(str((sell or {}).get("buyAmount") or "0"))
    token_qty = buy_amount / (10 ** max(token.decimals, 0))
    if sell_amount <= 0 or token_qty <= 0:
        raise RuntimeError("zerox_zero_sell_amount")
    return {
        "venue": "0x Ethereum",
        "bid": sell_amount / 1_000_000 / token_qty,
        "ask": args.target_usd / token_qty,
        "route_plan": zerox_route_plan(buy),
        "price_impact_pct": as_float(buy.get("priceImpactPct")),
    }


def parse_jupiter_tokens(payload: Any) -> list[TokenInfo]:
    tokens: list[TokenInfo] = []
    if not isinstance(payload, list):
        return tokens
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper().strip()
        address = str(item.get("id") or "").strip()
        decimals = as_int(item.get("decimals"))
        if not symbol or not address or decimals is None:
            continue
        tokens.append(
            TokenInfo(
                symbol=symbol,
                name=str(item.get("name") or ""),
                address=address,
                decimals=decimals,
                universe="jupiter_verified_tokens_v2",
                usd_price=as_float(item.get("usdPrice")),
                liquidity_usd=as_float(item.get("liquidity")),
            )
        )
    return tokens


def parse_evm_tokens(payload: Any) -> list[TokenInfo]:
    tokens: list[TokenInfo] = []
    raw_tokens = payload if isinstance(payload, list) else list((payload or {}).get("tokens") or [])
    for item in raw_tokens:
        if not isinstance(item, dict) or int(item.get("chainId") or 0) != ZEROX_CHAIN_ID:
            continue
        symbol = str(item.get("symbol") or "").upper().strip()
        address = str(item.get("address") or "").strip()
        decimals = as_int(item.get("decimals"))
        if not symbol or not address or decimals is None:
            continue
        tokens.append(
            TokenInfo(
                symbol=symbol,
                name=str(item.get("name") or ""),
                address=address,
                decimals=decimals,
                universe="evm_token_list",
            )
        )
    return tokens


def fetch_evm_token_list(preferred_url: str) -> tuple[Any, str]:
    errors: list[str] = []
    urls = [preferred_url, *[url for url in FALLBACK_EVM_TOKEN_LIST_URLS if url != preferred_url]]
    for url in urls:
        try:
            payload = fetch_json(
                url,
                {"accept": "application/json", "user-agent": "spreadarb-broad-dex/1.0"},
                timeout=60.0,
            )
            if parse_evm_tokens(payload):
                return payload, url
            errors.append(f"{url}:no_mainnet_tokens")
        except Exception as exc:
            errors.append(f"{url}:{clean_error(exc)}")
    raise RuntimeError("; ".join(errors[:3]))


def unique_by_symbol(tokens: list[TokenInfo]) -> tuple[dict[str, TokenInfo], dict[str, list[TokenInfo]]]:
    grouped: dict[str, list[TokenInfo]] = {}
    for token in tokens:
        grouped.setdefault(token.symbol, []).append(token)
    unique = {symbol: items[0] for symbol, items in grouped.items() if len(items) == 1}
    duplicates = {symbol: items for symbol, items in grouped.items() if len(items) > 1}
    return unique, duplicates


def price_screen(token: TokenInfo, refs: list[CexQuote], price: float) -> dict[str, Any]:
    best_bid = max(refs, key=lambda item: item.bid)
    best_ask = min(refs, key=lambda item: item.ask)
    dex_long = spread_pct(price, best_bid.bid) or 0.0
    cex_long = spread_pct(best_ask.ask, price) or 0.0
    return {
        "reference_price": price,
        "best_cex_bid_venue": best_bid.venue,
        "best_cex_bid": best_bid.bid,
        "best_cex_ask_venue": best_ask.venue,
        "best_cex_ask": best_ask.ask,
        "screen_dex_long_sell_cex_pct": dex_long,
        "screen_cex_long_sell_dex_pct": cex_long,
        "max_abs_screen_spread_pct": max(abs(dex_long), abs(cex_long)),
        "liquidity_usd": token.liquidity_usd,
    }


def cex_internal_screen(refs: list[CexQuote]) -> dict[str, Any]:
    best_bid = max(refs, key=lambda item: item.bid)
    best_ask = min(refs, key=lambda item: item.ask)
    internal = spread_pct(best_ask.ask, best_bid.bid) or 0.0
    return {
        "best_cex_bid_venue": best_bid.venue,
        "best_cex_bid": best_bid.bid,
        "best_cex_ask_venue": best_ask.venue,
        "best_cex_ask": best_ask.ask,
        "max_abs_screen_spread_pct": abs(internal),
        "cex_internal_best_spread_pct": internal,
    }


def fetch_json(url: str, headers: dict[str, str] | None = None, timeout: float = 30.0) -> Any:
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_json_with_retries(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = 30.0,
    retry_429: int = 1,
    rate_limit_s: float = 0.4,
) -> Any:
    attempts = max(1, retry_429 + 1)
    for attempt in range(attempts):
        try:
            return fetch_json(url, headers, timeout=timeout)
        except HTTPError as exc:
            if exc.code != 429 or attempt >= attempts - 1:
                raise
            sleep(max(rate_limit_s * 5, 2.0) * (attempt + 1))
    raise RuntimeError("unreachable fetch_json_with_retries fallthrough")


def safe_error_body(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8").replace("\n", " ")[:180]
    except Exception:
        return ""


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def jupiter_route_plan(payload: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in payload.get("routePlan") or []:
        if not isinstance(item, dict):
            continue
        swap = item.get("swapInfo")
        if not isinstance(swap, dict):
            continue
        label = swap.get("label") or swap.get("ammKey")
        if label:
            labels.append(str(label))
    return labels


def zerox_route_plan(payload: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    route = payload.get("route")
    fills = route.get("fills") if isinstance(route, dict) else payload.get("sources")
    for item in fills or []:
        if not isinstance(item, dict):
            continue
        label = item.get("source") or item.get("name")
        if label:
            labels.append(str(label))
    return labels


def summarize(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "updated_at": payload["updated_at"],
        "elapsed_seconds": payload["elapsed_seconds"],
        "cex_total_venue_token_listings": payload["cex"]["total_venue_token_listings"],
        "cex_total_usable_ticker_listings": payload["cex"]["total_usable_ticker_listings"],
        "cex_unique_symbols_with_quotes": payload["cex"]["unique_symbols_with_quotes"],
        "output_path": str(payload.get("output_path") or DEFAULT_OUTPUT),
    }
    for scan in payload["scans"]:
        prefix = "zerox" if scan["source"] == "0x" else scan["source"]
        for key in (
            "verified_tokens",
            "evm_tokens",
            "unique_symbols",
            "duplicate_symbol_groups",
            "crosslisted_unique_symbols_before_filters",
            "candidate_tokens_after_filters",
            "quote_attempted_tokens",
            "quote_success_tokens",
            "quote_error_tokens",
            "row_count",
            "positive_row_count",
            "research_row_count_ge_1pct_lte_90pct",
        ):
            if key in scan:
                summary[f"{prefix}_{key}"] = scan[key]
        summary[f"{prefix}_top_rows"] = scan["rows"][:10]
        summary[f"{prefix}_errors_sample"] = scan["errors"][:10]
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
