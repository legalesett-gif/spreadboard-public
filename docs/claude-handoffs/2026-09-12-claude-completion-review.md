# SpreadBoard — Claude completion review packet

Prepared 12 September 2026, 20:35 UTC. For independent Codex review.
Session scope was one working session, not the full A–F programme. Status below
is per-item and deliberately conservative: **no item is marked "done"**, and
code-only work is never reported as verified.

## 1. Base, final commit, changed files, deployed services

| Field | Value |
|---|---|
| Worktree | `/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-exchanges-trial` |
| Branch | `codex/exchanges-funding-trial-20260910` |
| Base commit at handoff | `394a186` (docs handoff), on `4959b5a` |
| Final commit this session | `1004837` — *Let a locked repository wait instead of losing the backup* |
| Pushed? | **No — not yet pushed.** See §7. |
| Changed files | `scripts/backup_spreadboard.py`, `tests/test_backup_spreadboard.py` |
| Deployed service | `app` only, via `./scripts/deploy_production.sh app`, exit 0 |
| App container restart | 2026-09-12T20:29:41Z, health `healthy`, restarts 0 |
| App source digest after release | `0bf9cb78c3140126` (was `c76535431bd865fb` at `4959b5a`) |
| Host marker `.deployed_revision` | updated `4959b5a` → `1004837` (see §7 finding) |
| Collector / accounting | **unchanged and not restarted** (app-only release) |

Worktree was clean at `394a186` before work began (`git status --short` = 0 lines).

## 2. Task status

| Task | Status | Note |
|---|---|---|
| A. Funding semantics/readability/rollover | **Not started (investigated only)** | Located the readability start point; no code change. See §5. |
| B. Encrypted offsite backups | **Implemented, deployed, partially verified; retention observing** | Root cause found and fixed; backlog retention executing at time of writing. |
| C. Reliability/memory acceptance | **Observing — boundary moved by this release** | See §4. |
| D. Coverage & subscriber journeys | **Partially verified (read-only spot checks)** | No browser acceptance performed. |
| E. Affiliate launch preparation | **Verified current state; onboarding pack not assembled** | All counters zero; no code change needed. |
| F. ML readiness | **Not run** | Deliberately deferred; would compete with the running repack. See §6. |

## 3. Task B — evidence

### Root cause (established, not inferred)

The 18:18:06 UTC run **saved its snapshot successfully** and then failed the unit:

```
Sep 12 19:25:49  processed 7915 files, 24.974 GiB in 1:03:58
Sep 12 19:25:49  snapshot fe52e857 saved
Sep 12 19:25:57  repo already locked, waiting up to 0s for the lock
Sep 12 19:26:32  unable to create lock in backend: repository is already locked by PID 2557734
Sep 12 19:26:32  lock was created at 2026-09-11 06:25:51 (37h0m41s ago)
Sep 12 19:26:34  CalledProcessError: restic forget ... --prune ... exit status 1
systemd: spreadboard-backup.service: Failed with result 'exit-code'
```

`ExecMainStatus=1`, `Result=exit-code`, 18:18:06 → 19:26:34 UTC. The handoff's
suspicion that the 19:04 `activating` state was not proof of success is
**confirmed**.

PID 2557734 verified dead (`ps -p 2557734` → not running); no `restic`/`rclone`
process was running at the time. The lock was genuinely stale.

**Why the existing stale-lock handling missed it:** `_ensure_repository()` does
sweep stale locks, but only when its probe is blocked. That probe is
`restic snapshots`, which takes a **non-exclusive** lock. A stale lock that
blocks only *exclusive* operations passes the probe untouched, then kills
`forget --prune`. Retention had therefore **never once completed**: 41 snapshots
existed against a 7-daily/4-weekly/3-monthly policy.

### Fix (`1004837`)

1. Sweep stale locks immediately before the exclusive retention step, where the
   contention actually is. restic's default `unlock` is age/owner-checked;
   `--remove-all` is still never used (asserted by test).
2. `--retry-lock` (global flag, restic 0.16.4 confirmed on host) on **every**
   restic invocation via `_run_restic`, because the probe, backup and check each
   take a lock and each failed instantly at "up to 0s".

Tests added to `tests/test_backup_spreadboard.py`:
`test_retention_clears_a_stale_lock_before_pruning`,
`test_the_stale_lock_sweep_never_removes_a_live_lock`,
`test_retention_waits_for_a_concurrent_run_instead_of_failing_at_zero`.
Two pre-existing phase-sequence assertions were updated to include the new
`unlock` phase (they enumerate phases and now also prove it is rclone-paced).

**Mutant verification** (both guards independently defended):

| Mutant | Result |
|---|---|
| remove the `unlock` sweep | 3 failed, 16 passed |
| remove `--retry-lock` | 1 failed, 18 passed |
| restored | 19 passed |

### Operational repair performed

| Step | Evidence |
|---|---|
| `restic unlock` | `successfully removed 1 locks` |
| Snapshot inventory | 41 snapshots, incl. `fe52e857` 2026-09-12 18:20:54 |
| Retention dry-run | would remove **30**, keep **11**; `fe52e857` **kept** (verified absent from the removal set) |
| Retention applied | `30 snapshots have been removed, running prune` |
| Prune plan | repack 305,784 blobs / 27.518 GiB; delete 176,672 blobs / 20.756 GiB; remaining 267,073 blobs / 19.650 GiB |

Dry-run was run and inspected **before** any deletion. The 30 removed are those
outside the configured policy; `keep-daily` retains the latest snapshot per day,
which is why same-day `60e9236c` (00:20) and `8b2eb99c` (06:25) are removed while
`fe52e857` (18:20) is kept.

### NOT yet proven (open)

- **Two consecutive ordinary timer invocations at exit 0** — not proven. The
  backlog repack (27.5 GiB over rclone/Drive at `TPSLIMIT=4`) was still running
  at 20:35 UTC, ~32 min elapsed.
- **Restore to an isolated directory + SQLite integrity/FK checks** — **not
  performed**, because restore needs a lock the running repack holds
  exclusively. This is a data-time blocker, not a decision.

### Temporary operational action — already reverted

`spreadboard-backup.timer` was **stopped** at ~20:26 UTC to stop the 00:21 run
colliding with the mid-repack exclusive lock (it would have failed and contended
for Drive quota). It was **restarted after the release**: `systemctl is-active`
→ `active`, next run **Sun 2026-09-13 00:17:57 UTC**. The timer is not left
disabled.

## 4. Task C — stability boundary

- Existing observer still running: PID 2617599,
  `scripts/stability_soak.py --output-dir /opt/spreadboard/runtime/stability/20260911-funding-acceptance --hours 49`,
  elapsed 1d11:41 at 19:49 UTC. **Left running; no duplicate observer started.**
- Raw samples: `/opt/spreadboard/runtime/stability/20260911-funding-acceptance/samples.jsonl`,
  12,053 lines / 33.8 MB, still being appended.
- **Generation boundaries:** app released 17:08:42 UTC (pre-existing), then
  **again at 20:29:41 UTC by this session's release**. No 48h clean window can be
  certified across either. A clean finite observation must start **after**
  20:29:41 UTC and after the next release, not before.
- Container state at 19:49 UTC: all three `healthy`, restarts 0, `OOMKilled`
  false — recorded as a readiness signal only, not a stability certificate.
- Memory limits **not changed**. No evidence of demonstrated headroom was
  produced this session, so the proposed 3,072/3,584/512 reduction remains
  uncertified.

## 5. Task A — what was established, and what was not

No code change was made. Established:

- The funding strip renders through `render_funding_windows` (`server.py`
  ~10679–10736); live updates target `.funding-window.current strong`
  (`server.py` ~7778).
- A mobile-only override already exists at ~21885
  (`.funding-realised .funding-window strong { overflow: visible; text-overflow: clip; }`),
  which implies a broader clipping rule is in force on wider viewports — the
  likely source of the ellipsis truncation in the desktop screenshot.
- No `text-overflow` rule targets `.funding-window strong` directly; the clip is
  inherited from a broader selector that still needs to be identified **from
  computed style in the live DOM**, not by reading CSS.

**Deliberately not claimed:** nothing about cell readability, the four filter
paths, settled-cell correctness after streaming, or stale-value honesty was
verified. The handoff requires DOM/computed-style/screenshot evidence for these
and none was gathered. The previous session's failed `24h settled` text
assertions remain unresolved and must not be counted as passing.

## 6. Tasks D–F — bounded read-only findings

**D.** Portfolio rows intact and unmerged (`id` DESC): 50 FATCOIN *open*,
49 ANSEM *open*, 48 ESPORTS *open*, 47 OPENAI *open*, 44 SKHX/SKHYNIX *open*,
46/45/43 *closed*. FATCOIN #50 from the separate ~18:51 task is present; no
database reversion occurred. No browser journey audit, no alert delivery test,
and no UACryptoInvest discrepancy pass were performed.

**E.** Affiliate schema present and **entirely unused**:
`affiliate_partners`, `affiliate_clicks`, `affiliate_attributions`,
`affiliate_commissions`, `affiliate_payout_batches` — **all 0 rows**. This
matches the ~17:22 UTC check. The system is implemented; nothing was rebuilt.
The onboarding pack, commission-basis audit and worked example were **not**
produced. No influencer record was created and no email was sent.

**F.** `scripts/research_ml_readiness.py` was **not run**. The handoff warns the
audit is resource-heavy; a 27.5 GiB repack was saturating the backup path, and
running both would have been reckless. No gate was weakened and no model was
trained. The last recorded blocker (exact lifecycle-cost completeness 0% against
a required 80%) is unchanged by anything in this session.

## 7. Rollback, risks, findings, cleanup

### Rollback

```bash
# Code (single commit, two files)
cd "/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-exchanges-trial"
git revert --no-edit 1004837
./scripts/deploy_production.sh app

# Or redeploy the previous release directly
git checkout 4959b5a -- scripts/backup_spreadboard.py
./scripts/deploy_production.sh app
ssh -i ~/.ssh/spreadboard_digitalocean root@178.128.126.204 \
  'printf "4959b5a\n" > /opt/spreadboard/app/.deployed_revision'
```

No database migration, no schema change, no runtime-file rewrite. The backup
change is additive to the restic command line; reverting restores the previous
(failing) retention behaviour only.

**Backups cannot be rolled back and should not be:** the 30 removed snapshots
were removed by the *configured* retention policy after an inspected dry-run.
The 11 policy-conformant snapshots remain, including `fe52e857`.

### Findings worth a reviewer's attention

1. **`.deployed_revision` is written by nothing in tracked source.** Confirmed by
   search across `scripts/`, `spreadboard/`, `docs/`. It is maintained by hand and
   had gone stale. I updated it to `1004837`, but the real fix is for
   `deploy_production.sh` to write it. **Not implemented** — flagged, because it
   changes release semantics mid-release. Until then, treat the marker as a
   hint and verify the source digest instead.
2. **A retention failure discards a good snapshot's run status.** `forget` runs
   under `check=True`, so a successful 64-minute backup still reports unit
   failure. Arguably the snapshot's success should be recorded separately from
   retention health. **Not changed** — it would mask real retention failure, and
   the correct fix (separate exit reporting/alerting) is a design decision for
   the owner.
3. **The soak now has two generation boundaries today** (17:08:42, 20:29:41).

### Approaches considered and rejected

- *Force the timer through the repack.* Rejected: guaranteed failure plus Drive
  quota contention during a 27.5 GiB repack.
- *`restic unlock --remove-all`.* Rejected outright: a live repack lock was held.
  The existing in-code policy comment forbidding it is correct and is now
  enforced by a test.
- *scp the single script to the host instead of a release.* Rejected: it would
  desynchronise host source from the marker/digest that review depends on.

### Cleanup confirmation

No disposable users, sessions or test accounts were created this session. No
secrets, environment dumps, auth headers or credential-bearing URLs were printed
(journal output was URL-sanitised before display; the backup env file was sourced
inside a remote shell and never echoed). Test-generated fixture drift in `data/`
(8 files) was identified against a recorded pre-test baseline and restored
precisely with `git restore`; no user data was touched and no broad
reset/clean was used. Background helper `/root/prune_once.sh` and its log
`/root/prune_once.log` remain on the host as evidence and can be deleted after
review.

## 7a. Coordination with the concurrent Codex release — IMPORTANT

The handoff was **amended at 21:26:26 UTC, during this session** (after my
20:29:41 release), adding two items that were not in the document I started
from:

1. Updated venue policy: remove **Phemex and HTX** from public discovery, quote
   sweeps, opportunity subscriptions and displayed opportunities — being
   implemented by Codex in `tmp/spreadboard-venue-retirement`, branch
   `codex/retire-phemex-htx-20260912`, with the instruction *"Do not overwrite
   that release with the old policy."*
2. A new funding audit requirement for missing/inaccurate **7d/30d leaders**
   (regression examples ESPORTS, ANSEM, SIREN, LOBSTER).

**Did my app release overwrite that work? No — verified, not assumed:**

| Evidence | Value |
|---|---|
| Host `src/spreadarb/venue_policy.py` mtime | **2026-09-11 01:23:53 UTC** — predates this session |
| Host `scripts/backup_spreadboard.py` mtime | 2026-09-12 20:26:17 UTC — my change |
| Host `venue_policy.py` md5 | `3d7380a7585dfb44a88f3b22e2968a26` |
| My branch's copy | `3d7380a7585dfb44a88f3b22e2968a26` (identical) |
| Retirement branch's copy | `fd6130258be35eaaf09cdc0ff8af2bf1` (differs) |
| App container | started 20:29:41Z, restarts 0 — no deploy after mine |

rsync rewrites only changed files and preserves mtimes, so the 09-11 mtime
proves my release did not touch that file. The retirement **has never been
deployed**; production has run the pre-retirement policy since 2026-09-11
01:23:53 UTC. Codex still needs to deploy it.

Their branch `b6eb204` already **merges my `1004837`**, so the backup fix is
carried into their release and does not need re-applying. Because
`deploy_production.sh` rsyncs a whole source tree, whichever worktree deploys
next must contain both changes — their branch already does; mine does not.

**Newly added scope not performed this session:** the Phemex/HTX retirement
(owned by Codex) and the 7d/30d funding-leader audit, including the reported
mixed hourly/four-hour Aster history rejected by a single-cadence validator,
LOBSTER catalogued under `龙虾`, and extreme leaders that are reverse
futures-long/spot-short routes requiring inventory/borrow — where gross carry
must not be presented as executable net return. These belong to task A and
remain open.

## 8. Gates

| Gate | Result |
|---|---|
| `uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests/ -q` | **2930 passed**, exit 0, 1205s |
| `uv run --frozen --with ruff python scripts/ruff_ratchet.py` | no new findings (500 known), exit 0 |
| Focused backup regressions | 20 passed |
| `deploy_production.sh app` | exit 0, digest verified |
| `https://spreadarbitrage.ink/api/health` | 200 in 0.295s |

## 9. Precise next actions

1. **Wait for the repack to finish** (`ps -p 3829220`; log `/root/prune_once.log`),
   then confirm the snapshot list is the expected 11.
2. **Prove backups:** let the 00:17:57 UTC timer run, then the next; require
   `ExecMainStatus=0` twice. Then restore `fe52e857` (or its successor) into an
   **isolated** directory and run `PRAGMA integrity_check` + `PRAGMA foreign_key_check`
   on the restored SQLite databases. Never restore over production.
3. **Task A** is the largest remaining engineering item and needs authenticated
   browser acceptance: identify the clipping selector from computed style at
   390/1440/laptop widths, then verify all four filter paths after live updates.
4. **Start the clean finite soak only after the next release**, given the
   20:29:41 boundary.
5. Push `1004837` once reviewed.
