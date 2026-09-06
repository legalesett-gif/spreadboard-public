# Stock pricing evidence checkpoint

## Official stock pricing evidence and release runbook — 2026-09-06 08:00 UTC

Production remains b4230a7 / 272beeec7e0e9f49. Candidate a5f1706 / 204ed15b27775248 remains frozen, undeployed, with no waiter queued. Corrected the release runbook's stale source pins and observer instructions; it now preserves sole corrective observer PID 561510 until approximately 09:16:39 UTC and records the backup unit as already installed. Fresh observer liveness at 07:55:43 was active, same PID.

Saved comparison corrective-comparison-0755.json covers 2,131.92 seconds, 18 coverage samples and four generations: 202,813–203,504 priced routes versus 202,936 baseline, within 10%, not yet a full hour. No sampled OOM/restart/unhealthy/failures. App anonymous peak 3,054,432,256 bytes; collector anonymous peak 3,542,568,960 and total cgroup peak 4,294,111,232. No websocket samples in this interval, so unmatched phase/duration maxima cannot establish memory savings or safe lower caps.

Primary documentation now found through https://entropy.io/ -> https://docs.entropy.io/ (not the unrelated entropyio.com). Official asset directory explicitly maps GPRO to GoPro Inc, a US equity market, USDC, 5x. Entropy equity terms describe cash-settled derivatives without ownership, 24/7, and corporate-action termination rules. Official architecture says Redstone maintains oracle feeds. Equity oracle uses the public primary-venue price in regular hours, then a depth-weighted internal/external blend outside hours; the external input can be an approved after-hours venue or last print. This proves published mechanics, not an identical oracle across venues or current executable hedge.

Sources: https://docs.entropy.io/asset-directory/equity-assets ; https://docs.entropy.io/market-types/equity-perpetuals ; https://docs.entropy.io/equity-perp-mechanics/oracle-price ; https://docs.entropy.io/ . Browser DOM read succeeded where web extraction rejected markdown. Copies: entropy-equity-assets.md, entropy-equity-terms.md, entropy-oracle.md, entropy-architecture.md under output/stability-20260906/.

Bybit's official help article, updated Sep 4, describes USDT-settled TradFi perps with weighted index components, session-dependent stale-component exclusion, transition smoothing and product-dependent anchor constraints. https://www.bybit.com/en/help-center/article/Introduction-to-TradFi-Perpetual-Contracts?category=4d5d8649cba144c1a8 . This is a different construction from Entropy; the precise current GPRO component set remains to inspect. No registry entry or verified status was granted.

Next: registry admission must be scoped to reviewed exact venue/type/symbol combinations. Current token-level registry with only a venue list cannot by itself prove every same-label instrument. Continue GPRO Bybit -> io:GPRO comparison using these primary documents, verify current component/native price data, and preserve research-only execution policy. Do not use a broad token entry merely to bypass missing evidence. Complete observer, guarded release and ordinary memory/coverage validation, then safe final caps, 48 hours and two subsequent normal green backups.

