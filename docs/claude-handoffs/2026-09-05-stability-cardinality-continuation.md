# SpreadBoard continuation for Claude

## Cache-revalidation candidate — 2026-09-06 05:30 UTC

**Live remains f047ccf / bc8129844c597cd1. Candidate 7b7f904 / f276ddefc946e561 is NOT deployed.** No release waiter is queued. Sole production observer499780 (`spreadboard-stability-combined-20260906.service`) was freshly active; preserve it to about07:12:50UTC. Recurring automation staysPAUSED.

Local reproduction found that a20second-old projection was discarded after its10second short TTL for an unchanged request key, but reused if only the price-file key changed. The candidate preserves that completed projection within the unchanged900second structural TTL. Foreground reuse still applies current values and requests revalidation; background lookup remains a miss without removing concurrent readers' fallback. A new background projection replaces membership. Empty payloads, structurally expired entries and incompatible structural/query signatures remain excluded, and serving does not extend the timestamp or cache bound.

Final gates:2,682tests passed139.30s exit0; Ruff no new516against unchanged517baseline exit0;23focused tests pass. Original lookup source fails both new foreground cases and passes3safety/rebuild cases. One initial mutant run exposed test-fixture in-flight state leakage under a frozen clock; that local process was stopped and fixture isolation corrected before final gates. Generated tracked test data restored. No product source changes after gates. Evidence `same-key-full-final.txt`, `same-key-original-source-final.txt`, `exact-key-expiry-{before,after}.json`; details `docs/operations/2026-09-06-same-key-cache-revalidation.md`.

This is a proven redundant-rebuild path, not causal attribution for the specific31.933s production request or delivered HTTP latency improvement. Retaining a fallback during rebuild can affect peak allocation; validate ordinary RAM/CPU/coverage and latency after any later release. Do not deploy during the current memory observation. Review its complete phase comparison and safe-cap decision first.

First15minute deployed-memory log:60host/8coverage samples,201341–201569priced,2generations, no sampledOOM/restart/unhealthy/endpoint failures. Appanonpeak2,794,663,936bytes; collectoranon2,858,545,152. CPU.865/2.195cores; `/free`3samples max14.906s; health8samples max2.47s. Different duration/phase mix means these smaller peaks are NOT a proved saving versus the prior2h log. `combined-observer-current.jsonl`, `combined-observer-analysis.json`, `combined-comparison-initial.json`. Current release still needs full-hour/ordinary publication evidence, safe limits, further backups andfinal48h.

## Prior checkpoint details

## Deployed combined release — 2026-09-06 05:17 UTC

**Live app and collector are f047ccf, digest bc8129844c597cd1.** Sole combined waiter PID93680/session39465 finished exit0 at05:11:54UTC. Both guards cleared; both container source digests matched; health200. Containers started05:10:48UTC and were freshly healthy, restarts0/OOMfalse. Do not restart the terminal waiter. Former selection-only waiter89383/session7455 remains terminal143. No deployment queued; caps unchanged; recurring automationPAUSED.

The previous observer434080 finished successfully. Frozen `release-observer-complete-before-combined.jsonl`/analysis:474host samples over7197.82s,42coverage samples over6922.19s,11generations,198399–200890priced. No sampledOOM/restart/unhealthy or endpoint failures/gaps. Baseline appanon3,164,200,960/collectoranon4,053,626,880bytes; CPU.812/1.989cores. `/free`max31.933s, health10.018s. Successful kernel read05:09:35 has no OOM records since03:08:53. This proves the prior two-hour baseline, not current-release savings orfinal48h.

**NEW SOLE observer:** `spreadboard-stability-combined-20260906.service`, PID**499780**, started**05:12:50UTC**,2h untilabout**07:12:50UTC**, RuntimeMax7500s. Prior running/activating stability units were checked absent before start. Output `/opt/spreadboard/runtime/stability/20260906-combined/samples.jsonl`. Freshactive/running. Same15shost/120shealthfirsthourthen300s/free300s/backup300s cadence. Do not duplicate; no diagnostic child load inside its ordinary window. Compare against frozen prior baseline using localcompare_release_memory.py. Post-release public health05:12:22 ready/gen1,201423priced/207748routes,1249tokens; not an hourproof.

Actual signed-in UI after deployment: OPENAI36routes/25onfirstpage, GateFutures→HyperliquidFutures~6.6% indicative, nativeio:OAI in chart link, DD/depth labels intact, eligibleFunding0. Funding849tokens/23862livepairs, projected versus settled windows distinct. `combined-release-ui.json` and `combined-release-health.json`. Page-level observation does not prove every native route or execution readiness.

The unusually large T current funding headline (~47.56%day projection) is corroborated by nativeKrakenPF_TUSD data: fundingRate-.00009182quote/base, mark.00462894917 implies-1.9836%/hour at current notional. Nativeinstrument tradeabletrue, contractSize1, baseT, `maxRelativeFundingRate=.02`, tradfifalse; the older generic0.5% cap cannot be applied to all instruments. fundingRatePrediction is a separate, much smaller positive value and must not silently replace current funding. Snapshot `kraken-t-native-funding.json`; no forecast/realized-return/entry claim. T settled windows remainedblank on the renderedpage.

Full release gates2677passed146.44s exit0; Ruff no new516against517baseline exit0; six new behavioural cases fail bypass mutants. No source changes since gates. GoalACTIVE: measure ordinary after-release RAM/CPU/phase overlap and coverage, resolve latency/safe caps, subsequent normal backup firings and final48h. No trades/messages/spend/force/guardweakening.

## Earlier release checkpoints (historical)

## Combined memory candidate and sole waiter — 2026-09-06 04:59 UTC

Candidate **f047ccf**, digest **bc8129844c597cd1**, includes selection cleanup2a8a03b. NOT deployed. It also pauses the optional websocket worker inside the heavy lock during post-discovery publication, with finally-resume. Actual peak/log correlation: discovery completed03:52:39, index finished03:56:28, collector anon peak03:56:04 had websocket959232KiB plus index1877432KiB. Ordinary publisher already paused it; this post-discovery call path did not. This is a measured overlap mechanism, not yet delivered RAM savings. Full2677tests passed146.44s exit0; Ruff no new516against517baseline exit0; four new publication-path cases fail a no-op pause mutant, and two selection cases fail cleanup bypass. No source edits after gates; test-generated tracked data restored.

Former release waiter PID89383/session7455 was intentionally stopped before edits, terminal exit143, no deploy. Do not restart it. NEW SOLE bounded release waiter **PID93680 / tool session39465**, `output/stability-20260906/wait_combined_release.py`, started04:58:44UTC,2hbound. It holds the same singleton lock and pins the combined source/helper. Fresh poll confirms observer434080 still running; preserve to finished around05:09UTC. The waiter requires successful terminal service+finished log, baseline analysis/noissues/onehourcoverage, and both protected-worker guards before guarded recreation. Events `combined-release-wait.jsonl`; deployment output `combined-release-deploy.txt`; frozen baseline will be `release-observer-complete-before-combined.jsonl`. No combined deployment yet. Source must remain unchanged while it waits.

Live remains **fb814c3 /3d0f3437aa390c87**. Latest99min baseline:391host/38coverage samples,9generations,198399–200503priced, no sampledOOM/restart/unhealthy/endpointfailures. `/free` max31.933s, so latency remains open independently of200status. Physical8,326,938,624bytes versus9,059,696,640declared limits. Collector target3.5GiB is295,530,496bytes below anon peak; hypothetical cleanup alone stillleaves187,708,396bytes beforemargin. App3GiB target has only57,024,512anon-only headroom. Do not lower caps or treat local allocation savings as production totals.

Next: poll SAME session39465; inspect actual terminal deployment/digests/endpoints if it ships, then observe ordinary RAM/CPU/coverage across publication and evidence cycles. Keep goalactive until safe caps, final48h and further normal backup firings are proved. No trades/messages/spend/force/guardweakening; recurring automation remainsPAUSED. Detailed evidence: `docs/operations/2026-09-06-post-discovery-memory.md`.

## Earlier checkpoints (historical)

Checkpoint: 2026-09-06 04:38 UTC. The goal remains active. This is a continuation handover, not a completion claim.

### Selection-cache candidate — 2026-09-06 04:38 UTC

Candidate **2a8a03b**, NOT deployed. Websocket selection now releases parsed row/response caches after the full lane batch, on its selection thread, including failure. Selected keys, saved-position priority,160cap/300srefresh floor and website-process caches remain unchanged. Offline30,504-row snapshot replay freed107,822,100bytes through the actual cleanup API; this is not whole-worker RSS/cap evidence. Full suite2673passed150.14s exit0; Ruff no new516against517baseline exit0; both new cases fail cleanup-bypass mutant exit1. No source edits after gates. Source/test changes committed; test-generated tracked data restored.

Live source remainsfb814c3 /3d0f3437aa390c87. Sole observer434080 freshly active, both protected workers clear at last probe; preserve until terminal around05:08:53UTC, then refresh guards before any deployment. No deploy waiter queued or cap changes. See collector-retention/docs/operations/2026-09-06-selection-cache-cleanup.md. Next: finish ordinary baseline, guarded release, compare normal RAM/CPU/coverage, continue necessary memory work andfinal48h/backups. Goalactive/recurringautomationpaused.

## Previous measured result

One-hour priced-coverage gate passed; the current copied log spans approximately79min with no sampled OOM/restart/unhealthy or endpoint failures. Collector ordinary anonymous memory peaked at3.775GiB, so its proposed3.5GiB cap is unsafe at present. Caps remain unchanged. The finite observer is still the sole observation job, expected end05:08:53UTC; refresh its state before acting.

Local catalogue sharing reduced paired-client retained Python allocations by67.8MB Gate and27.7MB Bybit, without symbol-count loss. This is not a deployed fix or production RAM saving. Read `docs/operations/2026-09-06-catalogue-memory-investigation.md` for exact evidence, dynamic-listing risks and the competing route-selection cache hypothesis. Candidate2a8a03b now implements selection-cache cleanup; catalogue sharing remains unimplemented. No deployment queued. Preserve the normal observation window.

### Sole selection release waiter — 2026-09-06 04:42 UTC

One bounded local waiter is ACTIVE: tool session7455, `output/stability-20260906/wait_selection_release.py`, started04:41:36UTC with a2h bound. It holds the existing guarded-release-wait.lock, so no second release waiter can run. Fresh poll recorded observer_running PID434080. Do not restart/duplicate or edit candidate source while it waits. Previous waiter59028/session78374 remains terminal and is not reused.

The waiter pins source digest74ceb35494a9c6d4 (candidate2a8a03b) and the deploy helper hash. It requires observer inactive/MainPID0/Resultsuccess AND a finished log marker, freezes and analyzes the complete baseline, requires one-hour coverage/no issue flags, checks both protected workers twice before calling the helper, which checks both again immediately before recreation. Failed/unknown observer completion stops without deployment; transient read errors retry within the bound. Seven completion predicate checks passed. Events `selection-release-wait.jsonl`; future deployment output `selection-release-deploy.txt`. No deployment has occurred at this checkpoint. Current live sourcefb814c3/digest3d0f3437aa390c87. Recurring automation remainsPAUSED.

Next action: poll the SAME session7455, preserve observer434080 to terminal (~05:09UTC), inspect actual deploy exit/digests/endpoints if it ships, then measure normal candidate RAM/CPU/coverage. Do not treat waiter timeout as deployment or success. Goal remains active; safe caps, subsequent backups andfinal48h remain outstanding.

## Start here

- Work only in `/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-collector-retention`, branch `codex/collector-retention-20260906`.
- Read `docs/operations/2026-09-06-acceptance-matrix.md` for verified evidence and remaining gates. Detailed chronology is in `docs/operations/2026-09-05-stability-review.md` and `2026-09-05-ua-comparison.md`.
- Read root `REMINDERS.md`, vault current state and project rules before mutations. Refresh volatile state; historical checkpoints are not live evidence.
- The separate `tmp/spreadboard-funding-current-truth` checkout is not the release checkout. Preserve unrelated work there.
- SSH: `ssh -i ~/.ssh/spreadboard_digitalocean root@178.128.126.204`. Server source `/opt/spreadboard/app`; runtime `/opt/spreadboard/runtime`; container Python `/app/.venv/bin/python`.

## Release state — deployed

The sole waiter PID59028/session78374 completed exit0 at03:00:26UTC. Do not restart it. Live app and collector source is **fb814c3 / 3d0f3437aa390c87**; both protected-worker checks cleared and both source digests matched. `output/stability-20260906/held-release-deploy.txt` is the terminal evidence.

CAP production probe now preserves HTX futures long/Kraken spot short with correctpositivecarry. Signed-in CAP/Funding pages populated; OPENAI first page includes Binance/Bitget→Hyperliquid around6.1% indicative, withio:OAI source identity andDDwarnings. No positive-spread route is hidden solely for negative funding in this observed case.

The old counts observer is terminal. The sole active observer is `spreadboard-stability-release-20260906.service`, PID434080, started03:08:53UTC for2h, output `/opt/spreadboard/runtime/stability/20260906-release/samples.jsonl`. Do not duplicate it. This measures ordinary releasedmemory/CPU/coverage before the cap decision; final48hacceptance remains open. See `output/stability-20260906/checkpoint-deployed.md` and the acceptance matrix.

## What the deployed release changes

1. Preserve the exact printed legs in legacy fallback ingestion and compact quote updates. A fresh CAP sample was HTX futures long / Kraken spot short with positive carry, but legacy code reversed its legs and carry sign. Current candidate removes automatic mirroring and computes short funding minus long funding. Spot short inventory/borrow prerequisites remain explicit.
2. Rank zero funding above negative funding; avoid treating zero as missing.
3. Share repeated immutable strings within each streamed route-index generation using a bounded 32,768-entry pool. Mutable dictionaries remain independent; unique route IDs and full validation remain unchanged.

Latest combined gates: **2,671 tests passed in 140.33 seconds, exit 0**; Ruff no new findings, 516 remaining against unchanged 517 baseline. Behavioral regressions fail original-source mutants. No source changes since these gates.

The 10,000-route allocation experiment reduced traced retained allocation by 17.35% with identical serialized SHA256. This is a bounded sample, not proven production RAM savings. Measure ordinary reloads after deployment before changing caps.

## What is already verified

- Ourbit excluded from audited admission/restoration and the complete captured funding generation.
- At 00:05 UTC: **1,987 futures token labels / 9,733 markets**, **3,962 spot token labels / 12,484 markets**. Zero duplicate exact market keys. The user's UA counts (2,534 futures / 4,370 spot) count tokens, not directed venue combinations; exchange sets and naming differ.
- Complete funding generation at 02:09: **222,417 directed pairs**, zero exact duplicate pairs, self-pairs or Ourbit entries, and zero independently recomputed funding arithmetic mismatches. This proves that snapshot's arithmetic and exact-key uniqueness, not every native identity or executable return.
- UA guest futures sample 01:16: 15/15 fresh exact counterparts; 14 in saved cache, one fresh-only at probe. Spot/futures sample 01:17: nine supported counterparts, two Ourbit exclusions, four Binance Alpha legs outside configured coverage. Samples overlap other checks; do not sum them.
- Reverse comparison: our Kraken leaders were not guest UA winners; UA explicitly marked Kraken as premium. Do not claim absence from the whole competitor site, bypass access or add exchanges. Reference rates were not simultaneous executions.
- Deployed filters/counts preserve final identity guards and pagination totals. OPENAI zero eligible funding pairs was correct for mirage-guarded rows; research routes remain labeled. Actual io:OAI route was observed, but recheck on final release.
- Live b42e595 streams funding-cache restoration. A bounded full CAP render completed under 768 MiB after earlier bounded decode failures; cold 20.432s versus warm 1.360s. Cold restoration and broad-page latency remain follow-ups.

## Finish in this order

1. Observe the same PID434080 and collect ordinary reload/coverage evidence. Do not run duplicate observers or diagnostic child profiles unnoticed inside that window.
2. Compare against pre-held-release-observer.jsonl, pre-held-release-summary.json and pre-held-release-cache-counters.jsonl. Collector measured anon3.775GiB exceeds its proposed3.5GiB cap; memory work is required before lowering it. Current3584/4096/768/192MiB caps remain unchanged.
3. Decide safecaps from measuredheadroom, then establish the final uninterrupted48h and30-sample/hour evidence. No forced recoveryrepeatinsidecleanwindow.
4. Verify subsequent normal backup timer firings. Latest success00:19:49–01:15:36; nexttimer previously06:20:56UTC. Do not countolder successes acrosslaterfailure.
5. Keep goal open until every original acceptance gate is proved. Candidateallocationexperimentisnot deliveredmemoryproof. No further deploymentiscurrentlyqueued.

## Boundaries

No trades, borrowing, repayment, transfer, conversion, withdrawal, messages, spend, cap/subscription increases, weaker freshness/identity/95% accuracy/settlement gates, or force deployment. Ourbit remains excluded. Public relevance is positive spread OR positive funding, with needed alternatives and history preserved.

The recurring `finish-spreadboard-stability-acceptance` automation remains **PAUSED**. A continuation is not authorization to reenable it. Do not duplicate workers, observers or deployment waiters. No subagents. Never print secrets or complete process arguments/configuration.

If source changes become necessary after the current waiter is terminal, rerun the full gates with actual exit codes before any later deployment:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests/ -q
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --with ruff python scripts/ruff_ratchet.py
```

Raw evidence is in `output/stability-20260906/`. The previous handover is archived as `2026-09-05-stability-cardinality-history.md`; its operational directions are superseded.

## Additional Spot category comparison — 2026-09-06 04:46 UTC

A fresh guest inspection of UA's Spot tab found15visible spot-to-spot leaders. The exact category is deliberately retired from SpreadBoard: `api_spreads.RETIRED_ROUTE_KINDS` contains SPOT andDEX-SPOT, and the2026-08-28continuous-stream handover explicitly preserves that product decision. Spot market books remain necessary for the retained futures/spot routes, charts, portfolio marks and token-price alerts.

Nine displayed routes used Binance Alpha (SIREN,MITO,MOG,SPX,CHIP,ONDO,POWER,CFG,MORPHO), and two used Ourbit (SHROOM,BULLA). The remaining four were UPC MexcSpot→BitgetSpot, FONE GateSpot→MexcSpot, LUNC KucoinSpot→BinanceSpot andNPC MexcSpot→GateSpot. Their venues are configured but the route family is retired. These overlapping scope reasons explain the guest leaders; they do not establish a missing retained futures/funding route. Do not restore spot-to-spot permutations in response to this comparison or present them as funding opportunities.

Actual signed-in SpreadBoard `/markets?q=UPC` showed no rows and only Futures-Futures/Futures-Spot/Futures-DEX/Allroutes categories, consistent with policy. This is a category-scope check, not a native token-identity, transfer-rail or executable-arbitrage audit. UA's Spot-Dex/Futures-Dex tabs were visibly premium-disabled; no bypass attempted. Evidence `output/stability-20260906/ua-spot-family-comparison.json`. This15-row sample is separate from earlier samples and must not be summed into an exhaustive coverage claim.

## Phase comparison preparation — 2026-09-06 05:05 UTC

The frozen99minute baseline (`release-observer-99min.jsonl`) contains67index samples:10overlapped websocket and peaked at4,053,626,880collector anon bytes;57without websocket peaked at3,173,052,416.146evidence samples peaked at3,329,355,776. These are sampled phase maxima, not a causal/additive delivered saving, and15s sampling can miss brief overlap.

Local `output/stability-20260906/compare_release_memory.py BEFORE AFTER` reports both original observation analyses, phase presence/counts/peaks and anon/CPU deltas. It never claims acceptance; unequal duration/coverage/phases/load require review. A same-log comparison produced zero deltas; five missing-counter/process checks passed and retained unknown values instead of zero savings. Candidate digestbc8129844c597cd1 remains unchanged. Sole combined waiter93680/session39465 remains active; no deployment at this checkpoint. Preserve it and observer434080 until terminal.

## Post-release funding generation audit — 2026-09-06 05:37 UTC

Copied the published complete funding file to the Mac and audited locally; no diagnostic child was added to the production observation. Embedded saved_at is **2026-09-06 05:26:40.191552UTC**;51,049,746bytes; SHA256`5bd9aa25ee230f1d7c5ea11d57d346b1d16e3de32dff94036e5c5b79f291e40e`. The local copy mtime is download time, not source generation time.

**222,522 routes independently recomputed, zero arithmetic mismatches, duplicate exact leg pairs, self-pairs, Ourbit entries, missing complete rate inputs or unavailable funding.** Families:80,740Futures→Futures;70,868Futures→Spot;70,914Spot→Futures. No retired Spot→Spot family.5,328token blocks,1,062nonempty.94,538positive daily carry,94,489negative,33,495zero. The sign counts do not establish public eligibility: positive basis can coexist with negative carry, and required alternatives/history must not be blindly pruned. Counts are not unique-token counts or a reason to reintroduce retired lanes.

Evidence `funding-snapshot-arithmetic-post-release.json`, `funding-copy-metadata.json`, and the copied `funding-replay/complete_funding_catalog.json`. This verifies the stored snapshot's arithmetic and exact identities as keys, not native economic identity, present quote freshness, settlement completeness or enterability. Current live overlays are separate. The audit completes fresh post-memory-release regression evidence without changing source or limits.

Live remainsf047ccf; localcandidate7b7f904/2682tests remains undeployed, no waiter. Sole observer499780 freshly active at this turn's check; preserve until07:12:50UTC. First15min raw data/analysis now frozen as`combined-observer-0015.jsonl` and`combined-observer-analysis-0015.json`. Goalactive, heartbeatpaused; full ordinary memory/caps/latency/backups/48h gates remain.
