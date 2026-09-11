# Funding correctness release: evidence and acceptance

Worktree: `tmp/spreadboard-exchanges-trial`, branch `codex/exchanges-funding-trial-20260910`.
This document is chronological evidence. Current status and remaining work are owned by Codex in this chat: see [completion record](2026-09-11-funding-completion.md). Statements below about pending deployment, unobserved ledger writes and Claude follow-up describe earlier checkpoints, not current state.

## Confirmed causes at the initial September 10 investigation

- Production web and collector run different source generations. The web currently lacks already-written settlement, Hyperliquid and live-row coherence fixes. Merged both source lines into this candidate, preserving newer Portfolio changes.
- Native ONG schedules at 2026-09-10 22:50–22:55 UTC: Binance, BingX and XT hourly. The website displayed 8h. Native schedule metadata must outrank generic defaults. XT's bulk `collection_internal` was omitted; Phemex's metadata `fundingInterval` uses seconds.
- CoinEx's CCXT `fundingRate` is the last settled rate. `nextFundingRate` is the current upcoming estimate. A common field name does not establish common semantics.
- Unknown schedules could still be annualised; a fresh response lacking an interval could reuse a stale discovery interval. Public projections must withhold these cases.
- The short history demand lane excluded its entire backlog from the ordinary rotating lane, starving unserved demanded legs. Both lanes now retain those priorities; due-time logic avoids refreshing already-current history unnecessarily.
- Exact events were repeatedly downloaded from scratch. A bounded SQLite event ledger deduplicates venue/symbol/timestamp, overlaps a day for corrections, and retains 32 days; incomplete archives still backfill fully.

## ONG native settlement comparison (22:55 UTC)

| Leg or pair | 24h | 7d | 30d |
| --- | ---: | ---: | ---: |
| Binance contract, short receives positive | -1.530024% | -14.329302% | -76.206937% |
| Bitget contract, short receives positive | -2.384600% | -14.358300% | -75.071100% |
| Bitget long / Binance short | +0.854576% | +0.028998% | -1.135837% |

These historical pair values matched the displayed rounded totals. The misleading current projection was a separate defect. Negative individual rates can yield a positive long-short pair because the long receives a negative rate. No claim is made that historical returns predict future funding.

Evidence: `output/release-20260910/ong-native.json`, `ong-settled-audit.json`, `live-funding-before.json`. The source responses are native public APIs; no private trading actions were used.

## Venue-wide validation

The first live parser audit covered 20 enabled venue labels, including spot-only labels with zero funding. Saved in `output/release-20260910/venue-parser-audit.json`. It exposed assumed schedules on Binance/Aster/Phemex, XT schedule omission, and an unsupported 24h MEXC metadata value. A parser returning rows is not proof that every field is correct; independent semantics checks and regressions remain necessary. Hyperliquid's base CCXT funding call does not cover builders: the native all-builder bulk lane must be verified separately.

## Acceptance checklist at the initial candidate

- Final full pytest and unchanged Ruff ratchet must pass; no failed gate may be masked.
- Native public all-builder Hyperliquid read, final venue schedule audit, rolling window expiry and mixed-cadence gap checks.
- Guarded deployment and source parity for both web and collector; never interrupt discovery or finalization. Preserve current runtime identity registry and Portfolio/account data.
- Fresh UI/API: no HTX/Ourbit, Hyperliquid routes, exchange exclusions on either leg across ranking/pagination/export, coherent rates/net values in collapsed and expanded rows, historical unavailable states.
- Trial activation on verified private Telegram identity, one atomic claim, no extension on relinking/deletion, expiry, paid access preservation. Draft ad only.
- Original stability work remains open: collector OOM flag observed September 10, backup service failed at 18:22 UTC, measured memory reduction/caps, clean 48h soak and two normal successful backup firings. Do not raise limits or restart monitoring disabled by the operator.

## Release candidate verification, 2026-09-10 23:22 UTC

- Full suite: **2860 passed in 125.52s**, command exited 0. Ruff ratchet: **no new findings, 502 known**, baseline unchanged. `git diff --check` passed.
- Six deliberately reintroduced faults were detected by call-site regressions; original source restored afterward. Evidence: `output/release-20260910/mutation-results.json` and per-mutant outputs.
- Latest operator decision: **CoinEx and Phemex removed from public funding only**. Bulk collection stops before network work, exact-history collection skips them, cached funding pages are rejected before reranking. Prices remain enabled. HTX/Ourbit remain excluded from opportunities generally.
- Native Hyperliquid lane: 317 active contracts across base, XYZ, PARA, MKTS and IO, including `IO-OAI/USDC:USDC`. Both canonical and native identity aliases are keyed intentionally; do not count aliases as distinct contracts.
- Additional shared fixes: book-only updates use only unexpired exact funding cache; sampled missing legs do not contribute zero; assumption flags cannot be cleared by quote synchronization; missing schedules are not defaulted by public daily/APR conversion. A window expires when the oldest included payment leaves, as well as at the next settlement.
- UI registration preview rendered the Telegram-required seven-day terms. Trial tests cover exact expiry, concurrent identity claims, unlink/relink, account deletion, Gmail aliases and paid-access preservation.
- Funding count audit: 9,877 venue-contract rows versus 2,370 distinct base labels before active-status/identity deduplication, excluding CoinEx/Phemex. These labels are not a verified asset count. Negative contracts must remain available because two negative legs can form a positive net pair.
- Rollback backup: `/opt/spreadboard/backups/release-20260910T231424Z-funding-trial`; online SQLite backup checked `ok`, source archive and original image IDs saved with restrictive permissions.
- Candidate source digest: `745eddbf3b028a1e`. Production verification will be recorded below; these checks alone do not assert deployment.

## First deployment and recovery follow-up

- Both containers verified `745eddbf3b028a1e`; health returned 200, restart counts zero and OOM flags false after recreation. Trial tables were created successfully.
- Native production cache now publishes hourly Binance/BingX ONG schedules. Funding refresh rotates all enabled venues; old entries age out or are replaced per leg.
- Fresh health showed 207,908 priced routes / 215,062 indexed routes, with 1,310 priced token groups. This is one warm sample, not stability acceptance.
- Initial exact funding searches stayed empty while the previous catalogue was rejected for retired venues and a new build waited behind a structural materializer. Native prices were unaffected. The normal publisher completed a new 44,814,105-byte catalogue at 23:30:23 UTC, 5,902 tokens, 32.7 seconds of child work, 177.2 MB peak child RSS and 204.9 seconds including queue wait.
- Recovery follow-up: lossless v2 packed catalogues can filter retired venues before ranking while preserving every allowed alternative. Reduced legacy generations still fail closed. This avoids page-wide warming on later venue-policy changes. Full follow-up suite: **2864 passed in 117.36s**; unchanged Ruff ratchet, 502 known findings.

## Follow-up release accepted

Commit `d349e95` deployed successfully to both services, source digest `4c532517549fed61`; deployment exit 0 and health 200. Full tests: 2864 passed; no new Ruff findings. Lossless packed funding catalogues now retain allowed routes across venue-policy changes without waiting for a whole rebuild.

Final health still reports incomplete historical coverage: 3116/9400 current 24h, 2992/9400 current 7d and 2576/9400 current 30d legs. ONG production showed consistent projected group values and an explicit unavailable Gate 30d gap. Incremental SQLite production writes have not yet been observed; native BitMart also needs equivalent storage integration. The 48h stability and normal-backup acceptance remain open. This was the earlier checkpoint; the current completion record above supersedes these open items.
