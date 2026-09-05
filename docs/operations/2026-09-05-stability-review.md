# SpreadBoard stability review and acceptance ledger

Task: critically review commits through `16899ee` and finish the owner's
24/7 handoff. Production changes remain gated on the full pytest suite and
unchanged Ruff ratchet. No trading actions, Telegram sends, Pushover enablement,
subscription increase, accuracy-gate relaxation, cgroup increase or droplet
spend is authorized.

Current runtime and built app/collector images match `aee0953` (including
`a995b91`), source `3ed1500fd5c9b96a`, verified after recreation at 15:58:50 UTC.
Both protected-worker checks were clear. At 16:01:50 both services were healthy,
OOM 0, /api/health 200, /free 200 and 138,604 priced routes. The first startup
/free request timed out at 45 seconds and later recovered without another restart.

Ourbit is retired. Sampled funding math and all 12 tabs pass. The preceding
14:55–15:56 deployment-free count hour PASSED with 31 samples over 3,623.598s,
max deviation 0.67873%; both required scheduled backups also passed. The new
cardinality audit found zero duplicate exact markets or economic routes: 2,171
futures / 3,965 spot token labels, 142,711 routes for 1,232 tokens. Rich route
storage, funding completeness beyond the 500-token shortlist, cold-start delay,
safe lower caps and clean 48-hour acceptance remain open.

The owner requested Claude continuation. Read
`docs/claude-handoffs/2026-09-05-stability-cardinality-continuation.md` for exact
source/container IDs, evidence, scope and next steps. The Codex schedule stays PAUSED.

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

## Candidate dip and prepared streaming reader (04:51 UTC)

Production remains `fcdec15` / `cf3607b528c8dee1` in both containers. No deploy
or memory-cap change was made during this heartbeat. The first 18 minutes were
stable, but the 40-minute partial evidence now contains a real dip: 21 health
samples over 2,418.5 seconds, priced 97,238–150,147, maximum deviation 33.627%.
All 21 health and nine free-page requests were 200; all 160 host observations
were healthy with no OOM kills. App/collector CPU averaged 0.667/1.995 cores,
app anon peaked 3,268.5 MiB, app HWM 3,555.8 MiB and collector anon 2,825.4 MiB.
The count gate has failed; preserve the full hour for representative evidence.

The dip was observed at 04:46:37 in generation 6, 55 seconds after the
155,615-row index install at 04:45:42. Its refresh took 15.253 seconds, with
39,857 futures, 28,601 futures-spot and 28,580 spot-futures priced routes.
This is timing correlation, not proof that index loading caused it. The normal
worker already subtracts processing from its 20-second period; no further
cadence correction was inferred. Book-age evidence from the earlier sampler
ended before this event. A new 90-minute read-only age sampler,
`spreadboard-stability-scoped-book-ages-20260905.service`, now writes
`public-book-ages.jsonl` beside the current candidate to catch the next event.
It uses 64 MiB/5% CPU and does not make venue API requests or change quotes.

The remaining app peak overlaps decoded structural generations with a complete
input/decoder buffer. Local experiments used an archived 241,758,200-byte,
99,822-row artifact, SHA256
`6cf5590eb7d2b620e4f05b3a82e0850325792af4a8f7c673cc234a49bf014b04`, while retaining
a prior decoded generation. This is deliberately not a current route snapshot.
All compared readers reproduced the full object exactly. Memory mapping did
not establish a reliable RSS benefit and was not implemented. A plain streaming
parser repeated field-name strings; bounded per-load sharing addresses that
retention while returning ordinary dict/list objects.

The prepared implementation adds locked `ijson==3.5.1` (no other dependency
version changes), streams the actual Store.live_route_index call in at most
64 KiB chunks, hashes exactly the consumed bytes and verifies total bytes/hash
before returning any generation. It requires an object root containing object
rows, validates numbers, rejects trailing/incomplete data, and uses a fresh
2,048-field-key pool per load. Route keys, tokens, symbols, fields and quote
values are preserved. Source/path/schema checks remain in the Store. Failed
loads preserve the previous live generation through the real restore call site.

Decimal parsing is intentional: the local YAJL backend with use_float=True
rejected valid uint64 values above int64. Only decimal values become floats;
integers preserve their exact value and the prior supported 64-bit range.
This was checked with unsigned 64-bit max, signed min, quote microseconds,
Unicode across chunks, nested values and signed floating zero. Sources for
parser semantics: https://github.com/ICRAR/ijson#options and
https://github.com/ijl/orjson#deserialize .

The actual prepared Store reader reproduced every archived row. Untraced local
wall/CPU time increased from 2.613/1.785 seconds to 7.376/6.145 seconds. Peak
traced allocation fell from 4,150,362,830 to 938,228,535 bytes. Traced allocation
is not RSS: the untraced Mac RSS observations did not demonstrate a reliable
RSS reduction (671,072,256 versus 959,463,424 bytes in those runs). This remains
a guarded production trial, not delivered memory recovery or safe-cap evidence.
Measure the real app and index worker after deployment, including latency and
collector CPU; correct/revert the reader if the tradeoff is not beneficial.
The existing web structural-install minimum is 600 seconds; worker read cadence
is separate and must be included in the CPU comparison.

Prepared release gate: all 2,396 tests passed in 105.46 seconds, exit 0; Ruff
unchanged at 517. Sixteen new mutants were caught (97 total), including the
real store call site reverted to a whole-file read, chunk bounds, root/row
validation, checksum/byte verification, numeric fidelity, field sharing and
previous-generation preservation. Evidence is under local
`output/stability-20260905/`, including `index-reader-experiments.json`,
`index-reader-store-trace.json` and `mutants/stream-index-results.json`.
Prepared source digest is `1070d86638e8df3b`; it is NOT deployed. Do not deploy
before preserving the current hour through at least 05:08 UTC, then refresh
protected discovery/finalizer status. Final acceptance and lower caps remain open.

### Access failure after the measurement window (12:41 UTC resume)

The user resumed after both bounded sampling windows should have elapsed.
Repeated SSH attempts timed out, as did `/api/health` and `/free`. DNS still
resolved to `178.128.126.204`; direct TCP ports 22, 80 and 443 timed out.
A control request to example.com returned 200. This establishes failure from
the current Mac's network path, not a proven global outage or host-side cause.
The public web tool could not open the site, and Check-Host's API returned
403 without producing an independent measurement. DigitalOcean's public status
page reported operational services (https://status.digitalocean.com/), which
does not establish this droplet's condition.

The Chrome DigitalOcean dashboard required login. No doctl installation,
usual config file or token environment was present. The user was asked to
sign in to the opened dashboard so the existing droplet can be inspected.
The VPN was not connected; no network settings were changed. No reboot,
container restart, deployment, trading action or notification was performed.

The prepared `842f6f2` source digest was reverified as `1070d86638e8df3b`.
Only diagnostic output was untracked; the 2,396-test/517-Ruff gates and 97
mutation checks remain the prepared-release evidence, not a production result.
The measured 04:46 count dip already fails acceptance. Do not assume the later
hour, host state, OOM counters or two scheduled backup invocations succeeded.
Retrieve the full scoped-lookup samples and public-book-age evidence before
deciding on this guarded reader trial. Keep caps unchanged until actual memory
and CPU measurements support lowering them.

Fresh local evidence: `output/stability-20260905/access-check-1243.json`
(the record itself is timestamped `2026-09-05T12:40:44Z`). The existing quiet
heartbeat now prioritizes access recovery and evidence retrieval. The goal
control freshly reports active, but none of the remaining acceptance gates
is marked complete.

## Droplet recovery, Ourbit retirement and reboot repair (14:48 UTC)

The owner powered the existing droplet on and resumed work. Boot ID is
`0109f4f2-8c77-42d3-94df-2951c0f2fd94`; no agent-initiated droplet reboot or
spend occurred. The prior fcdec15 source was reverified in both services before
release. The recovered completed candidate spans 04:06:18–06:06:15 UTC:
471 host samples, 42 health samples over 6,931.73 seconds, and 24 free-page
requests. All HTTP and container-health observations were green, with zero
cgroup OOM kills. Priced routes ranged 97,238–156,013, mean 149,233.238,
maximum deviation 34.8416%, so the count gate FAILED. Mean app/collector CPU
was 0.682762/2.000518 cores; app anon peak 3,268.5 MiB, HWM 3,581.617 MiB;
collector anon peak 3,235.336 MiB and index-worker HWM 1,970.008 MiB.

The recovered 540 book-age samples span 04:51:42–06:21:32. No contemporaneous
book sample exists for the first 04:46 dip. The 05:21:42 dip to 126,103 priced
rows has a sample 0.51 seconds earlier with 25,847 of 25,903 books aged at most
90 seconds. This supports investigating resident refresh/install timing; it
does not prove causation. Do not treat broad current-book coverage as a passed
route-count gate. Evidence: `scoped-lookup-recovered-full.jsonl`,
`scoped-lookup-recovered-summary.json`, `scoped-book-ages-recovered-full.jsonl`.

The owner explicitly removed Ourbit from opportunity coverage. Commit b826ae9
introduces one venue policy across discovery, native collection, chart markets,
structural publication/restoration, warm queries and funding. Filter before
ranking/pagination and before short-leg funding collapse: dropping an Ourbit
winner afterward could hide an eligible alternative. Old ranked caches are
rejected, while a separately verified structural index can still boot a warm
projection through an empty presentation shell. Existing adapters and historical
account records are retained. No other default discovery venue was removed.

A fresh health check after boot found bulk prices, generation and snapshot
current, but the last completed fast cycle was four hours old. Stack-only
profiles identified discovery sleeping inside the OKX rate-file lock and fast
workers waiting for that lock. The saved monotonic value was 2,829,213.908839341
against current uptime-clock 2,258.823216778: a 32-day erroneous sleep after
host reboot. The ordinary watchdog restarted the collector at 14:27:33 after
15 unhealthy checks; this was autonomous remediation, not an agent-forced scan
interruption. Commit edfe9d6 clamps backward elapsed time to zero, waits one
full unchanged provider interval, and persists the new clock under the lock.
Real HTTP call-site regressions also cover subsequent spacing and non-finite
saved state. First post-release complete fast cycles at 14:33:29, 14:34:29 and
14:35:50 updated 86, 84 and 85 of 93 selected routes. Collector health passed
all five gates; the 14:34 check saw the completed cycle age at 19 seconds.

Normal `deploy_production.sh app collector` shipped edfe9d6, including the
previously prepared ijson streaming reader, at 14:32:36. Fresh protected-worker
checks before the operation and before recreation found neither discovery nor
finalizer. Both runtime digests matched `771e01736a0385d7`; ijson 3.5.1 was
independently confirmed. No memory caps changed. At 132 seconds app HWM was
2,171 MiB and collector cgroup peak 2,963 MiB, which are startup observations,
not representative memory recovery or safe-cap evidence.

The 14:35 inside-app bounded probe checksum-verified all 141,016 published
rows: no Ourbit rows, 62,060 FUTURES, 39,167 SPOT-FUTURES, 38,939 FUTURES-SPOT
and 850 DEX-FUTURES. The chart catalogue retained 22,411 markets without Ourbit.
The OPENAI API returned six Hyperliquid pairs and excluded Gate Spot against
the current 1,486.9 oracle. These include research quotes and positive-funding
negative-entry-basis routes; they are not all matched-size execution claims.
The public /free page returned 200 with zero Ourbit mentions. Token detail
queries intentionally retain audit context; the main list still uses current
positive-spread eligibility, and Funding Now uses positive current net carry.

The first funding probe correctly failed with `funding catalogue still warming`.
Its old 40.7 MB collapsed cache contained Ourbit and was rejected. The existing
navigation worker retried that cache instead of repairing it until discovery
finished. Commit ec5e770 rebuilds an unavailable catalogue in the isolated
navigation worker that already owns the heavy-build slot; a failed repair
cannot publish. At 14:48, only that standalone entrypoint was atomically replaced
in both running containers. Matching replacement images were built and their
full source digests verified in bounded network-disabled helper containers.
No resident server module was patched. Runtime and image source all match
`118c242fd853ac1f`, while both container IDs and discovery PID 22538 remain
unchanged. Evidence is the remote resumed/funding-worker-rollout.json. Verify
the ordinary worker's next successful publication and current funding math.

Final gate: 2,430 tests passed in 92.48 seconds, exit 0; Ruff unchanged at 517.
The first follow-up suite had one test-only PIE807 finding (2,429 passed), which
was corrected before rerunning the full suite. Twenty venue-policy, three
reboot-rate and three cold-funding mutants were caught, bringing the total to
123. Evidence lives in output/stability-20260905/mutants/. No benchmark saving
is inferred from the new venue count or from startup memory.

Backup evidence now satisfies the two-scheduled-run gate: the scheduled
06:20:32 invocation finished successfully at 07:13:59, and the persistent
post-boot timer catch-up at 13:50:18 finished successfully at 14:26:46. Both
are automatic timer invocations after the repaired manual 01:00:38 success.
The earlier failed 00:21 invocation remains in the chronology.


## 15:38 cardinality audit and Claude continuation

Owner requested a duplicate/irrelevant-token memory audit using UA CryptoInvest's
reported 2,534 futures and 4,370 spot token counts. The production catalogue has
2,171 futures and 3,965 spot labels (union 5,363), across 22,411 distinct listing
keys. A checksum-verified index had 142,711 directed routes for 1,232 tokens and
13,692 distinct leg keys. There were zero duplicate exact market keys, zero
duplicate routes under the real economic-identity helper and zero Ourbit legs.
The comparator counts are owner-provided; anonymous website text did not expose them.

The index is 347,633,474 bytes against a 3,324,045-byte market catalogue. Rich
route representations and overlapping generations remain the larger memory
priority. 4,591 current books mapped to spot-only catalogue tokens, but their
stored depth JSON totalled just 156,539 bytes. Do not equate that with a measured
large resident memory saving or drop chart/watchlist/position support blindly.
Read-only probe peak was 214,220 KiB, 11.90 seconds, with streamed index inspection.

Funding postflight succeeded: 171 sampled current routes, 329 matching tokens /
6,450 routes, zero independent exact-leg funding math errors and no Ourbit.
All 12 funding navigation views were populated. The earlier 512 MiB virtual
address-limited diagnostic process failed decoding the 37 MB funding catalogue;
this was a probe limit, not a production rebuild failure. The corrected 1 GiB
virtual limit probe used only about 267 MiB actual peak RSS and passed.

Real HTML rendering of the 15-token API sample also passed: 581 futures routes,
1,623 per mixed lane, 183 excluded context rows per lane hidden. The earlier
assertion that all raw API rows must be displayable was wrong; the renderer
performs its own evidence filtering. Four fresh OPENAI/Hyperliquid rows remained
visible at 15:46 and the Gate spot/oracle trap was excluded at index 1,494.8.

New funding fixes: a995b91 includes the zero-rate spot hedge in shortlist bounds;
aee0953 keeps an entirely expired live cache explicit instead of reviving old Now
carry. Four bound mutants plus the restored-empty-fallback mutant were caught;
total 128. Final full gate: 2,435 passed in 90.77 seconds, exit 0; Ruff 517 unchanged.
An initial test-only PIE807 finding was corrected before the final full green run.
Combined source digest is 3ed1500fd5c9b96a; see handover checkpoint for rollout state.

The 500-token funding shortlist still limits completeness. The corrected bound
addresses proven omissions but does not establish exhaustive opportunity coverage.
Do not claim no missed funding routes. The new handover prioritizes compact
market/leg storage and broad cheap funding selection with bounded detailed views.

Continuation: `docs/claude-handoffs/2026-09-05-stability-cardinality-continuation.md`.
Raw evidence and bounded probes are in local `output/stability-20260905/`.


## Completed deployment-free hour preserved at 15:56 UTC

`ourbit-hour.jsonl` and `ourbit-hour-summary.json`: 242 host observations,
31 health requests spanning 3,623.598 seconds, plus 13 /free checks. Every HTTP
response was 200, every sampled container was healthy, and cgroup OOM kills
remained zero. Container IDs AND start timestamps were unchanged throughout;
boot ID remained 0109f4f2-8c77-42d3-94df-2951c0f2fd94.
Priced routes ranged 136,856–138,620 around mean 137,685.484, maximum deviation
0.67873%: the required one-hour count gate PASSED for ec5e770 / 118c242fd853ac1f.

Mean app/collector CPU was 0.71674 / 2.00726 cores. Sampled anon peaks were
2,746.04 / 2,832.34 MiB; cgroup peaks 3,183.36 / 4,097.70 MiB. Index worker HWM
was 1,835.48 MiB, websocket HWM 1,245.44 MiB. Small bounded probe processes were
included in some cgroup samples. This is not evidence that lower caps are safe.
No final 48-hour acceptance has been completed.

The subsequent rollout uses a temporary copy of the normal deployment helper
with the original worktree root preserved and an extra fail-closed protected
worker check immediately before Compose recreation. The original source helper
is unchanged. The guard reads process argv only in memory, emits script/PID
matches only, and aborts if discovery or snapshot finalization is active. The
pre-build guard was clear; final guard and source parity are recorded in the
handover checkpoint. Sampler observations after recreation must be split from
this preserved accepted hour.


## Final checkpoint — 16:02 UTC

Both services were recreated at **15:58:50 UTC** with `aee0953`, including
`a995b91`, and both running source digests matched **3ed1500fd5c9b96a**.
Normal helper steps ran via `output/stability-20260905/deploy_with_final_guard.sh`:
only the original worktree root and an additional pre-recreation guard differed
from the normal helper. Source `scripts/deploy_production.sh` was not edited.
Both pre-build and immediate pre-recreation checks found no protected workers;
no discovery/finalizer was killed. The copy and guard are reviewable in output/.

- App container: `3e7d0a46128219aeaea504f831666c9f7c55b2d8085327c8a1eb62bf692c2897`.
- Collector: `6180dcc3a96fc82cbb783ca5685f744c797433c1b02062a15fbd6263fb2a8b62`.
- Boot unchanged: `0109f4f2-8c77-42d3-94df-2951c0f2fd94`.
- At 16:01:50 both healthy, OOM 0, restart count 0. `/api/health` 200 in 1.003s,
  `/free` 200 in 3.424s; 138,604 current priced routes / 1,236 priced tokens.
- First post-restart sample at uptime 56s had priced=0 and `/free` timed out at
  45.059s. The later check recovered without another restart. **Cold-start page
  availability remains a concrete follow-up**, not a clean-availability claim.
- After rollout, 170 funding routes independently matched live exact-leg math
  with no errors, no Ourbit; all 12 tabs populated. Four Hyperliquid OPENAI
  routes appeared and Gate Spot stayed excluded against oracle 1,495.8.
- The current funding catalogue was still the previous valid 500-token generation
  during postflight. Verify the next ordinary rebuild uses the new spot bound;
  code deployment alone is not proof that LCAP/READY now rank into the stored page.
  Do not force a concurrent heavy rebuild to obtain that proof.
- At the last process read, materialized-view worker PID 63368 was active;
  discovery/finalizer absent. Refresh before any next deploy.

The deployment-free **14:55–15:56 hour PASSED**: 31 health samples over
3,623.598s, 242 host samples, 13 `/free` requests, all HTTP 200/healthy/OOM 0;
priced 136,856–138,620, max deviation 0.67873%. Container IDs and start times
were constant. App/collector CPU 0.71674/2.00726 cores, sampled anon peaks
2,746.04/2,832.34 MiB, cgroup peaks 3,183.36/4,097.70 MiB. This accepted hour
belongs to the preceding `ec5e770` release. **Do not append post-deploy samples
and call the combined window deployment-free.** Safe lower caps still unproven.

Evidence: `ourbit-hour.jsonl`, `ourbit-hour-summary.json`,
`final-runtime-postflight.jsonl`, `funding-postflight-final.jsonl`,
`openai-postflight-final.jsonl` under `output/stability-20260905/`.
The output directory contains local untracked evidence; preserve it when changing
worktrees. Source fixes are committed; a separate docs commit records this handover.
The finite samplers end around 16:55/16:57. No recurring automation was enabled.

Continue in this order: inspect fresh runtime and sampler state; verify the next
funding rebuild; investigate startup delay and measure compact route/leg storage
plus complete cheap funding selection; validate representative memory headroom;
only then reduce caps and start the clean 48-hour acceptance. No goal completion
is claimed. The owner explicitly requested this Claude continuation note.

## Codex continuation supersedes the handover — 19:55 UTC

The owner subsequently chose continued Codex implementation. Current work is in
`tmp/spreadboard-funding-publication`, branch
`codex/funding-publication-cadence-20260905`. The historical 16:02 checkpoint
above and its accepted hour belong to their stated revisions, not the current
release. The current detailed continuation and UA discrepancy ledger is
[`2026-09-05-ua-comparison.md`](2026-09-05-ua-comparison.md).

Current production is `a6e82e1` / `4a7e5f96478f5fd0`, deployed 19:27:31 UTC.
Full gate was 2,476 passing tests with Ruff 517. Full lossless funding candidates,
independent ordinary catalogue publication, current-mark Kraken point/bulk
agreement and exact-history scheduler fairness are deployed. All twelve views
consumed a new settlement generation, with member-page values independently
reproduced. Ourbit remains excluded. The updated comparison covers 45 visible
UA exact-route references; premium Kraken/XT coverage is not verifiable from
guest search absence. Token counts and directed route counts are distinguished.

A further archive-memory correction is under test because the ordinary evidence
worker still approached 2 GB RSS and the collector touched its ceiling. Safe
lower limits, current-release one-hour acceptance, cold-request latency and the
final clean 48-hour acceptance remain open. The Codex recurring automation is
still paused. No trading, messages, paid access, cap increases or weaker
accuracy/freshness guards are authorized by this work.
