## Catalogue memory investigation — 2026-09-06 04:29 UTC

Local public CCXT paired-client experiments completed successfully; no application source, production configuration or limits changed. Sharing a loaded catalogue with the second client BEFORE its load avoided duplicate retained allocations:

| Venue | Independent retained bytes | Shared retained bytes | Reduction | Markets per client |
|---|---:|---:|---:|---:|
| Gate | 135,790,373 | 67,941,390 | 67,848,983 (49.97%) | 6,579 |
| Bybit | 56,943,048 | 29,223,666 | 27,719,382 (48.68%) | 3,703 |

Both clients retained their spot/swap defaultType settings and identical symbol sets within each run. These are fresh public loads in separate local processes, not frozen identical responses or whole-worker/production RSS savings. Earlier independent Gate comparisons differed only in limits (examples show moving price bounds), not symbol/type/contractSize/precision. Earlier Bybit had 3,699 markets versus 3,703 later: catalogue coverage can change during operation and must not be frozen indefinitely.

Evidence: `output/stability-20260906/ccxt-catalog-preload-comparison.json`, `ccxt-*-preload-*.json`, `ccxt-gate-sharing-detail.jsonl`, and the two `profile_ccxt_catalog_*.py` scripts. All local profile processes ended exit0. Sharing AFTER both clients already loaded retained extra memory because a completed async markets_loading task may still hold the original map; use the supported set_markets_from_exchange API before the second load, not undocumented task surgery.

No fix is implemented yet. Before implementation, prove same adapter/configuration compatibility; serialize the initial shared load without merging book connections; preserve marketHelperProps and contract-size conversion; handle unknown newly selected symbols and catalogue refresh; test concurrent initial calls, failed load/retry, reset, and new listings. Do not generalize Gate/Bybit evidence to every venue. Keep all current 160-subscription, freshness and identity constraints.

A competing source remains unmeasured: `_board_legs` runs ten board queries; `api_spreads._ROW_CACHE` retains the parsed route universe with a 900s TTL, while selection can repeat every 300s. `_desired_legs_cached` calls gc.collect but does not evict referenced rows. This is a source-level retention hypothesis, not a measured leak. Profile that cache locally before deciding whether catalogue sharing is enough; indiscriminate eviction can increase reparsing CPU/peak memory. Do not load profiling children on production during the sole ordinary observer.

Fresh copied observation: 311 host samples over 4,719.86s; 34 coverage samples over 4,515.43s; 198,399–199,902 priced routes over six generations. No sampled OOM/restart/unhealthy, endpoint failure or cadence-gap flags. Collector anon peak remains 4,053,626,880 bytes, above the proposed 3.5GiB cap. App anon peak now 3,164,200,960 bytes. `/free` maximum 24.025s and health maximum 10.018s: all200 is not fast latency. Frozen evidence `release-observer-0120.jsonl` and `release-observer-analysis-0120.json` (approximately79min, filename is a checkpoint label).

Sole observer PID434080 was freshly active; scheduled end05:08:53UTC. Do not duplicate/restart it. Live source remains fb814c3 / 3d0f3437aa390c87, no deployment queued; recurring automation remains paused. Goal remains active: memory/cap work, final48h and further normal backup firings remain outstanding.
