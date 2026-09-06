# Same-key projection revalidation

Local candidate, not deployed. Production remainsf047ccf while its sole ordinary observer499780 runs.

## Reproduced problem

A20second-old materialized live-query projection, with a10second short TTL, was discarded if the request key was unchanged. Changing only the fast-quote signature reused the identical payload with exact_generationfalse, allowing current-value overlay and background revalidation. This is a cache-path inconsistency; it is not proof that the specific31.933second production request was caused by it. Evidence `exact-key-expiry-before.json` and `probe_exact_key_cache_expiry.py`, local with no network.

## Change and constraints

Keep the completed projection within the existing900second structural TTL when the short projection TTL expires. Treat it as non-current, so foreground requests use the existing compatible fallback path and request a background refresh. Background lookup still returns a miss, but no longer destroys the fallback needed by concurrent foreground requests. The normal cache-count bound remains unchanged. Empty projections are not reused as fallbacks; changed structural/metadata/rail/query signatures remain incompatible. Serving never advances the write timestamp. `_sync_telegram_client_universe` still applies current spread/funding freshness before returning, and a completed background build replaces old membership.

No quote-age, identity, settlement,95%accuracy, subscription or chart-refresh constraint changes. Explicit no-cache requests retain their behaviour. Preserving one existing object during rebuild can affect peak retention; measured HTTP latency and ordinary memory still require after-release evidence. No claim of faster production pages yet.

## Verification

23focused tests passed3.44s. Against the original lookup function, the two new foreground cases fail and three safety/rebuild cases pass (exit1,2failed/3passed,3.55s). The background replacement case confirms new ETH membership replaces old BTC membership under the same key. Empty/expired fallback guards remain in force. Local after-probe reuses both identical-key and changed-price-key cases and marks both for revalidation. Ruff has no new findings,516against unchanged517baseline. Final full suite completed exit0:2,682passed in139.30seconds, `same-key-full-final.txt`. Test-generated tracked data was restored, and no product source changed after the gates.

An initial original-source test run hung because a failed case left an in-flight marker while the test clock was frozen. That local process was stopped, fixture isolation corrected, and the final original-source run terminated with the expected failures. Production was untouched.

## Deployment boundary

Do not deploy during the current ordinary memory observation, and do not duplicate observer499780. No deployment waiter is queued. Review the completed normal-load comparison and safe-cap decision before another release. Candidate source changes require fresh full gates; observed cache-decision correctness must not be reported as delivered latency improvement.
