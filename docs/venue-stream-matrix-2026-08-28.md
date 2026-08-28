# Venue and market-leg stream matrix — measured 2026-08-28

All figures are first-party, measured from the production discovery snapshot
(`api_discovery_latest.json`, 22,111 rows) and the live route index
(`live-route-index-current.json`). Nothing here is inferred from a CCXT method
name; venue capability claims that would require official documentation are
marked `unverified` rather than guessed.

## Headline

| Measure | Value |
|---|---|
| Catalogue markets | 23,004 |
| Markets with a fresh book (180 s gate) | 20,981 (**91.21 %**) |
| Missing books | 2,023 |
| Live route index rows | 112,434 |
| Catalogue kinds | FUTURES 6,504 · FUTURES-SPOT 7,730 · SPOT-FUTURES 7,730 |
| Distinct tokens | 1,433 |
| Venue/market-type combinations | 39 |

Retired Spot-Spot and Spot-DEX do not appear in the catalogue kinds, confirming
the product-lane retirement holds at the generation layer.

## The adapter boundary — why 100 % streaming is not currently reachable

`spreadboard.fast_quotes.VENUE_IDS` carries **21** venues. Three venues in the
catalogue have **no CCXT adapter at all**, so they cannot use the `ccxt.pro`
`watch_order_book` path that `scripts/websocket_book_worker.py` depends on:

| Venue | Market type | Leg occurrences | Path today |
|---|---|---|---|
| Ourbit | Futures | 1,955 | direct REST (`contract.ourbit.com`) |
| Upbit | Spot | 660 | REST only |
| Lighter | Futures | 51 | REST only |
| **Total** | | **2,666 (6.0 %)** | |

Ourbit is **Futures-only** in the catalogue — there is no Ourbit Spot at all.
That is a hard, evidence-backed answer to the `CHZ` Ourbit-Spot comparator
absence, and it is not a bug to fix but a venue we do not carry.

## Bulk-quote classes (from `spreadboard.bulk_quotes`)

| Class | Venues |
|---|---|
| Native complete bulk | Binance, Kraken Futures, Kucoin Futures, WhiteBIT, XT |
| Native partial bulk | Phemex |
| Native futures-only bulk | Hyperliquid |
| Skipped | Coinbase |
| CCXT `fetch_tickers` | the remaining `VENUE_IDS` entries |

## Leg distribution by venue and market type

Leg occurrences across the 22,111 discovery rows (a leg is
`venue + market_type`; one fresh book reprices every route touching it, which is
why the scalable unit is the leg and not the pair):

```
Mexc Futures 3018   Gate Futures 2616   Bingx Futures 2583   Bybit Futures 2479
Bitget Futures 2382 Mexc Spot 2278      Gate Spot 2164       WhiteBIT Futures 2062
HTX Spot 2038       Ourbit Futures 1955 Kucoin Spot 1940     Kucoin Fut 1641
XT Futures 1611     Bingx Spot 1497     Binance Futures 1367 OKX Futures 1348
HTX Futures 1213    Binance Spot 1170   Bitget Spot 1159     Phemex Spot 1035
Aster Futures 950   Bybit Spot 922      WhiteBIT Spot 696    OKX Spot 683
Upbit Spot 660      CoinEx Spot 651     Hyperliquid Fut 333  Phemex Futures 255
XT Spot 249         Kraken Spot 220     CoinEx Futures 184   Kraken Fut 157
Coinbase Intl 90    Coinbase Spot 62    Lighter Futures 51
```

## OKX DEX — separately budgeted, never a CEX stream

OKX DEX legs are split by chain id and are **request/response quotes**, not an
order-book stream:

| Chain | Legs |
|---|---|
| OKX DEX 56 (BSC) | 243 |
| OKX DEX 1 (Ethereum) | 186 |
| OKX DEX 501 (Solana) | 67 |
| OKX DEX 8453 (Base) | 7 |
| **Total** | **503** |

* Endpoint: `https://web3.okx.com/api/v6/dex/aggregator/quote`, signed per
  request (`src/spreadarb/dex/okx_quotes.py`).
* Cadence: the fast-quote loop runs every 60 s
  (`SPREADBOARD_FAST_QUOTE_SECONDS`), selecting 93 routes with
  `SPREADBOARD_FAST_DEX_ROUTES=90`, `SPREADBOARD_FAST_DEX_CONTRACTS=45`,
  `SPREADBOARD_DEX_QUOTE_CHUNK_SIZE=8`.
* Measured throughput: 84-85 routes updated per completed cycle, 35-37
  DEX-FUTURES tokens current, matched notional 500 USD.
* **Cost envelope:** at one quote call per updated route and one cycle per
  minute, ~85 x 60 x 24 x 30 = **~3.67 M calls/month**; two-sided quoting for
  rows lacking cached decimals raises the ceiling toward ~7.3 M.

**Unverified and owner-blocking:** the OKX DEX v6 aggregator rate limit and
free-tier allowance are not recorded anywhere in this repository, and this audit
did not fetch OKX's official documentation. The call volume above is measured
first-party fact; whether it fits a free tier is **not established**. Do not
describe the DEX plan as free until that quota is confirmed against OKX's
published limits.

## Why literal 100 % streaming is not promised

1. 6.0 % of legs sit on venues with no CCXT adapter (above).
2. `scripts/websocket_book_worker.py` subscribes at most
   `SPREADBOARD_WS_BOOKS=160` books; the catalogue holds 23,004 markets. The
   remaining tail is served by venue-wide bulk passes.
3. The collector's 4 GiB cgroup is already fully committed (see
   `spreadarb-spreadboard-oom-concurrency-fix-2026-08-28`), so raising the
   subscription budget inside the collector is what previously caused OOM
   kills. A dedicated lightweight stream service must be measured first.
4. OKX DEX is request/response and cannot be subscribed at all.

Achieved first-party coverage today is **91.21 % of catalogue markets carrying a
book fresher than 180 s**, measured on a completed generation.
