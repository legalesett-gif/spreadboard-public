# SpreadBoard continuation for Claude

Checkpoint: 2026-09-06 02:49 UTC. The goal remains active. This is a continuation handover, not a completion claim.

## Start here

- Work only in `/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-collector-retention`, branch `codex/collector-retention-20260906`.
- Read `docs/operations/2026-09-06-acceptance-matrix.md` for verified evidence and remaining gates. Detailed chronology is in `docs/operations/2026-09-05-stability-review.md` and `2026-09-05-ua-comparison.md`.
- Read root `REMINDERS.md`, vault current state and project rules before mutations. Refresh volatile state; historical checkpoints are not live evidence.
- The separate `tmp/spreadboard-funding-current-truth` checkout is not the release checkout. Preserve unrelated work there.
- SSH: `ssh -i ~/.ssh/spreadboard_digitalocean root@178.128.126.204`. Server source `/opt/spreadboard/app`; runtime `/opt/spreadboard/runtime`; container Python `/app/.venv/bin/python`.

## One deployment waiter is already running

**Do not start another waiter or manually deploy while this one is active. Do not change source while it waits.**

Local PID `59028`, Codex tool session `78374`, started 02:41:04 UTC. It waits at most two hours for protected workers; it is not a recurring automation. Observe PID and `output/stability-20260906/guarded-release-wait.jsonl`. Deployment output will be `held-release-deploy.txt` in the same directory.

It checks the expected tested source digest `3d0f3437aa390c87`, polls both protected workers every 55 seconds, and invokes `output/stability-20260906/deploy_with_final_guard.sh app collector` once when clear. The helper checks both workers again immediately before recreation. Nonzero completion requires inspecting the event and deployment logs; never retry blindly. A tool observation timeout does not mean the process stopped.

Last fresh check, approximately 02:47 UTC: discovery PID `407100` alive, elapsed 25:58; app and collector healthy, restarts zero, OOMKilled false; finite observer active. No deployment had started. Scan duration is not permission to interrupt it.

Live source at the last verified digest check: **b42e595 / bc130c63e6b760de**, started 02:02:29 UTC. Tested candidate: **fb814c3**, including **d216037**. Documentation commits may be newer than the source commit.

## What the candidate changes

1. Preserve the exact printed legs in legacy fallback ingestion and compact quote updates. A fresh CAP sample was HTX futures long / Kraken spot short with positive carry, but legacy code reversed its legs and carry sign. Current candidate removes automatic mirroring and computes short funding minus long funding. Spot short inventory/borrow prerequisites remain explicit.
2. Rank zero funding above negative funding; avoid treating zero as missing.
3. Share repeated immutable strings within each streamed route-index generation using a bounded 32,768-entry pool. Mutable dictionaries remain independent; unique route IDs and full validation remain unchanged.

Latest combined gates: **2,671 tests passed in 140.33 seconds, exit 0**; Ruff no new findings, 516 remaining against unchanged 517 baseline. Behavioral regressions fail original-source mutants. No source changes since these gates.

The 10,000-route allocation experiment reduced traced retained allocation by 17.35% with identical serialized SHA256. This is a bounded sample, not proven production RAM savings. Measure ordinary reloads after deployment before changing caps.

## What is already verified

- Ourbit excluded from audited admission/restoration and the complete captured funding generation.
- At 00:05 UTC: **1,987 futures token labels / 9,733 markets**, **3,962 spot token labels / 12,484 markets**. Zero duplicate exact market keys. The user's UA counts (2,534 futures / 4,370 spot) count tokens, not directed venue combinations; exchange sets and naming differ.
- Complete funding generation at 02:09: **222,417 directed pairs**, zero exact duplicate pairs, self-pairs or Ourbit entries, and zero independently recomputed funding arithmetic mismatches. This proves that snapshot's arithmetic and exact-key uniqueness, not every native identity or executable return.
- UA guest futures sample 01:16: 15/15 fresh exact counterparts; 14 in saved cache, one fresh-only at probe. Spot/futures sample 01:17: nine supported counterparts, two Ourbit exclusions, four Binance Alpha legs outside configured coverage. Samples overlap other checks; do not sum them.
- Reverse comparison: our Kraken leaders were not guest UA winners; UA explicitly marked Kraken as premium. Do not claim absence from the whole competitor site, bypass access or add exchanges. Reference rates were not simultaneous executions.
- Deployed filters/counts preserve final identity guards and pagination totals. OPENAI zero eligible funding pairs was correct for mirage-guarded rows; research routes remain labeled. Actual io:OAI route was observed, but recheck on final release.
- Live b42e595 streams funding-cache restoration. A bounded full CAP render completed under 768 MiB after earlier bounded decode failures; cold 20.432s versus warm 1.360s. Cold restoration and broad-page latency remain follow-ups.

## Finish in this order

1. Observe the existing waiter until terminal. On success verify both running source digests, health and the actual deployed direction probe:
   `/app/.venv/bin/python /app/runtime/stability/20260905-coverage/probe_fallback_direction.py` inside `app-app-1` with workdir `/app`. Recheck printed legs/sign using current data.
2. Recheck authenticated CAP/OPENAI Spreads and Funding pages, exact filters and sidebar, and io:OAI identity. Distinguish current projections from complete settled history.
3. Measure normal index reloads, CPU, anon memory, row/book coverage and cgroup/kernel OOM counters. Current caps 3584/4096/768/192 MiB remain; proposed lower caps are not justified by current peaks.
4. The sole finite observer `spreadboard-stability-counts-20260906.service`, PID 364378, ends around 03:07:19 UTC. Do not duplicate it. Its cadence crossed deployments, so it does not prove 30 priced samples over the final release's first hour. After it actually terminates, arrange an uninterrupted final window with 30 samples/hour and five-minute endpoint observations for 48 hours only once release/cap decisions are settled.
5. Latest normal backup succeeded 00:19:49–01:15:36, snapshot e46b3d4c, after 46 rate-limit retries. Next timer was freshly 06:20:56 UTC. More normal green firings are required; prior failures break consecutive reliability evidence. Do not run another manual backup during ordinary-load acceptance without a reason.
6. Keep the goal open until all original acceptance gates are evidenced. No forced recovery repeat inside a clean soak; that drill and durable restart-cap regressions already have evidence.

## Boundaries

No trades, borrowing, repayment, transfer, conversion, withdrawal, messages, spend, cap/subscription increases, weaker freshness/identity/95% accuracy/settlement gates, or force deployment. Ourbit remains excluded. Public relevance is positive spread OR positive funding, with needed alternatives and history preserved.

The recurring `finish-spreadboard-stability-acceptance` automation remains **PAUSED**. A continuation is not authorization to reenable it. Do not duplicate workers, observers or deployment waiters. No subagents. Never print secrets or complete process arguments/configuration.

If source changes become necessary after the current waiter is terminal, rerun the full gates with actual exit codes before any later deployment:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests/ -q
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --with ruff python scripts/ruff_ratchet.py
```

Raw evidence is in `output/stability-20260906/`. The previous handover is archived as `2026-09-05-stability-cardinality-history.md`; its operational directions are superseded.
