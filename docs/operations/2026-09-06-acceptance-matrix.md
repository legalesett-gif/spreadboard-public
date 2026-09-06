# SpreadBoard acceptance matrix

Checkpoint: 2026-09-06 02:38 UTC. Goal remains open.
Live source is **b42e595 / bc130c63e6b760de**. The tested **fb814c3** candidate includes exact fallback-direction, zero-ranking, and bounded string-sharing fixes but is **not deployed**. Discovery PID407100 is running; both discovery and finalization must be idle before deployment. No force, trades, messages, spend, cap increases, or alert reactivation.

| Requirement | Evidence inspected | Verdict / remaining work |
|---|---|---|
| Remove Ourbit, including restored data | `funding-snapshot-arithmetic.json`: 222,417 routes, zero Ourbit entries; persisted-reader regression rejects disabled venues | Verified for the audited generation; retain admission/restoration tests |
| Avoid duplicate market/route inflation | `catalogue-counts-0005.json`: zero duplicate venue/type/symbol market keys. Whole funding snapshot: zero duplicate exact directed leg pairs or self-pairs | Verified for captured data. Route permutations are not token counts |
| Funding arithmetic agrees with printed legs | All 222,417 stored routes independently recomputed from leg rates and intervals without mismatches | Verified for snapshot arithmetic, not every native venue or executable return |
| Exact route identity through fallback paths | Fresh CAP probe exposed automatic reversal in legacy paths; seven regression cases cover ingestion, compact updates and zero ranking | Candidate fix passed; deployment and repeat production probe pending |
| Correct filters, counts, pagination and retained alternatives | Full regressions plus authenticated CAP/OPENAI UI checks; strict volume filter returned zero without broad-cache fallback | Verified for tested cases; final-release browser recheck pending |
| Hyperliquid io:OAI reaches the board | Authenticated OPENAI group included Bitget→Hyperliquid with current indicative spread; native index/identity guards retained | Previously observed; final-release exact route recheck pending |
| Compare against UA in both directions | 01:16 guest futures:15/15 fresh exact counterparts. 01:17 spot/futures:9 supported counterparts,2 Ourbit exclusions,4 Binance Alpha outside configured coverage. Reverse Kraken leaders checked against native data; UA guest UI marks Kraken premium | Sampled comparison explained. Not exhaustive premium parity; no bypass or new exchange scope |
| Recovery after unhealthy state | Prior drill ledger: watchdog restart00:32:34, health/free20000:34:28, safety fallback cancelled unused; persisted-state cap regressions retained | Prior drill completed. Do not repeat inside clean soak |
| Durable restart cap and startup protection | Current full suite includes watchdog/state-roundtrip regressions and earlier mutant evidence | Regression verified; operational state continues to be observed |
| Stable priced coverage | b42e595 post-startup samples197272,197308,197680,197414 over several index generations | Encouraging, but not30samples over an uninterrupted final hour |
| Measured memory and CPU, safe caps | b42e595 sampled app anon peak~3048MiB/cgroup~3417MiB; collector anon~2839MiB/cgroup~3865MiB. Sampled string-sharing experiment saves17.35% traced retention with equal data | Lower caps not justified yet. Measure ordinary final-release reloads and CPU; do not extrapolate sampled savings |
| Host memory limits below physical RAM | Current caps remain3584/4096/768/192MiB. Target3072/3584/512/192MiB requires headroom proof | Not complete |
| No OOM and endpoint/health durability48h | Current finite observer active; captured segment healthy with no OOM/restarts, but contains deployments and diagnostic children. `/free` varies~3.9–20.3s | Clean48h final-release window and kernel OOM evidence missing |
| Backup manual recovery plus two normal timer successes | Latest normal run00:19:49→01:15:36 succeeded, including retention and check;46 Drive quota errors retried. Next timer freshly scheduled06:20:56UTC | Do not count earlier successes across a later failure as future reliability. Further normal firings pending |
| Deployment gates/source parity | Candidate full suite2671passed140.33s; Ruff no new findings,516 remaining against unchanged517 baseline; behavioral mutants fail | Tests complete, shipping not complete. Repeat both protected-worker guards and verify both container digests |

## Counts answer

At00:05UTC, configured coverage contained **1,987 futures token labels / 9,733 futures markets** and **3,962 spot labels / 12,484 spot markets**. The user's UA figures were2,534/4,370 token counts, with different exchanges and naming. These are not comparable to the hundreds of thousands of directed venue pairs. At02:09, the funding file had5,319 token blocks but only1,061 nonempty blocks and222,417 unique directed pairs. The published page filters and ranks those alternatives; keeping an alternative available does not require displaying all of them.

## Next sequence

1. Poll the same discovery PID and finalizer state without interruption. Do not infer completion from elapsed time.
2. Deploy the unchanged tested candidate only once both guards clear, using `output/stability-20260906/deploy_with_final_guard.sh app collector`; confirm health and source digests.
3. Repeat the exact fallback-direction probe and browser checks; measure ordinary reload memory/CPU. Diagnostic children are excluded from ordinary-load acceptance.
4. Let the existing finite observer finish (about03:07UTC), without duplication. Arrange the final uninterrupted sampling window with30priced samples/hour and5-minute endpoint cadence48h only after release/caps are settled.
5. Verify subsequent normal backup timer outcomes. Close the goal only with complete evidence; recommend capacity honestly if the prescribed no-spend box cannot meet acceptance.

Detailed chronology and raw evidence: `docs/operations/2026-09-05-stability-review.md`, `docs/operations/2026-09-05-ua-comparison.md`, and `output/stability-20260906/`. This matrix is an evidence map, not a completion claim or live-entry authorization.
