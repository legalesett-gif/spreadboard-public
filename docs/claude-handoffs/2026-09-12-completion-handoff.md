# SpreadBoard — Claude completion handoff

Prepared 12 September 2026; fresh operational check at 19:04 UTC.
User requested Claude to complete the outstanding website work and then return it to Codex for independent review. This supersedes older notes saying no Claude continuation. This document conveys the relevant working context and evidence; it does not claim to reproduce the entire chat transcript.

## 1. Your objective and authority

Read this document completely, inspect current instructions/source/runtime, then finish the outstanding engineering items below. Implement, test and safely deploy scoped fixes. Keep a finite acceptance checklist and produce a review packet for Codex. Do not stop at a plan, and do not restart completed investigations without contradictory evidence.

This is a website/data/reliability handoff. It gives no new authority to place exchange orders, move money, borrow, repay, convert, withdraw, change leverage/margin, activate generic live execution, send group messages, publish marketing or pay affiliates. Existing trading activity in other tasks must be preserved. Routine safe engineering changes and scoped releases are intended. Remain within free provider tiers; identify any unavoidable paid decision before spending.

## 2. Mandatory orientation and working location

1. Read applicable AGENTS.md, CLAUDE.md and relevant installed skills. Apply skill routing and project memory conventions. Avoid recursively delegating the whole task back to Codex; the user intends Claude to execute and Codex to review afterward.
2. Read `/Users/sviatoslav/Desktop/Spread Arbitrage/REMINDERS.md`.
3. Read `/Users/sviatoslav/vault/Knowledge/Memories/spreadarb-CURRENT-STATE.md`, then `spreadarb-INDEX.md` in that directory. These are dated snapshots, not fresh account/runtime proof.
4. Read `docs/operations/2026-09-11-funding-completion.md` in the worktree below. Its earlier source IDs and no-Claude wording are superseded by this handoff; its detailed acceptance gates and evidence remain useful.
5. Use linked chronological notes only when investigating their component. In particular: `docs/operations/2026-09-11-funding-release-journal.md`, `docs/operations/2026-09-05-stability-review.md`, and `docs/claude-handoffs/2026-09-05-stability-cardinality-continuation.md`. Do not treat old candidate/observer IDs as current.

Implementation worktree:
`/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-exchanges-trial`

Branch: `codex/exchanges-funding-trial-20260910`
Git remote: `https://github.com/legalesett-gif/spreadboard-public.git`
Latest source commit at handoff: `4959b5a0b16e82a3fd4f91e8c73eea82f39bc557`, pushed.
Worktree was clean before adding this handoff. Preserve later changes and inspect status first.

The root project and other worktrees contain unrelated trading and development work. Do not reset/clean/redeploy them. The older collector-retention worktree contains original patch `ec0c5ad`, already cherry-picked as `4959b5a`; do not apply it twice. For architecture tracing, run the required `graphify-auto ensure .` and read its report before broad source searching.

## 3. Production map and latest evidence

- Host: `root@178.128.126.204`; SSH key path `/Users/sviatoslav/.ssh/spreadboard_digitalocean`.
- Host source: `/opt/spreadboard/app`; runtime: `/opt/spreadboard/runtime`.
- App: `app-app-1`; collector: `app-collector-1`; private accounting: `app-accounting-worker-1`.
- Site: `https://spreadarbitrage.ink`; use `/api/health` there. Do not assume `.com` routes identically; previous `.com` health request did not reach the same endpoint.
- Host marker: `/opt/spreadboard/app/.deployed_revision` = `4959b5a` at 19:04 UTC.
- Last app release: 17:08:42 UTC September 12. Collector start: September 11 05:50:40 UTC; accounting start: September 9 23:12:33 UTC. All restart counts 0, OOMKilled false. These counters alone are insufficient for a long uptime claim.
- App source digest verified during release: `c76535431bd865fb`. Collector intentionally retains the prior release because this was app-only. Verify each service against its intended version, not one blanket marker.
- `/api/health` 200; Telegram query snapshot and webhook ready; position-alert loop running at 15 seconds; private accounting configured, read-only and running. This proves readiness signals, not actual push delivery.
- Last crypto readiness check: checkout/watcher ready, prepaid subscriptions, USDC and USDT supported. Stripe and Whitepay unavailable; these are optional, not blockers to crypto billing.
- At 19:04 UTC the normal backup service was `activating` with no completion timestamp. Its intermediate Result=success does NOT establish a completed backup. Earlier 12:18 timer invocation failed at 15:09:56 on Google Drive `rateLimitExceeded` and restic could not save the snapshot.
- Existing finite observer: PID 2617599, `scripts/stability_soak.py --output-dir /opt/spreadboard/runtime/stability/20260911-funding-acceptance --hours 49`; elapsed about 34h56m at 19:04 UTC. Refresh PID/service identity before using.

Funding exact-window health at 19:04 UTC:

| Metric | 24h | 7d | 30d |
|---|---:|---:|---:|
| Current complete legs | 8,295 | 7,950 | 6,827 |
| Current completeness | 88.74% | 85.04% | 73.03% |
| Stored complete legs | 9,061 | 8,799 | 7,490 |
| Overdue legs | 766 | 849 | 663 |

All 9,348 catalogue legs were source-checked/classified; deep backlog 0 and retryable errors 0. Earlier at ~17:22 UTC overdue legs had reached zero and completeness was 97.03/94.21/80.20%. This is an observed recurring expiry/recovery problem, not evidence that all missing history is unavailable upstream. Investigate the ordinary rollover queue and measure recovery; do not force numbers to reach 100%.

## 4. What the last release did, and what its proof did not cover

`4959b5a` changed `spreadboard/server.py` and four regression files. Funding uses one `/funding` endpoint with rank query filters `now`, `1d`, `7d`, `30d`; labels are Live 24h estimate, Settled 24h, Settled 7d, Settled 30d. Historical cells in compact pair rows explicitly say settled. If a current rolling window is incomplete, the renderer can show an exact last-complete total with an as-of stamp; otherwise a dash. It never intentionally substitutes current-rate projections for history.

Tests: full production branch 2,927 passed; focused 532 passed; Ruff ratchet no new findings (500 existing). Source digest passed. Authenticated Funding opened and desktop/mobile global overflow was zero. Disposable audit user and sessions were removed, database quick_check ok and FK violations 0.

Important limitations requiring follow-up:

- The desktop screenshot visibly truncates some funding values with ellipses. Global document overflow=0 does not prove values are readable. Fix individual cell width/wrapping and inspect expanded rows at 390px, 1440px and a normal laptop width.
- Browser text checks for `24h settled` etc. returned false although accessibility snapshots contained them, potentially due to uppercase CSS/line breaks. Resolve through actual DOM cells, computed style and screenshots; do not count those assertions as passed.
- Full post-stream settled-cell correctness and all four filter interactions were not exhaustively tested. Check initial HTML AND subsequent browser updates. Current live updates are embedded in `server.py` around `.funding-window.current strong`.
- The existing four filters still navigate with query links. Do not claim this is a no-reload single-page interface. Assess the user's request: keep one consistent Funding surface; if smooth in-place filtering is needed, implement progressive enhancement over the same API, retaining URL/back/forward, filters and fallback. Avoid loading four full catalogues into the browser.
- The new stale fallback is display-only. Audit its economic identity, window endpoints, maximum age and truthfulness. A stale result must remain clearly dated and must not silently enter current rolling-window rankings or sums. Use a full date for older values; HH:MM alone is ambiguous across days. If those conditions cannot be proved, show unavailable.

Last screenshots are in `.playwright-cli/page-2026-09-12T17-14-41-740Z.png` (desktop) and `...17-14-50-840Z.png` (mobile). Treat them as diagnostic evidence, not a clean acceptance certificate.

## 5. Priority work packages with completion criteria

### A. Finish Funding semantics, readability and rollover freshness

Start with `server.py`, `venue_funding_history.py`, `funding_history_demand.py`, `funding_navigation.py`, `funding_radar.py`, `complete_funding_catalog_worker.py`, and the incremental settlement store located through imports.

Trace official event ingestion → retained exact events → per-leg completeness → paired net total → ranking → API/export → initial HTML → streamed DOM. Check both legs and their rate signs/cadences. A positive short receives positive funding; a long pays it. Apply contract multipliers correctly. Missing funding is not zero. Rate/cadence changes, new listings, delistings, pagination and duplicate event boundaries need coverage.

Now remains a labelled live 24-hour estimate. 24h/7d/30d are trailing settled sums for the exact route direction, not rate times days. Negative historical totals remain visible on an otherwise positive current opportunity. Ranking must use the selected metric across the whole eligible catalogue before pagination. Preserve exchange filters, token search, farm type, counts, page-specific exports and economic-identity deduplication.

Use direct official endpoint samples and retained ledger arithmetic. Include formerly problematic Gate GUA/SIREN, Aster SIREN, ONG, Kraken S, Bybit ICX, WhiteBIT and BitMart as a regression map; select active exact symbols fresh. Source-check completion is distinct from window completeness. Prefer incremental overlap and bounded priority jobs; prove ordinary rollover recovery without relying on manually running a global rebuild.

Done when all four filter paths preserve the semantic contract after live updates, values are fully readable, stale values are honest, and measured rollover recovery has an explicit acceptable bound. Document genuine unavailable/new-market history separately from retryable lag. Do not weaken strict completeness to make blanks disappear.

### B. Repair and prove free encrypted offsite backups

Inspect `scripts/backup_spreadboard.py`, its tests, effective systemd unit/timer and restic/rclone configuration without printing secret values. Observe the currently running invocation first; do not duplicate it. The preceding failure was Google Drive project request quota, not proof of storage exhaustion.

Check request pacing, retry/backoff, concurrency, repository listing cost, bounded timeouts, lock handling, retention and shared OAuth-client quota. Reuse free storage and preserve encryption. If a private Google API client is needed, prepare exact setup instructions and identify the minimal owner action; do not buy capacity or silently replace destinations.

Prove two subsequent ordinary timer invocations finish with exit 0, identify actual snapshot IDs, and restore to an isolated directory. Verify restored critical SQLite databases with integrity/FK checks and expected tables/data. A running process, successful catch-up run, or restic log line before completion is insufficient. Never restore over production to test. Preserve prior valid snapshots.

### C. Finish bounded reliability and memory acceptance

Use existing append-only samples and `scripts/summarize_stability_soak.py`. The app release at 17:08:42 UTC September 12 splits the observation generation; do not certify 48 clean hours across it. Preserve previous evidence, identify the last final release/cap boundary, then complete one appropriate finite observation. No duplicate observers or infinite release/restart loop.

Existing acceptance: 30 priced-route samples spanning one hour, within ±10% on comparable catalogue generations; 48h endpoint checks every 5 minutes; no unhealthy period over 90 seconds; no cgroup/kernel OOM; actual latency distributions, not just HTTP 200. Explain new-listing changes rather than forcing route counts constant.

Current recorded limits: web 3,584 MiB, collector 4,096 MiB, accounting 512 MiB. Proposed lower limits 3,072/3,584/512 plus Caddy 192 were not certified. Inspect full discovery/finalization/index/quote/history/backup overlap and RSS plus cgroup peaks. Reduce limits only with demonstrated headroom. Do not hide pressure by reducing valid market coverage, shortening required retention or killing discovery. An earlier generation-reuse prototype gave no measured RSS saving and was rejected; read its evidence before revisiting.

### D. Verify coverage and normal subscriber journeys once

Run a bounded representative audit of Markets, Funding, Tokens, pair details, Charts, Watchlist/Alerts, Portfolio, Membership and public login/reset/trial paths. Test token-price alerts from supported entry points, not just spread alerts. Read current delivery configuration: old notes say Pushover was off for the stability task, while loop readiness is true; never conflate them. Do not send unsolicited test pushes or Telegram group messages. Use local/fake-provider delivery tests and read-only production evidence; list any actual notification test requiring the owner's participation.

Use UACryptoInvest as a discrepancy source, not pricing truth. Check top and tail exact pairs across supported lanes and trace every missing pair from catalogue through books, identity, generation, gates and rendering. The historical 45-pair sample was only a sample, not full-market proof. Distinguish token recall and pair recall. Preserve configured reconciliation timers and alert semantics, and report sample denominator and reasons for exclusions.

Preserve current venue policy: HTX/Ourbit public exclusions; CoinEx/Phemex funding exclusion with price coverage retained. Follow latest implemented DEX taxonomy and user policy, inspect differences with older OKX-only requests before changing it. No silent venue substitutions. Spot-Spot and Spot-DEX public lanes were retired by request; do not restore them casually.

Portfolio: preserve dated lots and allocation of funding from opened_at through closed_at, partial closes, actual fees and current marked PnL distinct from exit VWAP. Latest notes record FATCOIN row #50 created in a separate task at ~18:51 UTC; do not revert the database to earlier account snapshots. Existing OPENAI #47, ANSEM #49 and ESPORTS tranches must not be merged blindly. LOBSTER website closure was historically unverified; check first, correct only against exact retained lifecycle evidence. Do not use stale quantities or old warning levels as current truth.

### E. Finish affiliate launch preparation, not fictional onboarding

`spreadboard/affiliates.py` and existing cabinet/tests are implemented. Last direct DB check ~17:22 UTC found zero partners, clicks, attributions and commissions. Verify current state. Do not rebuild an existing system simply because no influencer has joined.

User terms: buyer gets 20% off first month; influencer gets 50% of subscription amount, including recurring paid renewals/restarts under durable attribution; manual weekly payouts acceptable; affiliate payouts in USDT. Inspect actual base (paid amount vs list price), refunds/reversals, tier changes, discount eligibility, self-referrals, attribution persistence, link uniqueness, partner access isolation and payout idempotency. Flag any mismatch before changing earned liabilities. Prepare a clear worked example: $149 first month discounted to $119.20, 50% of collected amount = $59.60 if that is the adopted contract basis; subsequent full $149 renewal = $74.50. Do not assume this basis is already in the signed terms.

Find existing affiliate document/email draft and legal terms. Consolidate a ready-to-send onboarding pack and minimal missing information: identity/name, contact, channel URL, optional preferred slug, USDT wallet AND network, signed terms, operator legal details if missing. Generate no real influencer record with invented identity; do not send the email or pay money. Crypto subscription checkout currently supports USDC/USDT; the historical 'always USDT' request should be clarified in terms of affiliate payout vs global checkout policy rather than silently removing accepted currencies.

### F. Prepare ML honestly; activate only after all gates pass

Run `scripts/research_ml_readiness.py` through `/app/.venv/bin/python` in the appropriate production service. Heavy read-only audits still consume resources; avoid competing with backup/finalization. Refresh worker and schema/version first.

Previous read at ~17:20 UTC: v5 114,584 observations, 60,039 24h labels, 4,869 routes, 28.96/30 labelled days, exact lifecycle-cost completeness 0% against required 80%; class balance/leakage/purged time split passed. No candidate. Exclude all 5,832 v4 rows. Public historical backfill cannot prove private execution costs.

Implement valid cost evidence collection if possible using real observed fee/slippage/other required inputs, retaining provenance and unavailable states. Never fabricate labels or weaken a gate. Once data is genuinely eligible, use walk-forward validation, purging/embargo, calibrated probabilities, baseline comparison, drift monitoring, shadow operation, rollback and deterministic fallback. LLM prose is not a calibrated margin/liquidation predictor. If the gates still fail, finish useful collection fixes and record the exact blocker; do not train merely because a date threshold passes.

## 6. Release, secrets and review rules

- Credentials stay in Keychain or the existing encrypted server store. Never print environment dumps, secrets, auth headers, cookies, wallet keys or full credential-bearing URLs. Prior browser CLI echoed a disposable test password; the test user/session were deleted. Avoid that pattern: suppress/securely route secret-bearing tool output and delete disposable users even after exceptions.
- Earlier provider-credential rotation is a historical follow-up, not proof of a current compromised key. Inspect newest rotation/deferment notes and evidence. Do not echo old keys or rotate unrelated providers as part of a display release. If a credential remains pending, document the exact provider and required coordinated action without secret values.
- Keep a single release owner; inspect active work before mutation. Commit/push the reviewed candidate, preserve source/image and database rollback where appropriate. App-only changes use `scripts/deploy_production.sh app`; collector changes require the protected discovery/finalizer guard to pass, including at recreation. Never `--force` through a productive scan for convenience.
- Inspect the deployment helper: it synchronizes whole source trees with rsync, so the selected worktree must contain the full intended release. It does not establish long-term stability by returning 200. Verify host/service source digests, baked data/registries, actual service identities and marker after release. Do not demand unchanged collector code match a newer app-only digest.
- Run focused behavioral regressions while changing, then full tests and Ruff for the final combined release. Preferred commands: `uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests/ -q` and `uv run --frozen --with ruff python scripts/ruff_ratchet.py`. Existing environment fallback from the last run is PYTHONPATH including `$PWD/src:$PWD:$PWD/.venv/lib/python3.13/site-packages` with root project `.venv/bin/python`; confirm dependencies before reusing.
- Tests currently write some tracked data fixtures. Record clean baseline and restore only proved test-generated changes, never user data. No broad git reset/clean. No schema destructive rewrite, runtime-file wipe or public-history contamination with test samples.
- Browser acceptance must inspect actual numbers and timestamps in expanded rows, streams and all filters, desktop/mobile, console errors, payload size and elapsed navigation. Do not stop at a screenshot of the login page or successful HTTP status.
- Update operator reminders, current-state, the completion checklist, daily vault note and durable memory/index. If /dream or autodream exists in your installed workflow, use it and record what it actually did; do not claim an unavailable command ran.

## 7. Finite stopping rule and Codex review packet

Work in this order: current-state/read-only checks → Funding correctness/readability → backups → remaining source fixes → one combined release → browser/normal-worker proof → finite clean soak → final report. Adjust dependencies based on live evidence, but avoid restarting soak for cosmetic changes. Independent investigation can proceed while the sole backup or observer runs. Do not poll indefinitely when owner credentials, signatures or real history are missing.

Create `docs/claude-handoffs/2026-09-12-claude-completion-review.md` (or dated successor), including:

1. Base/final commit, branch, precise changed files and deployed services/digests.
2. Each task A–F marked implemented, deployed, verified, observing, owner-input-needed or blocked; never use 'done' for code-only work.
3. Before/after evidence with UTC timestamps, exact commands and exit codes, regression tests, browser screenshots and measured timings. Sanitize all artifacts.
4. Backup snapshot IDs, successful timer invocation timestamps and restore proof; stability generation boundaries, raw sample path and acceptance report.
5. Funding source/window coverage, lag/recovery measurements, unavailable-history reasons and representative independent venue checks.
6. Affiliate draft/doc links and exact missing owner/influencer information; model eligibility report and any remaining cost gaps.
7. Rollback instructions, remaining risks, failed approaches and why they were rejected, and confirmation all disposable test users/sessions/processes were cleaned up.

Stop when engineering acceptance is met or every remaining item has a concrete external/data-time blocker and a precise next action. Leave existing authorized collectors operating. Do not claim universal market accuracy or promise every venue can stream every book for free. Return a short summary and the review-packet path so the user can ask Codex to independently verify your work.
