# SpreadBoard / UA comparison, 2026-09-05

The owner chose continued Codex implementation after the earlier Claude handover.
The goal remains open. The recurring Codex stability task stays paused.

## Comparable counts

The production audit counted **2,171 futures token labels and 3,965 spot token
labels**, not 142,711 tokens. The latter number was directed economic routes.
There were 22,411 exact market keys, no duplicate market keys and no duplicate
economic directed routes in that captured generation. The owner's UA counts
(2,534 futures / 4,370 spot) therefore do not establish overcollection on our side.
Different quote contracts, market types, venues and directions must not be
silently collapsed as if they were duplicate observations.

The expensive structure was repeated rich route data: 347.6 MB index JSON beside
a 3.32 MB market catalogue. That is the measured optimization target. Historical
counts are not a claim about current coverage; rerun the streamed cardinality
probe after later market-definition and index generations.

## UA to SpreadBoard

Reference: https://uacryptoinvest.com/arbitrage, visible browser tables, 45 exact
venue/type route examples. Fifteen Spot-Futures leaders were captured 16:48:48 UTC,
15 Futures spread leaders 16:51:18 UTC, and 15 Futures funding leaders 17:20:18 UTC.
This is a deliberately inspected sample, not a claim to have read every UA row
or its premium DEX surfaces. Capture times differ, so displayed prices are not
expected to match exactly across the two sites.

After normalizing venue aliases, **36/36 admissible, in-scope reference routes
had an indexed counterpart** at the 17:56 probe. Other differences:

| Exact example | Evidence and disposition |
| --- | --- |
| STONKS MEXC Spot → Aster Futures | Aster's public market definition was active; our old catalogue omitted it. Genuine definition lag. |
| CATE, ASTEROID, ANSEM, ALIGN positive funding | The 500-token cache cutoff excluded some eligible tokens. Membership moves with rates, so this is not a permanent per-token exclusion list. |
| KAITO WhiteBIT → Hyperliquid | A fresh catalogue pair set `quote_mismatch` for USDT/USDC even though construction admitted that dollar basis. A legacy scanner row could still appear. Fix applies to both Spreads and Funding; do not claim KAITO was absent everywhere. |
| ESPORTS Kucoin Futures → Bitget | Present. The first probe treated `Kucoin Futures` and `Kucoin` as different venues. Corrected with `coverage_reconciliation.normalize_venue`; no missing-market claim remains. |
| MICRODUCK / ROBINCAT Ourbit Spot routes | Intentional owner-requested Ourbit exclusion. |
| NAORIS / JCT / ESPORTS BinanceAlpha Spot routes | Exact venue outside our chosen coverage. Other venue routes are separate candidates. |
| BONER MEXC Spot → Gate Futures | MEXC BONER/USDT was inactive in direct definitions; BONER/USD1 was active but is a different quote market outside the present quote set. |
| ANSEM Aster Futures → Bingx Futures | Bingx's exact ANSEM contract was inactive in direct definitions. |
| OPENAI Gate Spot → Gate Futures | Gate spot ~867.6 versus perp ~1420.71 and its own index ~1420.77. Identity/index guard correctly excluded this apparent ~63% spread. |

UA's displayed F Spread and APR in the captured examples use an eight-hour
comparison: APR / 365 is the comparable daily projection. Our live arithmetic
normalizes each leg by its own interval, then subtracts long from short. A raw
2-hour or 1-hour funding rate is not directly comparable with an 8-hour rate.
Exact 1d/7d/30d columns remain separate settlement totals; missing windows stay
blank. No extrapolated current rate may fill those windows.

## SpreadBoard to UA

Most captured Futures funding leaders overlapped by token, although the selected
hedge, venue, quote contract and observation time differed. Direct searches for
1000BTT and BTT in UA Futures, and LCAP in UA Spot-Futures, returned no visible
rows in this anonymous session with no minimum-spread value entered. UA lists
both Kraken and XT in its referral menu; that menu is not proof of exact-market
coverage. Do not invent a reason for those absences. Our reverse futures-long /
spot-short rows also require inventory or borrow; they are research candidates,
not a statement that an account can enter them.

Kraken's unusually large negative rates were checked against its public tickers.
For PF_LCAPUSD at 17:53:56 UTC, raw hourly funding was -0.0345315 quote units per
base unit, mark ~6.998391, index ~6.9291. The adapter's percent-of-current-notional
normalization can therefore produce a daily projection near 12%. This is not a
100x arithmetic error or a guaranteed daily return. Kraken documents absolute
per-unit hourly funding and a +/-0.50% hourly relative limit:
- https://docs.kraken.com/api-reference/market-data/get-tickers
- https://support.kraken.com/articles/4844359082772-linear-multi-collateral-derivatives-contract-specifications?mode=consumerapp

The bulk adapter uses current mark as denominator while the point adapter uses
index; their exact relative-basis convention still needs reconciliation. Do not
silently label these current-notional calculations as Kraken's historical
funding-calculation-time relative rate.

## Implemented release and further correction

`ded65fa`, digest `269a74fa24ddb8b4`, deployed 18:04:37 UTC after both protected
worker guards cleared. Full suite 2,441 passed; Ruff 517 unchanged. It fixed
quote flags, removed the token cutoff and reduced each token before constructing
the next. The hourly code default did **not** change production's explicit
21,600-second Compose override. The fresh whitelist configuration check found
that oversight, and the next candidate corrects the actual Compose value. Its
production-config refresh-loop test fails against the old override.

At ~18:11 UTC, web-role probe: 149 sampled positive rows, zero exact-leg funding
math errors, twelve nonempty navigation views and zero Ourbit. The cache was
still the prior 500-token generation, so expanded production/UI coverage was
not yet accepted. A collector-role diagnostic initially compared persisted
rates with live rates; that was the wrong reader mode. The corrected probe uses
web role and one coherent live-rate generation, preserving the failed diagnostic
as evidence rather than calling it a website arithmetic bug.

A further real gap remained: three long alternatives per short were selected
using build-time rates. That can hide a later current winner, historical winner
or requested exchange. The new local candidate retains **every eligible pair**
losslessly and caps the display only after current/window/filter selection.
It processes ranking rows in bounded batches and chooses distinct exact short
contracts on the ranked list; exact-token detail retains all long alternatives.

Frozen public replay (17:34:57 snapshot; not fresh prices):

| Storage / build | Eligible retained pairs | Bytes | Builder peak |
| --- | ---: | ---: | ---: |
| Prior 500-token build | 18,246 | 37,491,658 JSON | 337.4 MiB |
| All tokens, three-long lane reduction | 45,909 | 95,603,889 JSON | 241.5 MiB |
| Lossless full-candidate packing experiment | 219,871 | 34,058,760 compressed | 146.2 MiB |

Actual new publication pipeline wrote a 47,952,096-byte JSON envelope (includes
base64 and token metadata). Build 28.18s; restore 4.18s; full Now query 11.29s;
12-view navigation 32.36s; whole pipeline peak 295.9 MiB. The later integrity
validation also checks advertised zero-row blocks before publication, so repeat
these timings on the final source. Replay lacks production's full historical
radar/archive and resident price overlay. Expanded scan CPU is a real trade-off;
measure production response time and child peaks before accepting it.

## Remaining acceptance

- Local final full suite: 2,457 passed in 261.79s, exit 0; Ruff 517 unchanged.
  Old shortlist, unpacked storage, eager batching, duplicate short display and
  zero-row validation mutants all caught. Actual production cadence and eager
  archive regression tests both failed before correction. Guarded deployment
  and digest parity for this candidate still pending.
- New catalogue generation actually loaded by readers, positive reference tokens
  discoverable, active Aster STONKS definitions and exact route arriving normally.
- Current and historical rankings independently complete; exact-filter and retained
  history paths validated against real data, not just the reference sample.
- Explicit explanation or unresolved status for reverse-comparison absences.
- Startup response time, steady query CPU and memory; safe lower caps only after
  evidence. No cgroup or subscription increase, spend, Pushover or trading.
- Deployment-free count gate and final clean 48-hour run. The finite read-only
  18:05–20:05 candidate sampler is preliminary; another release splits its window.

Local evidence is in `output/stability-20260905/`: `ua-reference-cases.json`,
`ua-cases-before-normalized.jsonl`, `ua-cases-coverage-deploy.jsonl`,
`ua-market-definitions.jsonl`, `kraken-funding-direct.json`,
`coverage-funding-after-web-role.jsonl`, `coverage-deploy.txt`, packed replay,
full-test and mutant files. This document does not certify live entry readiness.

## Review of the full-candidate change

- Production failure review: the build-time three-long cap was a lossy cache,
  not a display optimization. The live-rate flip and independent historical /
  exchange tests fail against the old reducer. Lossless per-token packing fixes
  that gap; filtering still rejects disabled venues and invalid identities.
- Maintenance review: a code default did not control the production cadence.
  The test now loads the actual Compose override into the running loop. The
  private iterator and explicit list-export wrapper distinguish bounded ranked
  reads from callers that intentionally need a complete export.
- Storage-integrity review: trusting a compressed block's advertised zero count
  could skip validation through a falsey sequence. Every restored block is now
  validated before publication; corrupt, inflated, truncated, trailing and
  false-count data retain the previous coherent cache. No pickle/executable
  format is used. Test proves removal of the zero-count validation is caught.
- Remaining measured risk: full selection spends more CPU than a lossy shortlist.
  Common navigation remains materialized, but uncommon full queries must be
  timed on production. Do not claim RAM recovery or lower memory caps from the
  local compressed-byte count alone. No new external or authenticated API path.

## Release checkpoint — 18:33 UTC

Full-candidate commit `cc9b776` deployed after both guards cleared. App and
collector both verified digest `c3b22a143f494f61`; new containers started
18:31:23 UTC. At 18:33:10, both healthy, restart 0 / OOM 0. Collector was running
the ordinary chart definition workers, so the corrected hourly cadence now
actually initiated a refresh. Persisted market and funding files were still the
previous generations at that instant; publication and page acceptance remain open.

The final local replay, while other work occupied the Mac, was slower: build
88.23s, restore 24.52s, Now 27.09s, navigation 62.21s; peak 230.9 MiB. It retained
the same 219,871 pairs and returned the same match counts. These timings are not
an isolated CPU comparison and must not be represented by only the faster prior
run. Production CPU and uncommon full-query latency still require measurement.

After this deployment, separate work modified `spreadboard/fast_quotes.py` and
`tests/test_live_charts.py` in the shared checkout (general Hyperliquid builder
namespace handling). Those changes were not part of the tested/deployed cc9b776
release and have been left intact. Recheck shared-tree status before any further
deployment; do not silently ship unrelated untested changes.
