# Websocket selection cache cleanup

Candidate, 2026-09-06. Not deployed.

## Problem and change

The websocket worker queries ten board lanes to choose subscription legs. Its query caches then retain the parsed route universe between selections, although sockets need only the selected venue/type/symbol keys. A copied public production snapshot with30,504 rows retained107,822,100bytes solely through the row cache after the query returned.

`api_spreads.release_query_caches` clears row and response caches under their existing lock. A selection-thread wrapper calls it in finally after the entire `_desired_legs` batch. All lanes retain cache reuse during selection; the selection algorithm, saved-position priority,160subscription limit,300s refresh floor and exact leg keys are unchanged. Website processes do not call this function and retain their own query caches. Selected output is not mutated. No exchange market catalogue or order-book cache is pruned.

## Evidence and limits

- Offline copied snapshot:30,504 rows, one cache entry. Before cleanup107,936,254 retained bytes; after114,154; released107,822,100. Peak231,105,479;66.54seconds under tracemalloc. No sidecars/live books/network/account requests. `selection-row-cache-release-api.json`.
- Initial baseline manual cache release produced the exact same107,822,100byte difference. `selection-row-cache-retention.json`.
-11focused tests passed2.62s. New tests call the actual worker selection method, assert object release after successful/failed selection, exact retained legs, unchanged refresh floor and no accepted signature on failure. Bypassing cleanup fails both new tests (exit1,2failed), `selection-retention-mutant.txt`.
- Ruff exit0, no new findings,516known against unchanged517baseline. Full suite terminal exit0:2,673passed in150.14s, `selection-retention-full.txt`. Test-generated data changes were restored after completion; candidate source was unchanged after the gates.

Review: cleanup is after the whole batch, not per lane; clears references rather than changing returned objects; runs in the background selection thread; finally handles failures. The fixed107.8MB allocation result does not prove Linux RSS/cgroup savings or sufficient headroom for lower caps. Allocator retention, repeated-selection CPU/peaks and full normal-worker coverage must be observed after deployment. Catalogue sharing remains a separate measured but unimplemented optimization, with dynamic-listing compatibility requirements.

Production source remains fb814c3 /3d0f3437aa390c87. Sole observer434080 was freshly active and both protected-worker checks were clear. Preserve observation through its terminal state before deploying; recheck both guards immediately before any later recreation. No cap changes or deployment queued.

### Sole selection release waiter — 2026-09-06 04:42 UTC

One bounded local waiter is ACTIVE: tool session7455, `output/stability-20260906/wait_selection_release.py`, started04:41:36UTC with a2h bound. It holds the existing guarded-release-wait.lock, so no second release waiter can run. Fresh poll recorded observer_running PID434080. Do not restart/duplicate or edit candidate source while it waits. Previous waiter59028/session78374 remains terminal and is not reused.

The waiter pins source digest74ceb35494a9c6d4 (candidate2a8a03b) and the deploy helper hash. It requires observer inactive/MainPID0/Resultsuccess AND a finished log marker, freezes and analyzes the complete baseline, requires one-hour coverage/no issue flags, checks both protected workers twice before calling the helper, which checks both again immediately before recreation. Failed/unknown observer completion stops without deployment; transient read errors retry within the bound. Seven completion predicate checks passed. Events `selection-release-wait.jsonl`; future deployment output `selection-release-deploy.txt`. No deployment has occurred at this checkpoint. Current live sourcefb814c3/digest3d0f3437aa390c87. Recurring automation remainsPAUSED.

Next action: poll the SAME session7455, preserve observer434080 to terminal (~05:09UTC), inspect actual deploy exit/digests/endpoints if it ships, then measure normal candidate RAM/CPU/coverage. Do not treat waiter timeout as deployment or success. Goal remains active; safe caps, subsequent backups andfinal48h remain outstanding.
