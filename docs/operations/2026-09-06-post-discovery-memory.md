# Post-discovery publication memory exclusion

Candidate extension to selection-cache cleanup, 2026-09-06. Not deployed.

## Evidence

The ordinary baseline's collector anonymous-memory peak was4,053,626,880bytes. The sampled process set contained a959,232KiB websocket worker and a1,877,432KiB live-route-index worker. Fresh production logs place discovery completion at03:52:39.835UTC and index publication at03:56:28.464UTC; the peak at03:56:04.823 falls inside that post-discovery index build. Source inspection confirms that `RefreshLoop.refresh_once` held the heavy-work lock but did not pause the websocket worker for its publication batch, while the ordinary index publisher already did so.

Evidence files: `release-collector-anon-peak-0100.json`, `peak-publication-events.json`, `memory-gap-and-slow-request.json`. The99minute copied baseline has391host samples/38coverage samples/9generations,198399–200503priced routes, no sampledOOM/restart/unhealthy or endpoint failures. `/free` reached31.933seconds at04:34:02UTC despite200. Nearby MemAvailable was2.2–2.8millionKiB; this is not evidence that low host free memory caused that slow request. Latency remains separately unresolved.

Fresh host MemTotal8,326,938,624bytes versus declared limits9,059,696,640bytes. Collector proposed3.5GiB is295,530,496bytes below observed anon peak. Even a hypothetical full107,822,100byte cleanup saving leaves187,708,396bytes above that limit before headroom. App's proposed3GiB has only57,024,512bytes above its sampled anon peak before kernel/allocator margin. No lower caps are justified by this arithmetic, and local savings must not be treated as additive production evidence.

## Change

Wrap the post-discovery heavy publication batch in a websocket-pause context inside the existing heavy-work lock. This covers enrichment, publication and index/funding/materialized products, with resume in finally on success, early return or exception. The network-bound discovery itself remains unpaused and outside the heavy lock. Bulk quote workers keep running, as in the existing ordinary publisher's pause policy. This changes no route membership, price/funding guards, subscription limits or chart refresh settings.

## Verification and release boundary

54focused tests passed2.26s. Four new actual refresh_once cases cover success, missing enrichment, missing publication and index failure; all fail when the pause context is replaced with a no-op. Ruff no new516against517baseline, exit0. Full suite terminal exit0:2,677passed in146.44s. No candidate source edits after gates; test-generated tracked data restored. All6new regression cases across both memory changes fail their respective bypass mutants.

Prior candidate-only waiter PID89383/tool session7455 was deliberately terminated before source edits and confirmed exit143. No deployment occurred. Do not restart that stale candidate waiter. Production observer434080 was not stopped. A combined release may be queued only after new full gates, with a new pinned digest and the existing singleton lock; preserve the observer to terminal and require both worker guards before recreation. More memory savings are not claimed until measured under normal production load.
