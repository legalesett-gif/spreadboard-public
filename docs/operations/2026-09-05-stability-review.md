# SpreadBoard stability review and acceptance ledger

Task: critically review commits through `16899ee` and finish the owner's
24/7 handoff. Production changes remain gated on the full pytest suite and
unchanged Ruff ratchet. No trading actions, Telegram sends, Pushover enablement,
subscription increase, accuracy-gate relaxation, cgroup increase or droplet
spend is authorized.

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
