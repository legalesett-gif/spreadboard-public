# SpreadBoard funding and trial continuation

## Work location and safety

Use `/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-exchanges-trial`, branch `codex/exchanges-funding-trial-20260910`. The root project and older clones have unrelated changes. Do not reset/clean them or deploy an older clone.

The parent source is the exact Sep10 deployed web revision. Merge `29d3cf9` incorporates the earlier stability/retention source while preserving recent Portfolio work. Feature commit `f95cce6` deployed to both web and collector as source digest `745eddbf3b028a1e`; follow-up commit `d349e95` is now deployed to both services with source digest `4c532517549fed61`.

No trades, borrowing, repayment, conversion, transfer or withdrawal. No Telegram channel messages. Status Pushover remains off. The old scheduled stability task remains paused; do not restart it. No raised RAM limits, subscription caps, or weakened accuracy gate.

## Operator decisions

- HTX/Ourbit excluded from opportunities generally.
- CoinEx/Phemex excluded from funding only; retain price/spread coverage.
- Exchange exclusions affect either leg, before funding ranking/pagination, including restored cached views.
- Seven-day full website trial requires verified private Telegram linking; one immutable claim per normalized email and Telegram identity. Paid access is not replaced. New account registration alone does not activate a trial.
- ONG is a regression case, not the scope. Correctness must hold across all venues/contracts.

## Implemented

Native current interval precedence, XT collection interval, unknown/invalid/nonfinite rejection from projections, no missing-leg-as-zero arithmetic, no stale discovery funding refill, consistent live group/row/leg snapshots, strict exact settlements, expiry at both new settlements and old-event window exit, bounded 32-day SQLite event retention with overlap/upsert, and fair demand-lane history rotation. Hyperliquid native builder collection remains separate from its incomplete base CCXT bulk call.

Trial claim and entitlement updates share the Telegram-link transaction. Claims survive unlink/deletion; unique constraints prevent concurrent duplicate claims. Registration leads to Account settings; registration/pricing/public teaser copy and trial status describe activation/expiry. Advertisement is draft-only in `docs/marketing/2026-09-11-seven-day-trial-telegram-draft.md`.

## Evidence

`docs/operations/2026-09-11-funding-correctness-audit.md` is the main report. `output/release-20260910/` contains test logs, six successful mutation detections, native ONG settlements, all-venue parser audits, Hyperliquid builder read, and deployment/health results. Never mistake an old snapshot for a fresh market reading.

The first accepted suite was 2860 passing tests and unchanged Ruff ratchet (502 known findings). Native ONG settled pair totals matched the old table, but current intervals were materially wrong. Both negative contract rates can produce positive pair carry. The final publication checks will be appended below.

## Continue with these outstanding items

1. Verify the latest source digest and deployment result below, then inspect fresh production health, funding rows, excluded venues, Hyperliquid, and exact-history coverage.
2. Historical coverage is not complete merely because all APIs were attempted. First post-deploy health showed 9390 attempted/classified legs but only ~33% current 24h and ~27% current 30d values. Histories need bounded native backfill; missing/stale cells must stay unavailable. Verify the new settlement ledger is actually being populated by the ordinary worker and that demanded profitable routes no longer starve.
3. Validate availability through cold start. First release rejected the old all-pair catalogue after venue-policy changes and waited behind the materialized worker. The follow-up keeps eligible alternatives from lossless v2 packed blocks, while still rejecting reduced legacy generations that could have discarded them. Validate both filter correctness and bounded memory.
4. Original 24/7 acceptance remains unfinished: memory/CPU measurement before tightening caps, earlier collector OOM, 48h clean health/route-count soak, backup repair and two normal successful timer firings. At Sep10 23:09 UTC backup service still reported exit-code/1; the root filesystem had 86 GB free. Do not equate fresh container OOM=false after recreation with a completed 48h OOM test.
5. The per-release rollback backup succeeded at `/opt/spreadboard/backups/release-20260910T231424Z-funding-trial` with a checked online SQLite copy, original image IDs and source archive. This does not prove the normal backup timer is repaired.
6. Do not advertise the trial until production access behavior is accepted. No live test account, live trial claim or Telegram message was created by this task.

## Required release procedure

Run unmasked full pytest and the existing Ruff ratchet; inspect actual exit codes. Preserve generated/runtime data and current identity registry. `scripts/deploy_production.sh app collector` checks discovery/finalization before build and again before recreation. Do not use `--force`. Avoid destroying other long-running structural rebuilds unnecessarily. Confirm both container source digests including baked data, then verify actual UI/API behavior; a health 200 alone is insufficient.

## Final deployment verification

- Follow-up deployment exited 0: health 200, app and collector source both `4c532517549fed61`, zero restarts and no OOM flags immediately after recreation. Full suite: **2864 passed**, Ruff ratchet unchanged at 502 known findings.
- Production browser recovered ONG with 56 exact pairs; group headline and Now cell agreed (+2.808% projected at that snapshot). Gate 30d remained unavailable with an explicit cadence-gap reason. This is evidence of truthful missing-data handling, not proof of complete coverage.
- Final health: 9400 catalogue legs, 3116 current 24h, 2992 current 7d, 2576 current 30d; 5 retryable errors. `catch_up_complete=false`.
- Fresh memory snapshot: app 1.79 GiB/3.5 GiB; collector 1.77 GiB/4 GiB. No limit changes.
- **Settlement ledger population remains unverified:** a fresh search in runtime and `/app` found no `funding_settlements.sqlite3` yet. Inspect ordinary history-worker scheduling and first successful write before claiming incremental production operation. Native BitMart history still bypasses the new CCXT incremental-store path; extend and test that separately.
- Final source worktree is clean before documentation-only closeout. Production exclusions and parser no-network behavior were covered by regression tests; complete post-release expanded-row, exchange-filter and Hyperliquid UI acceptance remains to be recorded.
