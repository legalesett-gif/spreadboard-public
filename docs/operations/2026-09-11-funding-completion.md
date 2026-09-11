# SpreadBoard completion and continuity record

Updated: 11 September 2026, 05:36 UTC. Owner: Codex in this chat. The full task remains active; no external Claude continuation is required.

## Current release

- **Production:** source candidate `56adbe1` (docs-only HEAD `2051ed7`), source digest `4430d9f68e5e62f7`, verified in web and collector. The guarded retry exited 0 after finalization finished. `/api/health` and `/free` returned 200 with a warm index of 209,293 priced routes out of 215,488 structural routes.
- **Release gates:** 2,924 tests passed in 217.59 seconds; canonical Ruff reported 500 known findings and none new. Native empty-history correction `ce8f3c4` is deployed; ordinary worker classification still needs verification.
- **Live processes checked at 05:36 UTC:** both application containers healthy, zero OOM kills/restarts. The same finite observer restarted warm at 05:36:27 UTC, PID `2526836`; no duplicate observer or recurring automation was created.
- **Working location:** `tmp/spreadboard-exchanges-trial`, branch `codex/exchanges-funding-trial-20260910`. Use this isolated worktree; preserve unrelated root-worktree changes.

## Requirements and evidence

| Requirement | Implemented and verified | Remaining qualification |
| --- | --- | --- |
| Remove unwanted exchanges | HTX and Ourbit excluded from public opportunities. CoinEx and Phemex excluded from funding while price/spread collection remains enabled. Live controls and parser regressions checked. | Continue preserving funding-only versus price exclusions. |
| Hyperliquid funding and builder markets | Native catalogue includes builder namespaces. OPENAI’s live Markets group contains the exact Hyperliquid `io:OAI` instrument and cross-exchange routes. | Identity and oracle guards remain mandatory. |
| Exchange filters | Live either-leg exclusion changed ONG results and leader; navigation retained selections. | Live JSON downloaded and parsed at 05:28 UTC: 42 unique ONG route keys, matching the UI count, with Gate and WhiteBIT absent on both legs. |
| Correct current funding | Native projected rates, settlement cadence, explicit zero, missing data and expiry handling are separate from historical funding. Fixes apply across venues. | A projection is not a promised settlement. |
| Correct 24h, 7d and 30d funding | Rolling totals use exact settlements in an incremental SQLite ledger. Audit checked 25,606 unexpired cached totals across 15 venues: zero arithmetic mismatches or missing boundaries. | This proves aggregation against the ledger, not independent verification of every exchange event. Short, gapped or unavailable archives remain blank. |
| WhiteBIT history and identity | Ordinary catalogue revision 4: 398 futures, 487 spot markets, 93 tokenized futures. All 398 futures had histories at the 04:33 UTC check. Announced future delistings remain visible until their cutoff. | Current rates and historical totals retain their own signs and time periods. |
| Complete archive checking | Overdue periods are prioritized; initial deep checks follow before already-current maintenance. First normal cycle cleared 20 delayed checks. | BitMart’s 197 remaining checks exposed the empty-native-result bug now deployed; ordinary worker recovery remains to be verified. |
| Relevant markets and resource use | 03:36 UTC catalogue audit: 2,242 futures labels across 9,738 contracts; 4,436 spot labels across 12,353 contracts. No duplicate exact venue/type/symbol keys. | Labels are not independently verified unique underlyings. Legitimate cross-exchange contracts must remain available for pairing. |
| Seven-day full-access trial | Verified Telegram linking required. Persistent, atomic email and Telegram claims prevent reuse; Gmail aliases normalized; paid access preserved. Local HTTP tests verify access before activation, during trial and after expiry across protected features. Production schema and live signup copy checked. | Real Telegram activation was simulated locally; no live identity was activated or message sent by this task. Multiple distinct identities cannot be ruled out entirely. |
| Telegram advertisement | [Revised draft](../marketing/2026-09-11-seven-day-trial-telegram-draft.md) prepared. | Unsent. Its publication gate remains explicit. |
| Self-healing and restart safety | Controlled recovery drill and persistent restart-cap tests are recorded in the [stability review](2026-09-05-stability-review.md). Startup, replacement-container and missing-inspect cases have regressions. | Keep disruptive drills outside the clean observation window. Historical drill results do not prove current 48-hour uptime. |
| Handover/completion note | This checklist, linked evidence and chronological journal provide continuity in this chat. | Update this checklist as gates close; no Claude dependency. |

## Open acceptance gates

1. **Verify ordinary worker recovery after the deployed native-history fix.** Deployment, source parity, warm priced routes and HTTP checks passed. Verify ordinary workers classify empty recent archives correctly; never manually change history flags. The first warm health sample still showed 197 pending checks.
2. **Finish ordinary-load memory measurements and safely lower limits.** Current limits are web 3,584 MiB, collector 4,096 MiB and accounting 512 MiB. Targets are 3,072 / 3,584 / 512 MiB, plus Caddy 192 MiB: 7,360 MiB total against about 7,941 MiB physical RAM. Accounting is already reduced. Full discovery, finalization, index publication and normal worker overlap must fit before reducing the other limits.
3. **Verify 30 priced-route samples over an hour, within ±10%, without deployment contamination.** The latest clean window is too short. The analyzer separates container, observer and cap generations.
4. **Complete the final 48-hour reliability window.** `/api/health` and `/free` must return 200 on a five-minute cadence; no unhealthy period may exceed 90 seconds; both cgroups must have zero OOM kills. Inspect kernel OOM records for the same period. A reset counter or short observation does not satisfy this gate.
5. **Verify two normal backup timer successes.** The catch-up run completed at 01:56:59 UTC, exit 0, snapshot `3e19180c`; all 172 selected data packs passed integrity checks. It is not one of the two normal runs. Next scheduled firing was freshly confirmed as 06:21:05 UTC. Inspect actual start, completion and exit status for each normal invocation.

## Latest measured memory behavior

The pressure cleanup returns already-freed allocator memory, preserving live caches and the 180-second full-GC cadence. Under higher pressure it may run every 20 seconds, while the ordinary trim remains on a 60-second cadence.

Production observed allocator-only trims at 04:50:27 and 04:50:51, 24.2 seconds apart: 2.596→2.159 GiB in 71 ms, then 2.556→2.422 GiB in 132 ms. This verifies that the new pressure path runs. It does not by itself prove the final lower cap is safe.

The pre-release 05:29 snapshot covered 44.98 clean minutes: 23 priced samples ranged from 208,817 to 209,649, with no endpoint failures. Anonymous memory peaks were web 2,976.6 MiB, collector 3,353.1 MiB and accounting 272.6 MiB. The collector peak overlapped discovery, market evidence, settlement and quote workers. The later finalizer completed before deployment; its full observations still need to be collected. No lower web/collector limit has been certified.

Process-level evidence records a web RSS high-water mark of 3,086 MiB. The collector peak coincided with the materialized-view builder, settlement worker and quote workers; the builder’s recorded RSS high-water mark was 2,054.2 MiB. RSS and cgroup anonymous memory are distinct measurements. Neither a sampled partial-cycle peak nor a successful trim certifies the proposed lower limits. See `output/continuation-20260911/ordinary-peak-processes.json`.

## Deployed native-history correction

Fresh BitMart history requests at supported limits 100 and 10 returned the same July-only latest settlements for sampled AAOI and 1000CHEEMS contracts, while BTC returned current September settlements. Current projected-rate metadata remains separate. The 32-day event store correctly discarded the old rows, but the native wrapper retained `ok` with no usable entries, creating endless deep-pending classifications.

`ce8f3c4` aligns that outcome with the CCXT path: no retained usable events becomes `no_history_rows`. It publishes neither an invented zero nor an unsupported historical total. The regression exercises native parsing, the event store and the persisted build result; removing the correction fails the test. See the [official history endpoint contract](https://developer-pro.bitmart.com/en/futuresv2/#get-funding-rate-history) and the dated public samples below.

## Release and observation procedure

Before each deployment, run both required gates and inspect their actual exit status and summary:

```sh
uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests/ -q
uv run --frozen --with ruff python scripts/ruff_ratchet.py
```

Deploy with `./scripts/deploy_production.sh app collector`; do not force past discovery/finalization. Preserve test-generated data as evidence before restoring only those tracked files from HEAD. Never reset, clean or deploy the unrelated root worktree.

After the final release is warm, restart the **same** finite observer if needed to obtain the full clean window. Its current run is 49 hours with a 50-hour service deadline. Preserve the append-only samples at `/opt/spreadboard/runtime/stability/20260911-funding-acceptance/samples.jsonl`; do not create another observer or recurring Codex task. The versioned reporting CLI, `scripts/summarize_stability_soak.py`, has 30 synthetic evidence scenarios, including required-container scope, missing identities, invalid counters, gaps and deployment boundaries. Run it with `uv run --frozen python scripts/summarize_stability_soak.py PATH_TO_SAMPLES.jsonl`. It matched the previous report exactly on real observations.

No trading, transfers, borrowing, repayment, conversion, withdrawals or external messages are authorized here. Pushover remains off; the recurring Codex automation remains paused. Preserve market identity, accuracy, funding, freshness and subscription limits. Do not increase RAM or spend.

## Evidence pointers

Paths below are relative to the isolated worktree:

- `output/continuation-20260911/versioned-report-full-tests.txt`: combined candidate, 2,924 passed in 217.59 seconds, exit 0.
- `output/continuation-20260911/versioned-report-parity.json`: exact report parity on real observations.
- `output/continuation-20260911/versioned-report-ruff-ratchet.txt`: no new findings, 500 known.
- `output/continuation-20260911/mutant-native-empty.txt`: original-code failure.
- `output/continuation-20260911/bitmart-old-native-archive-evidence.json`: dated native history/projection samples.
- `output/continuation-20260911/all-venue-ledger-audit.json`: 25,606 aggregate checks across 15 venues.
- `output/continuation-20260911/whitebit-ordinary-recovery.json`: native catalogue revision and coverage.
- `output/continuation-20260911/deploy-native-empty-retry.txt`: current production release verification; first attempt safely refused during finalization.
- `output/continuation-20260911/health-native-empty.json`: warm live health evidence.
- `output/continuation-20260911/live-filtered-export-verification.json`: parsed live download evidence.
- `output/continuation-20260911/latest-acceptance.json` and `samples.jsonl`: current observation report and raw chronology.
- `output/continuation-20260911/native-empty-deferred-live.txt`: active scan, container health and observed pressure trims.
- [Chronological release journal](2026-09-11-funding-release-journal.md): earlier changes, failed approaches, measurements, release gates and corrections.
