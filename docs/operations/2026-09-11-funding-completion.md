# SpreadBoard completion record

Owner: Codex in the current chat. There is no external Claude handoff. The older handover remains a linked chronology; this checklist controls the continuation.

## Completed acceptance checks

- Production funding controls omit HTX, Ourbit, CoinEx and Phemex. CoinEx/Phemex prices remain enabled.
- Live ONG exclusion test removed Gate and WhiteBIT from either leg, reduced 56 exact pairs to 42, changed the leader, and preserved exclusions across period/farm navigation. Export displayed “Current JSON downloaded.”
- Expanded funding rows retain their own rates, schedules and totals. Binance, BingX and XT hourly schedules were observed. Missing settlement windows explain whether coverage or refresh is pending.
- Hyperliquid funding results include ordinary and builder markets, including BRENTOIL. The OPENAI Markets group contains Hyperliquid io:OAI routes with explicit unresolved identity/oracle/trading-hours disclosures. Those disclosures must not be removed merely to promote a tokenized instrument into Funding rankings.
- Production registration copy describes Telegram linking, seven days, no payment and automatic expiry. A local HTTP integration check exercised the same session before linking (402), during Research Pro trial (200), and after expiry (402) across Funding, Charts, Intel and Playbook. No real Telegram message or live test identity was used.
- Ordinary production collection created the settlement ledger; successive checks grew from 21,851 events/74 contracts to 124,821 events/456 contracts. At the first completed history cycle, current 24h/7d/30d coverage was 37.30%/35.95%/30.85%; this is progress, not completeness.

## Additional fixes in this continuation

- Exact settlement collection has an independent bounded worker and durable priority queue, so the hourly refresh need not wait for full route ranking or materialization. A process lock prevents overlapping aggregate writers; failed launches retry.
- BitMart’s [documented history API](https://developer-pro.bitmart.com/en/futuresv2/#get-funding-rate-history) exposes the latest 100 settlements. Native history now uses the event ledger too, accumulating overlapping responses; periods absent from the provider cannot be invented.
- A timestamp index prunes expired events globally, including inactive/retired contracts.
- The backup repository had a 63-hour stale exclusive lock with no live restic owner. Normal stale-only unlock succeeded; no force-unlock was used. Backup retries now handle that condition.
- Sep8 backup history showed upload success after 1h51m followed by timeout during integrity checking. The service deadline becomes four hours, below its six-hour schedule; integrity checks remain required.
- Repository-index cache persists between runs and is excluded from snapshots. SQLite backup handles close before uploads.

## Release and observation

Commit e901d49 is deployed to app and collector; both source digests verified as 63ebb53a5a833b53, deployment exit 0. Final frozen-source suite: 2,875 passed in 202.50s. Ruff has no new findings (502 known); seven deliberate fault mutations were detected. The backup unit passed systemd validation and is installed with the four-hour deadline; the active upload was not interrupted. The current offsite backup upload is active and encountering shared Google Drive project-quota retries; do not call it successful until upload, retention and integrity checking exit successfully.

The remaining duration-dependent gates are a current-release 30-sample hour with priced-route counts within ±10%, ordinary memory/CPU measurements before any smaller caps, a clean 48h endpoint/cgroup observation, and two normal successful backup timer firings. Keep these as open checks owned here. No new spend, trade, Telegram publication, or recurring Codex automation is authorized by this record.


## Observation checkpoint, 2026-09-11 00:29 UTC

- Finite read-only observer `spreadboard-funding-acceptance-20260911.service` began at 00:23:14 UTC for 48h. Output: `/opt/spreadboard/runtime/stability/20260911-funding-acceptance/samples.jsonl`. This is acceptance instrumentation, not a reactivated recurring Codex task. Do not duplicate it.
- New-release endpoint samples return 200; containers are healthy with no restart/OOM increments. The sample is still too short for stability acceptance or cap reduction.
- SQLite reached 329,669 events across 15 venues, including 589 native BitMart events across eight contracts. History publication advanced independently to 00:27:44 UTC. Hourly-boundary catch-up is incomplete.
- Fresh Hyperliquid client indexes 821 symbols and active `IO-OAI/USDC:USDC`. Its prior stored `symbol_not_indexed` classification was still excluding it from the demand queue. A bounded hourly retry for missing/empty/paused classifications is being verified before the next release; no special OAI override.


## Classification correction deployed, September 11 00:37 UTC

- Commit `ff1f01a`, source digest `60ba0e6d0e7680b8`, verified in both app and collector. Guarded deployment exit 0, health 200, no restart-count increment or OOM flag.
- Missing-market, empty-history and paused-market classifications expire for requested legs after one hour. Repeat failures stay blank and do not consume every five-minute demand pass. Full suite: 2,878 passed in 170.53s; no new Ruff findings. Removing this branch caused all three call-site regressions to fail.
- Independent ledger arithmetic audit compared 2,777 recently published historical windows across all 15 collected venues, with zero mismatches and zero boundary/unmerged skips. This validates aggregate arithmetic against stored events; native API semantics and completeness are separate checks, and historical catch-up remains incomplete.
- The same finite observer was restarted at 00:37:35 UTC after deployment to begin a clean 48h window and fresh two-minute first-hour samples. Old evidence is retained in the append-only file. No duplicate observer or recurring automation was created.
- Analysis helper: `output/continuation-20260911/summarize_acceptance.py`. It separates container generations, refuses an hour pass before 30 samples, reports memory/CPU/OOM evidence, and counts only completed successful backup exits. Normal timer firings still require checking their start times.
