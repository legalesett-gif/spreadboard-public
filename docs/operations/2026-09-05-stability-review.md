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

- Deploy only after both full gates pass; record matching app/collector source
  digests and freshly probe the OPENAI group inside the app container.
- Measure collector RSS/anon peak, row/book counters and CPU after release.
- Exercise one controlled unhealthy app period, record watchdog recovery, then
  begin the uncontaminated stability window.
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
