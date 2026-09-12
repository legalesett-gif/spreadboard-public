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

**Pushed.** `8282617..3e57699` on
`origin/codex/exchanges-funding-trial-20260910`. (A first `git push` attempt was
refused by the sandbox permission classifier; the retry succeeded. I did not work
around the refusal.) I scanned `4959b5a..HEAD` for credential patterns — the only
matches are the words "secret"/"token" appearing in prose inside these handoff
documents. No keys.

### Production state at 23:50 UTC

| | |
|---|---|
| Marker `/opt/spreadboard/app/.deployed_revision` | **`8282617`** — your 23:22:34 release; I have **not** deployed since |
| `app-app-1`, `app-collector-1` | started 2026-09-12T23:22:34Z, both `healthy` |
| My Kucoin fix live under `8282617` | `BACKWARD_PAGING_VENUES` contains `Kucoin Futures` — verified in the collector |
| My backup fix live under `8282617` | `--retry-lock` present in the host script — verified |
| `https://spreadarbitrage.ink/api/health` | 200 in 0.220s |
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

**This is now automated — nothing to run by hand.** `/root/verify_backup.sh`
(confirmed alive as **PID 3926997** at 23:19 UTC, logging to
`/root/backup_verify.log`) is waiting on the repack under `setsid`, and will then:
list the remaining snapshots, restore the newest into an **isolated**
`/root/restore-test-<ts>/`, and run `PRAGMA integrity_check` plus
`PRAGMA foreign_key_check` on every restored SQLite database, printing
`ALL DATABASES VERIFIED` or naming each failure. It never restores over
production and never deletes a snapshot.

**Read `/root/backup_verify.log` first on review.** Status at 23:19 UTC: repack
still running at **3h20m**, 1.2% CPU, purely Google-Drive-quota-throttled
(`rateLimitExceeded` with successful retries), so it is progressing, not stuck.

**Still to confirm by hand:** two consecutive ordinary timer invocations at
`ExecMainStatus=0`.

**A deliberate choice you may want to revisit:** I left the timer **active** even
though the 00:17:57 UTC run will collide with the repack's exclusive lock. With
`--retry-lock` that run waits and then fails cleanly instead of thrashing, and the
following run should succeed once the repack is done. Masking the timer would
have avoided one failed unit, but it would leave backups disabled if nobody
restored it — a self-healing failure beat a manual state that depends on memory.
A valid snapshot (`fe52e857`, 18:20 UTC) is retained meanwhile.

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

Product-level confirmation, Kucoin legs holding a **complete** 30d window:

| time (UTC) | complete / 683 | |
|---|---:|---:|
| 22:28 | 583 | 85.4% |
| 22:49 | 583 | 85.4% |
| 22:53 | **633** | **92.7%** |
| 22:57 | 633 | 92.7% |

Rising monotonically as the refresh cycles through the remaining legs; the
stragglers still show the pre-fix signature exactly (111/180 events, 18.4d depth)
and fill in on subsequent passes, so expect this to continue toward the cohort
that is genuinely younger than 30 days. Worth re-checking on review: if it
plateaus well below ~97%, something else is also capping those legs.

### The remaining 30d gap is mostly genuine — please sanity-check this

Classified before the fix: of 1,346, **1,112** had history starting <29 days ago
and **234** had long-enough history with events missing (retryable).

> **Correction — my original wording said a dash is correct for those 1,112. It
> is not.** `REMINDERS.md` records the operator clarifying on 12 September that
> younger contracts **should** appear in the 30-day Funding view using their
> actual settled history since the first verified settlement, **with the duration
> shown** — never annualised or projected to 30 days, and never inferring a
> listing date from provider-limited history, with strict full-period fields kept
> separate. Codex has implemented exactly that (ANSEM: available 16.636% over
> 18.3d alongside separate strict 24h/7d). So the 1,112 are *displayable*, not
> blank; what must stay strict is the full-period field. My classification numbers
> stand; the display conclusion drawn from them was wrong and should not be acted
> on. Of the 468 `no_window_detail`: 299 `no_history_rows`,
136 `symbol_not_indexed`, 10 `market_paused` (all legitimately unavailable) and
**21 `api_error`** (retryable).

**So genuine retryable lag was ~255 legs (2.6%), and strict 30d completeness has
a real ceiling well under 100%.** Do not tune the *strict* figure toward 100% —
but under the clarified policy above those legs still show a dated, duration-
labelled return, so user-visible coverage is higher than strict completeness.

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

**The leading hypothesis is false, and I can show it from the stylesheet the site
actually serves.** I fetched the public `https://spreadarbitrage.ink/assets/app.css`
(200, 228,268 bytes) and enumerated every rule — including inside every
`@media` block — that sets `overflow` or `text-overflow` and could match
`.funding-window strong`.

**Exactly one rule matches, and it is the mobile override itself:**

```
@media (max-width: 960px)
  .funding-token-group > summary .funding-realised .funding-window strong
      overflow: visible; text-overflow: clip;
```

So **nothing applies `text-overflow: ellipsis` to a funding value at any
viewport.** The governing rules are:

```
.funding-window-strip { display:grid; grid-template-columns:repeat(4,minmax(0,1fr));
                        width:100%; min-width:0 }
.funding-window       { display:flex; flex-direction:column; min-width:0 }
.funding-window strong{ font-size:11px; font-variant-numeric:tabular-nums }
```

`white-space` is never set on `strong`, so it defaults to `normal`: with
`minmax(0,1fr)` columns a long value **wraps**, it does not ellipsise. The
max-width:960px override is therefore defensive and currently a no-op.

**What this means for the fix.** Chasing an inherited ellipsis rule is wasted
effort — there isn't one. The remaining candidates for what the screenshot showed,
in order of likelihood:

1. The ellipses were on a **neighbouring element**, not the value.
   `.funding-token-cell span` *does* carry
   `overflow:hidden; text-overflow:ellipsis; white-space:nowrap` (server.py
   ~20609) and sits next to the strip — a truncated token name is the obvious
   candidate.
2. Wrapped values inside 1/4-width grid cells read as clipped at a glance.

**Still needs the live DOM** to confirm which element truncated, but the search
space is now one check rather than a CSS hunt. I created **no disposable user**;
`/funding` redirects to Sign in and `/free` carries no funding strip
(`document.querySelectorAll('.funding-window').length === 0`, measured).

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

### 4.6 Affiliates — audited against the code; two gaps flagged

All five tables **0 rows**, matching the 17:22 check. Nothing was rebuilt — the
pack already existed (`docs/affiliate-partner-agreement-draft.md`,
`docs/affiliate/affiliate-outreach-email.md`, plus a built .docx/.pdf).

Full audit written to **`docs/affiliate/2026-09-13-commission-basis-verification.md`**.
Headlines: the commission base is the **collected** amount
(`base = max(0, list - discount)`), so $149 → buyer $119.20 / partner **$59.60**,
renewal $149 → **$74.50** — computed by calling the real code, and the agreement
draft §3.1/§4.1/§5.2 already matches it. Idempotency holds via
`affiliate_commissions.invoice_id NOT NULL UNIQUE` + `INSERT OR IGNORE`; the
discount is first-month-only with open-invoice reservation; attribution is
first-touch and durable.

**Two gaps flagged, deliberately NOT changed** (they touch earned liabilities):
no self-referral guard in `attach_registration`, and `void_commission` refuses
anything past `pending`, so a refund after payout has no reversal path. Neither is
exploitable today — no partners, no commissions.

**Owner input still required:** Provider legal name + registered address (the
draft's `[FULL LEGAL NAME]` / `[REGISTERED ADDRESS]`), and from the influencer a
USDT wallet **with network** — payouts are Arbitrum One only. Note the
"always USDT" rule is an *affiliate payout* policy; checkout legitimately accepts
USDC and USDT and should not be narrowed. No email sent, no partner created.

### 4.7 ML — measured; one gate can never clear by waiting

**Now run** — load had fallen to 3.83 (from 7.32), so I ran it read-only at
23:28 UTC. Exit 0; site health immediately after was **200 in 0.220s**, load
peaked at 5.44 and settled. Fresh gate state:

| Gate | Actual | Required | |
|---|---:|---:|:--|
| `version_selected` | v5 (115,686 obs) | — | PASS — v4's 5,832 excluded as instructed |
| `outcomes` | 60,451 | 5,000 | PASS |
| `routes` | 4,874 | 100 | PASS |
| `funding_class_balance` | 0.2095 | 0.10 | PASS |
| `spread_class_balance` | 0.3458 | 0.10 | PASS |
| `feature_leakage_scan` | — | — | PASS |
| **`span_days`** | **29.21** | 30.0 | **FAIL** |
| **`cost_complete_fraction`** | **0.0** | 0.8 | **FAIL** |
| `candidate_present` | false | — | FAIL — `no_candidate_model` |

`mode: shadow_only`, `model_configured: false`,
`activation_exclusion: public_market_only_no_identity_or_exact_account_costs`.
The purged chronological 60/20/20 split with a 24h embargo reports `valid: true`
(train 44,725 / calibration 6,623 / test 6,630, purged 2,473), and baselines are
recorded (funding Brier 0.4167, spread Brier 0.2615).

**Two distinct blockers, and only one is a waiting game.**

- `span_days` 29.21/30 clears itself in under a day of continued collection.
- **`cost_complete_fraction` is exactly 0.0** — not low, *zero*. No observation
  carries exact lifecycle costs, so this will never clear by waiting. The gate
  wants real observed fee/slippage/borrow inputs, and the only source of exact
  execution cost in this system is the operator's own private fills (the
  portfolio positions carry actual fees). Wiring private execution costs into a
  research dataset is a design and privacy decision for the owner, not a missing
  function — and the handoff itself notes public historical backfill cannot prove
  private execution costs. **I did not fabricate a cost, weaken the gate, or
  train anything.**

Compared with the earlier 17:20 UTC read (114,584 obs / 60,039 labels / 4,869
routes / 28.96 days / cost 0%), collection is advancing normally and cost
completeness has not moved off zero.

### 4.8 Coverage / subscriber journeys — spot checks only

**Your venue retirement is verified effective in live data**, not just in the
source. Policy reads `excluded_opportunity = ['htx','huobi','ourbit','phemex']`,
`excluded_funding = ['coinex','phemex']`, and the funding catalogue carries legs
from exactly 15 venues with **none** of the retired ones present:

```
Bingx 1226  Mexc 1178  Gate 1007  Bitget 842  Bybit 832  Binance 759
XT 723  Kucoin Futures 685  Aster 564  OKX 464  Hyperliquid 432
WhiteBIT 398  BitMart 355  Kraken Futures 284  Coinbase International 150
```

No Phemex, HTX, Huobi, Ourbit or CoinEx leg survives. Price coverage retention
for CoinEx was not separately re-verified.

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
