# Historical handover — superseded

Do not execute the deployment/worktree instructions below. See `2026-09-05-stability-cardinality-continuation.md` for the current handover. This file preserves chronology only.

# Claude continuation: SpreadBoard stability and unnecessary work

Owner request: continue the 24/7 stability goal, remove Ourbit, keep real positive
spread or funding opportunities, and investigate whether duplicate/irrelevant
tokens overload memory. Owner reports UA CryptoInvest tracks 2,534 futures and
4,370 spot tokens. Finish from the measured state below; do not restart the audit.

## Start here

- Worktree: `/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-funding-current-truth`
- Branch: `codex/alerts-one-shot-rail-flood-20260826`
- Full chronological ledger: `docs/operations/2026-09-05-stability-review.md`.
- Original brief: `/Users/sviatoslav/.codex/attachments/eea5dd03-b4f1-4e1e-8902-966a2dfdde5c/pasted-text-1.txt`.
- Original plan: `/Users/sviatoslav/.claude/plans/logical-churning-hare.md`.
  Its claim that quote staleness was ruled out was disproved; use the newer ledger.
- SSH: `ssh -i ~/.ssh/spreadboard_digitalocean root@178.128.126.204`.
- Server source `/opt/spreadboard/app`, data `/opt/spreadboard/runtime`.
  Inside containers use `/app/.venv/bin/python`, not the system interpreter.
- Read root `REMINDERS.md`, vault `spreadarb-CURRENT-STATE.md`, project rules and
  the final checkpoint below before any mutation. Refresh all volatile evidence.

## Owner constraints

No trading, orders, borrowing, repayment, transfers, conversions or withdrawals.
Never print secrets, complete environment, unfiltered process argv or sensitive
logs. No Telegram sends. Pushover remains OFF. No droplet spend, cgroup increases,
subscription increases above 160, or weakening the 95% accuracy gate.

The Codex automation `finish-spreadboard-stability-acceptance` is PAUSED by the
owner. **Do not reenable it.** The goal control itself reported paused at 15:31;
that is separate from the two already-running finite read-only measurement jobs.
Do not create another recurring task as a workaround.

Preserve both `api_discovery_worker.py` AND `snapshot_finalize_worker.py`.
Never force a collector restart. The deploy helper only checks discovery at its
start, not the finalizer or the end of its build: manually check both again before
container recreation. Do not reproduce the earlier destroyed-scan mistake.

Before every deploy, require direct exit 0 and the summary from:

```sh
uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests/ -q
uv run --frozen --with ruff python scripts/ruff_ratchet.py
```

Ruff baseline is 517, never increase it to pass. New behavior needs a failing
call-site regression verified against a mutant. No `tail` pipelines hiding exits.
Normal deploy is `./scripts/deploy_production.sh app collector` in this worktree.
Verify digest parity for both running containers, including baked identity data.

## Cardinality audit: the new user hypothesis

Read-only production audit at 15:38 UTC, SHA-verified complete structural index:

| Object counted | Count |
| --- | ---: |
| Futures token labels in chart catalogue | 2,171 |
| Spot token labels in chart catalogue | 3,965 |
| Union of those labels | 5,363 |
| Exchange/type/symbol/chain/contract listings | 22,411 |
| Duplicate exact listing keys | 0 |
| Structural directed routes | 142,711 |
| Tokens represented by those routes | 1,232 |
| Distinct route leg keys | 13,692 |
| Duplicate routes using real `catalog_pairs.route_identity` | 0 |
| Ourbit legs in the complete index | 0 |

Our figures are **lower than the owner's competitor counts when comparing token
labels**, not 142k versus 2.5k tokens. Labels are not a claim of independently
verified asset identity. Multiple contracts, quotes, venues and long/short
directions are distinct instruments/routes, not automatically duplicates.
The catalogue snapshot was generated at 13:51 UTC; these are fresh runtime reads
of that production catalogue, not fresh venue-by-venue listing revalidation.
The anonymous `https://uacryptoinvest.com/arbitrage` read exposed the lane shell
and an error, not counters. Competitor counters remain owner-provided.

The memory concern is still valid. The verified index is **347,633,474 bytes**;
the chart catalogue is only **3,324,045 bytes**. Rich route dictionaries repeat
long/short metadata, URLs, guards, funding fields and route-key text. BTC alone
has 727 directed routes. Streaming fixed the whole decode-buffer overlap and
shares field names, but still creates all rich row dictionaries in memory.
The index worker also holds old/new generations during a merge. It already drops
the old generation before serialization; do not propose that as an unimplemented fix.

The live-book table held 25,883 rows with zero duplicate venue/type/symbol keys;
23,669 were within 90 seconds. 4,591 current books mapped to spot-only catalogue
tokens with no futures listing. Their combined bids/asks JSON was only 156,539
bytes, so eliminating them is not demonstrated to recover gigabytes. Full-venue
ticker APIs also return these symbols in a single request: filtering persistence
does not necessarily reduce request or response cost. Retain chart, watchlist,
position, exact-token and verified DEX needs when designing a demand policy.

Evidence and reproducible bounded probe:
`output/stability-20260905/universe-cardinality.jsonl` and
`probe_universe_cardinality.py`. Probe streamed the index in 11.90s with
214,220 KiB peak RSS; it did not load a second rich index or modify market data.

## Correctness shipped before the final checkpoint

- Ourbit removal `b826ae9`: before discovery, native sweeps, current funding,
  catalogue restore, index construction/retention, ranking, pagination and funding
  short-leg collapse. Reject old ranked pages containing Ourbit so they rebuild.
  Existing adapters and account/history records are intentionally retained.
- `edfe9d6`: a persisted monotonic OKX rate timestamp survived the host reboot
  and made the lock holder sleep for 32 days. A backwards clock now waits one
  unchanged provider interval. Real HTTP rate-spacing regressions pass.
- `ec5e770`: cold funding navigation now repairs a rejected/incomplete catalogue
  in its existing isolated heavy-worker slot before attempting publication.
- Earlier stability work is in the ledger: compact Hyperliquid builder artifact,
  exact builder aliases/oracle propagation, current BBO consistency, quote-lane
  refresh timing, bounded retention cleanup, restart-cap persistence/locking,
  backup repair, and streaming index reads (`ijson==3.5.1`). Do not undo them.

The 14:48 runtime/images both matched `ec5e770`, digest `118c242fd853ac1f`.
Its standalone navigation entrypoint was atomically replaced in both running
containers after building and checking matching future images. This did not
patch a resident module or kill discovery. Both containers began at 14:32:36 UTC.

Funding verification succeeded: 171 sampled routes from 329 matching tokens /
6,450 routes all had positive current funding and independently recomputed exact
leg rate/interval math, with zero errors and zero Ourbit. All 12 persisted views
(3 route kinds x Now/1d/7d/30d) were nonempty and Ourbit-free. This validates the
sample and publication, not global completeness or entry readiness.

At 15:46 the real API/HTML renderer replay over 15 selected tokens passed:
581 rendered futures routes and 1,623 in each mixed lane; 183 excluded context
rows per lane were hidden by the real renderer. Four current Hyperliquid OPENAI
routes were visible; the Gate Spot mismatch remained excluded against oracle
1,494.8. Counts naturally change with quotes. Raw API rows can contain context;
do not mistake every raw row for a displayed opportunity.

## Two latest fixes and their validation

`a995b91`: `_funding_reach_bound` omitted the zero funding spot leg. A token with
one paying futures venue got bound zero and could be missed by the 500-token
shortlist. Add zero only when a spot market exists; cover positive and negative
payers, no invented spot hedge for futures-only tokens. Four mutants caught.

`aee0953`: `funding_catalog.page` converted an empty live cache to `None` and
then reused persisted carry. When every live rate expired, old funding reappeared
as Now. Preserve `{}` so rankings clear and exact-token detail shows unknown
current funding. A regression calls the server funding expansion, uses the real
rate-file reader, advances time without changing mtime, and kills the old-fallback
mutant. Structural lookup and exact historical funding stay available.

Combined source digest **`3ed1500fd5c9b96a`**. Full suite **2,435 passed in 90.77s,
exit 0**, Ruff **517 unchanged**; total **128 killed mutants**. An initial run
failed solely on a new test's PIE807 lambda; corrected before the final full run.
The HTTP fixture emitted a benign connection-reset traceback during the green
suite. Eight test-generated tracked data files were restored from HEAD afterward.
See the final checkpoint for whether this combined source has shipped.

## Coverage is still incomplete: do not claim nothing is missed

The operator-approved `SPREADBOARD_FUNDING_CATALOG_TOKENS=500` remains unchanged.
A prior 2,500-token expansion caused load 6.55 on four cores and ~11s `/free`.
An audit found 450 outside-cache tokens with a positive optimistic funding bound
and a spot listing. Fresh actual pair construction confirmed positive eligible
routes for LCAP, READY, MEW, SYN and XRP; these are research candidates, not trades.
The spot-zero correction repairs a real selection error but does not prove
complete coverage outside the 500-token budget. Unsupported inverse funding legs
can also inflate the optimistic bound; actual route eligibility still excludes them.
The direct exact funding-key/catalogue join found 1,219 unmatched keys out of
10,313 live legs. Aliases, builder namespaces and unsupported instruments need
classification before treating those as missing tokens or dropping them.

Prioritize a measured design that keeps one compact market/leg catalogue and
evaluates funding economics cheaply across all eligible tokens, materializing
rich route details only for ranked pages or exact requests. Preserve exact
settlements, direction flips, cold start, transient partial-book retention,
identity/oracle gates, all remaining venues and on-demand token/chart coverage.
Do not just lower a token cap or remove negative routes from structural discovery:
both can conceal the next positive spread or a positive funding hedge.

Measure Python allocations and worker old/new-generation overlap on captured
public data outside production before a broad schema rewrite. Serialized byte
size is not RSS. Validate real route identity sets before/after, provider routes,
positive-spread OR positive-current-funding display, adverse funding expiry,
history completeness, full tests/mutants, source parity and a clean deployment-free
count/CPU/memory window. Quantify savings; do not report predicted savings as delivered.

## Stability acceptance and finite evidence jobs

The recovered 04:06–06:06 candidate FAILED: 42 health samples / 6,931.73 seconds,
priced routes 97,238–156,013, maximum deviation 34.8416%; health/HTTP and OOM were
green. Source books were current at a later dip, so resident timing is a lead,
not proven causation. Keep this failed evidence in the record.

The replacement candidate started **14:55 UTC**, no planned deployments during
its first hour. At 15:39: 22 health samples / 2,536.20 seconds, priced
136,856–138,424 (max deviation 0.7742%), all sampled HTTP 200, healthy containers,
OOM 0. Too short for acceptance at that checkpoint. App/collector mean CPU was
0.722/2.093 cores, anon peaks 2,511.6/2,832.3 MiB; collector cgroup still touched
its 4 GiB cap, index HWM 1,835.5 MiB and websocket HWM 1,245.4 MiB. Small bounded
read-only probe processes are visible in the evidence; do not attribute them to
the resident application or pretend this isolates Ourbit's causal memory saving.

Finite read-only jobs (NOT recurring Codex automations):

- `spreadboard-stability-ourbit-20260905.service`, two hours from 14:55,
  output `/opt/spreadboard/runtime/stability/20260905-ourbit/samples.jsonl`.
- `spreadboard-stability-ourbit-books-20260905.service`, two hours from ~14:57,
  output `public-book-ages.jsonl` in the same directory; public SQLite aggregates.

Summarizer: `output/stability-20260905/summarize_candidate.py`.
The count gate needs >=30 samples spanning >=3,600 seconds within +/-10%.
Check boot ID, container ID **and started_at_unix**: same-ID restart resets counters.
Preserve evidence before a deploy; split any continued sampler at recreation.

Backup gate PASSED: repaired manual success at 01:00:38, scheduled success
06:20:32–07:13:59, and persistent-timer boot catch-up 13:50:18–14:26:46.
Controlled watchdog recovery and persisted restart cap tests already passed;
do not repeat the disruptive drill without a new reason.

Safe lower caps (app 3072, collector 3584, accounting 512 MiB) remain UNPROVEN.
Do not apply while relying on a quiet instant rather than representative peaks.
The final clean 48-hour availability/health/OOM acceptance remains OPEN and has
not been established by this work. Keep the goal unfinished until its evidence
actually passes; user explicitly wants Claude to continue.

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
