# SpreadBoard stability review and acceptance ledger

Task: critically review commits through `16899ee` and finish the owner's
24/7 handoff. Production changes remain gated on the full pytest suite and
unchanged Ruff ratchet. No trading actions, Telegram sends, Pushover enablement,
subscription increase, accuracy-gate relaxation, cgroup increase or droplet
spend is authorized.

Latest app and collector release: `fcdec15`, source `cf3607b528c8dee1`, deployed
04:01:00 UTC with both digests verified. The source-parity candidate started
04:06:16 UTC. Safe lower memory caps and final 48-hour/two-scheduled-backup
acceptance remain open; broad web row-cache retention still needs measurement.

## Fresh baseline

At 2026-09-04 23:19 UTC, app/collector image source digests both matched
`f6e6f8599e083a38`. Both were healthy; the collector had only been up about
five minutes. Cgroup OOM-kill counters were zero for those container instances.
The immutable baseline samples are in `output/stability-20260905/baseline.jsonl`.
In the first 22 minutes, current priced routes ranged from 3,723 to 88,297.
There was no deployment during that observation. Through 50 minutes the range expanded to 190–92,514. Same-container CPU deltas averaged app 1.033 cores and collector 1.938 cores; sampled collector anon peak was 2,768.1 MiB. Broad books remained current:
a read at 23:29 showed about 26k books and most venue families aged 23–57s.
Collector quote passes in that interval took 40.3–61.1s. App anon reached
3,487 MiB in one sample. A collector pressure line named the index worker at
1,511 MiB RSS. These are observations, not a clean stability acceptance.

## Review findings and corrections

1. Keyed retention is insufficient to fix the swing. The production worker's
   ten-slot priority schedule, at 20 seconds per slot, revisited each mixed
   lane every 100 seconds against a 90-second freshness limit. This independently
   causes expiry even with perfect SQLite reads. The handoff's claim that
   staleness was ruled out was not supported. The replacement alternates
   futures with the other three lanes, preserving the 20-second setting and
   revisiting each lane every 40 seconds before processing overruns.
2. The provider carve-out was unreachable on a full/cold build too:
   `_public_row` stripped `source_name` before admission. The structural
   serializer now preserves provenance and blockers. The warm builder reads
   a bounded side artifact written by the disposable snapshot finalizer,
   where the large snapshot is already parsed. The production subset was
   664 rows, 113 exact builder symbols, and 1,189,705 bytes before compact
   serialization. Missing/corrupt/mismatched artifacts retain the previous
   index and are rebuilt by the collector in its heavy-worker slot.
3. Discovery's `io:OAI` and the native bulk cache's `IO-OAI/USDC:USDC` were
   separate lookup keys. The cache now canonicalizes exact builder coins with
   their namespace preserved. This does not substitute instruments or add
   WebSocket subscriptions.
4. `apply_live_books` updated prices and timestamps but left discovery BBO
   fields in place. Public serialization could then restore the old spread
   under a new timestamp. BBO fields now move with the same observed books.
5. The collector's 180-second TTL alone does not evict idle rows. The collector
   memory watchdog now releases expired entries and trims freed memory.
   App TTL/row-cache policy and broad last-good book fallback remain unchanged.
6. The keyed fallback only pruned requested keys; removed routes could retain
   books forever. A bounded periodic expiry sweep now releases retired keys.
7. Watchdog restart history was lost on missing Docker inspect data. There was
   no explicit running-state/uptime guard, concurrent CLI lock or durable
   pre-action reservation. The watchdog now preserves the budget through
   missing/replaced containers, reserves attempts before Docker acts, includes
   timeouts in the three-per-hour cap, revalidates state and targets the
   observed container ID. A separate append-only remediation ledger records
   attempts/results. Read-only `--print` still does not remediate.

8. Real provider replay showed the index guard was disconnected from ingestion:
   `SpreadTerminalRow`, the funding cache serializer and the complete catalogue
   all discarded the index. The native Hyperliquid sweep already receives
   `oraclePx`; it now publishes a compact funding/oracle artifact without extra
   requests, preserving raw and unified names for the exact namespace. Ingestion
   and catalogue rows now carry the index. A mark price is no longer mislabeled
   as an index. The production-call-site regression excludes Gate Spot against
   the oracle while leaving the futures/futures provider row eligible.
9. The funding reader checked expiry only when file mtime changed. A stopped
   writer could therefore keep a previously loaded rate/index indefinitely.
   Expiry now advances independently, while unchanged file contents stay cached.
   Explicit missing/expired current funding clears the prior index too.

10. Pressure logging could print complete argv for a process without a `.py`
    argument; Docker inspect also retrieved unnecessary environment and health
    log contents. Both observations now select only the needed public health
    fields/script basename. The restricted Docker template was exercised on the
    production app and returned valid selected JSON.
11. The app still reached 3.2 GiB RSS while the captured route-index load peaked
    at about 735 MiB on the Mac. A bounded web allocator trim (at most once per
    180 seconds above 2 GiB RSS) now records before/after RSS and elapsed time.
    It does not evict live rows or books. Whether this delivers enough headroom
    remains a production measurement, not an assumed saving.

Docker's early successful healthcheck can end its own start-period grace;
therefore the watchdog independently enforces the configured grace. References:
https://docs.docker.com/reference/dockerfile/#healthcheck and
https://docs.docker.com/engine/containers/start-containers-automatically/ .

## Validation so far

Thirty source mutants were killed by regression tests covering warm/cold
provider admission, publication, exact cache aliases, BBO coherence, idle cache
release, startup/restarting guards, pre-action persistence, cap round trips,
retired-book expiry, the keyed-read call site and production refresh timing.
Raw results: `output/stability-20260905/mutants/`.

The first full suite exposed a compatibility failure in a pre-existing synthetic
DEX row lacking optional source provenance: `1 failed, 2320 passed`. This was
fixed, not ignored. The final full gate passed: `2337 passed in 133.46s`, exit 0. Ruff passed with 517 known findings and no new findings. Thirty mutants were killed, including the actual oracle pipeline, privacy boundaries and bounded allocator call. Ruff passes with 517 known
findings and no baseline change.

A local profile used the captured 130,325-row production index and book snapshot.
The in-memory repricing call took 3.694s under cProfile on the Mac; this excludes
production I/O and contention and is not a delivered latency measurement.
An alternating 30k-row current-evidence check showed no measurable slowdown
from the spot/perp-index guard (about 0.16–0.17s; timing noise dominates the
comparison). No weakening or removal of that guard is proposed.

## Open acceptance gates

- Latest deployment/probe passed as recorded below. Before any further deploy,
  rerun both full gates and verify matching app/collector source digests.
- Measure collector RSS/anon peak, row/book counters and CPU after release.
- The controlled unhealthy/recovery drill passed; keep it outside the clean
  acceptance window and do not repeat it unnecessarily.
- Obtain 30 priced-count samples over an hour within ±10%, with no deployment
  or intentional recovery drill during that window.
- Run the host-side `scripts/stability_soak.py` sampler for 48 hours. It records
  Docker health every 15s, HTTP every 5m, priced counts every 2m initially,
  cgroup identity/OOM/anon/CPU, per-worker RSS and backup completion properties.
  Also inspect new kernel OOM events over the same window.
- Do not lower memory limits yet: the app's observed 3,487 MiB anon peak is
  above the proposed 3,072 MiB cap. The target total remains 7,360 MiB only
  after measured retention/load permits it. If the app still approaches its
  cap, make a measured cache/representation decision.
- The manually triggered 23:10 backup completed at 23:51:47 UTC with exit 0
  and Result=success, freshly verified at 00:00 UTC. It retried Drive quota
  errors before succeeding. Two subsequent successful timer invocations remain.

No 24/7 or 48-hour reliability claim is made by this ledger.

## Latest acceptance checkpoint and operator direction

Both containers were deployed at 00:53:55 UTC with matching source digest
`40f9bf266cb590f8`. The repaired manual backup completed at 01:00:38 UTC,
exit 0 and Result=success. Two future green timer invocations remain required.

The first resident-board probe found two additional failures. The startup
materializer replaced the broad index with 4,058 scanner rows before the first
bulk publication; the live publisher subsequently grew it to 74,517 and 99,822.
This explains the low early memory reading and is not a retention improvement.
The publication thread was alive: a stack-only py-spy check found it polling
normally, with the funding navigation worker later owning the shared slot.
The new materializer gate waits for the live publisher's matching source
generation rather than replacing its broad structural artifact on restart.

The inside-app `server.api_market_spreads` probe, after the normal resident
restore, returned Hyperliquid futures routes but also the Gate Spot trap. The
compact live-price tuple discarded current oracle observations. The new tuple
option shares exact-leg index observations and their timestamps; warm and
cached HTTP overlays now consume them. Retained prices take the new index,
including explicit missing data, and a stopped refresher cannot freeze it.

The owner clarified that the website should focus on positive spreads or
positive funding rather than showing thousands of irrelevant pairs. Exact
token search now keeps a nonpositive basis only when current funding is
positive and price/identity checks pass. No arbitrary token cap is inferred.
A captured 99,822-row structural index contained 27,442 rows with neither
stored positive spread nor stored positive funding. These are snapshot fields,
not a current opportunity count; assess active-index reduction against live
coverage and new-opportunity delay before using it to justify lower limits.

Latest correction gate: `2347 passed in 91.93s`, exit 0; Ruff 517 unchanged.
Eight additional source mutants were killed (43 total). Results are in
`output/stability-20260905/mutants/resident-results.json`.

The initial 22-minute post-deploy sample recorded app anon peak 2,133.6 MiB,
collector 3,052.2 MiB, CPU averages 0.875 and 2.116 cores, and zero cgroup OOM
kills. The live-index worker's sampled HWM was 1,326.3 MiB, below the prior
roughly 1.6 GB worker. Collector rows/books reached 0/0 between work. The
startup coverage collapse contaminates this comparison: caps and the final
48-hour acceptance window remain pending.

### Deployed correction and candidate window

Revision `01f8e06` deployed successfully at 01:20:58 UTC; both app and collector
digests matched `f899e273d47c9c6b`, health 200 and no OOM or restart increment.
The 104,680-row index survived startup. The inside-app probe checksum-verified
the published artifact while streaming it, retained its 77 OPENAI rows, used
the normal warm universe with real current production books, and called
`server.api_market_spreads`. At 01:22 it returned 52 API rows including six
Hyperliquid futures pairs and excluded the Gate Spot trap against the 1,467
venue index. Peak helper RSS was 178,664 KiB. Its output is
`output/stability-20260905/probe-resident-openai-result.jsonl`. This is a bounded
published-token replay, not a claim to have authenticated a public HTTP session;
the unauthenticated HTTP probe correctly returned 401.

The two-hour candidate sampler started at 01:24:10 UTC, unit
`spreadboard-stability-candidate-20260905.service`, under
`/opt/spreadboard/runtime/stability/20260905-candidate/`. The previous preliminary
sampler was stopped and preserved. Do not deploy during the hour being measured.
This candidate window supports the memory-cap decision and the 30-sample gate;
it is not yet the final 48-hour acceptance at the reduced limits. The existing
15-minute heartbeat has been updated with these completed milestones and the
remaining gates, including the owner's relevance guidance.

### Candidate failure and native cleanup correction ready (02:01 UTC)

The same-container candidate is failing the priced-count gate. Through 02:00:11
UTC, 18 health samples ranged from 33,433 to 113,584 current priced routes.
All 18 health and eight `/free` requests returned 200, all 142 host samples
reported healthy containers, and both OOM-kill counters stayed zero. Entire
futures or mixed families expired between refreshes despite recent bulk books.
Observed warm refresh times reached 41 seconds; sampled venue book ages were
35–74 seconds. Retaining old quotes cannot cure this without violating freshness.

The app averaged 1.034 CPU cores, collector 2.145. Sampled anon peaks were
2,741 and 2,806 MiB respectively. The app process high-water mark was 3,343
MiB; cgroup peaks were 3,411 and 3,452 MiB. The index worker reached 1,572 MiB
HWM. These measurements do not support lowering the app to 3,072 MiB yet.
The earlier collector CPU baseline was 1.938 cores under a different workload;
do not attribute the increase specifically to row-cache TTL without evidence.

A 45-second, 30 Hz `py-spy --gil` profile of the actual web Python PID found
74.7% of 1,309 GIL-owned samples in `FastQuoteRefresher.close`, on its forced
`gc.collect()` call. Native charts accounted for 51.6 percentage points and
custom alert quotes 23.1. The ordinary all-thread profile included sleeping
thread stacks and is not a CPU-share measurement. Neither profile captured
locals or complete command arguments. Raw GIL evidence is saved at
`output/stability-20260905/app-cpu-gil-before.raw`; aggregate candidate evidence
is in `candidate-before-cleanup-summary.json` alongside its JSONL samples.

Native quote calls normally own no CCXT client, so their forced full collection
scans unrelated resident web objects. The correction performs forced collection
only after an actual client is released, both in final close and per-venue
batch disposal. Normal Python collection, client closure, and cache cleanup
remain enabled. Ten regressions execute the real native quote through chart,
alert and batch call sites, including missing quotes and owned-client cleanup.
Eight new mutants were caught, including deleted chart/alert close calls,
deleted client closure and unconditional or missing client collection (51 total).

Full gate: `2357 passed in 94.18s`, exit 0; Ruff unchanged at 517. This cleanup
correction is **not deployed yet**. Preserve the current deployment-free hour
through at least 02:24:15 UTC and collect its complete failed-gate evidence.
Then, after fresh discovery/finalizer checks, deploy app and collector and
verify their digests. Repeat the bounded OPENAI API replay, GIL profile and a
new deployment-free candidate window; a predicted CPU saving is not a delivered
one. Exact-key target lookups and custom chart matching also scan the full map
(about 8% and 4% of the GIL profile); these remain measured follow-ups, not edits
in this correction. Safe caps, the final 48-hour soak and two green scheduled
backups remain open. The task heartbeat is active; the app goal control reports
paused, so no claim is made that its goal runner is active.

### Native cleanup deployed and new candidate started (02:29 UTC)

The complete pre-correction hour was preserved: 31 health samples spanning
3,617 seconds, 242 host samples and 13 free-page requests. All HTTP requests
were 200, both containers stayed healthy, and cgroup/kernel OOM-kill checks
were clean. Nevertheless, current priced routes ranged 3,093–113,584 around a
63,390 mean (maximum deviation 95.1%); refreshes reached 57.35 seconds and the
slowest health request took 17.259 seconds. The route-count gate failed.

Full-hour app/collector CPU averaged 1.038/2.085 cores. Sampled anon peaks were
3,541/3,025 MiB; cgroup peaks were 3,584/4,043 MiB. The app process HWM reached
3,584 MiB, and the index worker reached 1,669 MiB while structural coverage
grew from 104,680 to over 131,000 routes. There is no safe app-cap reduction
demonstrated by this baseline. Full evidence and its evaluator are in
`output/stability-20260905/candidate-full-hour-before-cleanup*.json*`.

The sampler was stopped and an end event recorded before deployment. Fresh
checks found no discovery/finalization worker, including a repeated check
during the build. Another full suite passed (`2357 passed in 95.97s`, exit 0),
and Ruff stayed at 517. `c96d8f0` then deployed to both containers at 02:27:00
UTC, with both source digests matching `4184965aa1d4e0af`. Deployment health
was 200, restart counters zero and OOM flags false.

At 02:28:19 UTC, the bounded inside-app OPENAI replay checksum-verified the
136,857-row published index and retained 79 token rows. The real API call
returned 48 rows including six Hyperliquid futures pairs, and excluded the
Gate Spot trap against the current 1,467.2 venue oracle. Helper peak RSS was
178,152 KiB. These remain research quotes with no matched-size VWAP claim.
Both containers and `/free` passed the immediate post-release checks.

The new two-hour sampler started at 02:29:05 UTC:
`spreadboard-stability-native-cleanup-20260905.service`, evidence under
`/opt/spreadboard/runtime/stability/20260905-native-cleanup/`. Preserve its
deployment-free hour and require at least 30 health observations spanning
3,600 seconds before evaluating the count gate. This is a candidate window,
not the final 48-hour acceptance. Repeat profiling and measured CPU/memory
checks determine the actual benefit; startup memory alone is not a saving.

The matching 45-second, 30 Hz GIL-only profile completed at 02:30:26 UTC.
It contained 770 GIL-owned samples and **zero** in native quote cleanup,
versus 74.7% before. During this short profile the app/collector used
0.685/1.802 cores. That is a short post-startup observation, not a comparable
full-hour CPU saving or a memory-cap acceptance. Raw and summary evidence are
`output/stability-20260905/app-cpu-gil-after-cleanup*`.

The first four candidate samples had 133,074, 133,164, 86,762 and 134,608 priced
routes; refreshes were 3.26–7.15 seconds. The GC correction removed its measured
bottleneck but did not yet fix route stability. A separate read-only public-book
age sampler now records ten-second venue/type age distributions beside the
candidate samples, in `public-book-ages.jsonl`. It reads SQLite in read-only mode
with a 64 MiB/5% CPU service budget and does not query venues or change quotes.
Use that timing evidence to distinguish book publication age from the remaining
40-second family revisit. Do not weaken the 90-second quote truth gate.

### Exact lookup optimization prepared, not deployed

After removing GC, exact `target_rows` scans accounted for over 20% of the
GIL-owned samples; custom-route matching was another visible full-map scan.
The next correction uses dictionary lookups when only exact keys are requested,
while preserving token/lane/all-row union semantics and original quote times.
Existing callers consume the results by route key; the exact-only result has
deterministic key order. No additional full-size index is allocated.

Custom chart matches (including structural misses) reuse the existing bounded
compatibility cache. The lookup checks board path and exact route identity,
uses the existing index-install invalidation, rejects a stale write when a new
generation races the scan, and evicts associated path entries with their rows.
Repeated unchanged charts no longer allocate and scan a tuple of all routes.

Full suite: `2369 passed in 91.27s`, exit 0. Ruff remains 517. Eleven more source
mutants were caught (62 total), including the real saved-route warmer call,
cached lookup/write, identity/path guards, memory bound, paired eviction and
the actual index-restore invalidation. This optimization is local, not deployed.
Preserve the new candidate through at least 03:31 UTC and its full-hour sample
span. Inspect book-age evidence and remeasure memory before the next release.

### Quote-age diagnosis and next corrections (03:14 UTC)

Fresh correlation isolates a resident-lane gap: at 02:53 UTC futures had
59,720 priced routes; at 02:55 it had 2,462 while the mixed lanes retained
about 78,000 routes. A public-book sample 2.5 seconds before that health
response had 99.78% of recent books inside the 90-second truth window. The
main futures venue books were only 11.7–47.1 seconds old, with one stale
Coinbase International book among 131. This is not broad source-book loss.
The earlier two-slot schedule was insufficient, even after removing the GC
bottleneck. At that dip the reported warm refresh took 12.32 seconds.

The prepared correction refreshes all four priced lanes on each existing
20-second production tick. It retains the 90-second quote lifetime, real book
timestamps, exact oracle/funding observations and the separate headline path;
it adds no public venue requests. The strengthened actual `Worker.run` test
models 65-second provider age and five-second processing, checking served
timestamps during both processing and waits. It catches the old 40-second
revisit, a doubled wait and an omitted family (three further mutants).

Through 03:00 UTC, the current candidate averaged 0.702 app and 2.001 collector
CPU cores. It had 16 health/7 free-page requests, all 200, and 126 healthy host
samples with no OOM kills. Priced routes ranged 80,784–138,002. Sampled anon
peaks were 2,608/3,278 MiB; both cgroup peaks reached their current limits. The
app HWM was 3,372 MiB and the index worker 1,765 MiB. Lower caps remain unsafe
to claim from this window. Partial evidence is saved as
`output/stability-20260905/native-cleanup-*-partial.jsonl`.

Fresh memory logs still show 31,079 `SpreadTerminalRow` instances in the app,
with one row-cache entry. Idle expiry only ran in the collector. The next
correction enables the same expiry call in both roles: web keeps its existing
900-second TTL and collector its 180-second TTL. Only entries already unusable
under the normal lookup rule are released. Live rows, reader-held references
and the last-good book fallback are preserved. The watchdog records expired
entry counts and reserves its allocator-trim timestamp after expiry cleanup,
preventing a second forced collection on the same high-memory web tick.

Seven additional mutants cover the real web expiry call, physical-release
hook, exact TTL boundary, preservation of a still-live entry, duplicate-trim
guard and logged expiry evidence. This is an idle-retention correction, not
a claim that every currently retained app row has expired or that a memory
saving has already been delivered. Inspect post-release `rows_expired` counts,
heap counts, RSS peaks and CPU before changing caps.

At 03:13 UTC an active discovery worker (PID 2485336) was present. Preserve it
and any subsequent finalizer. After the candidate hour, an app-only release
can ship the web corrections while retaining the collector instance/scan;
verify both the unchanged collector PID and the app digest. Reconcile the
collector revision after discovery/finalization finishes, and do not start
final 48-hour acceptance before full source parity and safe caps.

Latest prepared release gate: `2370 passed in 95.15s`, exit 0; Ruff unchanged
at 517. Ten new schedule/expiry mutants were caught (72 total). These changes
include the previously tested direct/custom lookup correction. They are not
deployed yet; production remains `c96d8f0` in both services until the candidate
hour is preserved and the protected-worker preflight is repeated.

### App-only release with discovery preserved (03:39 UTC)

The native-cleanup candidate was closed before deployment, with 32 health
samples spanning 3,918 seconds, 14 free-page requests and 259 host samples.
All HTTP responses were 200 and both containers stayed healthy with zero OOM
kills. Priced counts nevertheless ranged 63,699–144,640 around a 128,304 mean,
failing the ±10% gate. Refreshes peaked at 12.319 seconds and health latency at
10.691 seconds. Full evidence is `output/stability-20260905/native-cleanup-full-hour*`.

Observed app/collector CPU averaged 0.743/1.952 cores, versus 1.038/2.085 in
the prior full candidate. These are measured periods with different discovery
workloads, not a controlled attribution of each saving. App/collector anon
peaks were 3,459/3,278 MiB and both cgroup peaks reached their original limits.
App HWM was 3,590 MiB; index-worker HWM 1,821 MiB. Lower caps remain unproven.

The clean local source still matched the tested `6c2afb7` digest. Discovery
PID 2485336 was active in collector `9a36a719a35d0f4b2fc12ad353ee4b6cd2815cf98eba53a5e7828822054b3080`.
The established `--force app` pattern bypassed the script's broad scan guard
but targeted only the web container. Its generic warning about discarding a
scan does not describe this rollout: the exact collector ID and discovery PID
were checked unchanged afterward. No collector restart was performed.

The app restarted at 03:36:09 UTC and matched source `181f6ecde1a5f335`.
Collector source was independently verified as the previous `4184965aa1d4e0af`.
Health and `/free` returned 200, both containers were healthy, and all restart
and OOM flags remained zero. The bounded 03:38:32 OPENAI replay checksum-verified
the published 149,051-row index, retained 95 OPENAI rows, and returned 48 API
rows including six Hyperliquid futures pairs. The Gate Spot trap was excluded
against the current 1,461.8 venue oracle. Helper HWM was 179,128 KiB.

An explicitly preliminary two-hour sampler started at 03:39:35 UTC:
`spreadboard-stability-all-lanes-preflight-20260905.service`, under
`/opt/spreadboard/runtime/stability/20260905-all-lanes-preflight/`. It records
the app-only release while waiting for the protected collector scan/finalizer.
It is not a clean final acceptance window. Reconcile collector source when
protected work finishes, preserve this preflight evidence, then begin the
candidate hour with full source parity. Do not delay safe collector parity
merely to preserve this explicitly preliminary measurement.

## Production rollout and preflight observations

- App-only deployment completed at about 00:17 UTC. App source digest matched
  `34127765168e4b70`; health returned 200. The combined command refused the
  active discovery scan. `--force app` bypassed that broad script guard but
  did not restart the collector: container ID and discovery PID `2406460`
  remained unchanged. The collector image is still the previous revision.
- `spreadboard-stability-preflight-20260905.service` records a two-hour
  preliminary window under `/opt/spreadboard/runtime/stability/20260905-preflight/`.
  This includes deployment/recovery work and is not the clean 48-hour window.
  The Codex heartbeat `finish-spreadboard-stability-acceptance` continues the
  open work every 15 minutes and stays quiet on unchanged evidence.
- The first allocator measurement (2.640 -> 2.643 GiB, 2.416s) recovered no
  measurable memory. Do not count the trim as a delivered memory saving.
- The 00:21:01 backup timer run failed at 00:22:02 with
  `backup_repository_unavailable`; the prior manual success does not satisfy
  the two-timer gate. A direct repository probe succeeded with 24 snapshots,
  but the exact hardened service environment is being checked separately.
- Recovery drill: the initial 00:22 signal paused Docker's init wrapper and
  left the web server healthy. That was not a valid unhealthy test. Init was
  resumed, and the actual server PID `2410313` was paused at 00:25:37 UTC.
  A 12-minute guarded same-PID/same-container-instance resume fallback was
  scheduled first. The ordinary two-minute watchdog observed unhealthy at
  00:28 with streak 1. Wait for recorded unaided restart and HTTP recovery;
  never include this intentional interruption in the clean soak.

- The watchdog restarted the actual app server unaided at 00:32:34 UTC. Its
  durable state and remediation ledger show one reserved attempt and success.
  Both `/api/health` and `/free` returned 200 at 00:34:28, and the fallback
  timer was cancelled unused. This satisfies the controlled recovery drill;
  cap simulation remains covered by persisted-state regression/mutant tests.
- An exact-sandbox restic probe succeeded, but took 105.133 seconds and reported
  read-only-filesystem/config-save failures. The configuration cannot persist
  refreshed OAuth tokens in the prior read-only secret directory. The fix gives
  rclone its own root-private writable directory and a private umask, while
  keeping the other secrets read-only. Move the existing configuration by
  rename, never copy or print credentials. Transient repository probes now retry
  at most three times with bounded delays/timeouts and safe diagnostic codes.
- Backup correction gates: `2340 passed in 95.47s`, exit 0; Ruff unchanged at
  517 findings; five further mutants killed (35 total). The failed 00:21 timer
  run remains in the record and is not counted as green.

## Legacy saved-route construction scope (03:56 UTC)

The post-all-lanes GIL profile (1,196 samples, requested 45 seconds; measured
43.49 seconds) had zero native cleanup/target-row scan samples. Its remaining
full discovery construction came from `tracked_route_warmer.check_once` through
`_find_canonical_route` into `load_spreads(q=token)`: ordinary fuzzy filtering
runs after every discovery dataclass is constructed. This explains the resident
31,079-row cache; expiry had not removed it in fresh postflight counters.

The legacy resolver now explicitly supplies an exact token scope. Snapshot and
fast-delta ingestion select matching raw tokens before construction, with the
same uppercase/strip normalization as `_row_from_api`. Funding propagation and
all row/identity guards remain in their original order. General website search
keeps its fuzzy semantics. Both result and single-entry row caches include the
scope; no new resident token index is introduced. Exact route-key matching still
returns no substitute when that route is missing.

The real resolver regression exercises the real loader/parser and rejects any
unrelated token construction. It checks both discovery buckets, delta-only
routes, refreshed prices, normalization, missing exact routes and both cache
orders against a normal fuzzy query. Nine mutants were killed, including the
resolver/loader/delta call sites, either filter and either cache-key scope.

Before this new scope change is deployed, the app-only preliminary sample has
9 health observations over 962 seconds: priced 143,645–146,145, maximum deviation
1.275% from the mean, all 9 health/4 free-page requests 200, all 68 host samples
healthy and OOM-kill zero. It is too short and lacks collector source parity.
App/collector average CPU is 0.718/1.905 cores; observed anon peaks are
3,065.5/3,823.5 MiB, app HWM 3,422.3 MiB and index worker HWM 1,844.2 MiB.
These are reasons to keep existing memory limits, not evidence for tightening.
Discovery and its snapshot finalizer have completed; re-check for a new scan
immediately before attempting a collector deployment.

Final scope-change gate: 2,373 tests passed in 93.05 seconds, exit 0; Ruff
unchanged at 517. The first run caught test-only style findings (2,372 passed,
1 Ruff-ratchet failure); they were fixed before this full green run. Nine new
mutants bring the reviewed total to 81. Prepared source digest: `cf3607b528c8dee1`.


## Full source parity and new candidate (04:06 UTC)

`fcdec15` shipped to app and collector at 04:01:00 UTC. Both digests matched
`cf3607b528c8dee1`, both were healthy with restart count zero and OOM-kill zero.
Fresh preflight at 03:59:59 proved the protected discovery/finalizer had ended;
normal deployment used no force. App container is
`5c194a27a8ea84711a96e4ba0560f3bd70d27936c019a0b2f7a88d39cbd0c8cf`, collector
`1a0446771401ae8a3a7fa6c441f15066d8f9b0af7da5aa14c51d3b75b181f094`.
The preceding app-only preliminary window closed with 11 health samples over
1,203 seconds, priced 143,645–146,145 (maximum deviation 1.308%), all HTTP and
80 host observations healthy, OOM-kill zero. Average app/collector CPU was
0.705/1.969 cores. App HWM reached 3,577.9 MiB and collector anon 3,823.5 MiB;
it neither satisfies the hour nor supports reduced limits.

The inside-app published-index OPENAI replay passed at 04:02:57: checksum and
row count 150,688 verified, 95 token rows installed, 53 API rows returned,
five Hyperliquid futures pairs present. Gate Spot stayed excluded against
oracle 1460.9. These remain research quotes, not verified matched VWAP.
Use `/app/.venv/bin/python` for these application probes; system Python lacks
CCXT. The initial system-Python probe failed at import and was then rerun with
the correct interpreter; it is not evidence of a production dependency failure.

The cold legacy fallback probe at 04:05:32 constructed and retained only
42 OPENAI rows, one cache entry, 3.22 seconds and 317,316 KiB helper HWM. Its
requested historical Kucoin-to-Hyperliquid exact key was absent and returned
None, without substitution. The previous probe's expectation that this old
key still existed was incorrect. The probe proves scoped construction and
missing-route behavior; the separate live-index API replay proves current
Hyperliquid visibility. Production startup still logged roughly 29,971 web
SpreadTerminalRow objects from broader loads. The targeted fallback correction
is therefore not proof that every source of broad row-cache retention is gone.

A new 45-second GIL profile (46.23 seconds including instrument setup/teardown,
733 samples) contained no `_load_api_discovery_rows` or native `close` stacks.
Heap diagnostics and repeated persisted Telegram-cache decoding are visible
residual CPU work, but no further behavior change is made on this short sample.
App/collector CPU during it was 0.613/1.953 cores. The app HWM at its end was
2,330.2 MiB. Treat all of these as startup observations, not full-window savings.
Evidence files are under local `output/stability-20260905/` with scoped-lookup
probe/profile names; the production postflight/profile files are beside the
new sampler evidence.

`spreadboard-stability-scoped-lookup-20260905.service` started at 04:06:16 UTC
for two hours under `/opt/spreadboard/runtime/stability/20260905-scoped-lookup/`,
after both source digests and the probes were rechecked. It uses 96 MiB/10% CPU.
Initial fresh health was 200, priced 147,958 of 151,940 routes across all four
lanes, refresh 6.318 seconds. Preserve its deployment-free hour through at
least 05:08 UTC and require 30 samples spanning at least 3,600 seconds. Measure
actual idle expiry, row/heap counts and representative CPU/memory before any
further retention change or lowering caps. The final 48-hour soak has not begun.
The repaired manual backup remains successful; next timer freshly confirmed
06:21:08 UTC, and two future scheduled successes are still required. The prior
watchdog recovery drill remains complete and must not be repeated.
