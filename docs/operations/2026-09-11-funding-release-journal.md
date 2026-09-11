# Funding release: chronological evidence

These are dated snapshots. The [completion record](2026-09-11-funding-completion.md) carries the current checklist.

# SpreadBoard completion record

Owner: Codex in the current chat. There is no external Claude handoff. The older handover remains a linked chronology; this checklist controls the continuation.

## Latest verified deployment

Both services run56362cd/2dcdb127627ba3db, guarded deployment exit0, exact source matches and warm health/free200 (209066priced routes). Initial deep-archive ordering fix deployed after2922tests204.44s and canonical Ruff500/no new. Ordinary first cycle cleared20deep checks; BitMart197 exposed a separate empty-native-result classification bug, now fixed and fully tested locally but NOT deployed during active discovery. Candidatece8f3c4:2923tests191.93s; canonical Ruff500/no new; native old-archive call-site mutant fails. Observer2494056 restarted warm04:44:47UTC, same finite49h/50h unit. Discovery2497121 freshly confirmed live; preserve it and its finalizer. App3584/collector4096/accounting512MiB unchanged. Memory/caps, final hour/48h and two normal backups remain open; next backup06:21:05UTC. No trading, external messages, alerts or recurring-task activation.

## Requirement-by-requirement status

| Requirement | Evidence and remaining acceptance |
| --- | --- |
| Venue policy | Live controls exclude HTX/Ourbit; CoinEx/Phemex funding-only exclusions preserve price collection. Native/public parser regressions pass. |
| Hyperliquid and builders | Native discovery covers all advertised builder namespaces; live funding shows builder routes. OPENAI Markets contains exact io:OAI routes with identity guards. Ordinary history worker recovered IO-OAI 24h/7d; its short archive cannot supply 30d. |
| Exchange filters | Live either-leg exclusions changed results and leader; period/farm navigation preserved selections. Export acknowledgement observed; exported content is covered by regressions, not a separately parsed live download. |
| Current and settled funding | Shared schedule/sign/missing-value fixes deployed. 2,777 published windows across 15 venues match ledger sums; missing/expired/gapped windows remain unavailable. Latest ordinary refresh has zero overdue periods across9,406 active contracts;210 deep-history checks remain open. |
| Relevant and nonduplicate markets | No repeated native contract IDs within any venue's futures catalogue. The earlier BitMart count-only assessment was insufficient: 359 trading includes four unsupported inverse USD contracts. Native catalogue correction retains 355 linear futures and 53 stable-quote spot markets with exact USDC settlement; collector source and ordinary catalogue publication verified53 spot/355 futures with no malformed symbols. Negative-rate legs remain necessary for positive net pairs. |
| Seven-day full trial | Telegram identity and email claims are atomic and persistent; live registration terms verified. Local HTTP checks prove entitlement before/during/after trial on four protected pages. Native Telegram callback was simulated locally, not exercised through a real live identity. |
| Telegram advertisement | Draft written; no channel message sent. |
| Spread continuity | OPENAI builder pairing verified; latest-generation route samples narrowly bounded so far. Required 30-sample hour remains open. No weakened quote/identity gates. |
| Memory budget | Accounting cap reduced in place from 768 to 512 MiB after a 290 MiB lifetime peak over nearly 26h. Web/collector caps remain outstanding. Funding-only client retention/concurrent construction reduction measured locally and tested. Production savings and ordinary rebuild peaks must be measured before caps change. Web anonymous memory reached about 3,001 MiB, making an immediate 3,072 MiB cap unsafe. |
| Backup | Stale lock recovered, bounded stale-only retry and persistent cache/four-hour deadline installed. Catch-up snapshot `3e19180c` saved (22.818 GiB, 7,742 files, upload 1:26:24); service exited successfully at01:56:59UTC after all172 selected data packs passed integrity checking. Two normal successful timer runs remain open. |
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

## Historical release checkpoint (superseded by latest deployment above)

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


## Native BitMart identity correction — September 11 01:40 UTC

The prior native-count comparison proved no duplicate IDs, but did not prove contract semantics. Installed CCXT 4.5.71 has no BitMart adapter, so catalogue refresh failed and retained legacy definitions. Direct native APIs work. Of 359 trading futures, four USD contracts are inverse/coin-margined; USDC contracts settle in USDC rather than the old USDT assumption.

- Native catalogue now requires trading status, perpetual product type, no expiry, exact ID/base/quote match, positive finite size, and USDT/USDC linear settlement. It produces 355 futures and 53 supported spot markets. TradFi metadata remains classified explicitly.
- Shared policy rejects inverse and malformed cached aliases at opportunity, funding, catalogue and historical-reader boundaries. It does not silently rewrite old identities. Native funding uses the same contract definition.
- Catalogue definition revision 3 requires both BitMart jobs to succeed, causing an ordinary refresh after upgrade rather than accepting the stale revision.
- Final full suite: 2,888 passed, exit 0. Nine deliberate faults across native identity, cached admission, event retention and catalogue revision failed their regressions. Native live local catalogue returned 53/355.
- Guarded combined deployment correctly refused an active discovery scan. Web-only deployment completed with verified new digest and HTTP 200; collector deployment awaits scan/finalizer completion. At 01:40 discovery PID 2352961 had run 1,749 seconds. No scan was killed.
- Before-release source/image rollback retained under `/opt/spreadboard/backups/funding-continuation-20260911/source-before-bitmart.tgz` and `images-before-bitmart.txt`.
- Backup has reached snapshot/tree/blob integrity checking. Upload completion alone is not acceptance; service remains activating without a terminal exit. Existing observer PID 2340559 remains active.

Evidence: `output/continuation-20260911/{bitmart-native-after.json,bitmart-mutations.json,bitmart-version-mutations.json,pytest-bitmart-final-release.txt,deploy-bitmart-web.txt}`.


## Backup retention correction verified locally — September 11 01:53 UTC

The running backup log exposed 36 separate one-snapshot retention groups because every consistent staging directory has a random temporary path. This defeats the intended seven-daily/four-weekly/three-monthly retention policy. Candidate `a886b08` groups by host/tag and restricts removal to `spreadboard-prod` plus the `spreadboard` tag; the existing calendar policy is unchanged. Backup parent selection uses the matching grouping.

A disposable real restic repository exercised five differently staged historical snapshots plus unrelated hosts/tags, applied retention through `run_backup()`, verified the intended survivors and restored the newest snapshot's contents. Both deliberate regressions (path grouping and missing host restriction) failed. Full suite: 2,889 passed in 171.06s, exit 0; no new Ruff findings (502 known). Production's first dry run encountered the active backup lock; no snapshot was removed and no lock was cleared. The strictly read-only `--dry-run --no-lock` production preview then succeeded: one scoped group,11 kept including3e19180c andc7437bae,25 surplus removals proposed. Nothing was deleted. Reviewed calendar-policy survivors before starting guarded app/collector deployment; actual retention waits for the normal backup workflow.

Ordinary observation at01:47:35: 24h/7d/30d current historical coverage83.90%/81.16%/70.34%; no endpoint failures in this short generation. Collector anonymous peak exceeded the proposed3584MiB cap, so no reduction is safe yet. A local copy of the public route index (215,263 rows,527,051,951 bytes) is being profiled; this adds no profiling worker to production.


## Release and successful catch-up backup — September 11 01:59 UTC

- App and collector both verify06e1c30be5a20324 after guarded deployment; no in-flight scan/finalizer was interrupted. Native BitMart catalogue artifact worker2382325 remains live (222s at01:59); do not infer publication from source parity.
- Backup service completed at01:56:59UTC, exit0, Result=success, inactive/MainPID0. All36 snapshot trees and all172 selected data packs passed; log says no errors. This is the manual catch-up, not one of the two required normal timer firings. Total duration2:03:21 also confirms the installed longer deadline accommodated integrity checking beyond the old two-hour limit.
- Restic noted107 unreferenced packs as noncritical. Corrected retention is deployed for future normal runs; the reviewed preview removed nothing. Service lifetime peak2.6GiB includes cache and is not a measurement of anonymous process memory; observe host/cgroup headroom during normal backups.
- Evidence: `backup-catchup-completed.json`, `backup-retention-production-preview.json`, `deploy-bitmart-retention.txt` under the continuation output folder.
- Local public-index profile: full previous index215,263rows,632,684,544-byte peakRSS/46.68s; prototype retaining898DEX rows plus27,139 safety-evidence entries,98,484,224-byte peakRSS/33.85s. This suggests removing the old full generation from the rebuild overlap. It is not implemented or a production saving: exact continuity, missing-route retention, identity collisions, safeguards and corruption handling must all be preserved and measured before release.


### Ordinary catalogue publication verified — 02:00:15UTC

Generation01:59:08 published BitMart Spot53/Futures355, both statusok, with zero USDinverse or USDC:USDT malformed symbols. Artifact worker2382325 exited. Binance756/Bitget836/Bybit826 futures jobs also succeeded. Hyperliquid job failed and retained317 cached markets, so the required global definition revision correctly remains2. This does not mean native Hyperliquid funding stopped; it is a catalogue freshness failure requiring a fresh diagnostic. Do not mark revision3 acceptance complete or silently lower the required-job gate.


## Compact prior-index candidate — September 11 02:23UTC

Candidate `f69b476` / source `a97d900e6117e8a0` is committed locally, not deployed. Production remains `a886b08` / `06e1c30be5a20324`. Active discovery PID2387856 was freshly confirmed running at02:22:48UTC, elapsed1,150s; wait for its finalizer too before guarded deployment.

- First read retains full DEX seeds and compact exact CEX identities/safety evidence. Even empty safety records retain duplicate-identity ordering. The second read retains full old rows only for still-listed economic identities absent from the current rebuild. Prices, metadata, warnings and missing-route continuity are preserved.
- Both reads bind to the same checksum-verified immutable generation. Discarded fields still undergo numeric validation; changed/corrupt second-pass data refuses publication. Source-signature validation runs after the continuity pass.
- Index cleanup previously counted the pointer as a retained generation, effectively keeping only one data file. It now retains two real generations, preserving a reader's pinned file across one publication. This uses an additional old artifact on disk; it does not retain that full generation in process memory.
- Full public-data comparison used the captured215,263-route index and a deterministic incomplete current cut. Both paths produced215,263 equal output rows/digest666a3b79f47e1eb880a6b05a656c480b3c3032c9a22da4207e6e8724487b530f. Baseline peakRSS1,128,087,552bytes/151.92s; candidate866,598,912bytes/149.30s. Saved249.375MiB (23.18%); runtime difference is small and not a production CPU claim.
- Full frozen-source suite:2,899 passed in195.32s, actual exit0; Ruff no new findings(502 known). Five deliberately injected faults were caught: missing safety evidence, bypassed compact callsite, skipped numeric validation, mixed generation selection, and swallowed continuity-read failure.
- Evidence: `rebuild-{full,compact}-profile.json`, `compact-index-mutations.json`, `pytest-compact-index-release.txt`. Required next checks remain ordinary deployed worker peaks/CPU, unchanged current priced-route continuity, and safe cap decisions. No limits changed here.

### Additional fresh checks

The production Hyperliquid catalogue function succeeded with317 markets in13.17s after the earlier failed job; this points to a transient failure. The published revision remains2 until a successful ordinary refresh; do not force the revision. Evidence: `hyperliquid-catalog-production-diagnostic.json`.

At02:18:57 the live funding file contained355 BitMart contracts, exactBTC/USDC:USDC and zero malformed aliases. At02:20:18 its settled-history entry was statusok with all three windows present and zero malformed BitMart history keys. At02:22 the public book store held24,398 books, each one level per side, so reducing stored book depth is not a measured useful memory fix and was not attempted.

The single finite observer is still running. Final acceptance must begin after final source/caps and warm readiness, avoiding deployment startup samples (the latest generation currently includes a priced=0 startup sample). Preserve earlier samples as chronology; reset/extend the same observer for the final clean window rather than treating this short generation as an hour/48h pass.


## Web cleanup measurement deployed — September 11 02:33UTC

A prior web cleanup reduced RSS from2.598GiB to2.090GiB in1.787s. The deployed measurement separates GC duration/collected-object count/post-GC RSS from allocator-trim duration, so further tuning need not assume which phase actually returned memory. Only the periodic web cleanup requests these measurements; existing cleanup behaviour and cadence remain unchanged. No limit or book depth was reduced.

- Commit8463178, digest3173a7a6e7de31e8, web deployed and verified. Collector remains06e1c30be5a20324. Discovery2387856 and observer2340559 freshly confirmed live at02:33:13UTC. Web startup samples around1.58–1.62GiB have not reached the2GiB periodic-cleanup trigger; phase results remain pending.
- 60 targeted tests and full2,899 tests passed (198.92s, actual exit0); Ruff no new findings502. No new mirrored tests were added for this measurement-only change.
- Rollback source/image references: `source-before-compact-phase.tgz` (1,085,742 bytes) and `images-before-compact-phase.txt` in the existing production continuation-backup directory.
- Production trial schema was checked in `spreadboard_accounts.sqlite3`: eligibility and claim tables present, unique Telegram-hash and email-hash indexes present, zero claims. This confirms installed constraints, not real-user Telegram activation; the advertisement remains an unsent draft. The separate unused `accounts.sqlite3` was not treated as the application database. No account data was printed or changed.
- Evidence: `pytest-allocator-phase-release.txt`, `allocator-phase-tests.txt`, `deploy-allocator-phase-web.txt`. Next: observe phase measurements, make a measured web-memory decision, and deploy collector after its scan/finalizer completes.


## Combined memory release — September11 02:54UTC

- First guarded attempt refused active discovery/finalization. Fresh inventory cleared before retry; no force or worker interruption. Both deployed services match digest a0c51127cc386193. Initial deployment probe timed out; independent later health and source checks passed.
- GC measurement on the prior release:1.547s, zero collected objects, unchanged2.112GiB RSS; allocator trim43ms reduced RSS to1.811GiB. This justified separate60s allocator-only cleanup while preserving180s full GC. Latest deployed full cleanup:2.213→1.611GiB, GC1.339s/zero objects, trim76ms. An allocator-only cycle still needs fresh runtime evidence.
- Targeted61 tests and full2,900 tests191.81s passed. The compact benchmark and injected-fault checks above remain scoped to local evidence.
- At02:54:28, six-minute startup segment: web anonymous peak2250MiB, collector2115MiB, accounting267.8MiB; zero observed OOM/restarts. This is not enough to lower caps. Web startup included61.42s conservative unhealthy bound, so final observation must start after warm readiness.
- Funding history coverage:24h95.75%,7d92.58%,30d79.84%;20 overdue7d legs,210 deep-history pending. Missing/gapped windows remain unavailable. Catch-up is not complete.
- Acceptance analyzer now separates observer, container and cap generations, checks an actual30-sample hour, endpoint/host gaps, unhealthy bounds and valid runtime fields. Eleven synthetic cases passed, including rejection of OOM, missing samples, deployment/startup contamination and short windows. This verifies the reporting logic, not production acceptance.
- Single observer remains active. Final48h must follow final source/caps and warm readiness; extend/reset this same finite unit when ready. Normal backup timer next06:21:05UTC; catch-up exit01:56:59 is not a normal firing.
- Evidence: deploy-compact-and-allocator.txt (initial verification failure retained), allocator-cadence-tests.txt, pytest-allocator-cadence-release.txt, acceptance-summary-checks.json, samples.jsonl.


## Ordinary refresh and observation restart — September11 03:00UTC

- Published catalogue02:52:45UTC is now definition_revision3: Hyperliquid futures317, BitMart futures355/spot53, all statusok. The earlier retained revision2 is superseded by an ordinary successful refresh.
- Production allocator-only cleanup observed: gc_ran=false, RSS2.292→2.235GiB, total122ms, allocator18ms. Full-GC behaviour remains separately scheduled.
- The same finite read-only observer was restarted after warm readiness (PID2422505, active), preserving append-only chronology. It now runs49h with a50h service deadline so a complete48h sample span is possible. This also restores two-minute first-hour route sampling. It is not a recurring Codex automation. Final cap changes would require another clean configuration window.
- At02:59:01 history cache had9,505ok,2ok_cached,225symbol_not_indexed,73no_history_rows,76market_paused statuses. Deep pending (whole retained cache, not active-catalog denominator): BitMart197, Bingx46, Kraken Futures2. Do not count venue-unavailable or too-young archives as invented complete histories.


## Rebuild deadline finding — September11 03:05UTC

The first bounded process measurement was not a successful publication: worker2423547 reached its180s deadline and the supervisor retained the prior complete generation. Sampled VmHWM was1,019,772KiB (995.9MiB), but this cannot certify completed-build memory or CPU. Earlier in the same release an ordinary full-discovery build successfully published215,330 rows (child139.121s; total227.2s includes heavy-slot wait), so publication is possible but not reliably within the current deadline. No cap reduction is justified yet. A local cProfile of the captured public-index comparison is running to identify CPU cost before another implementation decision.

At03:03:11, the warm observer had five minutes: priced209,037–209,372 across three samples, no endpoint/OOM/restart failures; anonymous peaks web2321.8MiB, collector2531.3MiB. These are short samples, not acceptance. Hourly funding rollover left813/835/719 stored windows overdue for24h/7d/30d respectively; collection must catch up rather than exposing expired totals.


## Reader safety and CPU release — September11 03:23UTC

- Found an initial-read failure path that converted an unreadable known generation to empty history. It now refuses publication so previously recorded identity restrictions cannot silently disappear. A same-length corrupted-artifact regression fails when the guard is removed; the pointer is preserved.
- cProfile of the captured215,263-row retention comparison attributed123.98s cumulative to recursive numeric validation and66.00s to normalized shared-field copies, within a361.33s instrumented run. Profiling overhead means these are attribution evidence, not normal production latency.
- Parser-owned numeric validation now uses a per-row stack and exact JSON/Decimal types; normalization avoids repeated type dispatch. All skipped fields, integer bounds, finite-number checks and whole-file checksum validation remain.38 focused tests pass, including nested integer and decimal overflow.
- Paired5,000-row numeric-validation CPU times: baseline0.260/0.236s, candidate0.183/0.122s. Full-size candidate output remains215,263 rows with identicalSHA666a3b79f47e1eb880a6b05a656c480b3c3032c9a22da4207e6e8724487b530f. Candidate whole-run232.31s/peak689,733,632bytes ran amid variable local load; do not compare that wall time or peak directly to earlier runs as delivered savings.
- Earlier first-guard-only suite2901passed234.08s is superseded by final frozen candidate2903passed211.44s. Eight test-generated tracked data files were preserved and restored; source stayed frozen for final gates.
- Deployed9691957/ddb5f720582fbaf5 to both services; guarded script exit0 and digest matches. Previous release recovered from the observed timeout and published215,309 rows in116.478s child time before this deploy. Thus timeouts were intermittent, not permanent failure; their absence on the new release must still be measured.
- Previous web allocator-only cleanup2.602→2.029GiB in107ms further supports allocator retention as a mechanism. No remaining cap was lowered.
- Same observer nowPID2439207 after warm readiness; no duplicate observer/recurring task. Normal backup next06:21:05UTC remains pending.
- Evidence: compact-rebuild-cpu.prof, numeric-validation-paired.json, compact-iterative-profile.json, mutant-first-read-safety.txt, pytest-iterative-reader-release.txt, deploy-iterative-reader.txt.


## Contract counts and partial-period refresh — September11 03:36UTC

Fresh read of ordinary catalogue (published02:52:45UTC) found2,242 distinct futures token labels across9,738 venue contracts and4,436 spot labels across12,353 venue contracts, excluding OKX DEX. Both categories have zero duplicate exact venue/type/symbol keys. These label counts are broadly comparable to the user's quoted2,534/4,370; larger contract counts include legitimate cross-exchange listings. Labels are not a claim of independently verified unique underlying identities.

- Read-only overdue-window audit applied the deployed expiry/due functions to the retained cache. All206 overdue retained-cache records were refresh-due; many are retired/outside the active catalogue. The remaining active partial windows exposed an ordering gap: staleness sorting used the next still-valid period, so a valid daily total could delay an expired seven-day total.
- The background queue now puts previously supported expired periods before known-empty and wholly current archives and applies the same live settlement-schedule tightening as the reader. Calculation, sign, completeness gates, fetch budgets and concurrency remain unchanged. Two actual-build regressions fail with the original source;59 focused tests pass.
- Full suite2,905passed191.62s exit0; Ruff no new findings502 after fixing an import-order issue in the new test. Eight test-generated tracked data files were preserved/restored; no production source changed during the full run.
- Previous reader release completed an ordinary structural build:215,327 routes,132.654s child time (258.0s total including heavy-slot wait). Bounded process samples observed VmHWM1,066,872KiB; sampling ended before process exit, so this is not a certified final per-process peak. The earlier timeout does not recur in this one observed successful build; sustained timing still needs measurement.
- Deployedbf80fc3/40f3266ece81b408 to both services; source/health/free/warm-index checks passed. Same finite observer restarted warm asPID2448898. No cap reduction or recurring automation creation.
- Evidence: overdue-window-audit.json, mutant-partial-expiry-priority.txt, partial-expiry-targeted.txt, pytest-partial-expiry-release.txt, deploy-partial-expiry.txt, iterative-worker-memory.jsonl. Next: verify ordinary clearing of partial expiries, complete ordinary memory/load cycles and final acceptance gates.


## Ordinary partial-expiry recovery — September11 03:38UTC

Fresh production health after the normal settlement worker:all9,406 active catalogue contracts attempted/classified, zero retryable errors, and zero overdue24h/7d/30d windows. Current-window catch-up is now true. Available windows:9,008daily (95.77%),8,727seven-day (92.78%),7,511thirty-day (79.85%);7,473contracts have all periods.210deep-history checks remain pending, so history_catch_up_complete is still false. This is observed ordinary recovery after the queue fix; archive completeness and final stability/caps remain separate open gates. Observer2448898 and settlement worker2447722 were freshly confirmed active.


## WhiteBIT native history audit — September11 04:09UTC

Active-catalogue classification audit found93 WhiteBIT contracts labelled symbol_not_indexed. They are native tradfiFutures, but installed CCXT4.5.71 parses them as spot and derivative-only pruning removes them. Thus100% source classification was not proof of correct archive coverage. The candidate funding-only parser preserves the exact native ID and raw info while recognizing explicitly declared perpetuals; ordinary spot pairs remain pruned. Native catalogue revision4 also propagates tokenized identity classification and requires successful WhiteBIT spot/futures definition jobs.

- Fresh public candidate audit:398 native futures,398 retained history-client futures, zero missing exact symbols,93 tokenized. AAOI/AAPL each returned93 real settlements. AAOI24h−0.03639%,7d−0.32557%,30d+0.35024%; AAPL24h+0.03%,7d−0.0014%,30d+0.59851%. These are dated samples, not permanent rates.
- WhiteBIT delistedAt is an announced epoch-second cutoff. An initial candidate incorrectly rejected any announcement; corrected before deployment. ICX remains included while tradesEnabled=true and its cutoff is future. Both spot/futures reject elapsed or malformed cutoffs. Invalid native source responses refuse publication rather than publishing an empty catalogue.
-62 focused tests pass. Four injected faults were detected: missing history-parser hook, missing native classification/admission, old definition revision, and premature announced-delisting exclusion. Final full suite:2,920passed205.09s, exit0; no new Ruff findings in changed files (503 existing repository findings). This candidate is not yet deployed.
- Official references: https://docs.whitebit.com/api-reference/market-data/funding-history and https://docs.whitebit.com/api-reference/market-data/market-info . Settlements use fundingTime, not earlier rateCalculatedTime.
- Acceptance analyzer now rejects invalid/missing memory/CPU samples, decreasing CPU counters, and any observed OOM/restart even if a later sample reports zero.17 synthetic cases pass. This validates evidence handling, not48h production acceptance.


## WhiteBIT deployment — September11 04:15UTC

Both services run7a78449/0183f440dd142774, guarded deploy exit0 and warm health/free200 at04:15UTC (209133 priced routes). WhiteBIT funding-only parser now recognizes93 native tradfiFutures, exact398/398 client coverage and signed live samples verified; catalogue revision4 publication and normal history recovery remain pending.2920tests205.09s, no new Ruff findings. Accounting512MiB; app3584/collector4096 unchanged because web anonymous peak3142.7MiB exceeds proposed3072MiB cap. Allocator trim returned0.819GiB in145ms; next memory work must use ordinary-load evidence. Same finite observer2473277 restarted warm04:15:23UTC. Final hour/48h and two normal backups open; next backup06:21:05UTC. Hourly settlement rollover remains catching up; no claim of permanently complete archives. No trading or external messages; recurring task paused.

The initial catalogue check still reported revision3; normal catalogue/history workers must demonstrate recovery. The source deployment is verified independently.17 acceptance-analyzer cases pass; prior short window showed no endpoint/OOM/restart failures but cannot certify48h. Evidence: deploy-whitebit-history.txt, health-whitebit-release.json, whitebit-fixed-native-audit.json, whitebit-full-tests.txt, whitebit-history-mutations.json, web-memory-before-whitebit.txt.


## Ordinary WhiteBIT recovery — September11 04:17UTC

The normal catalogue worker published revision4 at04:17:13UTC. WhiteBIT jobs succeeded:398 futures/487 spot,93 tokenized futures; future-announced ICX still retained. Normal history worker has recovered four previously missing tradfi contracts (309ok/89symbol_not_indexed); remaining recovery is still open. Evidence:whitebit-ordinary-recovery.json.

A pressure-trim candidate retains the existing60s cleanup but permits a trim every20s above2.5GiB. Existing180s full-GC cadence and all live caches remain intact. Production observed0.819GiB freed in145ms; this motivates the bounded change, not a claim that final3072MiB cap is already safe.16 focused tests pass; original-cadence fault detected; changed-file lint has zero new findings. Full suite2,921passed200.25s, exit0; source frozen for final gates.


## Pressure-trim release and evidence handling — September11 04:28UTC

Both services844095e/b7d64b77a9869b05 verified by guarded deploy exit0, source parity and warm health/free200 (208948 priced routes). Bounded pressure trim permits20s allocator cleanup above2.5GiB while retaining60s regular trim,180s fullGC and live caches.2921tests passed200.25s; exact required tests/ command also2921passed200.92s. Canonical Ruff ratchet passes500known/no new. Ordinary WhiteBIT catalogue revision4 verified398 futures/487spot,93tokenized; history recovery398ok verified04:33UTC. Observer2480414 restarted warm04:24:50UTC,49h/50h limit, same append-only unit. Startup1.28min peak web2036.3MiB/collector2108.7MiB is not cap evidence. Accounting512MiB; app3584/collector4096 unchanged. Final hour/48h, ordinary discovery memory cycle, remaining funding catch-up and two normal backups remain open; next backup06:21:05UTC. No trading, external messages, alert reactivation or recurring automation.

Fresh append-only samples exposed an analyzer crash on a historical deployment sample lacking container_id. The analyzer now treats missing/duplicate identities and missing caps as invalid boundaries, refuses incomplete latest evidence, and still accepts a later independently complete48h window.22 synthetic cases pass, including five new missing-identity/cap cases. No observer restart was needed for this local reporting fix.

Release-process correction: the canonical Ruff ratchet was run during the release rather than before invoking deployment, and passed500known/no new; the earlier changed-file Ruff comparison was not a replacement for that required command. The exact tests/ command was additionally verified after release. Future deployment must run both exact required commands before invoking the guarded script. Both full runs had actual exit0 and direct summary inspection.

Evidence:pressure-trim-full-tests.txt, pressure-trim-required-pytest.txt, pressure-trim-ruff-ratchet.txt, mutant-pressure-trim.txt, deploy-pressure-trim.txt, health-pressure-trim.json, acceptance-summary-checks.json, latest-acceptance.json.


## All-venue ledger audit and ordinary recovery —04:33UTC

04:33UTC ordinary recovery: all398 WhiteBIT histories nowok; current overdue24h/7d/30d allzero. Latest cache04:33:04 has no retryable errors; deep checks remain BitMart197/Bingx12/WhiteBIT8. Read-only audit25,606 unexpired cached windows over15venues matched SQLite settlement sums exactly, zero mismatches/boundary omissions (aggregation evidence, not an independent audit of every venue event). Canonical release remains844095e/b7d64b77a9869b05. Observer2480414 freshly active; no restart this turn. Current hour/48h, full ordinary discovery memory/cap acceptance and two normal backups remain open. No trading or external messages.

Funding availability at the fresh health sample:9131daily97.06%,8874seven-day94.32%,7601thirty-day80.79% over9408 active contracts. Short/gapped source archives remain unavailable; percentages do not certify unsupported history. Two transient retryable errors in that health sample cleared in the subsequent cache check.

The analyzer’s first4.08minute clean segment showed3priced samples208742–209046, no endpoint/OOM/restart failures, anonymous peaksweb2674.8MiB/collector2108.7MiB/accounting268.0MiB. No cap change is justified by that short startup segment. Ordinary allocator trims have been observed, but the new higher-pressure cadence still needs an actual load cycle.

Evidence:all-venue-ledger-audit.json, health-after-whitebit-recovery.json, recovery-live-processes.txt, latest-acceptance.json. Next ordinary discovery is due around04:44UTC from persisted snapshot timing; observe its real process and finalizer without forcing or restarting.


## Initial deep-archive scheduling —04:37UTC

Fresh queue evidence confirms217 active contracts await an initial deeper archive check (BitMart197/Bingx12/WhiteBIT8). For example Bingx NCSIB2002USD retained a valid24h total but no7d/30d, last attempted01:56:55, next valid-window expiry08:00. Background ordering placed it behind already-current contracts with earlier next settlements even though its existing page budget would request a deeper check.

The candidate preserves overdue-window priority, then gives successful incomplete archives with no prior deep check one bounded pass before already-current archives. Existing fetch/page/concurrency budgets and all completeness checks remain unchanged. Checked short archives return to normal rotation; the change does not manufacture unsupported30d history.60 focused tests pass and the original queue mutation fails the actual build-call-site test. Canonical Ruff ratchet passes500known; full required suite2,922passed204.44s, actual exit0, before deployment.


## Deep-priority release and native empty-archive correction —04:54UTC

Both services run56362cd/2dcdb127627ba3db, guarded deployment exit0, exact source matches and warm health/free200 (209066priced routes). Initial deep-archive ordering fix deployed after2922tests204.44s and canonical Ruff500/no new. Ordinary first cycle cleared20deep checks; BitMart197 exposed a separate empty-native-result classification bug, now fixed and fully tested locally but NOT deployed during active discovery. Candidate2923tests191.93s; canonical Ruff500/no new; native old-archive call-site mutant fails. Observer2494056 restarted warm04:44:47UTC, same finite49h/50h unit. Discovery2497121 freshly confirmed live; preserve it and its finalizer. App3584/collector4096/accounting512MiB unchanged. Memory/caps, final hour/48h and two normal backups remain open; next backup06:21:05UTC. No trading, external messages, alerts or recurring-task activation.

BitMart remaining197 were actually refreshed, but native API returned only old rows. Fresh limit100 and limit10 reads both put AAOI latest at2026-07-23 and1000CHEEMS at2026-07-25; BTC returned current September settlements. Native details still publishes current projected rates/next settlement times, so current projections remain independent. The existing32-day event retention correctly discards unusable old rows. The native wrapper then incorrectly retained statusok with an empty entry list, leaving an endless deep-pending classification.

Candidate aligns the native wrapper with the existing CCXT outcome: successful response with no retained usable events becomes no_history_rows, never an invented zero. Actual native parser/store/build regression and original-code mutation verify the persisted classification and deep-pending count.64 targeted tests pass; full2923tests191.93s, canonical Ruff500known/no new. Source unchanged during final gates; generated tracked data preserved/restored.

Official endpoint contract: https://developer-pro.bitmart.com/en/futuresv2/#get-funding-rate-history (maximum100, supportedlimit10); actual dated public samples are recorded inbitmart-old-native-archive-evidence.json. Candidate awaits guarded deployment after discovery and finalization finish. Do not rewrite retained histories or manually flip classification flags.


## Live pressure path and protected scan —04:54UTC

Fresh04:54UTC: discovery2497121 and observer2494056 live; both containers healthy/OOMfalse/restarts0. Pressure-triggered allocator-only trims observed04:50:27(2.596→2.159GiB,71ms) and04:50:51(2.556→2.422GiB,132ms),24.2s apart, confirming the bounded higher-pressure path executes. Clean9.72minute segment:five priced samples208817–209367, no endpoint failures; anonymous peaksweb2650.3MiB,collector3110.2MiB,accounting267.9MiB. Full discovery/finalizer and safe caps remain unproven; no deployment or cap change during the active scan.
Evidence:native-empty-deferred-live.txt and latest-acceptance.json.


## Completion record consolidated —05:00UTC
The current checklist now separates production56362cd from ready-but-undeployedce8f3c4, product evidence, remaining acceptance gates and exact release procedure. Earlier snapshots remain in this journal. Advertisement remains unsent; duplicate-trial wording now specifically refers to reuse of the same Telegram identity. Links/evidence paths checked. Fresh observer2494056 and discovery2497121 remained live (scan elapsed10:56); both containers healthy/OOMfalse/restarts0. No deployment or cap change during this interval.


## Versioned acceptance CLI —05:10UTC
The report now lives in scripts/summarize_stability_soak.py with tests/test_stability_acceptance_summary.py. It requires all three monitored containers with distinct identities, rejects boolean/nonfinite route counts, and handles absent latest health/counters without falsely passing or crashing.30 evidence scenarios pass; removing required-container scope fails the regression. Real-file outputs match the earlier session report exactly. Combined candidate full required suite2924passed217.59s, exit0; canonical Ruff500known/no new. Native empty-history fix remains included and undeployed while discovery2497121 runs. No server observer or production behavior changed this interval.


## Candidate ready, scan preserved —05:11UTC
05:11UTC: production remains56362cd/2dcdb127627ba3db. Ready candidate56adbe1/4430d9f68e5e62f7 includes native-empty-history correctionce8f3c4 and versioned scripts/summarize_stability_soak.py. Full required suite2924passed217.59s, canonical Ruff500/no new;30 synthetic evidence scenarios, required-container mutant failure and exact real-file report parity. Discovery2497121 liveelapsed21:37, observer2494056 live, both containers healthy/OOMfalse/restarts0. No deployment/cap changes during the scan. Latest copied21.72min window:11 priced samples208817–209380, no endpoint failures; anon peaksweb2887.2MiB/collector3110.2MiB/accounting272.6MiB. Full cycle/caps, final hour/48h and two normal backups remain open. Use the versioned report going forward; temporary report is historical.


## Ordinary peak attribution —05:18UTC
05:18UTC verified wait: discovery2497121 liveelapsed28:28, observer2494056 active. No deployment or cap mutation. New process-level peak evidence: web recorded RSS HWM3086MiB; sampled cgroup anon2976.6MiB. Collector sampled anon3110.2MiB coincided with materialized-view/settlement/quote workers; builder recorded RSS HWM2054.2MiB. Distinguish RSS from anonymous cgroup memory; full cycle/finalizer still required before choosing limits. Latest05:13 report has15priced samples208817–209649 over28.35min, no endpoint failures. Pending56adbe1 remains ready; production56362cd unchanged.

## 05:30 UTC — live export and guarded release deferral

The live Funding UI was filtered to ONG with Gate and WhiteBIT excluded. The actual downloaded JSON contained 42 group routes and 42 unique route keys, exactly matching the UI count; excluded venues were absent from both legs. The first route retained negative daily/weekly and positive monthly settled totals separately from projected carry. The browser download-event wait timed out, but the page acknowledged download and the file was located and parsed successfully. Evidence: `live-filtered-funding-export.json` and `live-filtered-export-verification.json`. No browser authentication state was saved.

The discovery process exited, but its publish finalizer started between the initial process check and deployment. The deployment script refused before syncing/restarting (actual exit 1); production remains unchanged. Full-cycle sampling through 05:29 reached collector anonymous memory 3,353.1 MiB during overlapping discovery, market evidence, settlements and quote workers; web peak remained 2,976.6 MiB. No cap was reduced. The clean window was 44.98 minutes with 23 priced samples, no HTTP failures, OOM kills or restarts; the hour/48-hour gates remain open.

## 05:36 UTC — native empty-history correction deployed

After the publish finalizer ended, the guarded retry completed with actual exit 0. Both app and collector match digest `4430d9f68e5e62f7` (tested source candidate `56adbe1`, documentation HEAD `2051ed7`). Warm health/free checks returned 200, with 209,293 priced routes of 215,488 structural routes. Containers were healthy with OOM=false and restart counts zero. The ordinary settlement worker still needs to convert the 197 prior empty-archive classifications; no flags were changed manually.

The same finite observer restarted after warm readiness at 05:36:27 UTC, PID2526836, preserving append-only chronology and its 49-hour run/50-hour deadline. Caps remain web3584/collector4096/accounting512 MiB; full earlier finalizer observations and safe lower-cap work are outstanding. First normal backup remains due06:21:05UTC, and final hour/48-hour acceptance remains open.

## 05:40 UTC — ordinary native recovery and completed-cycle peaks

Readonly persisted output confirms ordinary recovery: deep-pending197→144, with58 BitMart `no_history_rows` classifications. Supported window coverage is unchanged at96.74%/94.0%/80.55%. Recovery is in progress, not complete; no manual cache edits. Evidence: `native-empty-ordinary-recovery.json`.

Collected the complete previous generation through finalization and deployment. Anonymous peaks remain web2976.6MiB/collector3353.1MiB; neither cgroup recorded an OOM kill. `complete-cycle-peaks.json` includes process attribution. The proposed3072/3584MiB limits still lack comfortable demonstrated headroom; no cap mutation was performed. Reviewed existing streaming-index, structural install and allocator cleanup paths: bounded streaming and by-reference universe installs are already implemented, so do not reintroduce or repeat those changes as new fixes. Further optimization needs a measured remaining allocation source. Single current observer2526836 freshly active; runtime duration and normal backups still pending.

## 05:54 UTC — duplicate route identifier allocation removed

Measured full artifact:215263 routes each retained a distinct lookup-key string and equal row `route_key`, totaling69988872 redundant bytes. The reader now reuses the lookup string only when equal; absent/null/different payload IDs retain original values, and mutable row/nested objects remain independent. No global intern pool or extra persistent cache is introduced. Focused39tests pass; disabling sharing fails the regression. Full2925tests pass251.12s, actualexit0; canonicalRuff500known/no new,exit0. Test-generated tracked data preserved before exact restoration.

Baseline local maxRSS794738688 versus changed418611200 bytes varied more than the directly measured redundant strings; report the67MiB exact allocation reduction, not the fullRSS delta as a guaranteed saving. The changed profile retained all215263rows and zero duplicate identifier strings. Artifact was an earlier production-sized captured generation, not the latest live prices.

Commitc8af1eb/digest10ec5f99f3b8e3fd deployed guarded,actualexit0,source paritybothservices. First startup sample hadpriced0, so observer was not restarted until warmhealth05:53:41 returned209072priced/215309structural. Bothcontainershealthy/OOMfalse/restarts0;health/free200. Ordinarynativehistorypending197→105. Samefiniteobserver restarted05:54:01UTC,PID2538662. Caps unchanged; ordinary-load cap verification, finalhour/48h andtwo normalbackups remainopen,next06:21:05UTC.
