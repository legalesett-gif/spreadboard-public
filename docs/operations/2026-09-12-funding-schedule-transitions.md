# Funding schedule transition integrity

## Scope and diagnosis

User requested reliable verification of 1h/4h changes in both directions, not a token-specific repair. Traced current native schedules (`fast_quotes`), published funding observations (`bulk_quotes`), settlement acquisition/store, rolling validation/expiry/priority refresh (`venue_funding_history`), and historical fallback/catalogue consumers. No account/trading logic changes.

Fresh production on source61448a7 was healthy. Aster's public `fundingInfo` reported4h for ESPORTS/SIREN/龙虾, matching the live cache. A deterministic reproduction against deployed code proved that a4h→1h month retained4h expiry and could accept a missing hourly payment (197 actual vs180 expected). The prior native mixed-cadence fix only ran if the percentile check failed; that shortcut allowed false positives. Stored windows needing revalidation:52 1d,263 7d,220 30d at audit time.

## Repair and evidence rules

- Every mixed window validates all observed cadence runs, in either direction. Extra hourly events never compensate for a missing payment elsewhere. Require sustained regimes; isolated unexplained gaps fail closed.
- Expiry uses the latest regime, not the slowest historical percentile. Rolling oldest-event expiry still applies independently.
- Transition bridges require bounded exhaustive native history plus UTC alignment. Generic observed segments are explicitly labelled `observed_segmented_cadence`, not an official announced schedule.
- A slower current interval alone cannot extend a stored total. Extension requires an exhaustive read through the old due time plus compatible published current schedule. Faster live schedules still expire missing payments immediately. Assumed intervals cannot tighten historical expiry.
- Old percentile-based mixed caches require revalidation, including the last-complete fallback; enqueue refresh. Persist cadence version/segments/source-range proof with new aggregates.
- Malformed fundingInfo cannot become a trusted default. An Aster symbol missing from its current config is unresolved, not stale8h metadata. Current interval source survives publication.
- Live funding publication retains a bounded1000-event schedule-change journal: previous/current interval, detection time, prior observation, next funding time and source. `effective_at` is null: observation time is not proof of exact exchange effective time. It does not reconstruct changes before deployment or send notifications.

## Verification

Transition/missing-payment/expiry/cache/native-metadata/journal regression matrix added. Focused runs133,66 and44 tests passed; Ruff0 new (500 baseline). Full suite and production deployment pending at initial document creation.

Fresh native Aster history replay with new code preserves gross settled30d totals: ESPORTS8.562355%, SIREN6.654209%, 龙虾11.023827%. Figures are dated observations, not projected returns or executable profit.

## Limitations and next checks

Observed sustained event spacing is not the same as an exchange-provided historical schedule version. A provider that systematically omits events can imitate a different schedule; this cannot be certified away with local tests. Maintain source provenance, explicit gaps and direct reconciliation. No claim of universal all-venue historical effective-time proof. A very recent ambiguous transition can temporarily remain unavailable pending confirming events/full history. Broader historical coverage and legacy filter work from the previous release remain open.

Sources: https://asterdex.github.io/aster-api-website/futures/market-data/ (bounded ascending funding history and current fundingInfo); https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-Info .
