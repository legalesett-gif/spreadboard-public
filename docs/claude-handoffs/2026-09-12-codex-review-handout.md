# Codex review handout — Claude session, 12 September 2026

Written 22:30 UTC. Supersedes `2026-09-12-claude-completion-review.md` (kept for
its earlier backup detail). Review target: **`f898e78`** on
`codex/exchanges-funding-trial-20260910`.

Two defects were found, fixed, deployed and measured. Four work packages remain
open, each with a concrete blocker and next action. **No item is marked "done".**

---

## 1. What to review

| Commit | Subject | Files |
|---|---|---|
| `1004837` | Let a locked repository wait instead of losing the backup | `scripts/backup_spreadboard.py`, `tests/test_backup_spreadboard.py` |
| `f898e78` | Page Kucoin funding history backwards past its 100-row cap | `spreadboard/venue_funding_history.py`, `tests/test_venue_funding_history.py` |
| `33413ea`, `6d1f631` | review-packet docs | docs only |

Branch now also contains **your** `6f4f5d7`, `3a36941`, `34458f2` and the merge
`61448a7`; `1004837` is an ancestor of your deployed `34458f2`, so the backup fix
was carried into your 21:04:37 release (verified on host: `LOCK_RETRY` present).

**Not pushed — needs the owner.** My `git push` was refused by the sandbox
permission classifier and I did not work around it. The branch is 4 commits ahead
of `origin/codex/exchanges-funding-trial-20260910` and 0 behind, so it is a clean
fast-forward. Owner action:

```bash
cd "/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-exchanges-trial"
git push origin codex/exchanges-funding-trial-20260910
```

I scanned `4959b5a..HEAD` for credential patterns: the only matches are the words
"secret"/"token" appearing in prose inside these handoff documents. No keys.

### Production state at 22:30 UTC

| | |
|---|---|
| Marker `/opt/spreadboard/app/.deployed_revision` | `f898e78` (I set it by hand — see §4.1) |
| `app-app-1`, `app-collector-1` | started 2026-09-12T22:16:04Z, both `healthy`, restarts 0 |
| `https://spreadarbitrage.ink/api/health` | 200 in 0.084s |
| Phemex retirement | **live** (`EXCLUDED_OPPORTUNITY_VENUES` includes `phemex`) — your release, intact |
| Backup timer | `active`, next **Sun 2026-09-13 00:17:57 UTC** |

---

## 2. Fix 1 — backup retention had never once completed

**Symptom.** The 18:18:06 UTC run reported unit failure (`ExecMainStatus=1`,
`Result=exit-code`, 18:18:06 → 19:26:34).

**What actually happened.** The snapshot *succeeded* —
`snapshot fe52e857 saved`, 7,915 files, 24.974 GiB in 1:03:58 — and then
`forget --prune` could not take its **exclusive** lock:

```
repo already locked, waiting up to 0s for the lock
repository is already locked by PID 2557734 ... created 2026-09-11 06:25:51 (37h0m41s ago)
```

PID 2557734 verified dead; no restic/rclone running. **41 snapshots existed
against a 7-daily/4-weekly/3-monthly policy — retention had never completed.**

**Root cause.** `_ensure_repository()` sweeps stale locks, but only when its probe
is blocked, and that probe is `restic snapshots` — a **non-exclusive** lock. A
stale lock that blocks only exclusive operations passes the probe untouched and
then kills retention.

**Fix.** Sweep stale locks immediately before the exclusive step, and add
`--retry-lock` (global in restic 0.16.4, confirmed on host) to **every** restic
invocation, because the probe, backup and check each take a lock and each died at
"up to 0s". `unlock --remove-all` is still never used — asserted by test.

**Mutants:** drop the sweep → 3 failed; drop `--retry-lock` → 1 failed; restored
→ 19 passed.

**Operational repair.** `restic unlock` → `successfully removed 1 locks`.
Dry-run inspected **before** deleting: would remove 30, keep 11, with
`fe52e857` confirmed in the KEEP set. Applied:
`30 snapshots have been removed, running prune`.

### Open, with a real blocker

The backlog prune is **still running at 2h20m** (repack 305,784 blobs / 27.518
GiB over rclone/Drive). Its log shows `rateLimitExceeded` with successful
retries — quota-bound but progressing. Consequences:

- **Two consecutive clean timer invocations: NOT proven.**
- **Restore proof: NOT performed.** Restore needs a lock the repack holds
  exclusively.

**Next actions.** When `ps -p 3829220` is gone and `/root/prune_once.log` shows
`EXIT=0`: confirm 11 snapshots remain; let 00:17:57 UTC and the following run
complete, requiring `ExecMainStatus=0` twice; then restore the newest snapshot
into an **isolated** directory and run `PRAGMA integrity_check` and
`PRAGMA foreign_key_check` on each restored SQLite database. **Never restore over
production.**

*Note for you:* I stopped the timer at ~20:26 to prevent the 00:21 run colliding
with the mid-repack exclusive lock, and **restarted it after the release**. It is
active. Do not assume it was left disabled.

---

## 3. Fix 2 — Kucoin could never complete a 30d window

This one corrects the amendment's hypothesis, so please check my reasoning.

**The amendment said** the 7d/30d gap was "mixed hourly/four-hour Aster history
rejected by a single-cadence validator". Measured reason distribution over 9,898
legs (before my fix):

| Reason | 1d | 7d | 30d |
|---|---:|---:|---:|
| complete | 94.8% | 93.1% | 80.4% |
| `insufficient_event_coverage` | 16 | 175 | **1,346** |
| `no_window_detail` | 468 | 468 | 468 |
| `mixed_cadence_revalidation_required` | — | — | **1** |
| `settlement_cadence_unresolved` | — | 4 | 4 |

The mixed-cadence validator accounted for **one leg**. The gap was
`insufficient_event_coverage`, and a large cohort of it was **ours, not the
venues'**.

**Root cause.** Kucoin's endpoint returns at most **100 rows** whatever range is
requested and ignores `since`. At 4h cadence that is 16.6 days against the 180
events a 30d window needs. Kucoin had **no branch** in `_history_pages`, so it
used the generic forward cursor (`since = cursor + 1`), re-requested the same
newest slice, and the repeated-page guard stopped it after one page.

Evidence, live endpoint, `Kucoin Futures|0G/USDT:USDT`:

```
page1 (from=now-31d)          rows=100  oldest=16.6d
page2 (to = page1.oldest-1)   rows=86   oldest=30.9d   -> 186 events total
```

Store held **111 of 180** events, history starting 18.4d ago. 683 Kucoin legs sat
at a **median depth of 18.4d**. No venue is API depth-capped — Kucoin's own p90
reached 29.9d, which is what first told me this was a cursor we never sent.

**Fix.** A Kucoin branch paging backwards on its native `to` bound, plus — this
is the part worth your attention — the backward-paging venue list was
**duplicated** in two places (cursor derivation and boundary stop). A venue in
one but not the other silently keeps paging forward. It is now one named
constant, `BACKWARD_PAGING_VENUES`. My first patch added only the branch and the
test still failed, which is how the second half surfaced.

**Mutants:** drop Kucoin from the set → 2 failed; drop the branch → 2 failed.

**Measured in production** (deployed 22:16:04, re-measured 22:28):

| | before | after |
|---|---:|---:|
| Kucoin median history depth | 18.4d | **29.9d** |
| 30d completeness | 80.4% | **82.0%** |
| `insufficient_event_coverage` (30d) | 1,346 | **1,185** |

### The remaining 30d gap is mostly genuine — please sanity-check this

Classified before the fix: of 1,346, **1,112** had history starting <29 days ago
(new listings — a dash is correct) and **234** had long-enough history with
events missing (retryable). Of the 468 `no_window_detail`: 299 `no_history_rows`,
136 `symbol_not_indexed`, 10 `market_paused` (all legitimately unavailable) and
**21 `api_error`** (retryable).

**So genuine retryable lag was ~255 legs (2.6%), and 30d completeness has a real
ceiling well under 100%.** Do not tune toward 100%.

**I then swept every other venue** with the same method (request a page, then
re-request with `to/until = oldest-1`; if older rows come back, the cursor was
ours to send). After the fix all 15 venues show a median history depth of
29.9–30.0d except **BitMart at 25.8d** (96 legs), so I checked BitMart directly:
`end_time`, `endTime`, `before` and `to` all return the identical newest 100
rows. BitMart genuinely offers no backward pagination, exactly as the existing
comment in `_bitmart_history` says. At its 8h cadence 100 rows is 33.3 days, so
30d is covered; the 25.8d median is sub-8h-cadence markets, which only fill in by
accumulating successive reads — already what the code does. **No fix available
and none needed; correctly classified as unavailable.** Kucoin was the only
fixable venue.

---

## 3a. Rollover recovery has a measured bound — and the "problem" is normal

Task A asked for measured rollover recovery with an explicit acceptable bound,
without running a global rebuild. I ran none. Extracted from the 679 funding
samples in the existing soak series (post-warmup, i.e. after the run first
reached overdue=0):

| | |
|---|---:|
| Spells with 30d overdue > 0 | ~30, one per hour |
| Typical recovery | **0.17 – 0.50 h** |
| **Worst case recovery** | **1.33 h** |
| Spells that failed to recover | **0** |
| Final state | 30d coverage **84.95%**, overdue **0/0/0** |

The shape is unambiguous: overdue jumps at the top of almost every hour (`XX:01`)
as rolling windows expire, then returns to zero within ~20 minutes. Deeper spikes
(peak overdue 4,097–7,499) line up with discovery generation changes and still
recovered inside 0.92h.

**This reframes the handoff's premise.** The "recurring expiry/recovery problem"
is ordinary bounded rollover, not a defect. The alarming 19:04 UTC reading
(88.74 / 85.04 / 73.03%, overdue 766 / 849 / 663) was sampled **three minutes
after the 19:01 expiry spike** — mid-recovery. Compare the same metrics at 22:40:
**96.89 / 95.16 / 84.95%, overdue 0 / 0 / 0**, with
`current_window_catch_up_complete: true` and `deep_history_pending_leg_count: 0`.

Proposed acceptance bound for review: **30d overdue must return to 0 within 2h of
any expiry spike**, which the measured 1.33h worst case satisfies. Sampling
completeness within ~20 minutes of the hour boundary will always look degraded
and should not be treated as a fault.

## 4. Outstanding work, with next actions

### 4.1 `.deployed_revision` is written by nothing (confirmed)

Searched `scripts/`, `spreadboard/`, `docs/`: no writer. It is maintained by hand
and had gone stale at `4959b5a`. I set it to `1004837` then `f898e78`. **The real
fix is for `deploy_production.sh` to write it** — not implemented, because
changing release semantics mid-release seemed wrong. Until then treat the marker
as a hint and verify the source digest. **This is a review-integrity hazard.**

### 4.2 Funding readability — NOT fixed

Desktop values still truncate with ellipses. Established only: the strip renders
via `render_funding_windows` (`server.py` ~10679–10736); live updates target
`.funding-window.current strong` (~7778); a mobile-only override at ~21885
already sets `overflow: visible; text-overflow: clip`, which implies a broader
clipping rule applies at wider viewports. **No `text-overflow` rule targets
`.funding-window strong` directly — the clip is inherited and must be identified
from computed style in the live DOM.** Needs authenticated browser work at
390px / 1440px / laptop width. I deliberately created **no disposable user**.

### 4.3 Filter paths and streamed settled cells — NOT verified

All four `now/1d/7d/30d` paths after live updates, initial HTML *and* subsequent
DOM updates, remain unverified. The previous session's failed `24h settled` text
assertions are still unresolved and must not be counted as passing.

### 4.4 Reverse-route semantics — NOT verified

Leg-level cadence arithmetic is internally consistent (720 events × 1h = 30d;
180 × 4h = 30d). Extremes are real: top `Kraken Futures|RARE/USD:USD` +30.85%,
tail `Kraken Futures|LCAP/USD:USD` −92.42% over 30d. A large negative means longs
were paid, so the profitable side is futures-long/spot-short, which needs
inventory or borrow. **Whether the UI presents that gross carry as executable net
return is unverified** — it needs the rendered page, not the store.

### 4.5 Stability acceptance — cannot be certified

Observer PID 2617599 (`--hours 49`) left running; no duplicate started. Samples:
`/opt/spreadboard/runtime/stability/20260911-funding-acceptance/samples.jsonl`.
**Generation boundaries today: 17:08:42, 20:29:41 (mine), 21:04:37 (yours),
22:16:04 (mine).** No 48h clean window exists. A clean finite observation must
start after the final release. Memory limits **unchanged**; the proposed
3,072/3,584/512 reduction stays uncertified — I produced no headroom evidence.

### 4.6 Affiliates — verified, nothing built

`affiliate_partners`, `affiliate_clicks`, `affiliate_attributions`,
`affiliate_commissions`, `affiliate_payout_batches` — **all 0 rows**, matching the
17:22 check. Onboarding pack, commission-basis audit and the
$149→$119.20→$59.60 worked example were **not** produced. No influencer record,
no email.

### 4.7 ML — not run

`scripts/research_ml_readiness.py` deliberately not executed: it is
resource-heavy and a 27.5 GiB repack was saturating the backup path. No gate
weakened, nothing trained. Last known blocker (exact lifecycle-cost completeness
0% vs required 80%) unchanged.

### 4.8 Coverage / subscriber journeys — spot checks only

Portfolio rows intact and unmerged: 50 FATCOIN, 49 ANSEM, 48 ESPORTS, 47 OPENAI,
44 SKHX/SKHYNIX open; 46/45/43 closed. FATCOIN #50 from the ~18:51 task present;
no DB reversion. **No browser journeys, no alert-delivery test, no UACryptoInvest
discrepancy pass.** I sent no test pushes and no Telegram messages.

---

## 5. Gates

| Gate | Result |
|---|---|
| `pytest tests/ -q` (final, on `f898e78`) | **2973 passed**, exit 0 |
| `ruff_ratchet.py` | no new findings (500 known), exit 0 |
| `deploy_production.sh app collector` | exit 0, digest verified, guard passed (0 scan workers) |
| `/api/health` | 200 in 0.084s |

Test-generated drift in `data/` (8 files) was identified against a recorded
pre-test baseline and restored with a scoped `git restore`. No user data touched,
no broad reset/clean, no schema change.

---

## 6. Rollback

```bash
cd "/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-exchanges-trial"
git revert --no-edit f898e78          # Kucoin pagination
git revert --no-edit 1004837          # backup locking
./scripts/deploy_production.sh app collector
ssh -i ~/.ssh/spreadboard_digitalocean root@178.128.126.204 \
  'printf "34458f2\n" > /opt/spreadboard/app/.deployed_revision'
```

Reverting `f898e78` restores the 100-row Kucoin ceiling (no data loss; the store
keeps fetched events). Reverting `1004837` restores the failing retention path.
**Backups must not be rolled back:** the 30 removed snapshots were removed by the
configured policy after an inspected dry-run, and 11 policy-conformant snapshots
remain.

## 7. Cleanup

No disposable users, sessions or test accounts were created. No secrets,
environment dumps, auth headers or credential-bearing URLs printed; journal
output was URL-sanitised and the backup env file was sourced inside a remote
shell, never echoed. Host artefacts left deliberately as evidence, safe to
delete after review: `/root/prune_once.sh`, `/root/prune_once.log`, and probes
`/tmp/reasons.py`, `/tmp/classify.py`, `/tmp/depth.py`, `/tmp/leaders.py`.

## 8. Please check my reasoning on

1. That the 30d ceiling is genuinely <100% because 1,112 markets are younger than
   30 days — I may have misclassified some as young when they are backfill gaps,
   exactly as I initially did for Kucoin.
2. ~~Whether other generic-branch venues have the same unsent-cursor problem.~~
   **Checked and closed** — see §3: Kucoin was the only one; BitMart's shortfall
   is a genuine venue limit.
3. Whether suppressing retention failure separately from snapshot success is
   wanted, or whether loud failure is preferred (I left it loud).
