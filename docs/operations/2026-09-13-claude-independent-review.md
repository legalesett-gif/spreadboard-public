# Independent review of Claude's September 12 completion handout

Review started September 12 23:42 UTC (September 13 London). This is a
finite review and repair of the handout, not certification of every venue,
subscriber journey, or trading position. No exchange action, customer message,
push notification, partner creation, or payout is authorized by this review.

## Baseline and verified findings

- Reviewed Claude commits `1004837` (backup locks) and `f898e78` (KuCoin
  backward pagination), subsequent handout amendments, and the original
  completion handoff. Working branch `codex/exchanges-funding-trial-20260910`.
  At review start, source release `8282617` was deployed; `7885bcd` was HEAD.
- The passed KuCoin cursor tests are meaningful: the code uses native `to`,
  walks backward, and applies the same backward-venue set to cursor movement
  and the stopping boundary. The [official endpoint documentation](https://www.kucoin.com/docs-new/rest/futures-trading/funding-fees/get-public-funding-history)
  supplies the public-history contract. At this review's fresh read, 653/683
  retained KuCoin legs had complete current 30d history (95.61%), rather than
  the handout's earlier 633/683. No pagination rollback is warranted.
  A fresh native request pair also returned 100+100 unique settlements over
  33.332 days, with the second page strictly older. The second request must
  retain the required `from=0` parameter as the actual CCXT adapter does.
- **Global shorter-history ranking now passed:** supervised generation
  `1789256269281925349-496d2356d70e` has all 12 views, no empty view, policy
  `verified_available_period_v1`, and 357,794 navigation routes. ANSEM ranks
  26 in Futures–Spot up-to-30d, with 16.63613333% over its available period.
  This closes the prior global-publication acceptance item; it is dated
  evidence, not a claim these ranks never change.
- The live browser **refutes** the handout's no-ellipsis claim. The descendant
  rule `.funding-token-group > summary > div:not(.asset-identity) strong`
  applies to the nested funding values. At 1440px, 58/100 sampled values
  clipped; at 1280px, 100/100 clipped; at 390px, 0/100 clipped. Page-wide
  overflow was zero at every width. CSS-only selector inspection and global
  overflow checks had missed the actual failure.
- A real reverse route was rendered: Kraken Futures long / MEXC spot short
  LCAP, +92.42% settled 30d and negative live carry. The page explicitly says
  short-spot inventory is required; expanded rows say inventory or borrow.
  This is gross funding research, not proof of an executable net profit.
- Baseline global 30d opened in 17.68 seconds; its structural refresh retained
  settled labels. The following global Now navigation exceeded 45 seconds,
  despite a server HTTP 200 log. Latency is a real remaining failure.
- Fresh catalogue-based health later in this review: 9,348 legs, 100%
  source-checked, 99.94% classified, 12 retryable errors; complete windows
  96.88%/95.15%/86.66% for 1d/7d/30d, overdue 0/0/0. The earlier retained-leg
  cohort used a different denominator; do not compare unlike populations.

## Repairs selected after tracing dependencies

1. **Backup retry budget:** `_run_restic` now budgets the configured lock wait
   plus the bounded operation timeout. Previously a 120s probe (360s on
   rclone) killed the advertised 15-minute wait early. Duration parsing is
   bounded, with explicit overrides covered by tests. Retention failure stays
   loud; it is not silently reclassified as complete backup success.
2. **False-green restore proof:** Claude's waiting shell used `set -u`,
   continued after nonzero restore exit, accepted an empty database list, and
   returned shell success after database failures. The replacement refuses
   failed restores, checks exact expected database inventory from the selected
   snapshot, requires the accounts schema and nonempty users, checks every
   database's integrity and foreign keys, and returns nonzero on failure.
   It uses a newly created private directory, not a caller-selected live
   target, and retains PID start identity plus a bounded wait. No live prune
   is killed, no snapshot deleted, and no live DB restored over.
3. **Deployment identity:** successful deployments write an atomic per-service
   receipt only after every requested service passes source and health checks.
   Failed/partial verification cannot advance it. App-only deployments retain
   the collector's separately recorded version. `.deployed_revision` is the
   legacy WEB APP revision, not a whole-stack equality claim; the JSON receipt
   is authoritative. Dirty source is explicitly marked, and a checkout change
   during deployment is rejected. This does not turn last-known verification
   into continuous runtime attestation; live digests remain necessary.
4. **Funding readability:** desktop summary totals use a two-by-two layout
   with an explicit non-ellipsis numeric rule; mobile's full-width four-column
   strip remains. Expanded pair values also cannot inherit numeric ellipses.

## Acceptance and genuinely outstanding work

| Work package | Verdict / remaining action |
|---|---|
| Funding data | KuCoin correction retained; global shorter-history publication verified. Unknown start of retained history is NOT proof of listing date. Full windows and labelled available periods remain separate. Remaining provider failures/backfill gaps need source-specific evidence. |
| Funding browser | Numeric clipping repaired and live-verified at1440/1280/390px. All four Futures–Spot period paths passed initial and structural refresh checks; one actual Now SSE event left historical values unchanged. Historical navigation1.2–2.7s; Now11.3s remains a performance gap, even though the timeout did not recur. Other farm/browser combinations and broader journeys remain separately scoped. |
| Rollover | Recorded recovery is useful, but a proposed two-hour recovery limit is not an accepted freshness SLO for this product. Distinguish current-rate age, exact rolling settlement expiry, ranking generation age, and visibly dated fallback. Do not relabel a lagging window as current. |
| Backups | Existing encrypted free-tier prune still progressing under provider quota; restore must wait. Two ordinary successful timer runs and successful isolated production restore are NOT yet proved. Preserve last valid snapshots and active timer. |
| Stability | Repeated releases split the observation. No 48h clean window certified; retain the existing finite observer and its generation boundaries. No memory-cap reduction without overlap/peak evidence. |
| Affiliates | Pack, email, agreement and commission-basis document exist. Fresh production counts are zero in all five affiliate tables. Economics match $149 → $119.20 → $59.60 first payment and $74.50 on a full-price renewal. Owner legal identity/address/notice/jurisdiction and real partner details/signature remain required. Guard against self-referral and support post-batch/paid refund adjustments before launch; the pack already excludes self-referrals, so “terms do not forbid it” is not an adequate dismissal. Do not create fake partners or change earned balances. |
| ML | Claude's dated 23:28 report says v5 span29.21/30d and exact costs0%/80%, no candidate; source remains shadow-only. Waiting alone cannot supply exact costs. No private-ledger training ingestion or gate weakening is authorized by this review. Full independent readiness rerun was deferred while backup/collector load competes. |
| Subscriber/market coverage | Broad cross-venue reconciliation, all subscriber journeys and actual notification delivery were NOT retested in this bounded pass. Existing unit/no-send proofs do not certify phone delivery. Preserve exact market identity and venue exclusions. |

## Evidence and final release

Local evidence directory (public UI only):
`/Users/sviatoslav/Desktop/Spread Arbitrage/output/review-2026-09-13`.
The browser uses a disposable account, retains no password/cookie artifact, and
deletes that account and its sessions in `finally`. Baseline cleanup passed
SQLite quick_check with zero foreign-key violations.

The new restore verifier passed a real isolated local restic backup/restore,
not just mocked command tests. Full-suite, deployed service identities and
post-release browser results are appended after verification. A running
production restore job must never be described as completed proof.

Release `7af2d0f` was pushed and deployed **app only**. Full suite: **3,024
passed in245.76s**, Ruff:0new/500known, shell syntax and diff checks passed.
An earlier run had3,023passed and one lint-ratchet failure (unused import left
by autofix); that import was removed and the entire suite rerun successfully.
The eight known test-generated tracked runtime fixtures were restored to the
recorded clean pre-test baseline; no runtime data was deployed.

App source fingerprint `f23a7b6969e15be5`, health200, OOMfalse/restart0; collector
was deliberately **not recreated** and retains `8282617` /
`f5be5e7a72b1d7a9`. The automated receipt records the app release. The
collector entry was seeded only after an independent live digest/health check,
so it is not falsely labelled with the newer app revision.

The unsafe waiting verifier PID3926997 was stopped only after checking its
exact command and that it had not begun a restore. Existing prune PID3829220
and the backup timer were left intact. Replacement systemd unit
`spreadboard-restore-verification.service`, PID3993389 at startup, is active
with status `waiting_for_existing_prune`; six-hour wait, six-hour restore,
fourteen-hour service maximum, private output directory and sanitized logs.
**Active/Result=success is not completed restore proof.** Check its final
exit and `status=verified` evidence, then the next two ordinary timer results.

Rollback: prefer a new targeted revert, not the old handout's hard-coded
marker write. For display rollback, revert the four CSS additions only and
run the protected app-only deployment. Preserve the safer backup verifier,
retained funding history and private accounting. Always verify source digests
and the per-service receipt after any rollback.

Additional bounded checks before release:

- Read-only SQLite aggregate independently reconfirmed v5: 115,747 observations,
  60,460 24-hour outcomes, 4,874 routes, 29.2083 labelled days and **zero**
  exact-cost outcomes. This deliberately did not repeat the heavy feature
  leakage/baseline scan while workers were busy; those remain Claude-reported.
- Existing soak summary at23:57UTC: only34.42minutes on the latest generation,
  no observed restart/OOM, no sampled endpoint failure, six priced samples,
  therefore neither the one-hour route gate nor48h gate passes. Web anonymous
  peak3025.6MiB and collector3275.4MiB do not justify reducing memory limits.
- The affiliate pack also needs its product description reconciled with
  optional encrypted read-only account connections before sending; its blanket
  claim that the product never connects to customer exchanges is too broad.
  The commercial examples and existing draft files are not a launch clearance.

## Post-release browser acceptance — September13 00:03–00:04UTC

- Authenticated real Chromium, production HTTPS, no application responses
  mocked. A listener observed an actual SSE event; diagnostics also invoked
  the application's own structural-refresh event and awaited completion.
- Futures–Spot global30d: HTTP200 in2.735s; 100/100 summary numbers readable
  initially, after refresh and expanded at1440,1280,390px. No page overflow.
- GlobalNow: HTTP200 in11.301s; 92/92 numbers readable initially and after
  refresh. One actual board event observed; zero historical-cell mutations.
- Global1d: HTTP200 in1.861s; global7d: HTTP200 in1.162s. Both initial and
  refreshed cells retain one live estimate plus three clearly settled windows;
  no NaN/undefined/Infinity and no clipped values.
- No browser page errors or refresh notices. Both baseline and final audit
  identities/sessions deleted; quick_check=ok and foreign-key violations=0.
- Screenshots visually inspected, not merely captured. Final evidence:
  `output/review-2026-09-13/post-release/browser-evidence.json` and
  `funding-1440.png`, `funding-1280.png`, `funding-390.png` under the project
  root. The baseline is in the parent evidence directory for comparison.
- This closes the reported numeric clipping and this four-filter browser
  regression matrix. It does **not** certify all historical figures at every
  venue, full subscriber journeys, sub-second Now navigation, 48h stability,
  two ordinary backup successes, or production restore completion.
