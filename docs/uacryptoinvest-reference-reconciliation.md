# UACryptoInvest reference reconciliation

Last sampled: 2026-08-28 12:03 UTC.

UACryptoInvest is a useful discovery oracle, not an execution oracle. A route
seen there becomes a reconciliation candidate. SpreadBoard still admits it only
after the exact live instruments, direction, current books, token identity and
product-specific evidence gates pass.

## Sample result

The sampled leading Futures-Futures list included GIGADEVSTOCK, NES, ACE,
ANTHROPIC, ONE, HOME, XCU, BASECAT, RVN, FONE, ONT, BABY, SHEINSTOCK, BARD,
NG, ONG, STORJ and EDEN. The sampled Spot-Futures list included OPENAI, SPCX,
CATE, BASECAT, FONE, TAC, KII, QUID, BISCOTTI, FOLKS, AURASOL, ALIGN, ARIA,
4, POWER, XPIN, MARSCOIN and BLUAI.

Most exact venue pairs were present in SpreadBoard. The apparent ticker gaps
for `GIGADEVSTOCK` and `SHEINSTOCK` were naming differences: Hyperliquid's
builder DEX and Ourbit publish `GIGADEV` and `SHEIN`, which SpreadBoard keeps
as the exact exchange tickers. `XCU`, `FONE`, `NG`, `ONG` and `EDEN` existed in
the structural route catalogue; where an exact pair briefly did not render,
the cause was current-book/materialized-generation timing or the spread moving
below the selected threshold, not missing market discovery.

No competitor headline is allowed to bypass the current 5% tokenized-
instrument price/identity guard. A large mismatch can indicate a different
economic instrument even when the display ticker looks similar.

## Repeatable triage

For each sampled competitor leader:

1. Normalize only display aliases; preserve the official venue ticker and exact
   market symbol in stored identity.
2. Look for both exact markets in the chart catalogue.
3. Look for the exact directed pair in the structural route index.
4. Overlay simultaneous current books and reproduce top-of-book and matched
   notional economics.
5. Trace any absence through discovery, book residency, identity/deduplication,
   public evidence filters, threshold filtering and materialized rendering.
6. Fix a missing adapter, classification or generation defect; accept an
   exclusion when current evidence really fails, and record the reason.

This comparison should be sampled periodically across the top Futures-Futures,
Futures-Spot and Futures-DEX rows. It is not a reason to copy stale prices,
invent token aliases, or weaken evidence gates.

## Retired pair products

Standalone Spot-Spot and Spot-DEX are no longer public products. In the
production route artifact sampled before retirement they occupied 22,148 of
147,702 rows (14.99%): 21,853 Spot-Spot and 295 Spot-DEX. They accounted for
approximately 52 MB of the 330 MB raw JSON artifact, before Python object
overhead.

Spot instruments and spot books remain collected for Futures-Spot routes,
charts, portfolio marks and token-price alerts. Only standalone Spot-Spot and
Spot-DEX route generation, resident installation, ranking, warming, navigation
and public catalogue output are retired.
