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
| Funding browser | Numeric clipping reproduced and repaired in source; live post-release acceptance is recorded below. Global Now latency remains a separate gate until measured passing. Four actual initial/update filter paths must be checked, not inferred from tests. |
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
