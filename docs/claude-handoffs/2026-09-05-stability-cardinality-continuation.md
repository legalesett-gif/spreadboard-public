# SpreadBoard continuation for Claude

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
