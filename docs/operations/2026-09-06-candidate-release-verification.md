# Candidate release verification

Prepared 2026-09-06 06:41 UTC. This is a runbook, not release or acceptance evidence.

The tested candidate is `f260666`, source/data digest `c8b10e2fa4585bcd`. Production remains `f047ccf`. Full suite: 2,708 passed; Ruff: no new findings against unchanged 517 baseline. The frozen local manifest in `output/stability-20260906/candidate-release-manifest.json` also hashes build inputs and the guarded helper, which the Python source digest does not cover. Recheck these before release; source changes invalidate the recorded gates.

## Before release

1. Preserve `spreadboard-stability-combined-20260906.service` (PID 499780), expected finish about 07:12:50 UTC. Require terminal state, successful exit and the log's `finished` marker before replacing this observation. Freeze the complete JSONL and analyze it locally. Do not launch a second observer or restart after a timeout.
2. Keep its failed coverage baseline: generation 5 fell from about 202,000 priced routes to 64,083 before recovery. Empty `issues` does not override `coverage.within_10pct=false`. A corrective release may proceed with this explicitly failed baseline after review; never falsify it to satisfy an old waiter.
3. The normal backup formerly PID 532835 is now terminal FAILED (06:21:16–06:49:00 UTC, exit 1). Snapshot b691030d was saved, but forget/prune failed opening the rclone repository after Drive quota errors; this is not a successful full backup. Inspect this failure before any backup changes. Check the same normal backup unit. `Result=success` while activating is not completion. Preserve it; do not manually trigger another backup. Record terminal start/exit timestamps and exit code when available.
4. Keep current limits for this corrective release. Collector anonymous memory already exceeded the proposed 3.5 GiB limit. Synthetic serialization savings do not establish production headroom. The aggregate cap budget remains unresolved.
5. Check both protected discovery/finalizer gates immediately before deployment. Use the existing guarded helper, which checks again immediately before recreation. Never force, weaken the guard or reuse old waiters with different source pins. A refusal means wait and inspect, not restart the worker.

## After the corrective release

| Check | Required evidence | Failure action |
| --- | --- | --- |
| Source parity | Candidate source/data digest matches both app and collector; build input hashes match manifest | Do not report release success; resolve mismatch |
| Serving | Health and public pages respond; new container IDs/start times recorded | Diagnose deployment before claiming acceptance |
| Coverage | At least a full hour spanning multiple structural generations; priced routes stay within the existing 10% gate; inspect every install dip and recovery | Keep acceptance failed; investigate actual expiry, refresh delay and install behavior |
| Funding UI | Same exact route's headline, leg rates, direction, cadence and Net edge update together; already-open calculator updates without resetting input | Retain screenshots/DOM and exact route/times; reopen correctness work |
| Funding semantics | Positive/negative/zero and unavailable/recovery handled; 24h/7d/30d settlement windows retain exact-history meaning | Do not replace missing history with projected current funding |
| Index publication | Ordinary publication advances; no missing/partial index or checksum errors; coverage survives publication | Inspect writer/reader failures; do not fabricate freshness |
| Memory and CPU | Ordinary index, discovery, navigation and websocket phases sampled; collect anon, total/cgroup, host available/swap, CPU, endpoint latency | Do not derive safe caps from allocation replay or unmatched phase maxima |
| Navigation pauses | Normal navigation completes and websocket resumes; established route quotes stay fresh during longer pauses | Investigate pause/cadence without removing safety guards |
| Backups | Successful terminal normal timer runs with timestamped evidence | Do not count in-progress state or prior successes across a failure |

Use one host-side observation, preserving the existing intervals (host 15 seconds; health 120 seconds for the first hour then 300 seconds; public page and backup 300 seconds). Do not launch diagnostic Python children inside production during this observation. A two-hour observation is intermediate evidence, not the final 48-hour gate.

For UI comparison, refresh native public venue data at the time of the check. The earlier ICX sign flip is a useful regression route, not a prediction that the sign will flip again. Automated behavioral tests cover sign flip, zero, expiry/recovery and open-calculator updates; a static live sample cannot prove all transitions.

UA comparison remains guest-sample scope. Explain exact route exclusions: Ourbit and retired route families are intentional; Bitget PURRSTOCK versus native Hyperliquid PURR is a stock/token identity mismatch. Preserve supported alternatives with positive spread OR positive funding. Do not compare directed route counts to unique token counts or claim exhaustive UA coverage.

## Final acceptance still outstanding

Choose a defensible memory budget from post-fix ordinary-load evidence, then validate the final deployed source and limits for 48 hours. Require no OOM, no unhealthy period over 90 seconds, endpoint checks every five minutes and two subsequent successful normal backup firings. Keep the goal active until these and the correctness/coverage checks pass. Recurring Codex automation stays paused; no trades or messages are authorized by this runbook.
