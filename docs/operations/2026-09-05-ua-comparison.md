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
coverage. The 19:05–19:08 follow-up inspected the actual filters: Spot-Futures
marks Kraken and XT with crown badges and disabled styling; Futures marks XT
the same way and does not offer Kraken in its visible exchange selector. These
guest restrictions make the absent guest search results an invalid test of
premium-market parity. Exact premium coverage remains unverified; no paywall
was bypassed. Our reverse futures-long /
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

The bulk adapter uses current mark as denominator; the point adapter previously
used index. Candidate `7875f13` makes the point reader use the same current-mark
basis and reject suspended/nonfinite observations. Its actual CCXT parser
comparison and invalid-input tests failed before the correction. Do not label
these current-notional calculations as Kraken's historical funding-calculation-time
relative rate. Production verification of this correction is still pending.

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

## Independent publication correction — release 19:04 UTC

The separate Hyperliquid chart fix was committed and deployed as `57c4985`,
digest `54d470476d775c64`. Both production digests were freshly checked at
18:59 UTC. The app started 18:41:16 and collector 18:44:22; both healthy,
restart zero and OOM false. The new isolated working branch is
`codex/funding-publication-cadence-20260905` in
`tmp/spreadboard-funding-publication`. It includes `7875f13` plus the preserved
chart fix, cherry-picked as `c072e41`.

A fresh 18:41:53 probe still found the legacy 500-token funding cache, saved
18:02:04 and 2,389 seconds old. Its sampled 148 positive rows had zero funding
math errors and all twelve navigation views were nonempty; that did not prove
expanded coverage. The ordinary chart definition refresh did publish active
Aster STONKS at 18:34:46. Index and page arrival remain to be checked.

The split collector returns before the web startup refresh thread is created;
the web-role refresh helper intentionally declines to build. Consequently, the
nominal fifteen-minute funding catalogue refresh was still coupled to discovery
completion. The new independent collector publisher invokes the existing due,
retry and build-lock path every sixty seconds. It requests navigation refresh
only after a successful catalogue publication and leaves public quote/funding
I/O on their existing threads.

Review found that `complete_funding_catalog_worker.py` was also absent from the
shared heavy-worker set. The correction adds it, so a busy slot defers/retries
instead of allowing another heavy build. Actual collector bootstrap, publication
retry, navigation ordering and busy-slot regression tests cover these paths.
Removing bootstrap, navigation refresh or heavy-slot membership is detected.
The first full run reported 2,472 passing tests and one old exact-membership
assertion; that assertion was updated to include the deliberately serialized
worker. A repeated full run exposed an existing cold-reader test consuming a
cache left by an earlier run; that test now uses an isolated absent path and
resets its restore state. The final unmasked suite passed **2,473 tests in
101.00 seconds**, exit zero. Ruff ratchet remains 517 with no new
findings (an unrestricted `ruff check .` additionally scanned seven diagnostic
and documentation findings outside the established source/test ratchet).

Still required: refreshed UA references, production current-rate
checks and measured CPU/memory/latency. Do not call the finite sampler a clean
release window: the intervening deployments changed its container identities.

Fresh 19:01 UTC streamed production audit: 22,417 exact market records and
22,417 unique keys, zero duplicate extra rows, zero Ourbit, 2,171 futures token
labels and 3,962 spot token labels. The active Aster STONKS/USDT:USDT definition
is present. Funding remained v1: 500 tokens / 17,739 retained routes, 36,462,219
bytes, saved 18:02:04. This is the explicit before-publication baseline.

`861fae7`, source digest `54eb84e83a8185b3`, deployed to both containers at
19:04:23 UTC. Initial and immediate pre-restart discovery/finalizer guards were
clear. Both container source digests matched the tested source; HTTP health 200,
restart counts zero and OOM false. The initial cheap health sample had zero
priced rows while the index warmed; the 19:06:49 sample showed 174,555 priced
routes, 1,243 priced token labels and 0.858-second health response.

Automatic publication is now observed: saved 19:05:32 UTC, schema v2, 5,360
token payloads (including empty/nonmatching candidates), **220,079 retained
eligible pairs**, 50,731,947 file bytes. These payloads are not a count of unique
futures tokens or positive displayed opportunities. The worker reported a
40.002-second build and 258.6 MiB peak RSS; elapsed with scheduling was 61.9s.
At 19:06:49, public health confirmed the web reader had loaded this saved
generation and all 12 navigation views had refreshed using its source signature
(19:06:33 build, zero empty views). This proves actual publication and reader
arrival; detailed live arithmetic and route-reference verification are underway.

## Live verification and exact-history scheduling — 19:19 UTC

The new-generation funding probe passed: 146 sampled positive rows independently
matched their exact live funding legs, zero math errors / Ourbit; 956 tokens and
93,136 routes matched positive current carry. All twelve persisted views were
nonempty. Isolated read-only probe peak 227.5 MiB. Token-detail queries returned
STONKS, CATE, ASTEROID, ANSEM, ALIGN, KAITO, LCAP and 1000BTT. Exact detail includes
negative hedge directions for comparison; the broad ranked list remains positive.
The first isolated detail lookup took 12.75s, subsequent ones 0.06–0.62s: cold
lookup cost remains relevant and must not be represented only by warm timings.

The updated 45-case reference probe found 38 with both market definitions and
37 with an indexed route; the one defined but excluded route was the OPENAI
Gate spot/perp identity/index mismatch. The previously absent Aster STONKS route
is now both indexed and in the complete funding catalogue. Its exact Mexc Spot
to Aster Futures detail showed +0.03% current daily carry. The index still
carried a conservative mirage flag from its scanner evidence at that sample;
do not claim every indexed reference is immediately a positive, admitted spread.
CATE/ASTEROID/ANSEM/ALIGN and KAITO had exact catalogue/reference matches.

Actual authenticated Funding UI showed populated current leaders (including
1000BTT Mexc→XT), per-leg intervals, age labels, and many blank exact historical
columns marked settlement-refresh-overdue. Fresh collector logs repeatedly said
`market evidence deferred; current route index pending`. This was a real scheduler
starvation: the modern publisher's `publication_due()` overrode the older
recent-publication check, allowing every 120-second quote-index demand to veto
the 300-second history sweep even when a successful complete index was recent.

The local correction restores the recent-index allowance specifically for
history, retaining cold/stale index priority and all shared heavy-worker locks.
The regression uses the actual publisher's request/check/publication lifecycle,
proves history can take a turn with a recent complete index, proves cold and
stale indexes still preempt it, proves no overlapping index build, and proves
the pending publication runs after history releases the lock. The old scheduler
fails the recent case. Full suite **2,476 passed in 95.95s**, Ruff 517 unchanged;
old-method mutation caught. Deployment and actual settlement refresh remain to
be verified. No settlement completeness guard or refresh age was weakened.

A second finite read-only sampler began **19:14:36 UTC**, unit
`spreadboard-stability-publication-20260905`, output
`/opt/spreadboard/runtime/stability/20260905-publication`, duration two hours.
It is preliminary and will be split by any further deployment. The Codex
recurring automation remains paused; no notification or trading action occurred.

## Exact settlement publication verified — 19:55 UTC

The scheduler fix `a6e82e1` deployed at 19:27:31 UTC with matching app/collector
source digest `4a7e5f96478f5fd0` and both discovery/finalizer guards clear.
The first ordinary market-evidence worker ran to completion at 19:42:13; its
exact settlement file advanced at 19:34:56 and 19:38:46. At 19:51, public health
confirmed all twelve navigation views had consumed the new exact-settlement
and retained-radar generations, with zero empty views. An additional automatic
full-catalogue publication completed at 19:43:24 (49,972,625 bytes), confirming
that refresh continues beyond the first startup generation.

Authenticated Funding Now showed new exact totals for VELO Kraken Futures→Bybit
(9.20265008% / 63.61692023% / 47.38992667% for 24h / 7d / 30d) and ZIG
Mexc→Kraken Futures (4.18673069% / 5.20197141% / 34.81362272%). A fresh read of
the exact-leg cache reproduced those displayed numbers. These are observed
settlement totals, not projected returns. BEL, ICX and 1000BTT still had overdue
legs at that check; a second ordinary evidence worker was active by 19:54 and
had already advanced the file again. Remaining blanks are not declared fixed
from a file timestamp alone.

The first read-only history probe used `Kraken` instead of the canonical
`Kraken Futures` and an unsupported hypothetical LCAP hedge. Those probe cases
were discarded. `history-arrival-canonical.jsonl` uses observed exact routes;
do not use the preliminary `history-arrival-1945.jsonl` as missing-market proof.

The completed worker reached about 1,995 MiB RSS during archive collection and
the collector touched its 4,096 MiB cgroup ceiling, with OOM counters still zero.
The archive path retained rich dictionaries for all positive candidates even
after the earlier packed-catalogue change. A new regression invokes the actual
evidence worker: the old code retains 200 rich positive rows; the proposed
stream-and-compact path retains at most three, preserves all 200 records, and
keeps the same catalogue/warm/leader overwrite order and exact totals. Reverting
the archive iterator, the service call site, or compaction separately is caught.
Production-data allocation/output replay and the final release gate are being
completed before deploying this further reduction. Caps remain unchanged.

## Tested corrections awaiting a protected deployment window — 20:13 UTC

The final archive change yields positive/current-or-historical candidates and
compacts each to the existing radar field whitelist before retaining it. It
keeps the same identity key, catalogue/warm/leader ordering, JSON normalization,
retention and record cap. The real production-artifact replay wrote exactly
131,012,177 identical bytes before and after, SHA-256
`d7234dc829f48a29043ba196c84196df6896583ad27062ff2929a4171da57f3d`.
Initial local peak RSS fell from 1,207.80 to 798.03 MiB. An intermediate version
duplicated JSON conversion and was replaced with a plain field projection.
Local wall times varied substantially (65.53 / 88.59 seconds for the initial
before/final runs); a further compact replay took 228.43 seconds / 132.36 CPU
seconds and peaked at 561.66 MiB. Its comparison run was stopped to avoid
contending with validation. These are local allocation/output observations,
not delivered production savings or a passed production CPU gate.

The fresh queue probe exposed a separate correctness defect: OKX ICX's stored
history inferred a four-hour cadence, while its current funding schedule was
two-hourly. The public reader correctly expired the totals, but the priority
selector still said the leg was not due. The selector now receives the same
live schedule snapshot and uses the existing expiry logic to request the missing
settlement. No freshness or settlement-completeness guard is loosened. A real
`build()` regression first proves the public cell is expired, then proves a
provider fetch restores it; its companion proves a not-yet-due leg is left alone.
The old call site fails that test.

Final full gate: **2,479 passed in 153.99s**, Ruff **517 known / zero new**.
An earlier full run had 2,478 passes and one failure from an import-order lint
finding in the new test; the import was fixed and the entire suite rerun.
The earlier archive-only implementation also passed 2,477 tests before the
live-schedule regression was added. All three archive mutants and the old
schedule-selector mutant were caught. Test-generated runtime fixtures were
preserved in output and restored; source/runtime data from other tasks were
not touched.

Fresh deployment preflight at 20:06 found protected discovery worker **195600**
running, so no restart or source deployment was attempted. Production remains
`a6e82e1` / `4a7e5f96478f5fd0`. Wait for discovery and snapshot finalization to
finish, then repeat both the initial and immediate pre-recreation guards.
The 18:05–20:05 finite sampler ended successfully; the 19:14–21:14 sampler
continues. Both span deployments and require container-ID segmentation.

## Published schedule and exact-detail corrections — 21:25 UTC

The guarded release at **20:55:32 UTC** deployed `1d7f26c` with matching app and
collector source digest **1c6e27099ca47d3c**. Both discovery and finalizer guards
were clear before build and immediately before recreation. This includes the
previously pending archive compaction and history-schedule selector fixes.

A fresh additional fifteen-row UA capture expands the comparison to **59 unique
observed exact route identities**. All fourteen non-Ourbit references in that
capture had both definitions, an index entry, cached funding and a fresh price
pair; the fifteenth uses Ourbit and remains deliberately excluded. This is a
sample comparison, not exhaustive guest/premium parity. Earlier count evidence
remains 2,171 futures and 3,962 spot token labels, zero duplicate market keys;
large directed-pair counts are not extra tokens.

The comparison exposed incorrect assumed eight-hour funding schedules for BingX
and WhiteBIT, and missing Binance current schedule overrides. Fresh public
responses contain BingX 1h/4h/8h schedules, WhiteBIT explicit minute units, and
Binance current fundingInfo adjustments. The deployed readers preserve those
published schedules, prioritize current overrides over older market metadata,
and recognize BingX's nextFundingTimestamp. Invalid values still fail the same
normalization checks; unproven schedules remain explicitly estimated.

The normal funding writer, without a forced refresh, delivered **911 BingX +
305 WhiteBIT + 752 Binance = 1,968 matching exact legs with zero interval or
assumption-provenance mismatches** at 21:02:35 UTC. This does not prove every
WhiteBIT instrument was matched: the source has 398 rows and 305 matched keys
in that probe. Aster and Binance still have estimated legs outside the checked
published schedules. Evidence: `published-schedule-arrival-second.jsonl` and
the read-only `probe_published_schedule_arrival.py` in output.

A second defect was the exact-token Funding API taking the ordinary Spreads
shortlist path. A KAITO request claimed all exact pairs while rendering only
25 of 72. After bypass removal, the authenticated page rendered all **234 of
234** current/retained alternatives and a real funding age. Expanded coverage
can include negative or unavailable alternatives for exact-token investigation;
the broad ranked list still selects relevant opportunities.

Release validation: **2,500 tests passed in 174.54s**, Ruff **517 known, zero
new**. The exact-API old implementation fails both farm tests. The schedule
mutants fail 13 reader cases, 4 explicit-minute cases, and 3 current-override
cases respectively. The earlier preliminary parser fixture omitted markets_by_id
and was corrected; only the final tests/mutants are acceptance evidence.

A live ONG page then exposed a further display defect: net carry used the new
one-hour BingX rate while the leg caption still displayed the older rate and
"every 8h". The local correction synchronizes initial per-leg fields and the
SSE net/cadence/rate/age payload, updates collapsed groups by exact funding
identity, clears expired current data, and allows a blank current value to
recover. Historical selected totals are not converted to live projections.
This further change is **under full validation, not yet deployed** at this
checkpoint. Do not claim all Funding presentation is fixed yet.

The finite read-only current-release sampler began about 20:56 and ends about
22:56 (`spreadboard-stability-schedules-20260905`). Initial cold requests were
HTTP 200 but slow: health 20.631s and /free 35.209s. Later health recovered to
subsecond samples. Both containers remain healthy with zero observed OOM kills,
but the collector touched its 4 GiB ceiling and Funding navigation deferred at
2,165 MiB headroom versus its 2,200 MiB guard. At 21:20 the twelve nonempty views
still referenced the 20:44 generation. Limits remain unchanged. These facts
leave cold latency, navigation freshness, representative memory/CPU, safe lower
caps, a current-release hour and final clean 48-hour acceptance **open**. The
Codex recurring automation remains paused; no trading, messages, paid access,
notifications, cap increases or weakened accuracy guards were performed.

### Funding leg coherence release gate — 21:31 UTC

Final full suite: **2,507 passed in 198.90s**; Ruff **517 known / zero new**.
All thirty focused tests pass, including the unchanged 250 KB page budget.
The first full run passed 2,506 and failed that size guard; source indentation
and standalone JavaScript comment lines were removed from generated responses
before the full rerun. All source comments remain available to maintainers.
Old catalogue, stream-backend and browser-handler mutants are each caught.
The independent 21:27 catalogue read reproduced **22,417 unique market keys,
zero duplicates / Ourbit, 2,171 futures / 3,962 spot labels**, and 220,196 packed
funding pairs. Test-generated runtime data was preserved under output and
restored to committed bytes. Deployment and live page proof follow this gate.

## Funding coherence deployed and checked — 21:47 UTC

Release **767e464** started both services at **21:31:57 UTC**; both source
digests match **0e255781dba02cb5**. Initial and final protected-worker guards
were clear. Health returned 200, restart counts zero and OOM flags false.
The full gate remains 2,507 passing tests / Ruff 517 with zero new findings.
No further source change was made after that validated release.

Authenticated ONG detail rendered all **72 pairs**. At 21:35, a DOM-only audit
recomputed every displayed pair from its displayed rate and interval: **72
checked, zero arithmetic mismatches within explicit rounding tolerance, zero
unavailable values, zero Ourbit**. BingX -0.0606% hourly and Bybit +0.1053% hourly
corresponded to displayed +3.981% daily carry; the reverse matched -3.981%.
At 21:40 the same open page showed new rates -0.0579%/+0.0792% hourly and
+/-3.291% carry without a manual reload. These are timestamped UI observations,
not current trade recommendations. The browser initially lost a navigation
target; reconnecting to the same authenticated tab recovered it. An initial
DOM audit used unavailable parseFloat; the corrected Number-based audit ran.
Neither tooling issue is counted as an application/data failure.

The new ordinary Funding navigation generation at **21:38** reached all twelve
views. At 21:43 health confirmed zero empty views, 3,831 summed per-view token
memberships and 9,050 preview rows. Its worker reported **66.368s build time**,
**203.6s parent elapsed** and **777.7 MiB peak RSS**. These memberships are not
unique token counts. The earlier 20:44 generation had been delayed by headroom;
one later publication does not establish reliable ongoing cadence.

A post-release reconciliation of the same fifteen UA reference identities
again found all **14 non-Ourbit cases** in definitions, the current index,
complete Funding cache and fresh pairs. The excluded Ourbit route remains
absent. ONG's cache-derived net was positive (+2.940624% projected daily at that
later probe), so the prior wrong assumed-eight-hour sign mismatch is gone for
this observed snapshot. Peak probe RSS was 172,540 KiB. These source times
differ from UA's captured rows, so numerical equality is not asserted.

The performance-profiler baseline used the saved public production catalogue,
radar, history and funding files under a fixed clock. It produced twelve views,
3,226 groups, 7,876 previews and 163,688 matching routes. Peak RSS was 452.72 MiB
after ranking and 577.11 MiB after serializing the whole comparison result;
81.77s wall time. The full 43,411,193-byte output hash is
`1cd132005c64a443ff5047c67809d6b45c63efe046f3d52355f49f8459d7dfa1`.
This is an allocation baseline, not live correctness or Linux cap safety; no
speculative decoder, iterator or headroom change followed it. Graph extraction
and report reading completed; the optional hook installer failed on the Git
worktree's .git file, without affecting extraction or this release.

At 21:42–43 both containers were healthy with zero restarts/OOM counters and
about 184,900 priced routes. Initial sampled cgroup peaks were approximately
3,062 MiB app / 3,034 MiB collector. The ordinary history worker was still
running; its completed-cycle peak remains to be checked. **All memory limits
and the 2,200 MiB navigation headroom guard remain unchanged.** Cold latency,
sustained navigation publication, remaining resident memory, safe lower caps,
a deployment-free current-release hour and final clean 48-hour acceptance are
still open. The finite sampler ends about 22:56 and must be segmented by
container ID; the Codex stability heartbeat remains paused.

## 22:36 UTC — native funding coverage restored, final acceptance open

Release `8a1ddeb` (including `01bbf4a`) is live in app and collector with source
digest `0346ca22c4326ae9`, started at 22:27:33 UTC. Both protected-worker checks
were clear, including the immediate pre-recreation guard. No force was used.
The final full suite passed **2,574 tests in 100.54s** and Ruff reported **517
known findings, zero new**. The first native full run failed its Ruff test;
product-family failures now log only venue/family names, and the full gate was
rerun. Old-code mutants caught the identity, cache, audit and native-reader bugs.

| Venue | Before: matched current keys | After: matched catalogue keys | Cause and correction |
|---|---:|---:|---|
| WhiteBIT | 305 | 398 / 398 | CCXT treats `tradfiFutures` as spot. Read the native perpetual identity from the same funding response and remove obsolete spot-shaped cache keys. |
| BitMart | 0 | 359 / 359 | Installed CCXT has no BitMart adapter. Read active native perpetuals directly, preserve quote versus settlement identity, and use expected funding with its published schedule. |
| Coinbase International | 0 | 131 / 131 | No CCXT bulk funding method and no native bulk reader. Read current predictions and convert published nanosecond intervals to hours. |
| Bitget | 778 | 827 / 827 | The default bulk call reads USDT only. Request both USDT and USDC futures with current published schedules; isolate family failures. |

The ordinary collector published these **632 previously missing exact funding
keys** without manual cache injection. This is a count of contract data, not
632 new enterable opportunities. At 22:32:37 all 1,715 catalogue keys matched.
Independent native-field calculations at 22:33:26–28 found no missing keys or
interval/provenance mismatches for those 1,715. Rates did change between the
cache and source observations (ages approximately 20–103 seconds); this is not
a claim of simultaneous exact-rate equality or exhaustive trading readiness.

BitMart returned 1,215 records, of which 856 were delisted and excluded. Coinbase
returned 310 instruments, of which 131 were active perpetuals. The BitMart
current-rate choice follows its distinction between previous-period and next-
period funding; Coinbase documents its interval in nanoseconds. See the
[BitMart funding reference](https://developer-pro.bitmart.com/en/futuresv2/#get-current-funding-rate-v2)
and [Coinbase instrument reference](https://docs.cdp.coinbase.com/api-reference/international-exchange-api/rest-api/instruments/list-instruments).

The operator's Now audit also had false-success paths: it waived mismatches for
hourly venues and routes carrying settled-history metadata, and passed empty
or unverifiable runs. It now compares current projections for every venue,
requires explicit finite values and usable intervals, and returns incomplete
status when evidence is absent. Its existing tolerance is unchanged; this
operator diagnostic is not a substitute for the product's 95% accuracy gate.

The fresh 22:34:30 catalogue read contains **22,417 records and 22,417 distinct
venue/type/symbol keys**, zero Ourbit and zero duplicate keys. It represents
**2,171 futures token labels and 3,962 spot labels**. The owner's UA counts of
2,534 / 4,370 are comparable to these labels, not the 220,196 directed funding
alternatives. Eliminating every alternative would lose current, historical or
exchange-filter winners; the site-facing positive-opportunity policy remains.

Remaining discrepancies need explicit classification. At 22:24:55, 129 BingX
and 65 XT catalogue keys had no exact native ID in their current bulk funding
feeds. This does not prove delisting or an alias. Bitget's new feed also has
12 cache keys outside the catalogue (including native test/spot-shaped names);
they need an active-market identity check before retention is tightened. The
cumulative UA comparison remains 59 observed exact route identities, subject
to the earlier guest/premium restrictions. No exhaustive parity claim is made.

New browser verification was blocked by `ERR_BLOCKED_BY_CLIENT`; the former
authenticated tabs were gone. Backend/cache checks succeeded, but they are not
new browser-rendering proof. The 22:32 health request returned 200 in 0.926s
with 186,708 priced routes. All 12 navigation views were nonempty using the
retained 22:23 generation; that is not a new post-release navigation build.

Early new-release evidence covers only 27 host samples over 6.85 minutes:
zero OOM kills/restarts, both latest healthy, anon peaks 2,011 MiB app and
2,472 MiB collector. A sampled `/free` request returned 200 in **18.098s**.
The preceding release reached sampled anon peaks 2,917 / 2,868 MiB and cgroup
peaks 3,426 / 4,096 MiB. Neither window justifies smaller caps or 24/48h claims.

The rejected decoder experiment is recorded without a performance claim:
identical 99,822-row content, baseline 6.424s / 391.7 MiB versus prototype
7.334s / 468.9 MiB. No production decoder change was made. Earlier archive
compaction has not yet shown a delivered ordinary-worker RSS reduction.

Evidence is in `output/stability-20260905/`: `native-coverage-deploy.txt`,
`native-coverage-full-pytest-final.txt`, `native-coverage-ruff-final.txt`,
`native-coverage-final-arrival.json`, `native-arrival-independent-comparison.jsonl`,
`market-cardinality-native-release.json`, `native-release-early-soak-summary.json`,
`whitebit-*mutant.txt`, and `native-coverage-mutant-final.txt`.
