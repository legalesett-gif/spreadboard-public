# SpreadBoard / UA comparison, 2026-09-05

## SpreadBoard stability and UA comparison — Codex continuation (2026-09-06, 02:11 UTC)

- Goal ACTIVE; previous turn PROGRESS. LIVE app/collector b42e595, digest bc130c63e6b760de, started02:02:29UTC. Guard clear before deploy and immediately before recreation; no force. Deployexit0/health200/bothdigests match. Fresh02:05 healthy/restarts0/OOMfalse. Accounting and other checkouts untouched. No trades/messages/spend/cap increases/weaker guards. Heartbeat staysPAUSED, finite observer activePID364378 until~03:07; do not duplicate or restart it.
- Confirmed cold Funding allocation problem: full render_funding_page(CAP) in isolated768MiB address-space/90s child terminated139 twice at funding_catalog._read_persisted_cache whole-fileorjsondecode. File51212862bytes. Second probe usedfaulthandler and disabledcorefiles; trace containsstackonly. Mainappremainedhealthy. This is bounded child failure, not a production appOOM or valid beforelatency measurement.
- Deployedtoken-at-a-time funding envelope reader with complete-file validation, packed+legacy rows, uint64 preservation, disabled-venue rejection and strict duplicateheader rejection. SamefullCAPprofile now exits0 with110renderedfundingrows/399192HTMLbytes, peak512308KiB(~500MiB),20.432scold/1.360swarm. API+HTML isolatedprocess measurement; does not includeHTTP/auth/steady broadresidentindex. Coldrestore15.572s, PackedRoutesdecode10.621s/6381calls, restore6.438s/5319calls and policyiteration5.268s/1062calls overlap; do not sumoverlappingcumulative times. Warmprofilecontainsbackgroundcachewarming, so cumulativefunctiontotalsarenotpureforegroundwall.
- This proves the bounded fullpage now completes, not broad warm-latency or whole-cgroup savings. Coldrestore still slow. Profile executes normal demand enqueues (chart/history), no externalnotification/order. No source changes since deployedcandidate. Prefer sustained release observation and measured follow-up over further speculative redeploys.
- Gates:2662tests passed165.91s(exit0); Ruff517known/no new afterimportsort; new whole-reader regression failsoldsource. Testsvalidatepacked/legacy,dottedtokenname,unsignedtimestamp,invalidtrailingdataandOurbitrejection. Trackedtestdata individuallypreservedunderfunding-stream-test-data/restoredHEAD. Sourceb42e595.
- Actualpredeploy ordinarysite Funding navigationcompleted3.38stoolwall and FundingDOMconfirmed851tokens/24056livevenueroutes; this is one warm sample. Priorpost6ac315d CAP/OPENAIUI checks remain timestamped below, not newb42e595UIverification. Runtimeprofile verifiesactualnewfullrender; next actualbrowsercheck andordinarymemorycycle still useful.
- Evidence: funding-stream-{pytest,ruff,mutant,deploy}.txt; full-funding-cap-bounded-trace.txt; full-funding-cap-after.jsonl; profile_full_funding.py (remote /app/runtime/stability/20260905-coverage/). Profilechildren terminated, do not include their intervals as ordinary-load capacity proof. Observerrelease segment now starts02:02:29 and crosses120s->300s cadence02:07; still cannotprove30samples/houror48h. BackuplatestResultsuccess/ExecMainStatus0 freshly02:05; nextnormalfirings remain required.
- Next: finalreleasehour/48h evidence, ordinarynavigation/materializationCPU+anon/cgrouppeaks, safe lowercaps ifproved; coldcataloguerestore15.6s is measuredlatency follow-up. An own-service observer after currentfiniteobserverTERMINATES may be needed for correct30samples/hour+48h schedule; do not duplicate currentlyrunningobserver. No goalcompletionclaim.

- Corrected the prior sidebar diagnosis using fresh bounded runtime probe: OPENAI exact helper counted27positive rates but all sampled routes were mirage_guarded. Final overlay correctly excluded these leaders; the count was wrong. New shared identity eligibility applies to exact count and final leaders, and unique positive counts are recomputed after live overlay over all matching routes, including off-page alternatives. No guard weakened. Existing explicit quote/tokenized eligibility now also applies to final leaders.
- Regression evidence: full suite2661passed169.94s, Ruff517known/no new. Six full-response guard/live-overlay cases: prior source4failed/2passed; new source all pass. Tests cover an off-page winner, zero live rate replacing formerly positive rate, deduplication, and identity exclusions. Test-generated data individually preserved under funding-kpi-test-data then restored. Source commit6ac315d.
- ACTUAL postdeploy authenticated UI: CAP125routes,99eligible funding pairs, Top Funding Pairs populated with OKX->Bybit +1.320%current daily projection. OPENAI39routes,0eligible funding pairs, empty funding sidebar consistent with identity guards; research group still marked TOKENIZED/DD PENDING. Hyperliquid is PRESENT, Bitget->Hyperliquid~6.1%top-book with depth unavailable, not an execution certification. StartupCAPnavigation timed out in CUA then completedHTTP200; subsequentOPENAI navigation5.7s. Fullnavigationlatency remains open.
- Prior6eb1788 memory segment: ordinary live index advanced202689->203070, priced196345->196907 in two fresh health samples; noOOM/restarts. At~01:47 appVmHWM3115184KiB/cgrouppeak3386597376bytes (~3230MiB), below priorcappeak3584MiB but still above proposed3072MiB. Sidebar probe child appeared briefly; not pure ordinary-load acceptance. Collectorparent~149MiB, websocket worker~1138MiB, cgrouppeak3110043648bytes. CPU sampled cumulative collector850.8CPU-seconds over393.6seconds (~2.16cores). No comparable final sustained interval or safe lower-cap proof. Never claim48h/houracceptance from these samples.
- Next: sustained ordinary navigation/materialization CPU+anon/cgroup evidence; full HTTP cold/warm Funding profiling; 30samples/hour±10% and clean48h after final changes; lower caps only with headroom; next normal backup firings. Review final funding ranking's zero-versus-negative key (`or -inf`) separately if relevant; do not broaden this deployed fix silently. Current sidebar discrepancy is CLOSED as a count-policy mismatch, not by admitting guarded opportunities.
- New evidence: funding-kpi-{full-pytest,ruff,mutant,deploy}.txt, sidebar-probe.jsonl, probe_sidebar.py (remote /app/runtime/stability/20260905-coverage/), counts-observer-0153.jsonl (file label misleading: capture~01:47UTC; trust sample timestamps). Current finite observer is unchanged, release segment reset01:50:55.

- Counts commit d90efa6: asset facets count unique token labels rather than venue permutations; exact-token kind/asset facets include off-page alternatives and respect filters; shared spot/futures lane counts a token once. Exact pages now report routes, include Next/Previous and coherent matching/displayed counts. 2635 tests passed137.08s; old-source mutant4failed; Ruff517known/zero new.
- Rendering/sort commit feffc48: exact sort/direction now applied before pagination using the same route metrics as the broad page. Grouped renderer retains API-admitted sound negative-basis routes with positive current carry; stale/identity/thin-book/currency failures still excluded and spread_evidence_state is unchanged. 2643 tests passed147.96s; old-source mutant2failed; Ruff517known/zero new. Test-modified tracked data preserved under output then restored individually from HEAD. Source worktree clean apart from untracked evidence directory.
- Actual authenticated UI: OPENAI shows1tokenized asset/1futures token, route pagination and working Next/Previous. Initial second page was empty because renderer dropped carry candidates; final release displays Coinbase International->Mexc with -0.7%basis/+0.061%projected daily carry on page26-35of35routes. Fresh route counts can change between requests. The route-page description and exact-token subtitle are now corrected and verified in actual HTML. Superseded by 01:44 verification: KPI and strict advanced-volume filtering work; funding sidebar/count mismatch corrected and verified in01:53 release. Do not equate those cosmetic labels with duplicate stored markets.
- CAP catalogue ordinary rotation completed01:03:13Z. At01:04 all13non-Ourbit identities from the15-rowUA sample have fresh exact pairs;2Ourbit deliberately absent. CAP saved funding cache was still missing the pair in that probe, but actual Funding page later shows Bybit Futures->Bingx Futures: long-.1726%/4h, short-.1264%/4h, net+.277%projected/day, basis-.27%. Reverse direction also shown (+basis/negativecarry). Exact chart identity intact; incomplete24h/7d/30dsettlements remainblank. Thus sampled CAP visible coverage gap is CLOSED. ANSEM fresh pair exists through native alias joining even when raw catalogue-label lookup differs. Prior samples overlap; never sum them or claim exhaustive premium parity.
- Funding navigation repeatedly exceeded20sCUA wait, but the last request completed and was inspected/expanded. This is slow navigation evidence, not a confirmed persistent outage. Remaining cold-latency work must profile complete Funding exact-token/history expansion. User-facing publication and pair arithmetic are now verified for CAP, but broader both-direction comparator checks remain bounded samples.
- Earlier fixes retained: Ourbit exclusion; native closed-market filtering; public BingX status separate from API permission; ANSEM alias; native indexes from KuCoin/Phemex/MEXC/HTX/Bitget, with exact-ID joins and finite/fresh checks. Prior ordinary oracle rotation excluded all9false OPENAI GateSpot/perp routes while preserving io:OAI futures. No live-entry certification implied.
- Cardinality baseline00:05:1987futures/3962spot token labels;9733/12484markets; zero duplicate exact venue/type/symbol keys. UA user-reported2534/4370 are token-label counts, not directed routes. Counts alone do not demonstrate memory waste. Prior successful materializer318.276s/333.3total published17views/197321routes with collector parent~150MB/index_rows0; heavy index child still~2357MBRSS/cgroup3704MiB on that older segment. Caps remain3584/4096/768/192MiB; lower3072/3584/512/192unproved.
- Stability heartbeat remains **PAUSED**, freshly verified. Old finite native-status observer finished00:57:53UTC:566records,0host_errors,1healthtimeout23:48:50 and4HTTP502s at00:08/00:38 deployment windows. Not a clean final-release run. No active observer existed before starting one finite2h read-only observer **spreadboard-stability-counts-20260906.service**, PID364378, started01:07:19UTC, endsabout03:07:19,RuntimeMax7500. Evidence `/opt/spreadboard/runtime/stability/20260906-counts/samples.jsonl`; firsthourhealth120s, then300s; host15s/free300s. Do not duplicate. No recurring automation reactivated.
- BACKUP normal scheduled run now VERIFIED SUCCESS: started00:19:49UTC, terminal01:15:36UTC, MainPID0/ActiveStateinactive/Resultsuccess/ExecMainStatus0. Snapshote46b3d4c saved, retention policy7daily/4weekly/3monthly applied,28snapshots reported, final check said no errors were found. Script runs backup then forget--prune then check--read-data-subset1/20 with check=True, so successful exit includes all three stages. No manual backup/prune/config changes/process interruption this turn.46rateLimitExceeded errors were retried before success; approximately56minute duration and sharedOAuth/noTPSlimit/infinite service timeout remain reliability follow-ups, not a currently failed backup. A successful run does not prove future backend availability.
- Outstanding: cold Funding navigation; further advanced-filter UI review; final-release30samples/hour±10%, ordinary heavy/history/navigation cycles, safe lower caps and clean48h; backup retry/duration reliability; bounded bidirectional UA comparison evidence. Do not declare complete from tests or one healthy sample.
- Evidence: `output/stability-20260906/counts-{full-pytest,ruff,mutant,deploy}.txt`, `render-sort-{full-pytest,ruff,mutant,deploy}.txt`, `ua-reconciliation-0100.jsonl` (pre-rotation), `ua-reconciliation-0104.jsonl` (CAP added), `ui-verification-0109.json`, `release-0108.txt`, `native-status-finished.jsonl`. Deploy with own `output/stability-20260906/deploy_with_final_guard.sh app collector`, independently run protected_deploy_guard.py first and let helper repeat it immediately before recreation.

- New010882d verification:2644tests passed171.41s; Ruff517known/zero new. Old-source reuse regression fails. History reader now calls bulk funding only when archive/funding-file generation changes or an actual settlement boundary expires; a test proves immediate eight-hour->two-hour schedule rotation and expiry without weakening exact-window completeness. Test data mutations preserved individually under output/history-reuse-test-data and restored fromHEAD. Cosmetic exact-page wording now says "1 exact token" and "25 venue routes on this page".
- Bounded production profile (768MiB virtual limit/90s timeout, actual CAP persisted token, real futures-kind page+group HTML; does not include full HTTP wrapper or resident broad price index):before110routes,cold5.624s/warm.743s,446bulk funding reads;after110routes,cold4.660s/warm.974s,113/112reads. PeakRSS~456MiB unchanged. No general warm-latency or memory-saving claim. Initial all-kind220route profile is not a duplicate-market finding: it included both route kinds. Profile children terminated normally; their samples must not be treated as ordinary-load acceptance. Actual broad Funding navigation via site's link took12.46s toolwall, so full HTTP/navigation performance remains open.
- Fresh UA01:16 futures sample:15/15exact identities found in fresh pair construction;14also in saved fundingcache, ICX OKX->WhiteBIT absent there but fresh pair exists. This is sampled source coverage, not exhaustive proof of all currently published exact UI rows. Fresh01:17 spot-futures sample:9supported pairs found,2Ourbit exclusions,4BinanceAlpha legs outside configured coverage. ONE has distinct USDT/USD/USDC market symbols, not duplicate exact keys. ASTEROID quote moved to-.0523%basis while retaining+.12%daily funding; ICX positive2.17%basis has negative1.59%daily funding and belongs on Spreads. Other sampled pairs have positive spread and funding. Reference values are not simultaneous executions.
- Reverse comparison:actual ourFunding UI leaders include Kraken Futures T,HFT,S,ZIG,VELO,NIGHT,ICX, unlike the visible UAguest winners. UAexchange dropdown explicitly shows Kraken with premium crown, likewiseCoinbase/HTX/Phemex/XT/CoinEx; do not bypass paid access or claim these venues absent from UAoverall. Freshnative Kraken01:22:39 confirms funding velocity/mark normalization and cached35-second-old rates:Thourly-1.9984%vs-1.9943%cached,HFT+.55374%vs+.55451%,S+.50246%vs+.50214%,ZIG+.50518%vs+.50515%,VELO-.50072%vs-.50066%;NIGHT+.08279%,ICX+.07204%. These are projections on current notional, not settled24h or entry certification. Officialticker endpoint/docs used: https://docs.kraken.com/api-reference/market-data/get-tickers .
- The existing finite observer remainsactivePID364378 untilabout03:07:19UTC; no new observer or recurring automation created this turn. Currentrelease segment begins01:19:21, so firsthour cadence crosses the observer's120s->300s transition and does not provide30samples/hour for this release. Keep acceptance unproven and do not duplicate the observer. Stability heartbeat remainspaused.
- New evidence:history-reuse-{full-pytest,ruff,mutant,deploy}.txt;exact-futures-profile-before/after.jsonl;ua-funding-0116.json;ua-reconciliation-0116.jsonl;ua-spot-futures-0117.json;ua-spot-reconciliation-0117.jsonl;kraken-leaders-0123.jsonl;backup-terminal-evidence.json. Nativekrakenprobe and boundedprofile script are in /app/runtime/stability/20260905-coverage/ onproduction.


- 02:09 snapshot-wide read-only audit (audit_funding_snapshot.py): singleopenfilemtime1788660256.660728/51247526bytes;5319tokenblocks/1061nonempty/222417routes. Independently calculated short-rate*24/interval minus long-rate*24/interval for ALL222417storedroutes;0arithmeticmismatches,0numericvalueswithoutcompleteleginputs,0duplicateexact(venue,type,symbol)legpairs,0selfpairs,0Ourbit. EmptyCounterfields in rawJSON meanzero. Peak210296KiB, childexit0. This proves storedgeneration arithmetic/exact-key uniqueness, not freshness/identity/nativeexchange truth/executability of everyroute. No extraobserver, no redeploy thisturn.
- Actualb42e595UI nowverified: CAPSpreads128routes/99eligiblefundingpairs, populatedsidebar; broadFunding850uniquetokens/23931livevenueroutes andcurrent/historytabs. Broadnavigation10.66stoolwall versusprior3.38swarmexample; variabilityremains. Freshobservercapture containsstartuppriced0 followed197272; latestappanon2482110464/current2581385216/peak2583994368,collectoranon1532567552/current2167074816/peak3671539712,uptime280s. Bothhealthy/restart0/OOMkill0. Tooearlyandprofilechildrencontaminateordinarymemoryacceptance; no lowercaps.
- Next useful correctness review: api_spreads.normalised_funding and _public_row still describe/flip a FUTURES-SPOT candidate to a mirror trade without changingitslegidentity; tests/test_release_audit.py:2458-era testencodesoldmirrorassumption. Modern catalog/live-update/Funding paths use short-minus-long and snapshotauditpasses. Trace whether oldfallbackisreachable beforechanging; preserve exact route/no substitution. This is a SOURCE concern, not a freshly provenlivepage mismatch. Separately finalfundingmax keyuses `or -inf` sozero-vs-negativeorderingmerits focusedtest. Do not treatpotentialissuesascurrentmarketfailures.
- Evidence added: funding-snapshot-arithmetic.json, audit_funding_snapshot.py (remote /app/runtime/stability/20260905-coverage/), observer-current.jsonl. GoalACTIVE; thisturn made evidenceprogress, notblocked. Leave currentrelease running for sustainedacceptance while conductingread-only orlocalreviews. ExistingobserverPID364378 is solefiniteobserver andwasfreshlyactiveatturnstart.

## Comparable counts

The production audit counted **2,171 futures token labels and 3,965 spot token
labels**, not 142,711 tokens. The latter number was directed economic routes.
There were 22,411 exact market keys, no duplicate market keys and no duplicate
economic directed routes in that captured generation. The owner's UA counts
(2,534 futures / 4,370 spot) therefore do not establish overcollection on our side.
Different quote contracts, market types, venues and directions must not be
silently collapsed as if they were duplicate observations.

The expensive structure was repeated rich route data: 347.6 MB index JSON beside
a 3.32 MB market catalogue. That is the measured optimization target. Historical
counts are not a claim about current coverage; rerun the streamed cardinality
probe after later market-definition and index generations.

## UA to SpreadBoard

Reference: https://uacryptoinvest.com/arbitrage, visible browser tables, 45 exact
venue/type route examples. Fifteen Spot-Futures leaders were captured 16:48:48 UTC,
15 Futures spread leaders 16:51:18 UTC, and 15 Futures funding leaders 17:20:18 UTC.
This is a deliberately inspected sample, not a claim to have read every UA row
or its premium DEX surfaces. Capture times differ, so displayed prices are not
expected to match exactly across the two sites.

After normalizing venue aliases, **36/36 admissible, in-scope reference routes
had an indexed counterpart** at the 17:56 probe. Other differences:

| Exact example | Evidence and disposition |
| --- | --- |
| STONKS MEXC Spot → Aster Futures | Aster's public market definition was active; our old catalogue omitted it. Genuine definition lag. |
| CATE, ASTEROID, ANSEM, ALIGN positive funding | The 500-token cache cutoff excluded some eligible tokens. Membership moves with rates, so this is not a permanent per-token exclusion list. |
| KAITO WhiteBIT → Hyperliquid | A fresh catalogue pair set `quote_mismatch` for USDT/USDC even though construction admitted that dollar basis. A legacy scanner row could still appear. Fix applies to both Spreads and Funding; do not claim KAITO was absent everywhere. |
| ESPORTS Kucoin Futures → Bitget | Present. The first probe treated `Kucoin Futures` and `Kucoin` as different venues. Corrected with `coverage_reconciliation.normalize_venue`; no missing-market claim remains. |
| MICRODUCK / ROBINCAT Ourbit Spot routes | Intentional owner-requested Ourbit exclusion. |
| NAORIS / JCT / ESPORTS BinanceAlpha Spot routes | Exact venue outside our chosen coverage. Other venue routes are separate candidates. |
| BONER MEXC Spot → Gate Futures | MEXC BONER/USDT was inactive in direct definitions; BONER/USD1 was active but is a different quote market outside the present quote set. |
| ANSEM Aster Futures → Bingx Futures | Bingx's exact ANSEM contract was inactive in direct definitions. |
| OPENAI Gate Spot → Gate Futures | Gate spot ~867.6 versus perp ~1420.71 and its own index ~1420.77. Identity/index guard correctly excluded this apparent ~63% spread. |

UA's displayed F Spread and APR in the captured examples use an eight-hour
comparison: APR / 365 is the comparable daily projection. Our live arithmetic
normalizes each leg by its own interval, then subtracts long from short. A raw
2-hour or 1-hour funding rate is not directly comparable with an 8-hour rate.
Exact 1d/7d/30d columns remain separate settlement totals; missing windows stay
blank. No extrapolated current rate may fill those windows.

## SpreadBoard to UA

Most captured Futures funding leaders overlapped by token, although the selected
hedge, venue, quote contract and observation time differed. Direct searches for
1000BTT and BTT in UA Futures, and LCAP in UA Spot-Futures, returned no visible
rows in this anonymous session with no minimum-spread value entered. UA lists
both Kraken and XT in its referral menu; that menu is not proof of exact-market
coverage. The 19:05–19:08 follow-up inspected the actual filters: Spot-Futures
marks Kraken and XT with crown badges and disabled styling; Futures marks XT
the same way and does not offer Kraken in its visible exchange selector. These
guest restrictions make the absent guest search results an invalid test of
premium-market parity. Exact premium coverage remains unverified; no paywall
was bypassed. Our reverse futures-long /
spot-short rows also require inventory or borrow; they are research candidates,
not a statement that an account can enter them.

Kraken's unusually large negative rates were checked against its public tickers.
For PF_LCAPUSD at 17:53:56 UTC, raw hourly funding was -0.0345315 quote units per
base unit, mark ~6.998391, index ~6.9291. The adapter's percent-of-current-notional
normalization can therefore produce a daily projection near 12%. This is not a
100x arithmetic error or a guaranteed daily return. Kraken documents absolute
per-unit hourly funding and a +/-0.50% hourly relative limit:
- https://docs.kraken.com/api-reference/market-data/get-tickers
- https://support.kraken.com/articles/4844359082772-linear-multi-collateral-derivatives-contract-specifications?mode=consumerapp

The bulk adapter uses current mark as denominator; the point adapter previously
used index. Candidate `7875f13` makes the point reader use the same current-mark
basis and reject suspended/nonfinite observations. Its actual CCXT parser
comparison and invalid-input tests failed before the correction. Do not label
these current-notional calculations as Kraken's historical funding-calculation-time
relative rate. Production verification of this correction is still pending.

## Implemented release and further correction

`ded65fa`, digest `269a74fa24ddb8b4`, deployed 18:04:37 UTC after both protected
worker guards cleared. Full suite 2,441 passed; Ruff 517 unchanged. It fixed
quote flags, removed the token cutoff and reduced each token before constructing
the next. The hourly code default did **not** change production's explicit
21,600-second Compose override. The fresh whitelist configuration check found
that oversight, and the next candidate corrects the actual Compose value. Its
production-config refresh-loop test fails against the old override.

At ~18:11 UTC, web-role probe: 149 sampled positive rows, zero exact-leg funding
math errors, twelve nonempty navigation views and zero Ourbit. The cache was
still the prior 500-token generation, so expanded production/UI coverage was
not yet accepted. A collector-role diagnostic initially compared persisted
rates with live rates; that was the wrong reader mode. The corrected probe uses
web role and one coherent live-rate generation, preserving the failed diagnostic
as evidence rather than calling it a website arithmetic bug.

A further real gap remained: three long alternatives per short were selected
using build-time rates. That can hide a later current winner, historical winner
or requested exchange. The new local candidate retains **every eligible pair**
losslessly and caps the display only after current/window/filter selection.
It processes ranking rows in bounded batches and chooses distinct exact short
contracts on the ranked list; exact-token detail retains all long alternatives.

Frozen public replay (17:34:57 snapshot; not fresh prices):

| Storage / build | Eligible retained pairs | Bytes | Builder peak |
| --- | ---: | ---: | ---: |
| Prior 500-token build | 18,246 | 37,491,658 JSON | 337.4 MiB |
| All tokens, three-long lane reduction | 45,909 | 95,603,889 JSON | 241.5 MiB |
| Lossless full-candidate packing experiment | 219,871 | 34,058,760 compressed | 146.2 MiB |

Actual new publication pipeline wrote a 47,952,096-byte JSON envelope (includes
base64 and token metadata). Build 28.18s; restore 4.18s; full Now query 11.29s;
12-view navigation 32.36s; whole pipeline peak 295.9 MiB. The later integrity
validation also checks advertised zero-row blocks before publication, so repeat
these timings on the final source. Replay lacks production's full historical
radar/archive and resident price overlay. Expanded scan CPU is a real trade-off;
measure production response time and child peaks before accepting it.

## Remaining acceptance

- Local final full suite: 2,457 passed in 261.79s, exit 0; Ruff 517 unchanged.
  Old shortlist, unpacked storage, eager batching, duplicate short display and
  zero-row validation mutants all caught. Actual production cadence and eager
  archive regression tests both failed before correction. Guarded deployment
  and digest parity for this candidate still pending.
- New catalogue generation actually loaded by readers, positive reference tokens
  discoverable, active Aster STONKS definitions and exact route arriving normally.
- Current and historical rankings independently complete; exact-filter and retained
  history paths validated against real data, not just the reference sample.
- Explicit explanation or unresolved status for reverse-comparison absences.
- Startup response time, steady query CPU and memory; safe lower caps only after
  evidence. No cgroup or subscription increase, spend, Pushover or trading.
- Deployment-free count gate and final clean 48-hour run. The finite read-only
  18:05–20:05 candidate sampler is preliminary; another release splits its window.

Local evidence is in `output/stability-20260905/`: `ua-reference-cases.json`,
`ua-cases-before-normalized.jsonl`, `ua-cases-coverage-deploy.jsonl`,
`ua-market-definitions.jsonl`, `kraken-funding-direct.json`,
`coverage-funding-after-web-role.jsonl`, `coverage-deploy.txt`, packed replay,
full-test and mutant files. This document does not certify live entry readiness.

## Review of the full-candidate change

- Production failure review: the build-time three-long cap was a lossy cache,
  not a display optimization. The live-rate flip and independent historical /
  exchange tests fail against the old reducer. Lossless per-token packing fixes
  that gap; filtering still rejects disabled venues and invalid identities.
- Maintenance review: a code default did not control the production cadence.
  The test now loads the actual Compose override into the running loop. The
  private iterator and explicit list-export wrapper distinguish bounded ranked
  reads from callers that intentionally need a complete export.
- Storage-integrity review: trusting a compressed block's advertised zero count
  could skip validation through a falsey sequence. Every restored block is now
  validated before publication; corrupt, inflated, truncated, trailing and
  false-count data retain the previous coherent cache. No pickle/executable
  format is used. Test proves removal of the zero-count validation is caught.
- Remaining measured risk: full selection spends more CPU than a lossy shortlist.
  Common navigation remains materialized, but uncommon full queries must be
  timed on production. Do not claim RAM recovery or lower memory caps from the
  local compressed-byte count alone. No new external or authenticated API path.

## Release checkpoint — 18:33 UTC

Full-candidate commit `cc9b776` deployed after both guards cleared. App and
collector both verified digest `c3b22a143f494f61`; new containers started
18:31:23 UTC. At 18:33:10, both healthy, restart 0 / OOM 0. Collector was running
the ordinary chart definition workers, so the corrected hourly cadence now
actually initiated a refresh. Persisted market and funding files were still the
previous generations at that instant; publication and page acceptance remain open.

The final local replay, while other work occupied the Mac, was slower: build
88.23s, restore 24.52s, Now 27.09s, navigation 62.21s; peak 230.9 MiB. It retained
the same 219,871 pairs and returned the same match counts. These timings are not
an isolated CPU comparison and must not be represented by only the faster prior
run. Production CPU and uncommon full-query latency still require measurement.

After this deployment, separate work modified `spreadboard/fast_quotes.py` and
`tests/test_live_charts.py` in the shared checkout (general Hyperliquid builder
namespace handling). Those changes were not part of the tested/deployed cc9b776
release and have been left intact. Recheck shared-tree status before any further
deployment; do not silently ship unrelated untested changes.

## Independent publication correction — release 19:04 UTC

The separate Hyperliquid chart fix was committed and deployed as `57c4985`,
digest `54d470476d775c64`. Both production digests were freshly checked at
18:59 UTC. The app started 18:41:16 and collector 18:44:22; both healthy,
restart zero and OOM false. The new isolated working branch is
`codex/funding-publication-cadence-20260905` in
`tmp/spreadboard-funding-publication`. It includes `7875f13` plus the preserved
chart fix, cherry-picked as `c072e41`.

A fresh 18:41:53 probe still found the legacy 500-token funding cache, saved
18:02:04 and 2,389 seconds old. Its sampled 148 positive rows had zero funding
math errors and all twelve navigation views were nonempty; that did not prove
expanded coverage. The ordinary chart definition refresh did publish active
Aster STONKS at 18:34:46. Index and page arrival remain to be checked.

The split collector returns before the web startup refresh thread is created;
the web-role refresh helper intentionally declines to build. Consequently, the
nominal fifteen-minute funding catalogue refresh was still coupled to discovery
completion. The new independent collector publisher invokes the existing due,
retry and build-lock path every sixty seconds. It requests navigation refresh
only after a successful catalogue publication and leaves public quote/funding
I/O on their existing threads.

Review found that `complete_funding_catalog_worker.py` was also absent from the
shared heavy-worker set. The correction adds it, so a busy slot defers/retries
instead of allowing another heavy build. Actual collector bootstrap, publication
retry, navigation ordering and busy-slot regression tests cover these paths.
Removing bootstrap, navigation refresh or heavy-slot membership is detected.
The first full run reported 2,472 passing tests and one old exact-membership
assertion; that assertion was updated to include the deliberately serialized
worker. A repeated full run exposed an existing cold-reader test consuming a
cache left by an earlier run; that test now uses an isolated absent path and
resets its restore state. The final unmasked suite passed **2,473 tests in
101.00 seconds**, exit zero. Ruff ratchet remains 517 with no new
findings (an unrestricted `ruff check .` additionally scanned seven diagnostic
and documentation findings outside the established source/test ratchet).

Still required: refreshed UA references, production current-rate
checks and measured CPU/memory/latency. Do not call the finite sampler a clean
release window: the intervening deployments changed its container identities.

Fresh 19:01 UTC streamed production audit: 22,417 exact market records and
22,417 unique keys, zero duplicate extra rows, zero Ourbit, 2,171 futures token
labels and 3,962 spot token labels. The active Aster STONKS/USDT:USDT definition
is present. Funding remained v1: 500 tokens / 17,739 retained routes, 36,462,219
bytes, saved 18:02:04. This is the explicit before-publication baseline.

`861fae7`, source digest `54eb84e83a8185b3`, deployed to both containers at
19:04:23 UTC. Initial and immediate pre-restart discovery/finalizer guards were
clear. Both container source digests matched the tested source; HTTP health 200,
restart counts zero and OOM false. The initial cheap health sample had zero
priced rows while the index warmed; the 19:06:49 sample showed 174,555 priced
routes, 1,243 priced token labels and 0.858-second health response.

Automatic publication is now observed: saved 19:05:32 UTC, schema v2, 5,360
token payloads (including empty/nonmatching candidates), **220,079 retained
eligible pairs**, 50,731,947 file bytes. These payloads are not a count of unique
futures tokens or positive displayed opportunities. The worker reported a
40.002-second build and 258.6 MiB peak RSS; elapsed with scheduling was 61.9s.
At 19:06:49, public health confirmed the web reader had loaded this saved
generation and all 12 navigation views had refreshed using its source signature
(19:06:33 build, zero empty views). This proves actual publication and reader
arrival; detailed live arithmetic and route-reference verification are underway.

## Live verification and exact-history scheduling — 19:19 UTC

The new-generation funding probe passed: 146 sampled positive rows independently
matched their exact live funding legs, zero math errors / Ourbit; 956 tokens and
93,136 routes matched positive current carry. All twelve persisted views were
nonempty. Isolated read-only probe peak 227.5 MiB. Token-detail queries returned
STONKS, CATE, ASTEROID, ANSEM, ALIGN, KAITO, LCAP and 1000BTT. Exact detail includes
negative hedge directions for comparison; the broad ranked list remains positive.
The first isolated detail lookup took 12.75s, subsequent ones 0.06–0.62s: cold
lookup cost remains relevant and must not be represented only by warm timings.

The updated 45-case reference probe found 38 with both market definitions and
37 with an indexed route; the one defined but excluded route was the OPENAI
Gate spot/perp identity/index mismatch. The previously absent Aster STONKS route
is now both indexed and in the complete funding catalogue. Its exact Mexc Spot
to Aster Futures detail showed +0.03% current daily carry. The index still
carried a conservative mirage flag from its scanner evidence at that sample;
do not claim every indexed reference is immediately a positive, admitted spread.
CATE/ASTEROID/ANSEM/ALIGN and KAITO had exact catalogue/reference matches.

Actual authenticated Funding UI showed populated current leaders (including
1000BTT Mexc→XT), per-leg intervals, age labels, and many blank exact historical
columns marked settlement-refresh-overdue. Fresh collector logs repeatedly said
`market evidence deferred; current route index pending`. This was a real scheduler
starvation: the modern publisher's `publication_due()` overrode the older
recent-publication check, allowing every 120-second quote-index demand to veto
the 300-second history sweep even when a successful complete index was recent.

The local correction restores the recent-index allowance specifically for
history, retaining cold/stale index priority and all shared heavy-worker locks.
The regression uses the actual publisher's request/check/publication lifecycle,
proves history can take a turn with a recent complete index, proves cold and
stale indexes still preempt it, proves no overlapping index build, and proves
the pending publication runs after history releases the lock. The old scheduler
fails the recent case. Full suite **2,476 passed in 95.95s**, Ruff 517 unchanged;
old-method mutation caught. Deployment and actual settlement refresh remain to
be verified. No settlement completeness guard or refresh age was weakened.

A second finite read-only sampler began **19:14:36 UTC**, unit
`spreadboard-stability-publication-20260905`, output
`/opt/spreadboard/runtime/stability/20260905-publication`, duration two hours.
It is preliminary and will be split by any further deployment. The Codex
recurring automation remains paused; no notification or trading action occurred.

## Exact settlement publication verified — 19:55 UTC

The scheduler fix `a6e82e1` deployed at 19:27:31 UTC with matching app/collector
source digest `4a7e5f96478f5fd0` and both discovery/finalizer guards clear.
The first ordinary market-evidence worker ran to completion at 19:42:13; its
exact settlement file advanced at 19:34:56 and 19:38:46. At 19:51, public health
confirmed all twelve navigation views had consumed the new exact-settlement
and retained-radar generations, with zero empty views. An additional automatic
full-catalogue publication completed at 19:43:24 (49,972,625 bytes), confirming
that refresh continues beyond the first startup generation.

Authenticated Funding Now showed new exact totals for VELO Kraken Futures→Bybit
(9.20265008% / 63.61692023% / 47.38992667% for 24h / 7d / 30d) and ZIG
Mexc→Kraken Futures (4.18673069% / 5.20197141% / 34.81362272%). A fresh read of
the exact-leg cache reproduced those displayed numbers. These are observed
settlement totals, not projected returns. BEL, ICX and 1000BTT still had overdue
legs at that check; a second ordinary evidence worker was active by 19:54 and
had already advanced the file again. Remaining blanks are not declared fixed
from a file timestamp alone.

The first read-only history probe used `Kraken` instead of the canonical
`Kraken Futures` and an unsupported hypothetical LCAP hedge. Those probe cases
were discarded. `history-arrival-canonical.jsonl` uses observed exact routes;
do not use the preliminary `history-arrival-1945.jsonl` as missing-market proof.

The completed worker reached about 1,995 MiB RSS during archive collection and
the collector touched its 4,096 MiB cgroup ceiling, with OOM counters still zero.
The archive path retained rich dictionaries for all positive candidates even
after the earlier packed-catalogue change. A new regression invokes the actual
evidence worker: the old code retains 200 rich positive rows; the proposed
stream-and-compact path retains at most three, preserves all 200 records, and
keeps the same catalogue/warm/leader overwrite order and exact totals. Reverting
the archive iterator, the service call site, or compaction separately is caught.
Production-data allocation/output replay and the final release gate are being
completed before deploying this further reduction. Caps remain unchanged.

## Tested corrections awaiting a protected deployment window — 20:13 UTC

The final archive change yields positive/current-or-historical candidates and
compacts each to the existing radar field whitelist before retaining it. It
keeps the same identity key, catalogue/warm/leader ordering, JSON normalization,
retention and record cap. The real production-artifact replay wrote exactly
131,012,177 identical bytes before and after, SHA-256
`d7234dc829f48a29043ba196c84196df6896583ad27062ff2929a4171da57f3d`.
Initial local peak RSS fell from 1,207.80 to 798.03 MiB. An intermediate version
duplicated JSON conversion and was replaced with a plain field projection.
Local wall times varied substantially (65.53 / 88.59 seconds for the initial
before/final runs); a further compact replay took 228.43 seconds / 132.36 CPU
seconds and peaked at 561.66 MiB. Its comparison run was stopped to avoid
contending with validation. These are local allocation/output observations,
not delivered production savings or a passed production CPU gate.

The fresh queue probe exposed a separate correctness defect: OKX ICX's stored
history inferred a four-hour cadence, while its current funding schedule was
two-hourly. The public reader correctly expired the totals, but the priority
selector still said the leg was not due. The selector now receives the same
live schedule snapshot and uses the existing expiry logic to request the missing
settlement. No freshness or settlement-completeness guard is loosened. A real
`build()` regression first proves the public cell is expired, then proves a
provider fetch restores it; its companion proves a not-yet-due leg is left alone.
The old call site fails that test.

Final full gate: **2,479 passed in 153.99s**, Ruff **517 known / zero new**.
An earlier full run had 2,478 passes and one failure from an import-order lint
finding in the new test; the import was fixed and the entire suite rerun.
The earlier archive-only implementation also passed 2,477 tests before the
live-schedule regression was added. All three archive mutants and the old
schedule-selector mutant were caught. Test-generated runtime fixtures were
preserved in output and restored; source/runtime data from other tasks were
not touched.

Fresh deployment preflight at 20:06 found protected discovery worker **195600**
running, so no restart or source deployment was attempted. Production remains
`a6e82e1` / `4a7e5f96478f5fd0`. Wait for discovery and snapshot finalization to
finish, then repeat both the initial and immediate pre-recreation guards.
The 18:05–20:05 finite sampler ended successfully; the 19:14–21:14 sampler
continues. Both span deployments and require container-ID segmentation.

## Published schedule and exact-detail corrections — 21:25 UTC

The guarded release at **20:55:32 UTC** deployed `1d7f26c` with matching app and
collector source digest **1c6e27099ca47d3c**. Both discovery and finalizer guards
were clear before build and immediately before recreation. This includes the
previously pending archive compaction and history-schedule selector fixes.

A fresh additional fifteen-row UA capture expands the comparison to **59 unique
observed exact route identities**. All fourteen non-Ourbit references in that
capture had both definitions, an index entry, cached funding and a fresh price
pair; the fifteenth uses Ourbit and remains deliberately excluded. This is a
sample comparison, not exhaustive guest/premium parity. Earlier count evidence
remains 2,171 futures and 3,962 spot token labels, zero duplicate market keys;
large directed-pair counts are not extra tokens.

The comparison exposed incorrect assumed eight-hour funding schedules for BingX
and WhiteBIT, and missing Binance current schedule overrides. Fresh public
responses contain BingX 1h/4h/8h schedules, WhiteBIT explicit minute units, and
Binance current fundingInfo adjustments. The deployed readers preserve those
published schedules, prioritize current overrides over older market metadata,
and recognize BingX's nextFundingTimestamp. Invalid values still fail the same
normalization checks; unproven schedules remain explicitly estimated.

The normal funding writer, without a forced refresh, delivered **911 BingX +
305 WhiteBIT + 752 Binance = 1,968 matching exact legs with zero interval or
assumption-provenance mismatches** at 21:02:35 UTC. This does not prove every
WhiteBIT instrument was matched: the source has 398 rows and 305 matched keys
in that probe. Aster and Binance still have estimated legs outside the checked
published schedules. Evidence: `published-schedule-arrival-second.jsonl` and
the read-only `probe_published_schedule_arrival.py` in output.

A second defect was the exact-token Funding API taking the ordinary Spreads
shortlist path. A KAITO request claimed all exact pairs while rendering only
25 of 72. After bypass removal, the authenticated page rendered all **234 of
234** current/retained alternatives and a real funding age. Expanded coverage
can include negative or unavailable alternatives for exact-token investigation;
the broad ranked list still selects relevant opportunities.

Release validation: **2,500 tests passed in 174.54s**, Ruff **517 known, zero
new**. The exact-API old implementation fails both farm tests. The schedule
mutants fail 13 reader cases, 4 explicit-minute cases, and 3 current-override
cases respectively. The earlier preliminary parser fixture omitted markets_by_id
and was corrected; only the final tests/mutants are acceptance evidence.

A live ONG page then exposed a further display defect: net carry used the new
one-hour BingX rate while the leg caption still displayed the older rate and
"every 8h". The local correction synchronizes initial per-leg fields and the
SSE net/cadence/rate/age payload, updates collapsed groups by exact funding
identity, clears expired current data, and allows a blank current value to
recover. Historical selected totals are not converted to live projections.
This further change is **under full validation, not yet deployed** at this
checkpoint. Do not claim all Funding presentation is fixed yet.

The finite read-only current-release sampler began about 20:56 and ends about
22:56 (`spreadboard-stability-schedules-20260905`). Initial cold requests were
HTTP 200 but slow: health 20.631s and /free 35.209s. Later health recovered to
subsecond samples. Both containers remain healthy with zero observed OOM kills,
but the collector touched its 4 GiB ceiling and Funding navigation deferred at
2,165 MiB headroom versus its 2,200 MiB guard. At 21:20 the twelve nonempty views
still referenced the 20:44 generation. Limits remain unchanged. These facts
leave cold latency, navigation freshness, representative memory/CPU, safe lower
caps, a current-release hour and final clean 48-hour acceptance **open**. The
Codex recurring automation remains paused; no trading, messages, paid access,
notifications, cap increases or weakened accuracy guards were performed.

### Funding leg coherence release gate — 21:31 UTC

Final full suite: **2,507 passed in 198.90s**; Ruff **517 known / zero new**.
All thirty focused tests pass, including the unchanged 250 KB page budget.
The first full run passed 2,506 and failed that size guard; source indentation
and standalone JavaScript comment lines were removed from generated responses
before the full rerun. All source comments remain available to maintainers.
Old catalogue, stream-backend and browser-handler mutants are each caught.
The independent 21:27 catalogue read reproduced **22,417 unique market keys,
zero duplicates / Ourbit, 2,171 futures / 3,962 spot labels**, and 220,196 packed
funding pairs. Test-generated runtime data was preserved under output and
restored to committed bytes. Deployment and live page proof follow this gate.

## Funding coherence deployed and checked — 21:47 UTC

Release **767e464** started both services at **21:31:57 UTC**; both source
digests match **0e255781dba02cb5**. Initial and final protected-worker guards
were clear. Health returned 200, restart counts zero and OOM flags false.
The full gate remains 2,507 passing tests / Ruff 517 with zero new findings.
No further source change was made after that validated release.

Authenticated ONG detail rendered all **72 pairs**. At 21:35, a DOM-only audit
recomputed every displayed pair from its displayed rate and interval: **72
checked, zero arithmetic mismatches within explicit rounding tolerance, zero
unavailable values, zero Ourbit**. BingX -0.0606% hourly and Bybit +0.1053% hourly
corresponded to displayed +3.981% daily carry; the reverse matched -3.981%.
At 21:40 the same open page showed new rates -0.0579%/+0.0792% hourly and
+/-3.291% carry without a manual reload. These are timestamped UI observations,
not current trade recommendations. The browser initially lost a navigation
target; reconnecting to the same authenticated tab recovered it. An initial
DOM audit used unavailable parseFloat; the corrected Number-based audit ran.
Neither tooling issue is counted as an application/data failure.

The new ordinary Funding navigation generation at **21:38** reached all twelve
views. At 21:43 health confirmed zero empty views, 3,831 summed per-view token
memberships and 9,050 preview rows. Its worker reported **66.368s build time**,
**203.6s parent elapsed** and **777.7 MiB peak RSS**. These memberships are not
unique token counts. The earlier 20:44 generation had been delayed by headroom;
one later publication does not establish reliable ongoing cadence.

A post-release reconciliation of the same fifteen UA reference identities
again found all **14 non-Ourbit cases** in definitions, the current index,
complete Funding cache and fresh pairs. The excluded Ourbit route remains
absent. ONG's cache-derived net was positive (+2.940624% projected daily at that
later probe), so the prior wrong assumed-eight-hour sign mismatch is gone for
this observed snapshot. Peak probe RSS was 172,540 KiB. These source times
differ from UA's captured rows, so numerical equality is not asserted.

The performance-profiler baseline used the saved public production catalogue,
radar, history and funding files under a fixed clock. It produced twelve views,
3,226 groups, 7,876 previews and 163,688 matching routes. Peak RSS was 452.72 MiB
after ranking and 577.11 MiB after serializing the whole comparison result;
81.77s wall time. The full 43,411,193-byte output hash is
`1cd132005c64a443ff5047c67809d6b45c63efe046f3d52355f49f8459d7dfa1`.
This is an allocation baseline, not live correctness or Linux cap safety; no
speculative decoder, iterator or headroom change followed it. Graph extraction
and report reading completed; the optional hook installer failed on the Git
worktree's .git file, without affecting extraction or this release.

At 21:42–43 both containers were healthy with zero restarts/OOM counters and
about 184,900 priced routes. Initial sampled cgroup peaks were approximately
3,062 MiB app / 3,034 MiB collector. The ordinary history worker was still
running; its completed-cycle peak remains to be checked. **All memory limits
and the 2,200 MiB navigation headroom guard remain unchanged.** Cold latency,
sustained navigation publication, remaining resident memory, safe lower caps,
a deployment-free current-release hour and final clean 48-hour acceptance are
still open. The finite sampler ends about 22:56 and must be segmented by
container ID; the Codex stability heartbeat remains paused.

## 22:36 UTC — native funding coverage restored, final acceptance open

Release `8a1ddeb` (including `01bbf4a`) is live in app and collector with source
digest `0346ca22c4326ae9`, started at 22:27:33 UTC. Both protected-worker checks
were clear, including the immediate pre-recreation guard. No force was used.
The final full suite passed **2,574 tests in 100.54s** and Ruff reported **517
known findings, zero new**. The first native full run failed its Ruff test;
product-family failures now log only venue/family names, and the full gate was
rerun. Old-code mutants caught the identity, cache, audit and native-reader bugs.

| Venue | Before: matched current keys | After: matched catalogue keys | Cause and correction |
|---|---:|---:|---|
| WhiteBIT | 305 | 398 / 398 | CCXT treats `tradfiFutures` as spot. Read the native perpetual identity from the same funding response and remove obsolete spot-shaped cache keys. |
| BitMart | 0 | 359 / 359 | Installed CCXT has no BitMart adapter. Read active native perpetuals directly, preserve quote versus settlement identity, and use expected funding with its published schedule. |
| Coinbase International | 0 | 131 / 131 | No CCXT bulk funding method and no native bulk reader. Read current predictions and convert published nanosecond intervals to hours. |
| Bitget | 778 | 827 / 827 | The default bulk call reads USDT only. Request both USDT and USDC futures with current published schedules; isolate family failures. |

The ordinary collector published these **632 previously missing exact funding
keys** without manual cache injection. This is a count of contract data, not
632 new enterable opportunities. At 22:32:37 all 1,715 catalogue keys matched.
Independent native-field calculations at 22:33:26–28 found no missing keys or
interval/provenance mismatches for those 1,715. Rates did change between the
cache and source observations (ages approximately 20–103 seconds); this is not
a claim of simultaneous exact-rate equality or exhaustive trading readiness.

BitMart returned 1,215 records, of which 856 were delisted and excluded. Coinbase
returned 310 instruments, of which 131 were active perpetuals. The BitMart
current-rate choice follows its distinction between previous-period and next-
period funding; Coinbase documents its interval in nanoseconds. See the
[BitMart funding reference](https://developer-pro.bitmart.com/en/futuresv2/#get-current-funding-rate-v2)
and [Coinbase instrument reference](https://docs.cdp.coinbase.com/api-reference/international-exchange-api/rest-api/instruments/list-instruments).

The operator's Now audit also had false-success paths: it waived mismatches for
hourly venues and routes carrying settled-history metadata, and passed empty
or unverifiable runs. It now compares current projections for every venue,
requires explicit finite values and usable intervals, and returns incomplete
status when evidence is absent. Its existing tolerance is unchanged; this
operator diagnostic is not a substitute for the product's 95% accuracy gate.

The fresh 22:34:30 catalogue read contains **22,417 records and 22,417 distinct
venue/type/symbol keys**, zero Ourbit and zero duplicate keys. It represents
**2,171 futures token labels and 3,962 spot labels**. The owner's UA counts of
2,534 / 4,370 are comparable to these labels, not the 220,196 directed funding
alternatives. Eliminating every alternative would lose current, historical or
exchange-filter winners; the site-facing positive-opportunity policy remains.

Remaining discrepancies need explicit classification. At 22:24:55, 129 BingX
and 65 XT catalogue keys had no exact native ID in their current bulk funding
feeds. This does not prove delisting or an alias. Bitget's new feed also has
12 cache keys outside the catalogue (including native test/spot-shaped names);
they need an active-market identity check before retention is tightened. The
cumulative UA comparison remains 59 observed exact route identities, subject
to the earlier guest/premium restrictions. No exhaustive parity claim is made.

New browser verification was blocked by `ERR_BLOCKED_BY_CLIENT`; the former
authenticated tabs were gone. Backend/cache checks succeeded, but they are not
new browser-rendering proof. The 22:32 health request returned 200 in 0.926s
with 186,708 priced routes. All 12 navigation views were nonempty using the
retained 22:23 generation; that is not a new post-release navigation build.

Early new-release evidence covers only 27 host samples over 6.85 minutes:
zero OOM kills/restarts, both latest healthy, anon peaks 2,011 MiB app and
2,472 MiB collector. A sampled `/free` request returned 200 in **18.098s**.
The preceding release reached sampled anon peaks 2,917 / 2,868 MiB and cgroup
peaks 3,426 / 4,096 MiB. Neither window justifies smaller caps or 24/48h claims.

The rejected decoder experiment is recorded without a performance claim:
identical 99,822-row content, baseline 6.424s / 391.7 MiB versus prototype
7.334s / 468.9 MiB. No production decoder change was made. Earlier archive
compaction has not yet shown a delivered ordinary-worker RSS reduction.

Evidence is in `output/stability-20260905/`: `native-coverage-deploy.txt`,
`native-coverage-full-pytest-final.txt`, `native-coverage-ruff-final.txt`,
`native-coverage-final-arrival.json`, `native-arrival-independent-comparison.jsonl`,
`market-cardinality-native-release.json`, `native-release-early-soak-summary.json`,
`whitebit-*mutant.txt`, and `native-coverage-mutant-final.txt`.


## SpreadBoard stability and UA comparison — Codex continuation (2026-09-05, 23:18 UTC)

- Goal ACTIVE. Owner chose continued Codex work over the former Claude handover.
  Stability heartbeat `finish-spreadboard-stability-acceptance` is freshly
  confirmed PAUSED. No trading, messages, spend, Pushover, cap/subscription
  increases or weaker identity/freshness/exact-settlement gates.
- Own checkout `tmp/spreadboard-funding-publication`, branch
  `codex/funding-publication-cadence-20260905`. Leave separately dirty
  `tmp/spreadboard-funding-current-truth` untouched.
- LIVE source remains **8a1ddeb / 0346ca22c4326ae9**, started **22:27:33 UTC**.
  App/collector were freshly healthy, restart 0 / OOM false at 23:18. Local
  candidates **1b5a2bc** and **a90f108** are committed but NOT DEPLOYED.
  Protected discovery PID285544 is still running; guard returns exit12.
  Both discovery and snapshot-finalizer guards must clear before deploy and
  immediately before recreation. Use the final-guard deploy helper, no force.
- Latest full release gate: **2604 passed in 183.51s**, Ruff **517 known, zero
  new**. Status-only gate was2585; its first run had three incomplete test
  fixtures missing real settlement metadata, fixed without relaxing schedule
  assertions. Old status code fails9 cases; old builder code fails17 cases.
  Test-mutated tracked data is preserved under output and restored from HEAD.
- 1b5a2bc: native status vetoes in discovery, catalogue and bulk funding. At
  22:40, all128 BingX funding gaps were status25 despite true API flags; all65
  XT gaps had tradeSwitch=false despite isOpenApi=true. Later active definitions
  can reintroduce reopened contracts. Twelve Bitget funding-only extras were
  absent from current native contract definitions; reject unknown/spot IDs and
  prune their old malformed cache keys. Historical archives are not deleted.
- a90f108: Hyperliquid's PARA-ANSEM catalogue contract was not joined to ANSEM.
  Extend the existing per-market five-percent price-band alias gate across
  single, bulk, Funding/navigation and summary paths. Keep exact symbols,
  reject unpriced/disagreeing aliases, preserve native namespace for books,
  current funding and legacy native history. Equivalent native/CCXT identities
  dedupe; distinct builder contracts no longer collapse through shape fallback.
- Frozen production candidate replay at23:14:48: ANSEM27 ->39 exact routes;
  exact MEXC Futures -> Hyperliquid PARA-ANSEM route0 ->1, funding+0.43849008%
  projected/day, schedules4h/1h, basis-1.7894%, no mirage/quote mismatch.
  Both paths use one frozen book/funding cut. Probe peak129MiB. This is NOT
  deployed page evidence or an execution recommendation.
- Browser access recovered: authenticated Funding and UA tables were verified.
  Ordinary new navigation published22:46 (generation1788648395656080032-35a3134d0203)
  and a reloaded page consumed it. The older ranks label on an already-open
  page and the long publication gap remain freshness/UI follow-up items.
- UA funding sample at23:00:15 visible routes,12 exact matches in definitions,
  live index, funding cache and fresh pair builder; two intentional Ourbit
  exclusions; one ANSEM builder-alias gap now reproduced/fixed in candidate.
  All12 matched routes funding-eligible; values reconcile after APR/365 daily
  normalization with observation-time differences. Premium coverage prevents
  exhaustive parity. Keep earlier59 observed identities as a historical count,
  not an additive total with this overlapping sample.
- HFT: native Kraken ticker at22:47 showed roughly-1.59% per hour, consistent
  with the large current daily projection. Current Kraken/CEX long routes can
  exceed the5% gap guard without exact rail-contract identity, so broad Funding
  excludes them. Exact search showed a cooled retained route with fresh funding.
  Preserve that blocker; it is not proof of current entry readiness. No gate
  weakened to force comparator parity.
- Delivered8a1ddeb coverage remains:632 formerly missing funding keys restored
  across WhiteBIT/BitMart/Coinbase International/Bitget;1715 published schedules
  independently checked after ordinary rotation. Last cardinality22:34:22417
  distinct exact market keys, zero duplicates/Ourbit;2171 futures/3962 spot
  labels. Owner's UA2534/4370 label counts are not directed pair counts.
- Completed22:27:33–22:56:11 current-release segment:109 host samples, no OOM
  kills/restarts; anon peaks app3572.8MiB/collector3566.0MiB and cgroup peaks
 3584/4096.3MiB. App/collector CPU averaged0.87/2.09 cores. Five health probes
  200, priced186467–186707, max10.58s; five /free probes200, max18.098s.
  Only20.2min endpoint span, NOT the30-sample hour or48h. Smaller caps unsafe.
- Prior finite sampler finished successfully. New finite read-only observer
  `spreadboard-stability-native-status-20260905.service` started22:57:52,
  runs2h to00:57:52 UTC Sep6, runtime/stability/20260905-native-status/samples.jsonl.
  Freshly active. Segment every deploy; do not create duplicate observers.
- Next: wait for protected scan/finalizer completion; guarded deploy of both
  tested commits, digest parity and ordinary cache/index/navigation/UI arrival.
  Then investigate peak memory and cold/request latency, verify remaining exact
  history/ranking cases, current-release30-sample hour, safe smaller caps and
  clean48h. Prior watchdog drill/cap persistence and two backup timer firings
  already passed. Rejected decoder experiment remains rejected.
- Evidence: own `output/stability-20260905/` status/builder tests and mutants,
  `gap-market-status.jsonl`, `ua-funding-2300-reconciliation.jsonl`,
  `builder-alias-frozen-live-comparison.jsonl`, `hft-catalogue-gap.jsonl`,
  `native-release-through2256-summary.json`, current guard and observer logs.

Native status evidence: BingX contract status1 is active in its [official reference](https://raw.githubusercontent.com/BingX-API/api-ai-skills/main/skills/swap-market/api-reference.md).
The current native contract responses, retained in the probe, supply the XT
tradeSwitch and Bitget listing evidence. Hyperliquid's public metaAndAssetCtxs
response for dex para independently identifies para:ANSEM with active interest,
volume and hourly funding; the live catalogue already held its CCXT symbol.
Do not describe this as an absent exchange feed.
