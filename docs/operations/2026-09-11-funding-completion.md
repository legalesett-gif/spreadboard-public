# SpreadBoard completion record

Owner: Codex in the current chat. There is no external Claude handoff. The older handover remains a linked chronology; this checklist controls the continuation.

## Latest verified deployment

`a6e4ff2`, source digest `3209e2e14790ee10`, verified in both app and collector. Deployment exit 0, health 200. Full suite: 2,879 passed in 179.28s, no new Ruff findings (502 known). Ordinary observation restarted on the same finite unit at September 11 00:51:07 UTC, PID 2340559. Older checkpoints below are chronological.

## Requirement-by-requirement status

| Requirement | Evidence and remaining acceptance |
| --- | --- |
| Venue policy | Live controls exclude HTX/Ourbit; CoinEx/Phemex funding-only exclusions preserve price collection. Native/public parser regressions pass. |
| Hyperliquid and builders | Native discovery covers all advertised builder namespaces; live funding shows builder routes. OPENAI Markets contains exact io:OAI routes with identity guards. Ordinary history worker recovered IO-OAI 24h/7d; its short archive cannot supply 30d. |
| Exchange filters | Live either-leg exclusions changed results and leader; period/farm navigation preserved selections. Export acknowledgement observed; exported content is covered by regressions, not a separately parsed live download. |
| Current and settled funding | Shared schedule/sign/missing-value fixes deployed. 2,777 published windows across 15 venues match ledger sums; missing/expired/gapped windows remain unavailable. Full-universe current historical catch-up remains open. |
| Relevant and nonduplicate markets | No repeated native contract IDs within any venue's futures catalogue. BitMart's 1,215 native entries comprise 359 trading and 856 delisted; catalogue retains exactly 359. Negative-rate legs remain necessary for positive net pairs. |
| Seven-day full trial | Telegram identity and email claims are atomic and persistent; live registration terms verified. Local HTTP checks prove entitlement before/during/after trial on four protected pages. Native Telegram callback was simulated locally, not exercised through a real live identity. |
| Telegram advertisement | Draft written; no channel message sent. |
| Spread continuity | OPENAI builder pairing verified; latest-generation route samples narrowly bounded so far. Required 30-sample hour remains open. No weakened quote/identity gates. |
| Memory budget | Accounting cap reduced in place from 768 to 512 MiB after a 290 MiB lifetime peak over nearly 26h. Web/collector caps remain outstanding. Funding-only client retention/concurrent construction reduction measured locally and tested. Production savings and ordinary rebuild peaks must be measured before caps change. Web anonymous memory reached about 3,001 MiB, making an immediate 3,072 MiB cap unsafe. |
| Backup | Stale lock recovered, bounded stale-only retry and persistent cache/four-hour deadline installed. Catch-up upload still active; two normal successful timer runs remain open. |
| 48h reliability | One finite read-only observer; same unit resets only after a new deployed generation. No short sample or reset OOM flag counts as 48h acceptance. |
| Continuity record | This document and linked chronology remain owned by Codex in this chat. No external Claude dependency. |

## Completed acceptance checks

- Production funding controls omit HTX, Ourbit, CoinEx and Phemex. CoinEx/Phemex prices remain enabled.
- Live ONG exclusion test removed Gate and WhiteBIT from either leg, reduced 56 exact pairs to 42, changed the leader, and preserved exclusions across period/farm navigation. Export displayed “Current JSON downloaded.”
- Expanded funding rows retain their own rates, schedules and totals. Binance, BingX and XT hourly schedules were observed. Missing settlement windows explain whether coverage or refresh is pending.
- Hyperliquid funding results include ordinary and builder markets, including BRENTOIL. The OPENAI Markets group contains Hyperliquid io:OAI routes with explicit unresolved identity/oracle/trading-hours disclosures. Those disclosures must not be removed merely to promote a tokenized instrument into Funding rankings.
- Production registration copy describes Telegram linking, seven days, no payment and automatic expiry. A local HTTP integration check exercised the same session before linking (402), during Research Pro trial (200), and after expiry (402) across Funding, Charts, Intel and Playbook. No real Telegram message or live test identity was used.
- Ordinary production collection created the settlement ledger; successive checks grew from 21,851 events/74 contracts to 124,821 events/456 contracts. At the first completed history cycle, current 24h/7d/30d coverage was 37.30%/35.95%/30.85%; this is progress, not completeness.

## Additional fixes in this continuation

- Exact settlement collection has an independent bounded worker and durable priority queue, so the hourly refresh need not wait for full route ranking or materialization. A process lock prevents overlapping aggregate writers; failed launches retry.
- BitMart’s [documented history API](https://developer-pro.bitmart.com/en/futuresv2/#get-funding-rate-history) exposes the latest 100 settlements. Native history now uses the event ledger too, accumulating overlapping responses; periods absent from the provider cannot be invented.
- A timestamp index prunes expired events globally, including inactive/retired contracts.
- The backup repository had a 63-hour stale exclusive lock with no live restic owner. Normal stale-only unlock succeeded; no force-unlock was used. Backup retries now handle that condition.
- Sep8 backup history showed upload success after 1h51m followed by timeout during integrity checking. The service deadline becomes four hours, below its six-hour schedule; integrity checks remain required.
- Repository-index cache persists between runs and is excluded from snapshots. SQLite backup handles close before uploads.

## Release and observation

Commit e901d49 is deployed to app and collector; both source digests verified as 63ebb53a5a833b53, deployment exit 0. Final frozen-source suite: 2,875 passed in 202.50s. Ruff has no new findings (502 known); seven deliberate fault mutations were detected. The backup unit passed systemd validation and is installed with the four-hour deadline; the active upload was not interrupted. The current offsite backup upload is active and encountering shared Google Drive project-quota retries; do not call it successful until upload, retention and integrity checking exit successfully.

The remaining duration-dependent gates are a current-release 30-sample hour with priced-route counts within ±10%, ordinary memory/CPU measurements before any smaller caps, a clean 48h endpoint/cgroup observation, and two normal successful backup timer firings. Keep these as open checks owned here. No new spend, trade, Telegram publication, or recurring Codex automation is authorized by this record.


## Observation checkpoint, 2026-09-11 00:29 UTC

- Finite read-only observer `spreadboard-funding-acceptance-20260911.service` began at 00:23:14 UTC for 48h. Output: `/opt/spreadboard/runtime/stability/20260911-funding-acceptance/samples.jsonl`. This is acceptance instrumentation, not a reactivated recurring Codex task. Do not duplicate it.
- New-release endpoint samples return 200; containers are healthy with no restart/OOM increments. The sample is still too short for stability acceptance or cap reduction.
- SQLite reached 329,669 events across 15 venues, including 589 native BitMart events across eight contracts. History publication advanced independently to 00:27:44 UTC. Hourly-boundary catch-up is incomplete.
- Fresh Hyperliquid client indexes 821 symbols and active `IO-OAI/USDC:USDC`. Its prior stored `symbol_not_indexed` classification was still excluding it from the demand queue. A bounded hourly retry for missing/empty/paused classifications is being verified before the next release; no special OAI override.


## Classification correction deployed, September 11 00:37 UTC

- Commit `ff1f01a`, source digest `60ba0e6d0e7680b8`, verified in both app and collector. Guarded deployment exit 0, health 200, no restart-count increment or OOM flag.
- Missing-market, empty-history and paused-market classifications expire for requested legs after one hour. Repeat failures stay blank and do not consume every five-minute demand pass. Full suite: 2,878 passed in 170.53s; no new Ruff findings. Removing this branch caused all three call-site regressions to fail.
- Independent ledger arithmetic audit compared 2,777 recently published historical windows across all 15 collected venues, with zero mismatches and zero boundary/unmerged skips. This validates aggregate arithmetic against stored events; native API semantics and completeness are separate checks, and historical catch-up remains incomplete.
- The same finite observer was restarted at 00:37:35 UTC after deployment to begin a clean 48h window and fresh two-minute first-hour samples. Old evidence is retained in the append-only file. No duplicate observer or recurring automation was created.
- Analysis helper: `output/continuation-20260911/summarize_acceptance.py`. It separates container generations, refuses an hour pass before 30 samples, reports memory/CPU/OOM evidence, and counts only completed successful backup exits. Normal timer firings still require checking their start times.


### Ordinary worker verification, 00:38:45 UTC

The deployed worker recovered Hyperliquid `IO-OAI/USDC:USDC` from its stale missing-symbol classification: 203 actual events, 24h/7d available and 30d unavailable. BitMart `BTC/USDT:USDT` has 96 stored events and all three complete periods. Ledger total reached 527,586 events. This closes native incremental-write and newly indexed demanded-contract recovery checks; it does not imply a complete 30-day archive for recently listed instruments or full-universe current coverage.


## Funding-worker memory correction — September 11 00:51 UTC

- One per-venue initialization lock prevents simultaneous first history requests from loading duplicate full catalogues. Only the initialization is serialized; bounded history-fetch concurrency is retained.
- Funding-only clients remove spot entries from both symbol and native-ID lookup maps. Derivative objects, IDs, currency metadata and history request semantics remain unchanged; public price/spot clients are unaffected.
- Live local Bitget profile: 2,636 definitions to 856 derivatives, traced retained allocations 32,240,411 to 21,487,005 bytes (10,753,406 bytes released). Derivative JSON digests and native BTC funding-history responses matched exactly. This is a local retained-heap result, not a claimed production RSS saving.
- Removing either pruning or singleton initialization failed the real client-construction regression. Full suite passed 2,879 tests; Ruff no new findings. Evidence: `output/continuation-20260911/{client-memory-profile-exact.json,pytest-client-memory-release.txt,mutant-client_spot_retention.txt,mutant-client_duplicate_load.txt,deploy-client-memory.txt}`.
- Prior live web anonymous peak 3,001 MiB leaves inadequate margin for a 3,072 MiB cap. Collector ordinary materialization overlapped history collection near its 4 GiB cgroup limit, with substantial file cache. Next action is ordinary-cycle measurement on the new source before choosing caps, not another speculative release.


### First ordinary optimized history cycle, 00:55 UTC

The collector logged one successful settlement-worker completion. Observed process lifetime high-water RSS was 568.2 MiB, below the earlier roughly 697 MiB observation; the queue/workloads differ, so this is not a controlled whole-worker percentage-saving claim. Publication advanced to 3,485 current 24h / 3,387 current 7d / 2,923 current 30d contracts. The next ordinary worker is active. Backup PID 2307267 and finite observer PID 2340559 remain active; neither duration gate is complete.


### Live hourly rollover acceptance — 01:00 to 01:04 UTC

At 01:00:22, the production funding reader withheld all expired ONG Bitget/XT and IO-OAI windows. By 01:04:26, the ordinary worker had fetched the new 01:00 settlements and the same reader returned updated 24h/7d/30d ONG totals and updated 24h/7d IO-OAI totals. IO-OAI 30d remained unavailable. No manual queue insertion, competing aggregate writer, forced refresh or special token override was used for this rollover. Read-only helper processes were temporary validation overhead, not ordinary-worker RSS samples.

Evidence: `output/continuation-20260911/hourly-rollover-{expired,reader}.json`. This closes the sampled live expiry/recovery path, not full-universe archive coverage. Backup PID 2307267 and finite observer PID 2340559 were freshly confirmed active; no terminal backup result yet. Current source remains a6e4ff2 / 3209e2e14790ee10, with no additional source deployment this turn.


## Accounting memory cap applied — 01:11:27 UTC

Commit `03dd62d` changes the persistent accounting limit to 512 MiB. Fresh preflight showed nearly 26h uptime, lifetime kernel peak 290 MiB, no OOM kills and no restarts. Docker update reduced RAM 768→512 MiB and total RAM+swap 1536→1024 MiB in place. Container ID `cbc37a3f…`, PID 1911798 and restart count 0 remained identical; health stayed healthy. Web/collector Python source remains `3209e2e14790ee10`; neither was recreated.

- Required full suite: 2,879 passed in 183.43s, exit 0; one test-server connection-reset/teardown warning. Ruff: no new findings, 502 known. Compose validation exit 0.
- Persistent production Compose SHA256: `7416a768f6342be8dab164f2b4f79d8b77cb3b3956db80c56491d672ac7010f8`. Before-copy retained under `/opt/spreadboard/backups/funding-continuation-20260911/compose.before-accounting-cap.yml`.
- Evidence: `output/continuation-20260911/accounting-cap-change.json`. Total declared container RAM is now 8,384 MiB; this first reduction alone does not close the host overcommit requirement.
- The acceptance summarizer now tracks stable memory configuration separately from stable container/source generation. Final 48h acceptance must cover the final cap configuration; retain existing evidence and reset/extend the single finite observer only when the remaining cap decisions are final.
