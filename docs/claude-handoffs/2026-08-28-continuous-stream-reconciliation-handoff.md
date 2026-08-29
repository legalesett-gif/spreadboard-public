# Claude continuation prompt — SpreadBoard continuous market data and reconciliation

Copy everything between `BEGIN PROMPT` and `END PROMPT` into Claude. Do not
paste credentials, Keychain values, production environment files, database
contents, or private exchange/account responses into the chat.

---

## BEGIN PROMPT

You are continuing a mature production project called SpreadBoard / SpreadArb.
Do not redesign or rewrite working components before you understand the current
system. Your immediate job is to audit and improve continuous market-data
coverage and independent route reconciliation without weakening financial-data
integrity, breaking the website or Telegram bot, or changing live trading state.

### 1. Repository and environment

- Project root: `/Users/sviatoslav/Desktop/Spread Arbitrage`
- Active SpreadBoard worktree:
  `/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-funding-current-truth`
- Active branch: `codex/alerts-one-shot-rail-flood-20260826`
- Local and remote branch head at handoff: `025cdac`
- Default release branch also pointed to `025cdac` at handoff:
  `origin/codex/public-release`
- Production server: `root@178.128.126.204`
- SSH key location: `~/.ssh/spreadboard_digitalocean`
- Production site: `https://spreadarbitrage.ink`
- Production app container: `app-app-1`
- Production collector container: `app-collector-1`
- Production persisted application root: `/opt/spreadboard/app`
- Production shared runtime is mounted at `/app/runtime` in the containers.

Never print a secret. Required tokens and API keys are already stored in macOS
Keychain and/or root-owned production environment files. Inspect only presence,
permissions and redacted metadata. Do not copy values into chat, logs, git,
test fixtures or screenshots.

### 2. Mandatory reading before any edit or deployment

Read these files in this order and read each selected instruction file fully:

1. `/Users/sviatoslav/Desktop/Spread Arbitrage/AGENTS.md`
2. `/Users/sviatoslav/Desktop/Spread Arbitrage/REMINDERS.md`
3. `/Users/sviatoslav/vault/Knowledge/Memories/spreadarb-CURRENT-STATE.md`
4. `/Users/sviatoslav/vault/Knowledge/Memories/spreadarb-INDEX.md`
5. `/Users/sviatoslav/vault/Knowledge/Memories/spreadarb-margin-structure.md`
6. `/Users/sviatoslav/vault/Knowledge/Memories/project-spread-arbitrage-bot.md`
7. `/Users/sviatoslav/vault/Knowledge/Memories/spreadarb-spreadboard-continuous-capture-reconciliation-2026-08-28.md`
8. This handoff document.
9. The active worktree's `graphify-out/GRAPH_REPORT.md`.
10. `docs/uacryptoinvest-reference-reconciliation.md` and the latest relevant
    files under `docs/claude-handoffs/`.

Then inspect git status and history. Preserve all existing user/runtime files.
At handoff the working tree intentionally contained:

- modified `data/token_rankings.json`
- untracked `data/chart_warm_demand.json`
- untracked `data/funding_history_demand.json`
- untracked `data/historical_leg_cache/`
- an untracked historical-spread cache file
- many untracked `output/playwright/...` audit directories

Do not delete, reset, stage, overwrite or clean these. Do not use `git reset
--hard`, `git checkout --`, `git clean`, broad `rm`, or destructive database
commands.

### 3. Product decisions that are not open for reinterpretation

- Customer spread families are:
  - Futures–Futures
  - Futures–Spot in both economic directions
  - OKX DEX–Futures / Futures–OKX DEX
- Standalone Spot–Spot and Spot–DEX products are deliberately retired. Spot
  markets and books remain necessary for Futures–Spot, charts, portfolio marks
  and token-price alerts.
- In the customer taxonomy, **DEX means only OKX DEX**. Aster, Hyperliquid,
  Jupiter and similar venues remain in the ordinary exchange/perpetual area;
  do not put them in the OKX DEX product family.
- There is one customer opportunity list. Do not recreate “Research only” and
  “Verified only” tabs. Preserve inline evidence labels:
  - `Matched $500 VWAP` for current, exact, depth-proven economics.
  - `Indicative spread · DD: ...` when the lead is real but execution evidence
    is incomplete.
  - Continue excluding hard-invalid market or identity combinations.
- UACryptoInvest is an external discovery/regression oracle, never a price,
  identity or executability authority. Never copy its spread, funding, token
  alias or route simply to make parity pass.
- Funding `Now` is independent live current-rate information. `24h`, `7d` and
  `30d` are complete exact official settlement aggregates only. Never fill a
  missing historical window with the current rate, sampled history, a partial
  sum or synthetic zero. Incomplete or overdue windows stay blank/`-`.
- A route is not execution-ready merely because it appears. Preserve exact
  token identity, symbol/quote, contract multiplier, chain/contract for OKX
  DEX, matched-size depth, freshness, fees, liquidity and public execution
  gates.
- No live trade, order, borrow, repay, transfer, conversion, withdrawal or
  payment is authorized by this prompt.
- Do not send any Telegram group/channel message. Telegram testing must be
  no-send/render-only unless the owner separately authorizes a specific send.
- Owner diagnostics go only to the configured encrypted, opted-in Pushover
  destination. Never send diagnostics into the customer group.
- ML remains shadow-only. Do not weaken data gates, mix method versions,
  fabricate labels or activate a model early. The current deterministic method
  is `deterministic_dual_opportunity_evidence_v5`.

### 4. Major completed work that must be preserved

The latest local release chain is:

- `60bd060` — continuous opportunity and coverage guards
- `e768ba3` — DEX coverage reads the live overlay
- `0183701` — DEX Funding navigation remains current
- `025cdac` — Funding navigation reports manifest counts

Earlier prerequisite release work includes unified route presentation, exact
Funding windows, current spread continuity, product-lane retirement and
Telegram persistence. Read the git history rather than reimplementing it.

Completed functionality to preserve:

1. `spreadboard/opportunity_journal.py`
   - A 30-day WAL SQLite journal records genuine `opened`, `new_peak` and
     `closed` transitions for spreads at or above 0.5%.
   - It is fed from the existing hot live-route lanes.
   - It never extends a provider timestamp or promotes stale data.
   - `/api/token` can expose a recent 30-minute trail labelled
     `historical_observation_only`.
   - At the acceptance checkpoint it held 38,046 events, 4,834 states and
     1,114 active opportunities.

2. `spreadboard/coverage_reconciliation.py`
   - Token-scoped authenticated read-only endpoint:
     `/api/internal/reconciliation/uacryptoinvest`
   - Secret is stored under macOS Keychain service
     `SPREADBOARD/reconciliation/token`, as a GitHub Actions secret and in the
     root-only production application environment. Never print it.
   - It compares exact directed token, venue and market type against
     SpreadBoard, records absence reasons and flags spread differences above
     0.5 percentage points for direct-book investigation.
   - It has a separate OKX DEX 56 identity and matched-$500 monitor.

3. `.github/workflows/coverage-reconciliation.yml` and
   `scripts/uacryptoinvest_reconciliation_probe.py`
   - Scheduled at 06:17 and 18:17 UTC plus manual dispatch.
   - Current design samples the public comparator; it is not the main market
     collector.

4. Completed-generation book coverage health
   - Below 85% on a completed generation is critical.
   - Two consecutive completed generations below 90% warn.
   - A mid-cycle dip must not be counted as a failed completed generation.

5. Funding navigation publication
   - Every required lane must be non-empty.
   - An empty candidate generation is rejected, the previous valid generation
     is retained and an owner transition alert is raised.

6. Production source verifier
   - `scripts/verify_production_source_sync.py`
   - It compares every Python module in `spreadboard/` on the local worktree,
     persisted host, app container and collector container.
   - This whole-package hash verifier is more authoritative than a revision
     marker alone.

7. Existing website/product work
   - Crypto checkout, subscription lifecycle, password recovery/Resend,
     Telegram query snapshots/webhook, Portfolio lifecycle accounting, edit and
     delete actions, price/funding/spread alerts, token pages, charts,
     affiliate terms/dashboard groundwork, backups, uptime/Pushover, the
     editorial-terminal visual redesign and mobile/desktop checks all have
     substantial existing implementations. Do not assume any item is absent
     and build a second copy. Read the current code, tests, production health
     and current-state documents first; fix only evidence-backed gaps.

### 5. Exact explanation of the 45-pair reconciliation

The first scheduled comparison did **not** inspect only 45 because SpreadBoard
has 45 pairs. It was intentionally a small independent canary with a nominal
maximum of 50:

- per product tab: first 15 ranked routes plus up to 10 deterministic random
  tail routes
- tabs: Futures–Futures and Spot–Futures
- nominal target: 25 + 25 = 50

The persisted production payload proves the actual split:

- Futures–Futures: 25 rows = 15 top + 10 tail; highest sampled source rank 31
- Spot–Futures: 20 rows = 15 top + 5 tail; the scraper found only 20 unique
  acceptable public rows in that tab at that observation
- total: 45

So 45 was the comparator sample size at that instant, not the first-party data
coverage, route-universe size or endpoint cap. The endpoint accepts up to 200
rows. The sample was chosen to keep the competitor check bounded, independent
and non-authoritative. The user has correctly challenged whether this is enough
for durable coverage assurance.

First real scheduled run:

- GitHub run `33185066041`
- `https://github.com/legalesett-gif/spreadboard-public/actions/runs/33185066041`
- 37/45 exact directed pairs matched = 82.2222%
- eight absences
- three spread differences above the 0.5-point investigation threshold
- release gate correctly failed; do not suppress or redefine the failure

Recorded absences:

- `SKR`: Mexc Futures → Gate Futures — `route_not_generated`
- `SAMSUNGSTOCK`: WhiteBIT Futures → Mexc Futures —
  `missing_long_catalog_market`
- `SKHYNIXSTOCK`: WhiteBIT Futures → Mexc Futures —
  `missing_long_catalog_market`
- `SHEINSTOCK`: Bitget Futures → Mexc Futures —
  `missing_long_catalog_market`
- `PIPPIN`: Mexc Spot → Bingx Futures — `route_not_generated`
- `XMR`: Mexc Spot → Hyperliquid Futures — `route_not_generated`; a USDC
  variant existed, so the current monitor needs quote/symbol-aware reasons
- `DCR`: Kucoin Spot → Ourbit Futures — `route_not_generated`
- `CHZ`: Ourbit Spot → WhiteBIT Futures — `missing_long_catalog_market`; Ourbit
  Spot is not currently a supported catalogue/quote adapter

Staggered comparator differences requiring simultaneous official-book replay:

- `FONE` Mexc Futures → Aster Futures: comparator 1.73%, SpreadBoard -0.7251%
- `CATE` Mexc Spot → Mexc Futures: comparator 0.76%, SpreadBoard 2.83575%
- `KII` Bybit Spot → Bingx Futures: comparator 0.53%, SpreadBoard -0.63425%

These are not proof that SpreadBoard is wrong because the observations were not
atomic and may use different size/depth methods. Recheck both official books at
the same timestamp and canonical $500 matched notional.

### 6. Fresh handoff evidence — verify again before relying on it

At the final read-only handoff check on 2026-08-28:

- Local branch/source head: `025cdac`.
- The production runtime revision marker unexpectedly read `8780309`, an older
  ancestor, in both containers.
- Despite that marker mismatch, the complete source verifier passed 63/63 on
  the persisted host, app and collector against local `025cdac`, with zero
  changed, missing or unexpected modules.
- The reconciliation endpoint was present and correctly returned HTTP 401 to
  an unauthenticated empty POST.
- Therefore do **not** assume either a source rollback or a healthy marker.
  Diagnose why `/app/runtime/deployed_revision.txt` is stale or overwritten,
  and prove the code actually loaded by each running process before changing or
  restarting anything.
- `app-app-1`: running/healthy at the earlier checkpoint, restart count 6,
  current `OOMKilled=false`. A later forensic check in section 6A proved that
  this was not stable: it OOM-restarted again and reached restart count 7.
- `app-collector-1`: running/healthy, restart count 2, Docker history reported
  `OOMKilled=true`.
- Public `/api/health`: HTTP success and `ok=true`.
- Current market age was about 0.40 minutes.
- WebSocket cache: 26,002 books, newest age about 6.3 seconds.
- Live route index: 124,549 rows; 21,088/23,003 catalogue markets covered,
  91.67%; 1,915 missing books.
- Live query universe: 124,641 routes, 84,703 currently priced CEX routes,
  refresh about 22 seconds.
- Funding navigation: complete, 12 views, 4,412 token groups and 115,673
  navigation routes, but a nested legacy `route_count` field still reported
  zero. Treat that contradiction as a health/reporting check, not permission to
  rebuild from a foreground request.
- Funding history: 10,469/10,469 source-classified, but currently complete
  rolling windows were only 2,422 24h, 2,397 7d and 2,162 30d legs; archive
  status `archive_catching_up`. Missing/overdue windows must remain blank.
- Research evidence: 60,910 observations, 40,217 labelled 24h outcomes across
  3,686 routes and 14 days; `ml_ready=false`, mode
  `deterministic_shadow_calibration_only`.
- Telegram health: query snapshot and webhook ready. Do not send a message.
- Crypto checkout health: configured/ready; traditional Stripe remained test
  mode and Whitepay remained onboarding-only. Do not change billing in this
  task.

All production facts above are volatile. Repeat the safe read-only checks.

### 6A. Urgent Pushover and production-incident forensics

This section was added after the owner reported receiving error notifications
through Pushover. The investigation was read-only: it did not send a test push,
post to Telegram, restart a container or alter production.

#### What the owner-only Pushover alerts were

The persisted operator state and production logs reconstruct two current alert
classes:

1. `SpreadBoard route reconciliation`
   - Opened at `2026-08-28T15:27:54Z`.
   - Current state is still **active**.
   - Exact message:
     `Exact-pair recall=82.2222%; drop=0.0pp; absences=8; investigations=3; failures=exact_pair_recall_below_95_pct.`
   - This is a genuine failed release/coverage gate, not a website outage and
     not a false positive. Do not clear it merely to stop the notification.

2. `SpreadBoard Funding navigation`
   - Fault transitions were logged at `15:48:53Z`, `16:36:14Z`, `17:31:45Z`,
     `17:49:46Z` and `19:25:53Z` on 28 August.
   - Exact fault message:
     `Generation failed; previous valid snapshot retained. timeout=False; exit=-9.`
   - Successful intervening generations reset the transition state, so each
     later fault was a new transition rather than one logger repeating the
     same active state. The state recovered at `2026-08-28T18:03:37Z` with all
     12 required lanes non-empty, and successful generations continued through
     `19:18:38Z`, when the navigation held 124,165 routes. The worker was
     OOM-killed again at `19:25:52Z`; the owner incident reopened one second
     later and was **active at the final 19:28 UTC check**. The last valid
     124,165-route generation remains ready and non-empty.
   - The safety behavior worked: failed candidate generations were rejected
     and the previous valid snapshot remained available. The repeated worker
     deaths themselves remain a real incident.

Pushover provider validation was performed without sending a notification. The
application token is configured, the opted-in owner recipient decrypts, the
provider returned HTTP 200 and reported one active device. The second
administrator has no enabled Pushover destination. Recent personal alert
history contained legitimate token-price/funding threshold triggers, not a
provider error, and the personal market-alert worker was running with no active
rule error in its latest cycle.

Pushover does not expose a recipient inbox/history API to this application, and
the current code does not retain provider receipts for operator messages.
Therefore production can prove the transitions it attempted, while the
owner's received notifications are the only receipt proof for those pushes.

#### Proven root cause: active memory exhaustion, not harmless warming

Linux kernel evidence maps the Funding `exit=-9` failures to memory-cgroup OOM
kills in the collector. The funding-navigation child itself uses roughly
1.92-1.96 GiB RSS while the collector has a 4 GiB cgroup and is concurrently
running quote, stream, history and publication work.

The HTTP application is also repeatedly exhausting its separate 3.5 GiB
cgroup:

- App OOM/restarts were recorded at approximately `16:21`, `16:48`, `17:16`,
  `17:54`, `18:19`, `18:49` and `19:20 UTC` on 28 August.
- At `19:17 UTC` it used about `3.293 GiB / 3.5 GiB` (94.09%).
- The kernel killed the app at `19:20:09 UTC`; Docker restarted it at
  `19:20:10 UTC`, increasing restart count to 7.
- The collector had restart count 2, `OOMKilled=true`, a 4 GiB peak and an
  explicit current-cgroup `oom_kill=1`.
- The 8 GiB host had only about 1.77 GiB available and its entire 2 GiB swap was
  consumed at the first forensic checkpoint.

This means the site can return HTTP 200 and look healthy between restarts while
still being in an active reliability incident. Do not report it as stable until
a soak crosses multiple publication/navigation cycles with no OOM, no restart
increase and bounded RSS.

There is a second live correctness/reliability defect: concurrent mutation of
the in-memory route dictionary repeatedly raised
`RuntimeError: dictionary changed size during iteration` in
`spreadboard/warm_query_projection.py`:

- line 195 in `refresh_route_kinds`
- line 339 in `opportunity_rows`
- callers included the materialized live-route thread, the shared artifact
  watcher and opportunity-journal update

These exceptions were observed repeatedly from `15:47` through `19:05 UTC`.
Use immutable snapshots or correct locking around every iteration/publication;
do not merely catch and suppress this error, because it can silently skip a
route refresh or opportunity-journal transition.

#### Operator-alert implementation defects to fix

`spreadboard/operator_alerts.py::notify_transition` currently writes the new
state **before** calling Pushover. If Pushover rejects the request or the
network fails, the next poll sees no transition and will not retry. The caller
also discards the returned `delivered` and `errors` fields. The state file keeps
only the latest state per key, not an append-only transition/delivery history.

Required correction:

1. Persist an append-only, secret-free operator incident/delivery ledger with
   incident id, key, opened/recovered time, severity, sanitized evidence,
   attempt count, provider HTTP class, delivered count and last error type.
2. Mark a transition delivered only after provider acceptance. Retry failures
   with bounded backoff and deduplication; never lose an incident because state
   was written first.
3. Keep fault state and delivery state separate. A delivery failure must not
   clear the underlying incident.
4. Add direct container/cgroup health: alert on OOM/restart-count increase,
   sustained RSS pressure and stale process start time. A process killed by OOM
   cannot alert through itself, so this check must run outside the affected
   cgroup.
5. Link the fault and recovery with one incident id. Suppress repeated healthy
   chatter, but do not hide a new OOM after a prior recovery.
6. Preserve owner-only delivery. Never route operational incidents to the
   subscriber Telegram forum.
7. Add mocked tests for provider rejection, retry, restart during an active
   incident, state corruption, recovery correlation and no duplicate send.
   Do not send a real push during ordinary CI or browser acceptance.

#### External free uptime monitor is also red and currently misleading

The separate private repository is:
`/Users/sviatoslav/Desktop/Spread Arbitrage/ops/spreadboard-uptime-monitor`
(`legalesett-gif/spreadboard-uptime-monitor`). It sends owner alerts to a
private Telegram destination, **not Pushover**, so it was not the source of the
Pushover notifications above.

Fresh evidence:

- Workflow is enabled and nominally scheduled at minutes 17 and 47.
- The latest 20 listed workflow runs were failures.
- Incident issue `#27` has remained open since 20 August. Later failed runs say
  `duplicate alert suppressed`, but the issue body is not updated with the new
  failure set. This hides incident evolution.
- At `19:24 UTC` the newest scheduled run was still the `12:14 UTC` run, so a
  free GitHub scheduled workflow is not a reliable 30-minute wall clock.
- The latest scheduled run failed on research outcome accounting/yield and the
  public status endpoint. A fresh local external probe was also red:
  - recent 24h yield 78.52%, terminal missing history 20.70%
  - recent 8h yield 36.55%, terminal missing history 50.20%
  - public `/api/status` returned `ok=false`
- Public `/api/health` simultaneously returned `ok=true`, market age about
  0.18 minutes and 26,012 WebSocket books. This is not proof the monitor is
  wrong; it proves that availability, data completeness, research-label
  quality and product degradation are currently collapsed into one outage bit.
- `/api/status` reported DEX quotes degraded with `row_count=0`, although the
  fast-quote health reported 32 current `DEX-FUTURES` tokens. The status builder
  still reads the legacy `canonical.dex_spot_source` even though standalone
  DEX-Spot was retired. This is a current status-contract bug.
- The uptime monitor still expects both `DEX-FUTURES` and retired `DEX-SPOT`.
  Update it to the agreed product taxonomy; never reintroduce DEX-Spot merely
  to make the monitor green.
- Funding source classification was 100%, but exact rolling-window health was
  still `archive_catching_up` (only about 26.53%/26.21%/23.42% complete for
  24h/7d/30d at the checkpoint). `/api/status` marks that degraded. Keep the
  blanks honest, but distinguish a bounded advancing backlog from a stalled or
  regressing service incident.

Required correction:

1. Fix `/api/status` to derive OKX DEX health from the current supported
   `DEX-FUTURES` provider/fast-quote evidence, not retired `dex_spot_source`.
2. Separate external gates into at least `availability`, `current-market-data`,
   `historical-completeness`, `research-pipeline-quality` and `release-quality`.
   Only availability incidents should claim the whole production site is down.
3. Update the open issue on every material failure-set change and recovery;
   retain deduplication only for an unchanged set.
4. Add a truly external, frequent watchdog for HTTP/process/container OOM. If
   retaining GitHub Actions as the free option, treat it as a slower independent
   audit, not the sole uptime timer. Do not purchase anything without approval.
5. Prove the notification path through a synthetic failure and correlated
   recovery after the implementation, without taking production down and
   without posting to the subscriber group.

#### Minimum incident exit gates

Do not call this incident fixed until all are true:

- zero app and collector restart-count increase across at least 24 hours and
  multiple complete Funding-navigation, catalogue, quote and history cycles
- no cgroup `oom`/`oom_kill` increase; host swap and available memory retain a
  measured safety margin
- app and collector RSS remain below documented budgets under page and worker
  load
- zero `dictionary changed size during iteration` exceptions during the soak
- Funding navigation remains complete for all 12 required lanes while failed
  candidates continue to retain the previous valid generation
- Pushover delivery failures are durably recorded and retried, and provider
  validation remains green
- external availability probe is green; historical/research/release gates are
  separately visible rather than hidden or relabelled
- the reconciliation alert remains open until the real exact-pair gate passes
  or every exclusion is owner-approved with first-party evidence

### 7. The continuous-stream question: frame it correctly

The user believes all data can be streamed continuously for free. Take that
belief seriously, but do not promise literal 100% without a venue-by-venue
proof.

The current design already has a hybrid:

- `scripts/websocket_book_worker.py` streams only a selected set, default
  `SPREADBOARD_WS_BOOKS=160`.
- `spreadboard/bulk_quotes.py` makes venue-wide public BBO/ticker passes for the
  long tail.
- `scripts/run_spreadboard_service.py::BulkQuoteLoop` publishes those passes.
- Current code comments say one bulk request per venue closes the previous
  20–25 minute gap in roughly tens of seconds.
- The WebSocket worker is currently paused during heavy historical evidence
  work because overlapping object graphs exceeded the collector's 4 GiB cgroup
  and killed the WebSocket child.

The scalable unit is a **market leg**, not every pair permutation. One fresh
bid/ask for `venue + market_type + symbol` can reprice many cross-venue routes.
Do not create one network subscription per route.

Many venues do provide free public market WebSockets, including all-market BBO
channels on some venues and per-symbol order-book channels on others. However,
“100% for free” still has hard boundaries:

- per-connection subscription/request/argument limits
- disconnects, gaps, sequence resets and provider maintenance
- venues that require one topic per symbol rather than one all-market stream
- public adapters that omit size, require authentication or refuse some
  instruments
- OKX DEX quote economics are request/response API results, not an equivalent
  all-token CEX order-book stream
- the server's CPU, memory, bandwidth and persistent-write budget
- the difference between top-of-book coverage and exact $500 depth/VWAP

The target should therefore be:

1. 100% of the **classified, supported, stream-capable market-leg universe** is
   subscribed through official public streams, sharded within published
   provider limits.
2. Every leg that cannot be streamed has an explicit machine-readable reason
   and the best official bulk/REST fallback.
3. Sequence-aware snapshot/delta recovery prevents silent corrupt books.
4. Time-weighted freshness and outage metrics show the real achieved coverage;
   never relabel transient or unsupported gaps as streamed.
5. The route engine is event-driven from leg changes through a precomputed
   leg-to-route adjacency index, so a five-minute opportunity is recorded even
   if nobody opens a page.
6. REST remains recovery/reconciliation, not the normal request-time source.
7. OKX DEX is separately quota-budgeted and truthfully described; calculate
   the monthly call cost for 73+ tokens and the required refresh interval before
   calling a plan free.

Use official venue documentation only for the feasibility matrix. Do not infer
capability merely because CCXT/CCXT Pro has a method name.

### 8. Immediate tasks, in order

#### Phase 0 — stabilize the active OOM incident and monitoring truth

Before expanding streams or running another heavy reconciliation/deployment:

1. Repeat the read-only evidence in section 6A and capture cgroup memory events,
   kernel OOM lines, container start/restart state, host memory/swap, worker RSS,
   the three operator incident states and current external-monitor failure set.
2. Stop overlapping heavyweight object graphs by design. Measure which objects
   are simultaneously resident in web and collector, then isolate or compact
   them; do not simply raise cgroup ceilings on an already fully committed 8
   GiB host.
3. Fix the live-universe concurrent dictionary mutation using immutable
   publication/snapshots or correct locking, with a stress regression test.
4. Fix Pushover transition delivery accounting/retry and the stale external
   status/monitor taxonomy described in section 6A.
5. Run focused tests, the full suite, source parity and a bounded local soak.
6. Deploy once, then require the full incident exit gates in section 6A. If a
   24-hour soak cannot fit inside the interactive task, leave the monitor
   running and report the incident as observing—not complete.

#### Phase A — read-only production and architecture audit

1. Run all mandatory reads and inspect the graph report.
2. Record `git status`, current branch, local/remote heads and the ancestry from
   `8780309` through `025cdac`.
3. Run `scripts/verify_production_source_sync.py` and inspect the running
   process/container startup command, image, environment revision metadata and
   runtime marker. Explain the marker mismatch before any deployment.
4. Capture fresh public `/api/health`, container health/restarts/OOM, current
   book coverage, route-family counts, WebSocket coverage, Funding navigation,
   exact-window coverage and reconciliation status.
5. Inventory every current venue and market type in `spreadboard.fast_quotes`,
   `spreadboard.bulk_quotes`, `scripts/websocket_book_worker.py`, native
   adapters and public discovery. Produce a matrix:
   - venue
   - Spot/Futures support
   - official all-market BBO WebSocket
   - official per-symbol BBO/order-book WebSocket
   - subscription/connection limits
   - snapshot/delta/sequence semantics
   - size/depth availability
   - authentication or entitlement requirement
   - current SpreadBoard path
   - current coverage and age
   - proposed path
   - explicit reason if not streamable
6. Separately audit OKX DEX 56. Verify the actual endpoint, quota, call cost,
   token/chain/contract coverage and monthly budget. Do not treat it as a CEX
   WebSocket or spend paid quota without explicit approval.

Do not edit or deploy during Phase A. Present the evidence and a bounded
implementation plan first in your work log, then continue if the changes are
within this prompt's safe scope.

#### Phase B — make reconciliation broad and durable

1. Explain the 25+20=45 sample precisely in code/tests/docs.
2. Keep the lightweight twice-daily top+tail canary, but add one of these after
   verifying the comparator's public pagination/scroll behavior and terms:
   - a bounded full accessible census, preferably daily; or
   - deterministic shards that cover every accessible exact row within a
     defined maximum period, with a durable coverage ledger.
3. Do not hammer the comparator or turn it into production market data. Cache
   public HTML only for the audit run, apply bounded rate limits and fail
   honestly when a tab is incomplete.
4. Extend reconciliation identity to include quote currency and resolved market
   symbols where available. A USDC route must not count as a USDT match.
5. Trace every absence through:
   `catalogue -> fresh book -> identity/alias -> pair generation -> evidence
   gate -> ranking -> API/page/Telegram rendering`.
6. Replace coarse `route_not_generated` with the deepest proven reason without
   fabricating an explanation.
7. Investigate the eight recorded absences and three spread deltas with fresh,
   simultaneous first-party evidence. Admit a route only if the identity and
   economics are correct. Preserve justified exclusions.
8. Keep raw recall visible. You may add an adjudicated/eligible recall metric,
   but do not silently remove hard cases from the denominator or weaken the
   existing 95% gate without explicit owner approval.

#### Phase C — continuous first-party market-leg ingestion

1. Design a dedicated lightweight market-stream service independent from the
   heavy historical/funding analytics process. A history build must not pause
   or kill live CEX streams.
2. Prefer official native all-market BBO channels where available. For venues
   with per-symbol topics, shard all supported legs across supervised sockets
   within official limits.
3. Implement heartbeat, reconnect with bounded jitter, subscription
   acknowledgement, snapshot/delta application, sequence-gap detection,
   resnapshot and stale-leg expiry per venue.
4. Keep contract sizes and base quantities correct. Top-of-book without size is
   indicative and must not become matched-$500 evidence.
5. Precompute `market_leg -> affected_route_keys`. Reprice only affected routes
   on each event and atomically publish compact live overlays. Do not recompute
   the entire cross-product for every tick.
6. Keep a low-frequency official bulk/REST sweep as completeness and recovery
   verification. It should detect silently missing subscriptions and restore
   gaps, not compete with streams or overwrite a newer book.
7. Persist only what the product needs. Avoid writing every high-frequency
   delta individually to SQLite. Persist compact latest-book snapshots,
   coverage/sequence watermarks and opportunity transitions; measure WAL/write
   pressure before choosing storage.
8. Preserve the 30-day opportunity journal. Add source/venue coverage context
   so a missing event can be distinguished from “no opportunity.”
9. Make stream health publish completed, per-venue coverage rather than one
   aggregate that can hide a dead venue.
10. Do not raise the existing 160-subscription setting inside the memory-heavy
    collector as a shortcut. The previous single-process overlap caused OOM.
    First isolate and measure the lightweight stream service.

#### Phase D — verification and controlled release

Add regression/integration tests for at least:

- the exact 45-row sample breakdown and incomplete-tail behavior
- full-census/shard coverage and deduplication
- quote/symbol-aware reconciliation
- all eight absence reason paths
- simultaneous direct-book spread comparison
- venue subscription sharding and limits
- reconnect, snapshot reset and sequence-gap recovery
- stale expiry without timestamp extension
- one leg update repricing every affected route exactly once
- duplicate economic identity suppression
- five-minute synthetic opportunity captured without any page request
- REST recovery never overwriting a newer WebSocket book
- completed-cycle coverage and per-venue health
- OKX DEX remaining separate and quota-bounded
- Futures–Futures, both Futures–Spot directions and OKX DEX–Futures remaining
  populated
- retired Spot–Spot and Spot–DEX remaining absent
- website, charts, token pages, price alerts and Telegram no-send render paths
- Funding `Now` versus exact `24h/7d/30d` semantics

Then:

1. Run focused tests.
2. Run the complete suite. The previous accepted baseline was 1,930 passing
   tests with one existing unknown-`asyncio_mode` configuration warning.
3. Run Ruff on changed files and the repository ratchet; add no new findings.
4. Run a long enough local soak to cross reconnect, rotation and publication
   boundaries. Record CPU, RSS, file growth, socket counts, messages/sec,
   per-venue freshness and event-loop lag.
5. Do an adversarial review for silent stale data, sequence corruption,
   cross-ticker identity, quote mismatch, memory growth and false release-gate
   success.
6. Commit small coherent changes and push the intended branch.
7. Deploy once in a controlled maintenance operation only after all gates pass.
   Back up affected runtime/database files, verify DB integrity, do not restart
   repeatedly and do not touch live trading services.
8. Verify whole-package source parity, the loaded revision (not only the marker),
   service health, no new restarts/OOM, desktop and mobile pages, APIs, charts,
   token pages, Funding expansion, alerts, Portfolio and Telegram no-send
   commands.
9. Remove disposable audit accounts/sessions and local temporary credentials.
10. Update `REMINDERS.md`, `spreadarb-CURRENT-STATE.md`, the daily vault note and
    a durable memory note with evidence and honest residuals.

### 9. Acceptance criteria

Do not call the task complete unless all of these are true, or explicitly list
the provider-bound exceptions with evidence:

- Every catalogue market leg is classified as streamed, official-bulk fallback,
  auth/entitlement blocked, unsupported, delisted or identity-invalid.
- Every supported stream-capable leg is subscribed and appears in per-venue
  health; no silent “unknown” bucket.
- Achieved time-weighted freshness is reported honestly. A momentary screenshot
  is not an uptime proof.
- A controlled five-minute opportunity in any supported customer family is
  captured and journaled without a page request.
- No stream gap can silently preserve a stale quote as current.
- No older REST response overwrites a newer WebSocket quote.
- The independent comparator covers its complete accessible census or a proven
  bounded shard cycle, while remaining only a regression oracle.
- Each comparator absence has a precise stage reason.
- The existing 95% recall gate is not weakened; the current eight gaps are
  either fixed correctly or retained with evidence.
- Funding historical windows retain exact-settlement semantics.
- OKX DEX remains chain/contract exact, matched-$500 when labelled verified and
  within a proven free quota, or the cost/coverage shortfall is clearly stated.
- All customer route families remain populated through refresh and restart
  boundaries; retired families stay retired.
- Full tests and code-quality gates pass.
- Production source and loaded-process parity pass.
- Desktop/mobile/browser and Telegram no-send checks pass.
- No trade, payment, withdrawal or Telegram group message occurred.

### 10. Stop conditions and reporting style

Do one complete bounded audit/fix/verify cycle. Do not enter an infinite loop of
repeated restarts, warming or cosmetic redesign. If a provider limit, public
API gap, quota, ToS restriction or hardware ceiling prevents literal 100%, stop
after proving it and report:

- exact affected venue/legs
- official evidence
- measured achieved coverage
- safe fallback
- free option, if any
- smallest paid option and expected benefit, but do not purchase anything
  without owner approval

Lead the final report with the outcome, then list:

1. what was read and verified
2. root causes
3. exact changes and commits
4. tests and soak evidence
5. production/browser/Telegram evidence
6. achieved first-party coverage by venue and family
7. comparator census/recall and each remaining absence
8. OKX DEX quota/cost verdict
9. all remaining blockers and owner actions

Never claim “everything is fixed” merely because a unit test, one page or one
warm snapshot passes.

## END PROMPT
