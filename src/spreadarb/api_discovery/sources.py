"""Public-read discovery sources for CEX, DEX spot, and DEX derivatives."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
import gc
import json
import os
from time import monotonic, sleep
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import ccxt

from spreadarb.api_discovery.attestations import ExecutorAttestationRegistry, route_key
from spreadarb.api_discovery.identity import (
    IdentityResolution,
    IdentityRegistry,
    WatchAsset,
    pair_identity_blockers,
)
from spreadarb.api_discovery.models import (
    DiscoveryCandidate,
    MarketQuote,
    QUOTE_VERIFIED_STATE,
    SOURCE_API_DISCOVERED,
    SOURCE_DEX_DISCOVERED,
    SourceResult,
    SourceStatus,
    as_float,
    candidate_state_from_checks,
    clean_error,
    IDENTITY_VERIFIED_STATE,
    now_us,
    spread_pct,
    utc_now_iso,
)
from spreadarb.api_discovery.orderbook import depth_weighted_price
from spreadarb.public_runtime import keychain
from spreadarb.util.bitget_dns import install_bitget_dns_fallback

HttpGetJson = Callable[[str, Mapping[str, str], float], Any]

USDC_ETHEREUM = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_CURRENCY_PRIORITY = {"USDT": 0, "USDC": 1, "USD": 2, "USDH": 3}
ZEROX_API_KEY_SERVICE = "SPREADARB/0x/api_key"
JUPITER_API_KEY_SERVICE = "SPREADARB/jupiter/api_key"

# Exchange symbols are not globally unique. Keep known collisions separated
# before cross-venue pairing so an equal ticker cannot create a false spread.
CEX_INSTRUMENT_ALIASES = {
    ("Binance", "AI"): "SLEEPLESSAI",
    ("Gate", "AI"): "SLEEPLESSAI",
    ("Bitget", "AI"): "AIGENSYN",
    ("Coinbase", "AI"): "AIGENSYN",
    ("Kraken", "AI"): "AIGENSYN",
    ("Kucoin", "AI"): "AIGENSYN",
    ("Mexc", "AI"): "AIGENSYN",
    ("OKX", "AI"): "AIGENSYN",
    ("Binance", "ALL"): "BINANCE_ALL_INDEX",
    ("Gate", "ALL"): "GATE_ALL_INDEX",
    ("Mexc", "ALL"): "MEXC_ALL_INDEX",
    # QNTX is a tokenized-equity instrument. Some venue adapters expose it as
    # QNT, which otherwise collides with the unrelated Quant crypto asset.
    ("OKX", "QNT"): "QNTX",
    ("Hyperliquid", "XYZ-QNT"): "QNTX",
}
HIGH_DISLOCATION_IDENTITY_THRESHOLD_PCT = 5.0


class OkxDexQuoteSource:
    """Read-only OKX DEX quotes for exact-identity watchlist assets."""

    name = "okx_dex_quote"
    kind = "dex_spot"

    def __init__(
        self,
        *,
        request_interval_seconds: float = 1.1,
        max_rate_limit_retries: int = 2,
        monotonic_func: Callable[[], float] = monotonic,
        sleep_func: Callable[[float], None] = sleep,
    ) -> None:
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self.max_rate_limit_retries = max(0, max_rate_limit_retries)
        self._monotonic = monotonic_func
        self._sleep = sleep_func
        self._last_request_started: float | None = None

    def collect(self, context: DiscoveryContext) -> SourceResult:
        from spreadarb.dex import okx_quotes as okx_dex

        started_at = utc_now_iso()
        started = monotonic()
        credentials = okx_dex.load_okx_dex_credentials()
        if credentials is None:
            return SourceResult(
                status=SourceStatus(
                    name=self.name,
                    kind=self.kind,
                    status="skipped",
                    started_at=started_at,
                    finished_at=utc_now_iso(),
                    elapsed_seconds=monotonic() - started,
                    blockers=("api_credentials_missing",),
                )
            )
        reference_tokens = {
            quote.token.upper()
            for quote in context.reference_quotes
            if quote.market_type in {"Spot", "Futures"}
        }
        assets = [
            asset for asset in _dex_assets(context.watchlist) if asset.token in reference_tokens
        ]
        dynamic_assets, catalogue_errors = self._discover_okx_assets(
            context=context,
            credentials=credentials,
            okx_dex=okx_dex,
            existing_tokens={asset.token for asset in assets},
        )
        assets.extend(dynamic_assets)
        reference_quotes = _apply_unique_okx_identities(
            context.reference_quotes,
            dynamic_assets,
            registry=context.identity_registry,
        )
        quotes: list[MarketQuote] = []
        errors: list[str] = []
        errors.extend(catalogue_errors)
        for asset in assets:
            if context.timed_out():
                break
            contracts = dict(asset.evm_contracts or {})
            if asset.solana_mint:
                contracts[501] = asset.solana_mint
            for chain_id, contract in contracts.items():
                try:
                    quote = self._quote_asset(
                        asset,
                        chain_id=int(chain_id),
                        contract=str(contract),
                        credentials=credentials,
                        context=context,
                        okx_dex=okx_dex,
                    )
                    if quote is not None:
                        quotes.append(quote)
                except Exception as exc:
                    errors.append(f"{asset.token}:{chain_id}:{clean_error(exc)}")
        rows = dex_candidates(
            quotes,
            reference_quotes,
            source_name=self.name,
            min_spread_pct=context.min_spread_pct,
            max_spread_pct=context.max_spread_pct,
        )
        status = SourceStatus(
            name=self.name,
            kind=self.kind,
            status="partial" if errors else "ok",
            started_at=started_at,
            finished_at=utc_now_iso(),
            elapsed_seconds=monotonic() - started,
            rows=len(rows),
            errors=tuple(errors[:12]),
            blockers=tuple(["partial_source_errors"] if errors else []),
            details={
                "provider": "OKX DEX",
                "quote_count": len(quotes),
                "static_asset_count": len(assets) - len(dynamic_assets),
                "dynamic_asset_count": len(dynamic_assets),
            },
        )
        return SourceResult(status=status, rows=tuple(rows), quotes=tuple(quotes))

    def _discover_okx_assets(
        self,
        *,
        context: DiscoveryContext,
        credentials: Any,
        okx_dex: Any,
        existing_tokens: set[str],
    ) -> tuple[list[WatchAsset], list[str]]:
        limit = max(
            0,
            min(
                50,
                int(os.environ.get("SPREADBOARD_OKX_DEX_DYNAMIC_TOKENS", "0")),
            ),
        )
        if limit <= 0:
            return [], []
        reference_by_token: dict[str, list[MarketQuote]] = defaultdict(list)
        for quote in context.reference_quotes:
            if quote.market_type in {"Spot", "Futures"}:
                reference_by_token[quote.token.upper()].append(quote)
        candidate_symbols = set(reference_by_token) - existing_tokens
        candidate_symbols -= {"USDT", "USDC", "USD", "DAI", "FDUSD", "TUSD"}
        if not candidate_symbols:
            return [], []

        matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
        errors: list[str] = []
        for chain_id in (1, 56, 42161, 8453, 137, 501):
            if context.timed_out():
                break
            result = self._quote_with_retry(
                okx_dex.list_tokens,
                chain=str(chain_id),
                credentials=credentials,
            )
            if result.get("status") != "ok":
                errors.append(
                    f"catalogue:{chain_id}:"
                    + ";".join(str(item) for item in result.get("blockers") or ["unavailable"])
                )
                continue
            for token in result.get("tokens") or []:
                symbol = str(token.get("symbol") or "").upper()
                if symbol in candidate_symbols:
                    matches[symbol].append(token)

        unique_matches = {
            symbol: items[0]
            for symbol, items in matches.items()
            if len(
                {
                    (
                        str(item.get("chain_index")),
                        str(item.get("address")).casefold(),
                    )
                    for item in items
                }
            )
            == 1
        }

        def priority(symbol: str) -> tuple[float, int, float, str]:
            refs = reference_by_token.get(symbol) or []
            has_futures = any(quote.market_type == "Futures" for quote in refs)
            projected_funding_24h = max(
                (
                    abs(quote.funding_rate_pct or 0.0)
                    * 24.0
                    / max(quote.funding_interval_hours or 8.0, 0.01)
                    for quote in refs
                    if quote.market_type == "Futures"
                ),
                default=0.0,
            )
            volume = max((quote.volume_24h_usd or 0.0 for quote in refs), default=0.0)
            return (projected_funding_24h, 1 if has_futures else 0, volume, symbol)

        selected = sorted(unique_matches, key=priority, reverse=True)[:limit]
        assets: list[WatchAsset] = []
        for symbol in selected:
            item = unique_matches[symbol]
            chain_id = int(str(item["chain_index"]))
            address = str(item["address"])
            assets.append(
                WatchAsset(
                    symbol=symbol,
                    identity_key=(
                        f"solana:501/token:{address}"
                        if chain_id == 501
                        else f"eip155:{chain_id}/erc20:{address.casefold()}"
                    ),
                    decimals=int(item["decimals"]),
                    cex_enabled=True,
                    dex_enabled=True,
                    evm_contracts=None if chain_id == 501 else {chain_id: address},
                    solana_mint=address if chain_id == 501 else None,
                    solana_decimals=int(item["decimals"]) if chain_id == 501 else None,
                )
            )
        return assets, errors

    def _quote_asset(
        self,
        asset: WatchAsset,
        *,
        chain_id: int,
        contract: str,
        credentials: Any,
        context: DiscoveryContext,
        okx_dex: Any,
    ) -> MarketQuote | None:
        buy = self._quote_with_retry(
            okx_dex.quote_usdc_to_token,
            chain=str(chain_id),
            token_address=contract,
            notional_usd=Decimal(str(context.target_notional_usd)),
            credentials=credentials,
        )
        if buy.get("status") != "ok":
            raise RuntimeError(
                ";".join(str(item) for item in buy.get("blockers") or ["buy_quote_unavailable"])
            )
        quantity = Decimal(str(buy.get("out_qty") or "0"))
        if quantity <= 0:
            return None
        decimals = _as_int(buy.get("to_token_decimals"))
        if decimals is None:
            decimals = asset.solana_decimals if chain_id == 501 else asset.decimals
        sell = self._quote_with_retry(
            okx_dex.quote_token_to_usdc,
            chain=str(chain_id),
            token_address=contract,
            token_quantity=quantity,
            token_decimals=decimals,
            credentials=credentials,
        )
        if sell.get("status") != "ok":
            raise RuntimeError(
                ";".join(str(item) for item in sell.get("blockers") or ["sell_quote_unavailable"])
            )
        bid = as_float(sell.get("dex_sell_price_usd"))
        ask = as_float(buy.get("dex_buy_price_usd"))
        if bid is None or ask is None:
            return None
        network_fee_usd = as_float(buy.get("trade_fee_usd"))
        router = buy.get("router")
        return MarketQuote(
            token=asset.token,
            venue=f"OKX DEX {chain_id}",
            market_type="Spot",
            bid=bid,
            ask=ask,
            bid_vwap=bid,
            ask_vwap=ask,
            quote_ts_us=now_us(),
            source_name=self.name,
            identity_key=asset.identity_key,
            identity_source="watchlist",
            decimals=decimals,
            chain_id=chain_id,
            token_address=contract,
            gas_estimate_usd=network_fee_usd,
            route_plan=(str(router),) if router else (),
        )

    def _quote_with_retry(
        self,
        quote_func: Callable[..., dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for attempt in range(self.max_rate_limit_retries + 1):
            self._wait_for_request_slot()
            result = quote_func(**kwargs)
            if not _okx_rate_limited(result):
                return result
            if attempt < self.max_rate_limit_retries:
                self._sleep(self.request_interval_seconds * (attempt + 1))
        return result

    def _wait_for_request_slot(self) -> None:
        now = self._monotonic()
        if self._last_request_started is not None:
            remaining = self.request_interval_seconds - (now - self._last_request_started)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request_started = now


def _okx_rate_limited(result: Mapping[str, Any]) -> bool:
    blockers = " ".join(str(item) for item in result.get("blockers") or ())
    normalized = blockers.casefold()
    return "too many requests" in normalized or "rate limit" in normalized


def _apply_unique_okx_identities(
    quotes: Iterable[MarketQuote],
    assets: Iterable[WatchAsset],
    *,
    registry: IdentityRegistry | None,
) -> list[MarketQuote]:
    """Carry a unique OKX token-list identity onto same-cycle CEX quotes.

    OKX dynamic assets are discovered after the initial CEX catalogue pass.
    Without this bridge, their CEX legs remain unresolved until a later static
    registry update and valid DEX routes disappear from the public board.
    Known ticker collisions are never inferred, and inferred identities remain
    guarded for large dislocations in ``_dex_candidate_pair``.
    """

    registry = registry or IdentityRegistry.empty()
    inferred: dict[str, WatchAsset] = {}
    for asset in assets:
        symbol = asset.token.upper()
        if not asset.identity_key or symbol in registry.known_ticker_collisions:
            continue
        if symbol in inferred and inferred[symbol].identity_key != asset.identity_key:
            inferred.pop(symbol, None)
            continue
        inferred[symbol] = asset

    output: list[MarketQuote] = []
    for quote in quotes:
        asset = inferred.get(quote.token.upper())
        if quote.identity_key or asset is None:
            output.append(quote)
            continue
        output.append(
            replace(
                quote,
                identity_key=asset.identity_key,
                identity_source="okx_unique_symbol_inference",
                blockers=tuple(
                    dict.fromkeys(
                        (*quote.blockers, "identity_inferred_from_unique_okx_symbol")
                    )
                ),
            )
        )
    return output


class DiscoverySource(Protocol):
    name: str
    kind: str

    def collect(self, context: "DiscoveryContext") -> SourceResult: ...


@dataclass(frozen=True, slots=True)
class DiscoveryContext:
    tokens: tuple[str, ...]
    watchlist: dict[str, WatchAsset]
    deadline_monotonic: float | None
    target_notional_usd: float = 50.0
    min_spread_pct: float = 1.0
    min_funding_apr_pct: float = 25.0
    max_spread_pct: float = 0.0
    reference_quotes: tuple[MarketQuote, ...] = ()
    all_platform_tokens: bool = False
    max_orderbook_candidates: int = 100
    identity_registry: IdentityRegistry | None = None
    executor_attestations: ExecutorAttestationRegistry | None = None

    def timed_out(self) -> bool:
        if self.deadline_monotonic is None:
            return False
        return monotonic() >= self.deadline_monotonic

    def remaining_timeout(self, cap: float = 2.0) -> float:
        if self.deadline_monotonic is None:
            return cap
        return max(0.1, min(cap, self.deadline_monotonic - monotonic()))


class DisabledSource:
    def __init__(self, name: str, kind: str, reason: str) -> None:
        self.name = name
        self.kind = kind
        self.reason = reason

    def collect(self, context: DiscoveryContext) -> SourceResult:
        started = utc_now_iso()
        status = SourceStatus(
            name=self.name,
            kind=self.kind,
            status="skipped",
            started_at=started,
            finished_at=utc_now_iso(),
            elapsed_seconds=0.0,
            blockers=(self.reason,),
            disabled=True,
        )
        return SourceResult(status=status)


class CexCcxtSource:
    kind = "cex"

    def __init__(
        self,
        *,
        venues: Mapping[str, str],
        name: str = "cex_spot_ccxt",
        market_type: str = "Spot",
        source_kind: str = SOURCE_API_DISCOVERED,
        include_reference_quotes: bool = False,
        collect_funding: bool = False,
    ) -> None:
        self.name = name
        self.venues = dict(venues)
        self.market_type = market_type
        self.source_kind = source_kind
        self.include_reference_quotes = include_reference_quotes
        self.collect_funding = collect_funding

    def collect(self, context: DiscoveryContext) -> SourceResult:
        started_at = utc_now_iso()
        started = monotonic()
        errors: list[str] = []
        quotes: list[MarketQuote] = []
        symbols_by_venue_token: dict[tuple[str, str], str] = {}
        market_token_counts: dict[str, int] = {}
        funding_market_counts: dict[str, int] = {}
        for venue, exchange_id in self.venues.items():
            if context.timed_out():
                errors.append("time_budget_exhausted")
                break
            try:
                exchange = _build_ccxt_exchange(
                    exchange_id, self.market_type, context.remaining_timeout(10.0)
                )
                markets = exchange.load_markets()
            except Exception as exc:
                errors.append(f"{venue}:market:{clean_error(exc)}")
                continue
            symbol_map = _canonicalize_symbol_map(
                venue,
                _symbols_for_context(markets, self.market_type, context),
            )
            market_token_counts[venue] = len(symbol_map)
            for token, symbol in _symbol_items(symbol_map):
                symbols_by_venue_token[(venue, token)] = symbol
            funding_rates = (
                _fetch_funding_rates(
                    exchange,
                    list(symbol_map.values()),
                    context,
                    errors,
                    venue,
                )
                if self.collect_funding
                else {}
            )
            funding_market_counts[venue] = len(funding_rates)
            if context.all_platform_tokens:
                quotes.extend(
                    _ticker_quotes_for_symbols(
                        exchange=exchange,
                        venue=venue,
                        market_type=self.market_type,
                        source_name=self.name,
                        symbol_map=symbol_map,
                        funding_rates=funding_rates,
                        errors=errors,
                        context=context,
                    )
                )
                _release_ccxt_exchange(exchange)
                del markets
                continue
            for token, symbol in _symbol_items(symbol_map):
                if context.timed_out():
                    errors.append("time_budget_exhausted")
                    break
                try:
                    book = exchange.fetch_order_book(symbol, limit=20)
                    quote = _quote_from_book(
                        token=token,
                        venue=venue,
                        market_type=self.market_type,
                        source_name=self.name,
                        book=book,
                        target_notional_usd=context.target_notional_usd,
                        symbol=symbol,
                        identity=_resolve_quote_identity(
                            token=token,
                            venue=venue,
                            market_type=self.market_type,
                            symbol=symbol,
                            context=context,
                        ),
                        funding=_funding_values(funding_rates.get(symbol)),
                    )
                    if quote.bid is not None and quote.ask is not None:
                        quotes.append(quote)
                except Exception as exc:
                    errors.append(f"{venue}:{token}:order_book:{clean_error(exc)}")
            _release_ccxt_exchange(exchange)
            del markets
        candidate_quotes = (
            [*context.reference_quotes, *quotes] if self.include_reference_quotes else quotes
        )
        if context.all_platform_tokens and quotes:
            quotes = _verify_top_candidate_books(
                quotes,
                candidate_quotes=candidate_quotes,
                exchange_ids=self.venues,
                symbols_by_venue_token=symbols_by_venue_token,
                source_name=self.name,
                market_type=self.market_type,
                target_notional_usd=context.target_notional_usd,
                min_spread_pct=context.min_spread_pct,
                max_candidates=context.max_orderbook_candidates,
                errors=errors,
                context=context,
            )
            candidate_quotes = (
                [*context.reference_quotes, *quotes] if self.include_reference_quotes else quotes
            )
        rows = pairwise_candidates(
            candidate_quotes,
            source_kind=self.source_kind,
            source_name=self.name,
            min_spread_pct=context.min_spread_pct,
            min_net_funding_apr_pct=context.min_funding_apr_pct,
            max_spread_pct=context.max_spread_pct,
            identity_registry=context.identity_registry,
            executor_attestations=context.executor_attestations,
        )
        if self.include_reference_quotes:
            rows = [
                row
                for row in rows
                if self.market_type in {row.get("long_market_type"), row.get("short_market_type")}
                and (row.get("long_venue") in self.venues or row.get("short_venue") in self.venues)
            ]
        status = SourceStatus(
            name=self.name,
            kind=self.kind,
            status="partial" if errors else "ok",
            started_at=started_at,
            finished_at=utc_now_iso(),
            elapsed_seconds=monotonic() - started,
            rows=len(rows),
            errors=tuple(errors[:12]),
            blockers=tuple(["partial_source_errors"] if errors else []),
            details={
                "venues": list(self.venues),
                "all_platform_tokens": context.all_platform_tokens,
                "market_token_counts": market_token_counts,
                "funding_market_counts": funding_market_counts,
                "quote_count": len(quotes),
            },
        )
        return SourceResult(status=status, rows=tuple(rows), quotes=tuple(quotes))


class DexDerivativeCcxtSource(CexCcxtSource):
    kind = "dex_derivative"

    def __init__(self, *, venues: Mapping[str, str]) -> None:
        super().__init__(
            venues=venues,
            name="dex_derivatives_ccxt",
            market_type="Futures",
            source_kind=SOURCE_DEX_DISCOVERED,
            include_reference_quotes=True,
            collect_funding=True,
        )


class ZeroxQuoteSource:
    name = "0x_evm_quote"
    kind = "dex_spot"

    def __init__(
        self,
        *,
        chain_id: int = 1,
        api_key: str | None = None,
        http_get_json: HttpGetJson = None,  # type: ignore[assignment]
        base_url: str = "https://api.0x.org/swap/allowance-holder/price",
        slippage_bps: int = 50,
    ) -> None:
        self.chain_id = chain_id
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("ZEROX_API_KEY") or keychain(ZEROX_API_KEY_SERVICE)
        )
        self.http_get_json = http_get_json or fetch_json
        self.base_url = base_url
        self.slippage_bps = slippage_bps

    def collect(self, context: DiscoveryContext) -> SourceResult:
        started_at = utc_now_iso()
        started = monotonic()
        if not self.api_key:
            return SourceResult(
                status=SourceStatus(
                    name=self.name,
                    kind=self.kind,
                    status="skipped",
                    started_at=started_at,
                    finished_at=utc_now_iso(),
                    elapsed_seconds=monotonic() - started,
                    blockers=("api_key_missing",),
                )
            )
        errors: list[str] = []
        quotes: list[MarketQuote] = []
        for asset in _dex_assets(context.watchlist):
            contract = (asset.evm_contracts or {}).get(self.chain_id)
            if not contract or context.timed_out():
                continue
            try:
                quote = self._quote_asset(asset, contract, context)
                if quote is not None:
                    quotes.append(quote)
            except Exception as exc:
                errors.append(f"{asset.token}:0x:{clean_error(exc)}")
        rows = dex_candidates(
            quotes,
            context.reference_quotes,
            source_name=self.name,
            min_spread_pct=context.min_spread_pct,
            max_spread_pct=context.max_spread_pct,
        )
        status = SourceStatus(
            name=self.name,
            kind=self.kind,
            status="partial" if errors else "ok",
            started_at=started_at,
            finished_at=utc_now_iso(),
            elapsed_seconds=monotonic() - started,
            rows=len(rows),
            errors=tuple(errors[:12]),
            blockers=tuple(["partial_source_errors"] if errors else []),
            details={"chain_id": self.chain_id},
        )
        return SourceResult(status=status, rows=tuple(rows), quotes=tuple(quotes))

    def _quote_asset(
        self,
        asset: WatchAsset,
        contract: str,
        context: DiscoveryContext,
    ) -> MarketQuote | None:
        usdc_amount = int(context.target_notional_usd * 1_000_000)
        headers = {"0x-api-key": self.api_key or "", "0x-version": "v2"}
        buy = self._request(
            {
                "chainId": str(self.chain_id),
                "sellToken": USDC_ETHEREUM,
                "buyToken": contract,
                "sellAmount": str(usdc_amount),
                "slippageBps": str(self.slippage_bps),
            },
            headers,
            context.remaining_timeout(),
        )
        buy_amount = int(str(buy.get("buyAmount") or "0"))
        if buy_amount <= 0:
            return None
        sell = self._request(
            {
                "chainId": str(self.chain_id),
                "sellToken": contract,
                "buyToken": USDC_ETHEREUM,
                "sellAmount": str(buy_amount),
                "slippageBps": str(self.slippage_bps),
            },
            headers,
            context.remaining_timeout(),
        )
        token_qty = buy_amount / (10 ** max(asset.decimals, 0))
        if token_qty <= 0:
            return None
        bid = int(str(sell.get("buyAmount") or "0")) / 1_000_000 / token_qty
        ask = usdc_amount / 1_000_000 / token_qty
        return MarketQuote(
            token=asset.token,
            venue="0x Ethereum",
            market_type="Spot",
            bid=bid,
            ask=ask,
            bid_vwap=bid,
            ask_vwap=ask,
            quote_ts_us=now_us(),
            source_name=self.name,
            identity_key=asset.identity_key,
            identity_source="watchlist",
            decimals=asset.decimals,
            chain_id=self.chain_id,
            slippage_bps=self.slippage_bps,
            price_impact_pct=as_float(buy.get("priceImpactPct")),
            route_plan=_zerox_route_plan(buy),
        )

    def _request(
        self, params: Mapping[str, str], headers: Mapping[str, str], timeout: float
    ) -> dict[str, Any]:
        url = f"{self.base_url}?{urlencode(params)}"
        payload = self.http_get_json(url, headers, timeout)
        return payload if isinstance(payload, dict) else {}


class JupiterQuoteSource:
    name = "jupiter_solana_quote"
    kind = "dex_spot"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_get_json: HttpGetJson = None,  # type: ignore[assignment]
        base_url: str = "https://api.jup.ag/swap/v1/quote",
        slippage_bps: int = 50,
    ) -> None:
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("JUPITER_API_KEY") or keychain(JUPITER_API_KEY_SERVICE)
        )
        self.http_get_json = http_get_json or fetch_json
        self.base_url = base_url
        self.slippage_bps = slippage_bps

    def collect(self, context: DiscoveryContext) -> SourceResult:
        started_at = utc_now_iso()
        started = monotonic()
        if not self.api_key:
            return SourceResult(
                status=SourceStatus(
                    name=self.name,
                    kind=self.kind,
                    status="skipped",
                    started_at=started_at,
                    finished_at=utc_now_iso(),
                    elapsed_seconds=monotonic() - started,
                    blockers=("api_key_missing",),
                )
            )
        errors: list[str] = []
        quotes: list[MarketQuote] = []
        for asset in _dex_assets(context.watchlist):
            if not asset.solana_mint or context.timed_out():
                continue
            try:
                quote = self._quote_asset(asset, context)
                if quote is not None:
                    quotes.append(quote)
            except Exception as exc:
                errors.append(f"{asset.token}:jupiter:{clean_error(exc)}")
        rows = dex_candidates(
            quotes,
            context.reference_quotes,
            source_name=self.name,
            min_spread_pct=context.min_spread_pct,
            max_spread_pct=context.max_spread_pct,
        )
        blockers = ("watchlist_missing_solana_assets",) if not quotes and not errors else ()
        status = SourceStatus(
            name=self.name,
            kind=self.kind,
            status="partial" if errors else "ok",
            started_at=started_at,
            finished_at=utc_now_iso(),
            elapsed_seconds=monotonic() - started,
            rows=len(rows),
            errors=tuple(errors[:12]),
            blockers=blockers + tuple(["partial_source_errors"] if errors else []),
        )
        return SourceResult(status=status, rows=tuple(rows), quotes=tuple(quotes))

    def _quote_asset(self, asset: WatchAsset, context: DiscoveryContext) -> MarketQuote | None:
        usdc_amount = int(context.target_notional_usd * 1_000_000)
        buy = self._request(
            {
                "inputMint": USDC_SOLANA,
                "outputMint": asset.solana_mint or "",
                "amount": str(usdc_amount),
                "slippageBps": str(self.slippage_bps),
                "restrictIntermediateTokens": "true",
            },
            context.remaining_timeout(),
        )
        buy_amount = int(str(buy.get("outAmount") or "0"))
        if buy_amount <= 0:
            return None
        sell = self._request(
            {
                "inputMint": asset.solana_mint or "",
                "outputMint": USDC_SOLANA,
                "amount": str(buy_amount),
                "slippageBps": str(self.slippage_bps),
                "restrictIntermediateTokens": "true",
            },
            context.remaining_timeout(),
        )
        decimals = asset.solana_decimals if asset.solana_decimals is not None else asset.decimals
        token_qty = buy_amount / (10 ** max(decimals, 0))
        if token_qty <= 0:
            return None
        bid = int(str(sell.get("outAmount") or "0")) / 1_000_000 / token_qty
        ask = usdc_amount / 1_000_000 / token_qty
        return MarketQuote(
            token=asset.token,
            venue="Jupiter Solana",
            market_type="Spot",
            bid=bid,
            ask=ask,
            bid_vwap=bid,
            ask_vwap=ask,
            quote_ts_us=now_us(),
            source_name=self.name,
            identity_key=asset.identity_key,
            identity_source="watchlist",
            decimals=decimals,
            chain_id=101,
            slippage_bps=self.slippage_bps,
            price_impact_pct=as_float(buy.get("priceImpactPct")),
            route_plan=_jupiter_route_plan(buy),
        )

    def _request(self, params: Mapping[str, str], timeout: float) -> dict[str, Any]:
        url = f"{self.base_url}?{urlencode(params)}"
        payload = self.http_get_json(url, {"x-api-key": self.api_key or ""}, timeout)
        return payload if isinstance(payload, dict) else {}


def default_enabled_cex_source() -> CexCcxtSource:
    return CexCcxtSource(
        venues={
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
            "HTX": "htx",
            "Phemex": "phemex",
            "CoinEx": "coinex",
            "WhiteBIT": "whitebit",
            "BitMart": "bitmart",
            "XT": "xt",
            # Added 2026-08-01: venues the reference product quotes that we did not scan.
            "Upbit": "upbit",
            "Ourbit": "ourbit",
        }
    )


def default_enabled_cex_futures_source() -> CexCcxtSource:
    return CexCcxtSource(
        venues={
            "Binance": "binance",
            "Bybit": "bybit",
            "Bitget": "bitget",
            "OKX": "okx",
            "Gate": "gateio",
            "Mexc": "mexc",
            "Kucoin Futures": "kucoinfutures",
            "Bingx": "bingx",
            "Kraken Futures": "krakenfutures",
            "Coinbase International": "coinbaseinternational",
            "HTX": "htx",
            "Phemex": "phemex",
            "CoinEx": "coinex",
            "WhiteBIT": "whitebit",
            "BitMart": "bitmart",
            "XT": "xt",
            # Added 2026-08-01. Lighter is a perp DEX the reference product quotes.
            "Lighter": "lighter",
            "Ourbit": "ourbit",
        },
        name="cex_futures_ccxt",
        market_type="Futures",
        include_reference_quotes=True,
        collect_funding=True,
    )


def default_sources(
    *,
    include_network: bool = True,
    source_filter: set[str] | None = None,
) -> list[DiscoverySource]:
    enabled: list[DiscoverySource] = []
    if include_network:
        spot = default_enabled_cex_source()
        futures = default_enabled_cex_futures_source()
        spot_batches = _batched_cex_sources(spot, batch_size=5)
        futures_batches = _batched_cex_sources(futures, batch_size=5)
        source_specs: list[DiscoverySource] = [
            *[source for pair in zip(spot_batches, futures_batches) for source in pair],
            *spot_batches[len(futures_batches) :],
            *futures_batches[len(spot_batches) :],
            OkxDexQuoteSource(),
            DexDerivativeCcxtSource(venues={"Hyperliquid": "hyperliquid", "Aster": "aster"}),
        ]
        enabled.extend(source for source in source_specs if _source_enabled(source, source_filter))
    disabled_specs = [
        ("Crypto.com", "cex", "disabled_connector_spec_until_validated"),
        ("Bitfinex", "cex", "disabled_connector_spec_until_validated"),
        ("LBank", "cex", "disabled_connector_spec_until_validated"),
        ("Bitstamp", "cex", "disabled_connector_spec_until_validated"),
        ("dYdX", "dex_derivative", "disabled_read_only_research_spec"),
        ("Lighter", "dex_derivative", "disabled_read_only_research_spec"),
        ("ApeX", "dex_derivative", "disabled_read_only_research_spec"),
    ]
    enabled.extend(
        DisabledSource(name, kind, reason)
        for name, kind, reason in disabled_specs
        if source_filter is None or "disabled" in source_filter
    )
    return enabled


def _batched_cex_sources(
    source: CexCcxtSource,
    *,
    batch_size: int,
) -> list[CexCcxtSource]:
    venues = list(source.venues.items())
    batches: list[CexCcxtSource] = []
    for index in range(0, len(venues), max(1, batch_size)):
        batches.append(
            CexCcxtSource(
                venues=dict(venues[index : index + batch_size]),
                name=f"{source.name}_{len(batches) + 1}",
                market_type=source.market_type,
                source_kind=source.source_kind,
                include_reference_quotes=True,
                collect_funding=source.collect_funding,
            )
        )
    return batches


def pairwise_candidates(
    quotes: Iterable[MarketQuote],
    *,
    source_kind: str,
    source_name: str,
    min_spread_pct: float,
    max_spread_pct: float = 0.0,
    identity_registry: IdentityRegistry | None = None,
    executor_attestations: ExecutorAttestationRegistry | None = None,
    min_net_funding_apr_pct: float = 25.0,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pair in quote_candidate_pairs(
        quotes,
        min_spread_pct=min_spread_pct,
        max_spread_pct=max_spread_pct,
        min_net_funding_apr_pct=min_net_funding_apr_pct,
    ):
        long_quote = pair.long_quote
        short_quote = pair.short_quote
        token = pair.token
        known_identities = {
            identity for identity in (long_quote.identity_key, short_quote.identity_key) if identity
        }
        identity = next(iter(known_identities)) if len(known_identities) == 1 else None
        blockers = [
            *long_quote.blockers,
            *short_quote.blockers,
            *pair_identity_blockers(
                token,
                [long_quote.identity_key, short_quote.identity_key],
                identity_registry,
            ),
        ]
        if (
            identity is None
            and max(
                abs(pair.executable_spread_pct),
                abs(pair.depth_weighted_spread_pct),
            )
            >= HIGH_DISLOCATION_IDENTITY_THRESHOLD_PCT
        ):
            blockers.append("mirage_guard:high_dislocation_identity_unverified")
        if source_kind == SOURCE_DEX_DISCOVERED and long_quote.market_type == "Futures":
            blockers.append("dex_derivative_executor_missing")
        attestation_note = None
        attestation = None
        if executor_attestations is not None:
            route = route_key(
                token,
                long_quote.venue,
                long_quote.market_type,
                short_quote.venue,
                short_quote.market_type,
            )
            attestation = executor_attestations.get(route)
        if attestation is not None:
            attestation_blockers = attestation.validation_blockers(
                identity_key=identity,
                long_market_type=long_quote.market_type,
                short_market_type=short_quote.market_type,
            )
            blockers.extend(attestation_blockers)
            if not attestation_blockers:
                attestation_note = attestation.to_note()
                attestation_note["status"] = "ready"
        else:
            blockers.append("route_feasibility_unproven")
            blockers.append("executor_attestation_missing")
        state = candidate_state_from_checks(
            has_quote=True,
            has_identity=bool(identity),
            route_feasible=bool(attestation_note),
            executor_ready=bool(attestation_note),
        )
        funding = _funding_pair_metrics(long_quote, short_quote)
        rows.append(
            DiscoveryCandidate(
                token=token,
                long_venue=long_quote.venue,
                long_market_type=long_quote.market_type,
                short_venue=short_quote.venue,
                short_market_type=short_quote.market_type,
                source_kind=source_kind,
                source_name=source_name,
                validation_state=state,
                quote_ts_us=min(long_quote.quote_ts_us, short_quote.quote_ts_us),
                executable_spread_pct=pair.executable_spread_pct,
                depth_weighted_spread_pct=pair.depth_weighted_spread_pct,
                funding_spread_apr_pct=funding.get("net_apr_pct"),
                funding_daily_pct=funding.get("net_daily_pct"),
                identity_key=identity,
                blockers=tuple(blockers),
                executor_attestation=attestation_note,
                notes=_quote_pair_notes(long_quote, short_quote, funding=funding),
            ).to_row(allow_executor_ready=True)
        )
    rows.sort(key=lambda row: as_float(row.get("depth_weighted_spread_pct")) or -999, reverse=True)
    return rows


@dataclass(frozen=True, slots=True)
class QuoteCandidatePair:
    token: str
    long_quote: MarketQuote
    short_quote: MarketQuote
    executable_spread_pct: float
    depth_weighted_spread_pct: float


def quote_candidate_pairs(
    quotes: Iterable[MarketQuote],
    *,
    min_spread_pct: float,
    max_spread_pct: float = 90.0,
    min_net_funding_apr_pct: float | None = None,
) -> list[QuoteCandidatePair]:
    pairs: list[QuoteCandidatePair] = []
    by_token: dict[str, list[MarketQuote]] = defaultdict(list)
    for quote in quotes:
        by_token[quote.token.upper()].append(quote)
    for token, token_quotes in by_token.items():
        for long_quote in token_quotes:
            for short_quote in token_quotes:
                # Same venue, same market type is the same contract -- meaningless.
                # Same venue, DIFFERENT market type is cash-and-carry: buy the spot
                # and short the perp in one account, collect funding, move nothing.
                # It needs no transfer rail at all, so it is the safest farm on the
                # board, and excluding it cost us 4 of the reference product's top
                # 15 Spot-Futures rows (MEXC spot -> MEXC perp on UAI, US, CLANKER,
                # BLESS) plus every venue-exclusive token such as GEOD and HOODRAT.
                if (
                    long_quote.venue == short_quote.venue
                    and long_quote.market_type == short_quote.market_type
                ):
                    continue
                executable = spread_pct(long_quote.ask, short_quote.bid)
                depth = spread_pct(long_quote.ask_vwap, short_quote.bid_vwap)
                funding_qualifies = _funding_pair_qualifies(
                    long_quote,
                    short_quote,
                    min_net_funding_apr_pct,
                )
                if (
                    executable is None
                    or depth is None
                    or (executable < min_spread_pct and not funding_qualifies)
                ):
                    continue
                if _spread_ceiling_exceeded(executable, depth, max_spread_pct=max_spread_pct):
                    continue
                pairs.append(
                    QuoteCandidatePair(
                        token=token,
                        long_quote=long_quote,
                        short_quote=short_quote,
                        executable_spread_pct=executable,
                        depth_weighted_spread_pct=depth,
                    )
                )
    pairs.sort(key=lambda pair: pair.depth_weighted_spread_pct, reverse=True)
    return pairs


def _funding_pair_qualifies(
    long_quote: MarketQuote,
    short_quote: MarketQuote,
    minimum_apr_pct: float | None,
) -> bool:
    if minimum_apr_pct is None:
        return False
    metrics = _funding_pair_metrics(long_quote, short_quote)
    net_apr = as_float(metrics.get("net_apr_pct"))
    return net_apr is not None and net_apr >= float(minimum_apr_pct)


def _spread_ceiling_exceeded(*values: float | None, max_spread_pct: float) -> bool:
    if max_spread_pct <= 0:
        return False
    for value in values:
        if value is not None and value > max_spread_pct:
            return True
    return False


def dex_candidates(
    dex_quotes: Iterable[MarketQuote],
    reference_quotes: Iterable[MarketQuote],
    *,
    source_name: str,
    min_spread_pct: float,
    max_spread_pct: float = 90.0,
    min_net_funding_apr_pct: float = 25.0,
) -> list[dict[str, Any]]:
    quotes = [quote for quote in reference_quotes if quote.market_type in {"Spot", "Futures"}]
    rows: list[dict[str, Any]] = []
    for dex_quote in dex_quotes:
        token_refs = [quote for quote in quotes if quote.token.upper() == dex_quote.token.upper()]
        for cex_quote in token_refs:
            rows.extend(
                _dex_candidate_pair(
                    dex_quote,
                    cex_quote,
                    source_name=source_name,
                    min_spread_pct=min_spread_pct,
                    max_spread_pct=max_spread_pct,
                    min_net_funding_apr_pct=min_net_funding_apr_pct,
                )
            )
    rows.sort(key=lambda row: as_float(row.get("depth_weighted_spread_pct")) or -999, reverse=True)
    return rows


def _resolve_quote_identity(
    *,
    token: str,
    venue: str,
    market_type: str,
    symbol: str | None,
    context: DiscoveryContext,
):
    registry = context.identity_registry or IdentityRegistry.empty()
    resolved = registry.resolve_market(
        venue=venue,
        market_type=market_type,
        token=token,
        symbol=symbol,
    )
    if resolved.identity_key:
        return resolved
    watch_asset = context.watchlist.get(str(token).upper())
    if (
        watch_asset
        and watch_asset.cex_enabled
        and str(watch_asset.identity_key or "").startswith("asset:")
    ):
        return IdentityResolution(
            identity_key=watch_asset.identity_key,
            blockers=resolved.blockers,
        )
    return resolved


def _quote_pair_notes(
    long_quote: MarketQuote,
    short_quote: MarketQuote,
    *,
    funding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    notes = {
        "identity": {
            "long": _quote_identity_note(long_quote),
            "short": _quote_identity_note(short_quote),
        },
        "route_inputs": {
            "long": _quote_route_note(long_quote),
            "short": _quote_route_note(short_quote),
        },
    }
    funding = funding if funding is not None else _funding_pair_metrics(long_quote, short_quote)
    if funding:
        notes["funding"] = funding
    return notes


def _quote_identity_note(quote: MarketQuote) -> dict[str, Any]:
    note: dict[str, Any] = {
        "venue": quote.venue,
        "market_type": quote.market_type,
        "symbol": quote.symbol,
        "identity_key": quote.identity_key,
    }
    optional = {
        "identity_source": quote.identity_source,
        "decimals": quote.decimals,
        "chain_id": quote.chain_id,
        "token_address": quote.token_address,
        "settle_asset": quote.settle_asset,
        "contract_size": quote.contract_size,
    }
    for key, value in optional.items():
        if value is not None:
            note[key] = value
    return note


def _quote_route_note(quote: MarketQuote) -> dict[str, Any]:
    note: dict[str, Any] = {
        "source_name": quote.source_name,
        "symbol": quote.symbol,
        "bid": quote.bid,
        "ask": quote.ask,
        "bid_vwap": quote.bid_vwap,
        "ask_vwap": quote.ask_vwap,
        "volume_24h_usd": quote.volume_24h_usd,
        "gas_estimate_usd": quote.gas_estimate_usd,
        "slippage_bps": quote.slippage_bps,
        "price_impact_pct": quote.price_impact_pct,
        "route_plan": list(quote.route_plan),
    }
    return {key: value for key, value in note.items() if value not in (None, [], ())}


def _funding_pair_metrics(long_quote: MarketQuote, short_quote: MarketQuote) -> dict[str, Any]:
    if long_quote.market_type != "Futures" and short_quote.market_type != "Futures":
        return {}

    long_apr = long_quote.funding_apr_pct if long_quote.market_type == "Futures" else 0.0
    short_apr = short_quote.funding_apr_pct if short_quote.market_type == "Futures" else 0.0
    complete = long_apr is not None and short_apr is not None
    payload: dict[str, Any] = {
        "complete": complete,
        "long": _funding_quote_note(long_quote),
        "short": _funding_quote_note(short_quote),
        "sign_convention": "positive_net_means_route_receives_funding",
    }
    if complete:
        net_apr = float(short_apr) - float(long_apr)
        payload["net_apr_pct"] = net_apr
        payload["net_daily_pct"] = net_apr / 365.0
    return payload


def _funding_quote_note(quote: MarketQuote) -> dict[str, Any]:
    if quote.market_type != "Futures":
        return {"market_type": quote.market_type, "apr_pct": 0.0, "role": "no_funding"}
    return {
        "market_type": quote.market_type,
        "rate_pct": quote.funding_rate_pct,
        "interval_hours": quote.funding_interval_hours,
        "interval_assumed": quote.funding_interval_assumed,
        "apr_pct": quote.funding_apr_pct,
        "next_funding_ts_us": quote.next_funding_ts_us,
    }


def fetch_json(url: str, headers: Mapping[str, str], timeout: float) -> Any:
    request = Request(url, headers=dict(headers))
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - read-only public APIs.
        return json.loads(response.read().decode("utf-8"))


def _dex_candidate_pair(
    dex_quote: MarketQuote,
    cex_quote: MarketQuote,
    *,
    source_name: str,
    min_spread_pct: float,
    max_spread_pct: float = 90.0,
    min_net_funding_apr_pct: float | None = 25.0,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for long_quote, short_quote in ((dex_quote, cex_quote), (cex_quote, dex_quote)):
        executable = spread_pct(long_quote.ask, short_quote.bid)
        depth = spread_pct(long_quote.ask_vwap, short_quote.bid_vwap)
        funding = _funding_pair_metrics(long_quote, short_quote)
        funding_qualifies = _funding_pair_qualifies(
            long_quote,
            short_quote,
            min_net_funding_apr_pct,
        )
        if (
            executable is None
            or depth is None
            or (executable < min_spread_pct and not funding_qualifies)
        ):
            continue
        if _spread_ceiling_exceeded(executable, depth, max_spread_pct=max_spread_pct):
            continue
        identities = [dex_quote.identity_key, cex_quote.identity_key]
        known_identities = {identity for identity in identities if identity}
        identity = next(iter(known_identities)) if len(known_identities) == 1 else None
        inferred_identity = cex_quote.identity_source == "okx_unique_symbol_inference"
        blockers = [
            *dex_quote.blockers,
            *cex_quote.blockers,
            *pair_identity_blockers(dex_quote.token, identities),
            "route_feasibility_unproven",
            "executor_attestation_missing",
            "gas_estimate_missing",
        ]
        if not cex_quote.identity_key:
            blockers.append("cex_identity_unverified")
        if inferred_identity and max(abs(executable), abs(depth)) >= HIGH_DISLOCATION_IDENTITY_THRESHOLD_PCT:
            blockers.append("mirage_guard:high_dislocation_identity_inferred")
        rows.append(
            DiscoveryCandidate(
                token=dex_quote.token,
                long_venue=long_quote.venue,
                long_market_type=long_quote.market_type,
                short_venue=short_quote.venue,
                short_market_type=short_quote.market_type,
                source_kind=SOURCE_DEX_DISCOVERED,
                source_name=source_name,
                validation_state=IDENTITY_VERIFIED_STATE if identity else QUOTE_VERIFIED_STATE,
                quote_ts_us=min(dex_quote.quote_ts_us, cex_quote.quote_ts_us),
                executable_spread_pct=executable,
                depth_weighted_spread_pct=depth,
                funding_spread_apr_pct=funding.get("net_apr_pct"),
                funding_daily_pct=funding.get("net_daily_pct"),
                gas_adjusted_spread_pct=None,
                identity_key=identity,
                blockers=tuple(blockers),
                notes=_quote_pair_notes(long_quote, short_quote, funding=funding),
            ).to_row()
        )
    return rows


def _quote_from_book(
    *,
    token: str,
    venue: str,
    market_type: str,
    source_name: str,
    book: Mapping[str, Any],
    target_notional_usd: float,
    symbol: str | None = None,
    identity_key: str | None = None,
    identity: Any | None = None,
    source_quote: MarketQuote | None = None,
    funding: Mapping[str, Any] | None = None,
) -> MarketQuote:
    bids = _levels(book.get("bids"))
    asks = _levels(book.get("asks"))
    identity_key = getattr(identity, "identity_key", None) or identity_key
    market_identity = getattr(identity, "market_identity", None)
    source_quote = source_quote or MarketQuote(
        token=token,
        venue=venue,
        market_type=market_type,
        bid=None,
        ask=None,
        bid_vwap=None,
        ask_vwap=None,
        quote_ts_us=now_us(),
        source_name=source_name,
    )
    funding = dict(funding or {})
    contract_size = as_float(market_identity.contract_size if market_identity is not None else None)
    if contract_size is None:
        contract_size = as_float(source_quote.contract_size)
    if contract_size is None:
        contract_size = 1.0
    return MarketQuote(
        token=token,
        venue=venue,
        market_type=market_type,
        bid=bids[0][0] if bids else None,
        ask=asks[0][0] if asks else None,
        bid_vwap=depth_weighted_price(
            bids,
            target_notional_usd,
            contract_size=contract_size,
        ),
        ask_vwap=depth_weighted_price(
            asks,
            target_notional_usd,
            contract_size=contract_size,
        ),
        quote_ts_us=now_us(),
        source_name=source_name,
        symbol=symbol,
        identity_key=identity_key,
        identity_source=(
            market_identity.source if market_identity is not None else source_quote.identity_source
        ),
        decimals=market_identity.decimals if market_identity is not None else source_quote.decimals,
        chain_id=market_identity.chain_id if market_identity is not None else source_quote.chain_id,
        settle_asset=(
            market_identity.settle_asset
            if market_identity is not None
            else source_quote.settle_asset
        ),
        contract_size=(
            market_identity.contract_size
            if market_identity is not None
            else source_quote.contract_size
        ),
        funding_rate_pct=_coalesce(
            as_float(funding.get("rate_pct")), source_quote.funding_rate_pct
        ),
        funding_interval_hours=(
            _coalesce(as_float(funding.get("interval_hours")), source_quote.funding_interval_hours)
        ),
        funding_apr_pct=_coalesce(as_float(funding.get("apr_pct")), source_quote.funding_apr_pct),
        next_funding_ts_us=(
            _coalesce(_as_int(funding.get("next_funding_ts_us")), source_quote.next_funding_ts_us)
        ),
        funding_interval_assumed=bool(
            funding.get("interval_assumed", source_quote.funding_interval_assumed)
        ),
        volume_24h_usd=source_quote.volume_24h_usd,
        blockers=tuple(getattr(identity, "blockers", ()) or source_quote.blockers),
    )


def _levels(value: Any) -> list[list[float]]:
    levels: list[list[float]] = []
    if not isinstance(value, list):
        return levels
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        price = as_float(item[0])
        amount = as_float(item[1])
        if price is not None and amount is not None:
            levels.append([price, amount])
    return levels


def _zerox_route_plan(payload: Mapping[str, Any]) -> tuple[str, ...]:
    route = payload.get("route")
    fills: list[Any] = []
    if isinstance(route, Mapping):
        fills = list(route.get("fills") or [])
    elif isinstance(payload.get("sources"), list):
        fills = list(payload.get("sources") or [])
    labels: list[str] = []
    for item in fills:
        if not isinstance(item, Mapping):
            continue
        label = item.get("source") or item.get("name")
        proportion = item.get("proportionBps") or item.get("proportion")
        if label:
            labels.append(f"{label}:{proportion}" if proportion is not None else str(label))
    return tuple(labels)


def _jupiter_route_plan(payload: Mapping[str, Any]) -> tuple[str, ...]:
    labels: list[str] = []
    for item in payload.get("routePlan") or []:
        if not isinstance(item, Mapping):
            continue
        swap_info = item.get("swapInfo")
        if not isinstance(swap_info, Mapping):
            continue
        label = swap_info.get("label") or swap_info.get("ammKey")
        percent = item.get("percent")
        if label:
            labels.append(f"{label}:{percent}" if percent is not None else str(label))
    return tuple(labels)


OURBIT_HOSTS = {
    "spot": {"public": "https://api.ourbit.com", "private": "https://api.ourbit.com"},
    "spot2": {
        "public": "https://www.ourbit.com/open/api/v2",
        "private": "https://www.ourbit.com/open/api/v2",
    },
    "contract": {
        "public": "https://futures.ourbit.com/api/v1/contract",
        "private": "https://futures.ourbit.com/api/v1/private",
    },
    "broker": {"private": "https://api.ourbit.com/api/v3/broker"},
}


def _build_ourbit_exchange(params: dict[str, Any]) -> Any:
    """Ourbit has no ccxt adapter, but it is an MEXC white-label.

    Its spot API is MEXC's /api/v3 and its futures API is MEXC's
    /api/v1/contract, so a ccxt MEXC client pointed at Ourbit's hosts works
    without a bespoke REST client. Verified 2026-08-01: 831 spot + 703 swap
    markets, tickers parse correctly.
    """
    exchange = ccxt.mexc(params)
    exchange.urls = dict(exchange.urls)
    exchange.urls["api"] = {key: dict(value) for key, value in OURBIT_HOSTS.items()}
    exchange.id = "ourbit"
    return exchange


def _build_ccxt_exchange(exchange_id: str, market_type: str, timeout_s: float) -> Any:
    if exchange_id == "bitget":
        install_bitget_dns_fallback()
    aliases = {
        "gateio": ("gateio", "gate"),
        "gate": ("gate", "gateio"),
    }
    exchange_ids = aliases.get(exchange_id, (exchange_id,))
    klass = next(
        (getattr(ccxt, candidate) for candidate in exchange_ids if hasattr(ccxt, candidate)),
        None,
    )
    if klass is None and exchange_id != "ourbit":
        raise AttributeError(f"CCXT exchange adapter unavailable: {exchange_id}")
    params: dict[str, Any] = {"enableRateLimit": True, "timeout": int(timeout_s * 1000)}
    if market_type == "Spot":
        params["options"] = {"defaultType": "spot"}
    else:
        params["options"] = {"defaultType": "swap"}
    if exchange_id == "ourbit":
        return _build_ourbit_exchange(params)
    return klass(params)


def _release_ccxt_exchange(exchange: Any) -> None:
    close = getattr(exchange, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup must not hide market results.
            pass
    gc.collect()


def _find_symbol(token: str, markets: Mapping[str, Any], market_type: str) -> str | None:
    token = token.upper()
    if market_type == "Spot":
        candidates = (f"{token}/USDT", f"{token}/USDC", f"{token}/USD")
    else:
        candidates = (f"{token}/USDT:USDT", f"{token}/USDT", f"{token}/USDC:USDC")
    for candidate in candidates:
        market = markets.get(candidate)
        if market and _market_matches_type(market, candidate, market_type):
            return candidate
    prefix = f"{token}/"
    for symbol, market in markets.items():
        if str(symbol).startswith(prefix) and _market_matches_type(
            market, str(symbol), market_type
        ):
            return str(symbol)
    for symbol, market in markets.items():
        if not _market_matches_type(market, str(symbol), market_type):
            continue
        if _market_token(market, str(symbol)) == token:
            return str(symbol)
    return None


def _market_matches_type(market: Mapping[str, Any], symbol: str, market_type: str) -> bool:
    if market_type == "Spot":
        return bool(market.get("spot")) or not (
            market.get("swap") or market.get("future") or ":" in symbol
        )
    return bool(market.get("swap") or market.get("future") or ":" in symbol)


def _source_enabled(source: DiscoverySource, source_filter: set[str] | None) -> bool:
    if source_filter is None:
        return True
    kind = getattr(source, "kind", "")
    name = getattr(source, "name", "")
    if kind in source_filter or name in source_filter:
        return True
    if "dex-derivatives" in source_filter and kind == "dex_derivative":
        return True
    if "dex-spot" in source_filter and kind == "dex_spot":
        return True
    return False


def _symbols_for_context(
    markets: Mapping[str, Any],
    market_type: str,
    context: DiscoveryContext,
) -> dict[str, str]:
    if not context.all_platform_tokens:
        symbols: dict[str, str] = {}
        for token in context.tokens:
            symbol = _find_symbol(token, markets, market_type)
            if symbol is not None:
                symbols[token.upper()] = symbol
        return symbols
    return _all_market_symbols(markets, market_type)


def _canonicalize_symbol_map(
    venue: str,
    symbol_map: Mapping[str, str],
) -> dict[str, str]:
    return {
        CEX_INSTRUMENT_ALIASES.get((venue, str(token).upper()), str(token).upper()): symbol
        for token, symbol in symbol_map.items()
    }


def _all_market_symbols(markets: Mapping[str, Any], market_type: str) -> dict[str, str]:
    chosen: dict[str, tuple[int, str]] = {}
    for symbol, market in markets.items():
        symbol_text = str(symbol)
        if not _market_matches_type(market, symbol_text, market_type):
            continue
        if market.get("active") is False:
            continue
        quote = _market_quote(market, symbol_text)
        priority = QUOTE_CURRENCY_PRIORITY.get(quote)
        if priority is None:
            continue
        token = _market_token(market, symbol_text)
        if not token:
            continue
        current = chosen.get(token)
        if current is None or priority < current[0]:
            chosen[token] = (priority, symbol_text)
    return {token: symbol for token, (_, symbol) in chosen.items()}


def _symbol_items(symbol_map: Mapping[str, str]) -> Iterable[tuple[str, str]]:
    for token, symbol in symbol_map.items():
        yield str(token).upper(), str(symbol)


def _market_token(market: Mapping[str, Any], symbol: str) -> str:
    base = str(market.get("base") or symbol.split("/", 1)[0]).upper()
    return base


def _market_quote(market: Mapping[str, Any], symbol: str) -> str:
    quote = str(market.get("quote") or "").upper()
    if quote:
        return quote
    if "/" not in symbol:
        return ""
    right = symbol.split("/", 1)[1].split(":", 1)[0]
    return right.upper()


def _ticker_quotes_for_symbols(
    *,
    exchange: Any,
    venue: str,
    market_type: str,
    source_name: str,
    symbol_map: Mapping[str, str],
    funding_rates: Mapping[str, Any] | None,
    errors: list[str],
    context: DiscoveryContext,
) -> list[MarketQuote]:
    if not symbol_map:
        return []
    tickers = _fetch_tickers(exchange, list(symbol_map.values()), context, errors, venue)
    quotes: list[MarketQuote] = []
    symbols_by_value = {symbol: token for token, symbol in symbol_map.items()}
    for symbol, ticker in tickers.items():
        token = symbols_by_value.get(str(symbol))
        if not token or not isinstance(ticker, Mapping):
            continue
        bid = as_float(ticker.get("bid"))
        ask = as_float(ticker.get("ask"))
        if bid is None or ask is None:
            midpoint = (
                as_float(ticker.get("markPrice"))
                or as_float(ticker.get("last"))
                or as_float(ticker.get("close"))
            )
            bid = bid if bid is not None else midpoint
            ask = ask if ask is not None else midpoint
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            continue
        identity = _resolve_quote_identity(
            token=token,
            venue=venue,
            market_type=market_type,
            symbol=str(symbol),
            context=context,
        )
        market_identity = identity.market_identity
        market = getattr(exchange, "markets", {}).get(str(symbol)) or {}
        market_contract_size = (
            str(market.get("contractSize"))
            if market_type == "Futures" and market.get("contractSize") is not None
            else None
        )
        funding = _funding_values((funding_rates or {}).get(str(symbol)))
        volume_24h_usd = _ticker_volume_24h_usd(ticker)
        quotes.append(
            MarketQuote(
                token=token,
                venue=venue,
                market_type=market_type,
                bid=bid,
                ask=ask,
                bid_vwap=bid,
                ask_vwap=ask,
                quote_ts_us=now_us(),
                source_name=source_name,
                symbol=str(symbol),
                identity_key=identity.identity_key,
                identity_source=market_identity.source if market_identity is not None else None,
                decimals=market_identity.decimals if market_identity is not None else None,
                chain_id=market_identity.chain_id if market_identity is not None else None,
                settle_asset=market_identity.settle_asset if market_identity is not None else None,
                contract_size=(
                    market_identity.contract_size
                    if market_identity is not None and market_identity.contract_size is not None
                    else market_contract_size
                ),
                funding_rate_pct=as_float(funding.get("rate_pct")),
                funding_interval_hours=as_float(funding.get("interval_hours")),
                funding_apr_pct=as_float(funding.get("apr_pct")),
                next_funding_ts_us=_as_int(funding.get("next_funding_ts_us")),
                funding_interval_assumed=bool(funding.get("interval_assumed", False)),
                volume_24h_usd=volume_24h_usd,
                blockers=identity.blockers,
            )
        )
    return quotes


def _fetch_funding_rates(
    exchange: Any,
    symbols: Sequence[str],
    context: DiscoveryContext,
    errors: list[str],
    venue: str,
) -> Mapping[str, Any]:
    if context.timed_out() or not symbols:
        return {}
    native = _fetch_native_bulk_funding_rates(
        exchange,
        symbols,
        context=context,
        errors=errors,
        venue=venue,
    )
    if native is not None:
        return native
    method = getattr(exchange, "fetch_funding_rates", None)
    has_bulk = bool(getattr(exchange, "has", {}).get("fetchFundingRates"))
    if not callable(method) or not has_bulk:
        return {}
    try:
        try:
            payload = method(list(symbols))
        except TypeError:
            payload = method()
    except Exception as exc:
        errors.append(f"{venue}:funding_rates:{clean_error(exc)}")
        return {}
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, list):
        return {
            str(item.get("symbol")): item
            for item in payload
            if isinstance(item, Mapping) and item.get("symbol")
        }
    return {}


def _fetch_native_bulk_funding_rates(
    exchange: Any,
    symbols: Sequence[str],
    *,
    context: DiscoveryContext,
    errors: list[str],
    venue: str,
) -> Mapping[str, Any] | None:
    specs: dict[str, tuple[str, str, str, str, float]] = {
        "Mexc": (
            "https://contract.mexc.com/api/v1/contract/funding_rate",
            "data",
            "symbol",
            "fundingRate",
            1.0,
        ),
        "Kucoin Futures": (
            "https://api-futures.kucoin.com/api/v1/contracts/active",
            "data",
            "symbol",
            "fundingFeeRate",
            1.0,
        ),
        "HTX": (
            "https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate",
            "data",
            "contract_code",
            "funding_rate",
            1.0,
        ),
        "Phemex": (
            "https://api.phemex.com/md/v2/ticker/24hr/all",
            "result",
            "symbol",
            "fundingRateRr",
            1.0,
        ),
        "CoinEx": (
            "https://api.coinex.com/v2/futures/funding-rate",
            "data",
            "market",
            "next_funding_rate",
            1.0,
        ),
        "WhiteBIT": (
            "https://whitebit.com/api/v4/public/futures",
            "result",
            "ticker_id",
            "funding_rate",
            1.0,
        ),
        "BitMart": (
            "https://api-cloud-v2.bitmart.com/contract/public/details",
            "data.symbols",
            "symbol",
            "funding_rate",
            1.0,
        ),
        "Coinbase International": (
            "https://api.international.coinbase.com/api/v1/instruments",
            "",
            "symbol",
            "quote.predicted_funding",
            1.0,
        ),
    }
    spec = specs.get(venue)
    if spec is None:
        return None
    url, rows_path, id_path, rate_path, multiplier = spec
    try:
        payload = fetch_json(
            url,
            {"Accept": "application/json", "User-Agent": "SpreadBoard/1.0"},
            context.remaining_timeout(12.0),
        )
        rows = _nested_value(payload, rows_path)
        if not isinstance(rows, list):
            raise ValueError("funding payload did not contain a row list")
        by_market_id = {
            _normalize_market_id(_nested_value(row, id_path)): row
            for row in rows
            if isinstance(row, Mapping) and _nested_value(row, id_path)
        }
        result: dict[str, Any] = {}
        markets = getattr(exchange, "markets", {}) or {}
        for symbol in symbols:
            market = markets.get(symbol) or {}
            market_id = _normalize_market_id(market.get("id"))
            item = by_market_id.get(market_id)
            rate = as_float(_nested_value(item, rate_path)) if item else None
            if rate is None:
                continue
            interval_hours = _native_funding_interval_hours(venue, item)
            next_funding_ms = _native_next_funding_ms(venue, item)
            result[str(symbol)] = {
                "fundingRate": rate * multiplier,
                "interval": f"{interval_hours:g}h" if interval_hours else None,
                "nextFundingTimestamp": next_funding_ms,
                "info": item,
            }
        return result
    except Exception as exc:
        errors.append(f"{venue}:native_funding_rates:{clean_error(exc)}")
        return None


def _nested_value(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for key in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _normalize_market_id(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _native_funding_interval_hours(venue: str, item: Mapping[str, Any]) -> float | None:
    if venue == "Mexc":
        return as_float(item.get("collectCycle"))
    if venue == "Kucoin Futures":
        value = as_float(
            item.get("currentFundingRateGranularity") or item.get("fundingRateGranularity")
        )
        return value / 3_600_000.0 if value else None
    if venue == "WhiteBIT":
        value = as_float(item.get("funding_interval_minutes"))
        return value / 60.0 if value else None
    if venue == "BitMart":
        return as_float(item.get("funding_interval_hours"))
    if venue == "Coinbase International":
        value = as_float(item.get("funding_interval"))
        return value / 3_600_000_000_000.0 if value else None
    if venue == "HTX":
        return _timestamp_interval_hours(item.get("funding_time"), item.get("next_funding_time"))
    return 8.0


def _native_next_funding_ms(venue: str, item: Mapping[str, Any]) -> int | None:
    keys = {
        "Mexc": ("nextSettleTime",),
        "Kucoin Futures": ("nextFundingRateDateTime",),
        "HTX": ("next_funding_time", "funding_time"),
        "CoinEx": ("next_funding_time",),
        "WhiteBIT": ("next_funding_rate_timestamp",),
        "BitMart": ("funding_time",),
    }.get(venue, ())
    for key in keys:
        value = _as_int(item.get(key))
        if value is not None:
            return value
    return None


def _timestamp_interval_hours(current: Any, upcoming: Any) -> float | None:
    current_ms = as_float(current)
    upcoming_ms = as_float(upcoming)
    if current_ms is None or upcoming_ms is None or upcoming_ms <= current_ms:
        return None
    return (upcoming_ms - current_ms) / 3_600_000.0


def _funding_values(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    info = value.get("info") if isinstance(value.get("info"), Mapping) else {}
    rate = as_float(
        _first_present(
            value.get("fundingRate"),
            value.get("predictedFundingRate"),
            info.get("fundingRate"),
            info.get("currentFundingRate"),
        )
    )
    if rate is None:
        return {}
    raw_interval = (
        value.get("interval")
        or value.get("fundingInterval")
        or value.get("fundingIntervalHour")
        or value.get("fundingIntervalHours")
        or info.get("fundingInterval")
        or info.get("fundingIntervalHour")
        or info.get("fundingIntervalHours")
        or info.get("funding_interval")
        or info.get("funding_interval_hours")
    )
    interval_hours = _funding_interval_hours(raw_interval)
    interval_assumed = interval_hours is None
    interval_hours = interval_hours or 8.0
    next_ts = _as_int(value.get("nextFundingTimestamp") or value.get("fundingTimestamp"))
    if next_ts is not None and next_ts < 10**15:
        next_ts *= 1000
    rate_pct = rate * 100.0
    return {
        "rate_pct": rate_pct,
        "interval_hours": interval_hours,
        "interval_assumed": interval_assumed,
        "apr_pct": rate_pct * (24.0 / interval_hours) * 365.0,
        "next_funding_ts_us": next_ts,
    }


def _ticker_volume_24h_usd(ticker: Mapping[str, Any]) -> float | None:
    quote_volume = as_float(
        _first_present(
            ticker.get("quoteVolume"),
            ticker.get("quote_volume"),
            (ticker.get("info") or {}).get("quoteVolume")
            if isinstance(ticker.get("info"), Mapping)
            else None,
            (ticker.get("info") or {}).get("turnover24h")
            if isinstance(ticker.get("info"), Mapping)
            else None,
            (ticker.get("info") or {}).get("turnover")
            if isinstance(ticker.get("info"), Mapping)
            else None,
        )
    )
    if quote_volume is not None and quote_volume >= 0:
        return quote_volume
    base_volume = as_float(ticker.get("baseVolume") or ticker.get("base_volume"))
    last = as_float(
        _first_present(
            ticker.get("last"),
            ticker.get("close"),
            ticker.get("markPrice"),
        )
    )
    if base_volume is None or last is None or base_volume < 0 or last <= 0:
        return None
    return base_volume * last


def _funding_interval_hours(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        if parsed > 86_400:
            return parsed / 3_600_000.0
        return parsed if parsed > 0 else None
    text = str(value).strip().casefold()
    try:
        if text.endswith("h"):
            return float(text[:-1])
        if text.endswith("m"):
            return float(text[:-1]) / 60.0
        return float(text)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coalesce(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value


def _first_present(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _fetch_tickers(
    exchange: Any,
    symbols: Sequence[str],
    context: DiscoveryContext,
    errors: list[str],
    venue: str,
) -> Mapping[str, Any]:
    if context.timed_out():
        errors.append("time_budget_exhausted")
        return {}
    try:
        if getattr(exchange, "has", {}).get("fetchTickers"):
            try:
                return exchange.fetch_tickers(list(symbols))
            except TypeError:
                return exchange.fetch_tickers()
            except Exception:
                return exchange.fetch_tickers()
    except Exception as exc:
        errors.append(f"{venue}:tickers:{clean_error(exc)}")
    tickers: dict[str, Any] = {}
    for symbol in symbols:
        if context.timed_out():
            errors.append("time_budget_exhausted")
            break
        try:
            tickers[symbol] = exchange.fetch_ticker(symbol)
        except Exception as exc:
            errors.append(f"{venue}:{symbol}:ticker:{clean_error(exc)}")
    return tickers


def _verify_top_candidate_books(
    quotes: list[MarketQuote],
    *,
    candidate_quotes: list[MarketQuote] | None = None,
    exchange_ids: Mapping[str, str],
    symbols_by_venue_token: Mapping[tuple[str, str], str],
    source_name: str,
    market_type: str,
    target_notional_usd: float,
    min_spread_pct: float,
    max_candidates: int,
    errors: list[str],
    context: DiscoveryContext,
) -> list[MarketQuote]:
    pairs = quote_candidate_pairs(
        candidate_quotes or quotes,
        min_spread_pct=min_spread_pct,
        max_spread_pct=context.max_spread_pct,
        min_net_funding_apr_pct=context.min_funding_apr_pct,
    )
    pairs = [
        pair
        for pair in pairs
        if market_type in {pair.long_quote.market_type, pair.short_quote.market_type}
    ]
    if max_candidates <= 0:
        selected_pairs = pairs
    else:
        spread_slots = max(1, int(max_candidates * 0.75))
        funding_slots = max(1, max_candidates - spread_slots)
        spread_pairs = _balanced_route_candidates(
            sorted(
                pairs,
                key=lambda pair: (
                    _candidate_is_publicly_rankable(pair),
                    pair.depth_weighted_spread_pct,
                ),
                reverse=True,
            ),
            spread_slots,
        )
        funding_pairs = _unique_token_first(
            sorted(
                (pair for pair in pairs if _pair_net_funding_apr(pair) is not None),
                key=lambda pair: _pair_net_funding_apr(pair) or -999999.0,
                reverse=True,
            ),
            funding_slots,
        )
        selected_pairs = list(
            {
                (
                    pair.long_quote.venue,
                    pair.long_quote.market_type,
                    pair.short_quote.venue,
                    pair.short_quote.market_type,
                    pair.token,
                ): pair
                for pair in [*spread_pairs, *funding_pairs]
            }.values()
        )
    selected_by_venue: dict[str, list[MarketQuote]] = defaultdict(list)
    for pair in selected_pairs:
        for quote in (pair.long_quote, pair.short_quote):
            if quote.market_type != market_type or quote.source_name != source_name:
                continue
            selected_by_venue[quote.venue].append(quote)
    # DEX comparison cannot depend on a CEX-to-CEX route first winning the
    # general spread budget. Exact registry matches get one matched-size book
    # check so every known CEX leg can reach the later OKX DEX source.
    for quote in _exact_dex_watchlist_quotes(quotes, context):
        if quote.market_type == market_type and quote.source_name == source_name:
            selected_by_venue[quote.venue].append(quote)
    # Once a token wins the global candidate budget, fetch every leg owned by
    # this source batch. This preserves alternate venue routes (for example
    # Gate and Kucoin spot against the same Bybit perp) without deep-fetching
    # every market on every exchange.
    expanded_tokens: list[str] = []
    expanded_limit = max(10, min(30, max_candidates // 3 if max_candidates > 0 else 30))
    for pair in selected_pairs:
        token = pair.token.upper()
        if token not in expanded_tokens:
            expanded_tokens.append(token)
        if len(expanded_tokens) >= expanded_limit:
            break
    expanded_token_set = set(expanded_tokens)
    for quote in quotes:
        if (
            quote.token.upper() in expanded_token_set
            and quote.market_type == market_type
            and quote.source_name == source_name
        ):
            selected_by_venue[quote.venue].append(quote)

    verified: dict[tuple[str, str, str], MarketQuote] = {}
    for venue, venue_quotes in selected_by_venue.items():
        if context.timed_out():
            errors.append("time_budget_exhausted")
            break
        exchange_id = exchange_ids.get(venue)
        if exchange_id is None:
            continue
        try:
            exchange = _build_ccxt_exchange(
                exchange_id,
                market_type,
                context.remaining_timeout(10.0),
            )
            exchange.load_markets()
        except Exception as exc:
            errors.append(f"{venue}:depth_market:{clean_error(exc)}")
            continue
        try:
            for quote in venue_quotes:
                key = (quote.venue, quote.market_type, quote.token.upper())
                if key in verified:
                    continue
                symbol = quote.symbol or symbols_by_venue_token.get(
                    (quote.venue, quote.token.upper())
                )
                if symbol is None:
                    continue
                try:
                    book = exchange.fetch_order_book(symbol, limit=20)
                    book_quote = _quote_from_book(
                        token=quote.token,
                        venue=quote.venue,
                        market_type=quote.market_type,
                        source_name=source_name,
                        book=book,
                        target_notional_usd=target_notional_usd,
                        symbol=symbol,
                        identity_key=quote.identity_key,
                        source_quote=quote,
                    )
                    if book_quote.bid is not None and book_quote.ask is not None:
                        verified[key] = book_quote
                except Exception as exc:
                    errors.append(f"{quote.venue}:{quote.token}:order_book:{clean_error(exc)}")
        finally:
            _release_ccxt_exchange(exchange)
    return list(verified.values())


def _unique_token_first(
    pairs: Iterable[QuoteCandidatePair],
    limit: int,
) -> list[QuoteCandidatePair]:
    """Use scarce depth checks on distinct assets before extra venue permutations."""

    ranked = list(pairs)
    if limit <= 0:
        return ranked
    selected: list[QuoteCandidatePair] = []
    deferred: list[QuoteCandidatePair] = []
    seen_tokens: set[str] = set()
    for pair in ranked:
        token = pair.token.upper()
        if token in seen_tokens:
            deferred.append(pair)
            continue
        seen_tokens.add(token)
        selected.append(pair)
        if len(selected) >= limit:
            return selected
    selected.extend(deferred[: max(0, limit - len(selected))])
    return selected


def _exact_dex_watchlist_quotes(
    quotes: Iterable[MarketQuote],
    context: DiscoveryContext,
) -> list[MarketQuote]:
    exact_identity_keys = {
        str(asset.identity_key)
        for asset in context.watchlist.values()
        if asset.dex_enabled and asset.identity_key
    }
    return [
        quote
        for quote in quotes
        if quote.identity_key and str(quote.identity_key) in exact_identity_keys
    ]


def _balanced_route_candidates(
    pairs: Iterable[QuoteCandidatePair],
    limit: int,
) -> list[QuoteCandidatePair]:
    """Keep one route class from consuming every matched-depth check."""

    ranked = list(pairs)
    if limit <= 0:
        return ranked
    lanes: dict[str, list[QuoteCandidatePair]] = defaultdict(list)
    for pair in ranked:
        market_types = {
            pair.long_quote.market_type,
            pair.short_quote.market_type,
        }
        if market_types == {"Futures"}:
            lane = "FUTURES"
        elif market_types == {"Spot"}:
            lane = "SPOT"
        else:
            lane = "FUTURES-SPOT"
        lanes[lane].append(pair)
    active = [lane for lane in ("FUTURES", "FUTURES-SPOT", "SPOT") if lanes[lane]]
    if not active:
        return []
    base, remainder = divmod(limit, len(active))
    selected: list[QuoteCandidatePair] = []
    for index, lane in enumerate(active):
        lane_limit = base + (1 if index < remainder else 0)
        selected.extend(_unique_token_first(lanes[lane], lane_limit))
    return selected[:limit]


def _candidate_is_publicly_rankable(pair: QuoteCandidatePair) -> bool:
    """Keep obvious mirages from consuming the limited order-book budget."""

    dislocation = max(
        abs(pair.executable_spread_pct),
        abs(pair.depth_weighted_spread_pct),
    )
    if dislocation < HIGH_DISLOCATION_IDENTITY_THRESHOLD_PCT:
        return True
    identities = {
        identity
        for identity in (
            pair.long_quote.identity_key,
            pair.short_quote.identity_key,
        )
        if identity
    }
    return len(identities) == 1 and all(
        quote.identity_key in identities for quote in (pair.long_quote, pair.short_quote)
    )


def _pair_net_funding_apr(pair: QuoteCandidatePair) -> float | None:
    return as_float(_funding_pair_metrics(pair.long_quote, pair.short_quote).get("net_apr_pct"))


def _dex_assets(watchlist: dict[str, WatchAsset]) -> list[WatchAsset]:
    return [asset for asset in watchlist.values() if asset.dex_enabled]
