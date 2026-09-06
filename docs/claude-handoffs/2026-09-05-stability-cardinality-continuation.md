# SpreadBoard continuation for Claude

## Exact contract evidence gate — 2026-09-06 08:06 UTC

Latest candidate **ef5ea99 / 553b9ae674866098**, NOT deployed, no waiter. Includes prior builder/native stock fixes. The tokenized registry previously accepted only a token and venue list, so one reviewed instrument could certify another same-label market. Each side now requires exactly one matching venue, market type and market symbol, with its own underlying, instrument, oracle, schedule and source evidence. Conflicting underlying, duplicate mappings, missing leg evidence and missing exact symbols stay blocked. Execution policy remains research_only. The full market-evidence array is not copied into every public row payload.

Tests: 22 focused passed; 8 new regressions fail the previous implementation. Full **2728 passed in 151.53 seconds, exit 0**; Ruff no new findings (516 known, unchanged 517 baseline), exit 0. Evidence exact-market-{original,full,ruff}.txt and exact-market-gate-exits.json. Source frozen after gates and generated tracked data restored. Manifest updated. Existing token-level-only registry entries now remain blocked until exact markets are reviewed; current bundled registry is empty. No registry entries installed and no production change.

Fresh native 08:05 UTC: Bybit GPROUSDT Trading, LinearPerpetual, stock/GoPro Inc; Hyperliquid io:GPRO active with native market metadata and current oracle/mark/funding. Saved gpro-exact-native-0805.json. Together with primary documents in the prior checkpoint, this permits preparing an exact scoped mapping; it does not prove identical pricing constructions or executable hedge readiness.

Fresh observer 08:03:55 remains sole PID 561510 active. Saved corrective-observer-0805.jsonl (filename approximate) spans 2,832.68 seconds with 24 coverage samples/five generations, 202,813–203,697 priced, within 10%, still not a full hour. No sampled OOM/restarts/unhealthy. Peaks unchanged from prior checkpoint. Preserve until approximately 09:16:39 UTC.

Next: prepare/review exact GPRO Bybit and Entropy registry records using official contract and pricing sources, prove retained row admission with copied public data. Runtime registry is separate from the helper's four baked data files; any installation needs explicit source parity/review, never assume helper deploys it. Then guarded candidate release after this observer finishes, normal-load coverage and RAM evidence, final defensible caps/48h/two successful normal backups. No cap changes, duplicate observers, trades or messages.


## Official stock pricing evidence and release runbook — 2026-09-06 08:00 UTC

Production remains b4230a7 / 272beeec7e0e9f49. Candidate a5f1706 / 204ed15b27775248 remains frozen, undeployed, with no waiter queued. Corrected the release runbook's stale source pins and observer instructions; it now preserves sole corrective observer PID 561510 until approximately 09:16:39 UTC and records the backup unit as already installed. Fresh observer liveness at 07:55:43 was active, same PID.

Saved comparison corrective-comparison-0755.json covers 2,131.92 seconds, 18 coverage samples and four generations: 202,813–203,504 priced routes versus 202,936 baseline, within 10%, not yet a full hour. No sampled OOM/restart/unhealthy/failures. App anonymous peak 3,054,432,256 bytes; collector anonymous peak 3,542,568,960 and total cgroup peak 4,294,111,232. No websocket samples in this interval, so unmatched phase/duration maxima cannot establish memory savings or safe lower caps.

Primary documentation now found through https://entropy.io/ -> https://docs.entropy.io/ (not the unrelated entropyio.com). Official asset directory explicitly maps GPRO to GoPro Inc, a US equity market, USDC, 5x. Entropy equity terms describe cash-settled derivatives without ownership, 24/7, and corporate-action termination rules. Official architecture says Redstone maintains oracle feeds. Equity oracle uses the public primary-venue price in regular hours, then a depth-weighted internal/external blend outside hours; the external input can be an approved after-hours venue or last print. This proves published mechanics, not an identical oracle across venues or current executable hedge.

Sources: https://docs.entropy.io/asset-directory/equity-assets ; https://docs.entropy.io/market-types/equity-perpetuals ; https://docs.entropy.io/equity-perp-mechanics/oracle-price ; https://docs.entropy.io/ . Browser DOM read succeeded where web extraction rejected markdown. Copies: entropy-equity-assets.md, entropy-equity-terms.md, entropy-oracle.md, entropy-architecture.md under output/stability-20260906/.

Bybit's official help article, updated Sep 4, describes USDT-settled TradFi perps with weighted index components, session-dependent stale-component exclusion, transition smoothing and product-dependent anchor constraints. https://www.bybit.com/en/help-center/article/Introduction-to-TradFi-Perpetual-Contracts?category=4d5d8649cba144c1a8 . This is a different construction from Entropy; the precise current GPRO component set remains to inspect. No registry entry or verified status was granted.

Next: registry admission must be scoped to reviewed exact venue/type/symbol combinations. Current token-level registry with only a venue list cannot by itself prove every same-label instrument. Continue GPRO Bybit -> io:GPRO comparison using these primary documents, verify current component/native price data, and preserve research-only execution policy. Do not use a broad token entry merely to bypass missing evidence. Complete observer, guarded release and ordinary memory/coverage validation, then safe final caps, 48 hours and two subsequent normal green backups.


## Bybit stock metadata and document review — 2026-09-06 07:52 UTC

Latest local **a5f1706 /204ed15b27775248, NOTdeployed**, includesbuilder946d43d andclassification2041056. FreshBybitGPROUSDT nativeinstrument identifies symbolTypestock,fullNameGoProInc,underlyingTickerGPRO/regionUS (bybit-gpro-native.json). AddedexactSWAPsymbolTypestock tosamepropagationpath. Fourfocusedcasespass;Bybitcasefailsoriginal; full **2718passed153.76sexit0**,Ruffno newexit0. Sourcefrozen/datarestored;candidate manifestupdated. Liveb4230a7/272beeec7e0e9f49unchanged,observer561510freshlyactive07:45:52;preserveuntil09:16:39.

FreshLOCALcatalogue actualthreevenuefetch: Binance754markets/155nativetagged,Bitget827/310,Bybit815/177;BNC/GPROtaggedtokenizedonall3. Evidence native-classified-catalog.json. Countsarevenue-markets,notuniquetokens ormemorysavings. Existingregistry/guardnotweakened; classificationisnotunderlyingequivalenceverification.

Primarydocuments: BitgetBNC https://www.bitget.com/support/articles/12560603888404 identifiesCEAIndustriesInc and24/7stockperpetuals; GPRO https://www.bitget.com/support/articles/12560603894181 identifiesGoProInc. Generalpricing https://www.bitget.com/support/articles/12560603835927 namesPyth/dxFeed/Massive/Intrinio andoff-hoursmarkEMA,24/7. BybitofficialGPROannouncement https://announcements.bybit.com/pt-PT/article/new-listing-gprousdt-tradfi-perpetual-contract-with-up-to-25x-leverage--art8e542b406998/ confirmsGoPro/24x7. Binanceofficialsearch-visiblelisting https://www.binance.com/en-AE/support/announcement/detail/84ad610bdd284699bc451b7baaa0ff7d mapsBNCtoCEAIndustries;directopenredirectsUKannouncementlanding. GeneralTradFiFAQ similarlyredirects;do nottreatredirectpageaspricingevidence. No accesscircumvention. EntropyIOpricing/oracle documentationnotverified;entropyio.comsearchhit isunrelatedbusinesssiteandmustnotbeused. No registryentrywrittenorverifiedstatusgranted. Preserveunresolvedvenuepricing/tradingschedulewhereevidenceincomplete;do not fillnonemptyregistrystringsmerelytopassguard.

Next: preserveordinaryobserver,reviewphaseRAM/coverageatfullhour; remainingexactstockcross-venuemapping/completeness andlatency;protectedcandidate releaseafterobservation,thenfinalcaps/48h/backups. No newwaiter/deploy/capchange.


## Exact-market RWA classification candidate — 2026-09-06 07:44 UTC

Latest local **2041056 /98f6041c157621e8**, includes946d43dbuilderfix, NOTdeployed. Liveb4230a7/272beeec7e0e9f49unchanged. PositiveBitgetisRwaYES/BinanceunderlyingTypeEQUITYmetadataonSWAPmarketsonly nowcarriedincatalogueitem->Leg->_routeanddiscoverytickerMarketQuote->identitynote->_row_from_api->publicrow. Requotedbookpreservessourcequoteclassification. Classifyhonorsexactrow/legassetclass,so bareBNC/GPROcannotbypassguardthroughmissingSTOCKsuffix. Doesnotclassifysame-tickerspotcrypto ordeclarelegalinstrument/oracle/identityverified. UnprovedRWAremainsresearch-only; fundinggateconsistentacrossspellings. No newregistryentry/fundingadmissionrelaxation.

Three newbehaviorcasescovernativecataloguepairsguardandspotnegativecontrol (bothvenues), plusdiscoveryticker->notes->publicserialization.27focusedpass;threeoriginalpipelinefailures; full **2717passed162.05s exit0**,Ruffno newexit0. Trackedgenerateddatarestored/sourcefrozenaftergates. Evidencestock-class-{targeted,original,full,ruff}.txt. Newfieldsarepositive-evidenceonly oncatalogue/rows; avoidnewper-rowmarketlookup/cache orbulkJSONparse. Changespublicdataonly.

Freshobserver561510active07:41:32. Frozen corrective-observer-0740.jsonl/analysis:1492.49s (~24.87min),13coverage/3gens,202813–203345pricedvs202936base, within10%true buthourprooffalse; nosampledissues. Preserveuntil09:16:39;no waiter/deploy/capchanges. Next: reviewremainingstockidentity/oracle/hours evidence andadmissioncompleteness, verifyBNC/GPROexactnativecounterparts (no blanketalias), thenprotectedreleaseafterobservationandordinarycoverage/memory/UIvalidation. No claimthatclassificationfixalonemakesstockFundingcomplete.


## Builder catalogue fix tested — 2026-09-06 07:36 UTC

Latest local **946d43d /af270de5de2d61b8, NOT deployed**. Production remainsb4230a7/272beeec7e0e9f49; observer561510 remainssolepreservedjob until09:16:39. Newsharedpublicclientconfiguration removesCCXT's first9builder truncation usinga looplimit sentinel; installedadapter stopsattheactualadvertisedmetadata length. Explicitdexlistsandotherexchangesunchanged. Wiredchartcatalogue,bulkclient,fastclient andfastfundingmetadatareload. Existingmarketstatus/price/identitygatesunchanged; executionclientsuntouched.

OfflineactualinstalledCCXTfetch_hip3_markets test throughallthreeclientcallers proves10thbuilderloadedexactlyonce; explicitlistcasepreserved. Originalthreecallers allfail. Fourfocusedpass; full **2714passed156.73s exit0**,Ruffno newexit0. Earlytargetedtestcaughtmissingfastclientwiring andwasfixedbeforefullgates;sourcefrozenaftergates/testgenerateddatarestored. Evidencebuilder-all-{targeted,original,full,ruff}.txt.

FreshLOCALpubliccatalogfetch13.40s returned315supportedHLmarketsvsproduction310: addedIO-OAI,IO-ANTH,IO-SNDK,IO-NBIS,**IO-GPRO/USDC:USDC**, nativeid200006. Evidencebuilder-all-native-catalog.json. This provesdiscoverythroughrealadapter, notproductionmembership/carry/identityreadiness orRSS. Candidate manifestupdated; livepinretainedseparately. Needstockclassificationfixnext: per-marketBitgetRWA/BinanceEQUITYmetadatacurrentlydiscarded, plainBNC/GPRO wronglycrypto whileSTOCKspellingsblocked. No newreleasewaiter/deploy/capchange.


## Stock comparison root causes — 2026-09-06 07:30 UTC

Liveb4230a7 unchanged; no source edits in this checkpoint. Observer561510 freshlyactive07:25:28, preserve to09:16:39.

**GPRO missing io catalogue root cause found:** installed ccxt.hyperliquid options fetchMarkets.hip3.limit=10; actual fetch_hip3_markets uses `for i in range(1,maxLimit)`, so loads only first9builders. NativeperpDexs has10non-nullbuilders and io is10th. Thus freshlygeneratedchartcatalog07:24:38 advertises Hyperliquidok310markets but contains NOio markets; nativeio:GPROisactive. Existingdiscovery snapshot06:35:18 contains3HyperliquidGPROrows (HL->XT/HTX andHTX->HL), provingnativecollectorpath seesit, underGPRO notGPROSTOCK. Maincatalogue/completepair path lacksio, soBybit->io:GPROnotconstructed. FixpublicCCXTconfiguration toloadallreturnedbuilderDEXes consistently inchartcatalog/bulk/fast clientpaths; retainnativecontractidentity, pricecorroboration andguards. Do not merelyaddGPROalias orraiseRAMcaps. Inspectinstalledadapter behavior; useoffline actualadapter test demonstrating10thbuilderomission andcallerwiring. Nofixyet.

**BNC comparison resolved as spelling, but classification defect found:** copiedfundingcatalog hasBNC20rows includingexactBitget Futures BNC/USDT:USDT->Binance Futures same symbol,+1.205754%day at saved07:07:57; BNCSTOCK0. So12priorcounterparts becomes13whenexactnativeidentityspellingmapped, plusOurbitexcluded andGPROHLremaininggap; do not justsumoverlappingcomparisons. BNCrow's tokenized_guard sayscrypto/not_applicable, mirage_guardedfalse. GPRO56rowsalso crypto, whileSTOCKspellingblocked. Fresh nativecontracts07:29:20: BitgetBNCUSDTandGPROUSDTisRwaYES; BinancebothcontractTypeTRADIFI_PERPETUAL/underlyingTypeEQUITY. Currentchartcatalogdrops these nativeclassification fields. `tokenized_assets.classify` infersSTOCKsuffix/knownnames/narrowbuildernamespacesonly, so baretickers bypassstockguards. Correctper-market metadata propagation/classification withoutclassifyingunrelatedBNCspotcrypto asstock, conflatinginstruments, orblanketweakeningrankability. ExactRWA/equityevidence is in stock-native-contracts.json. Nofixyet.

Relevantfiles: spreadboard/chart_catalog.py _load_venue createsplainCCXTclient~300anddropsnativefields; spreadboard/bulk_quotes.py _client; spreadboard/fast_quotes.py _client~1283; spreadboard/catalog_pairs.py _builder_spelling_targets handleshyphenCCXTnames andprice-gatedstockjoining; _make_row classification~1609; spreadboard/tokenized_assets.py classify; src/spreadarb/api_discovery/sources.py buildercollect~988. Evidencechart-catalog-0727.json/chart-stock-markets.json,discovery-0725.json/discovery-stock-rows.json (snapshotactually06:35),stock-native-contracts.json,hyperliquid-gpro-bnc-native.json. Worklocallywhileordinaryobserverruns; no productiondiagnosticchildren or redeploy yet.


## Early release verification and new UA gaps — 2026-09-06 07:24 UTC

Live remainsb4230a7/272beeec7e0e9f49. Soleobserver561510 freshlyactive07:19:23. Initial12hostsamples167.68s,2coveragepoints202936–203067/singlegen1, no sampledissues; /free17.704s. Too early forcoverage/memoryacceptance. Frozen corrective-observer-initial.jsonl/analysisinitial. Do not restart observer or altercaps.

Actual deployed Funding ICX page loaded after a navigationtimeout by rereading SAMEtab28. Mexc Futures->Kraken Futures +2.976%day; legs-.0833%/4h and+.1032%/1h corroborate arithmetic; exact24h/7d/30dblank due settlementcoverage. Fresh native07:20:43 Mexc-.0808%/4h;Krakenabsolute.0000147331744355/mark.01438972954 gives current%rate; signs/cadencesagree, ratesmoved slightly fromUI's2minsnapshot. Evidenceicx-native-post-release.json. UIlatency remainsopen.

UA~07:21 visible15Futuresleaders: BONERMexc->Gate,ACEWhiteBIT->Bingx,ANSEMAster->HL,ONGBinance->Bybit,LAMexc->WhiteBIT,STORJMexc->WhiteBIT,GPROSTOCKBybit->HL,SPACEHOODMexc->Gate,BNCSTOCKBitget->Binance,COTIGate->Bingx,SKRBybit->Bingx,SHROOMGate->Aster,PONSHL->Ourbit,ESPORTSMexc->Bitget,NESOKX->Kucoin. Copiedcatalog saved07:07:57 has12exactcounterparts; PONSOurbit intentionallyexcluded. **GPROSTOCK and BNCSTOCK unresolved**, zeroFundingcatalogroutes; do not claimcompletecomparison. Evidenceua-futures-0722-comparison.json andcopiedfunding-replay/ua-0722-catalog.json (filenameapproximate).

Actual GPROSTOCKSpreadsUI has31routes,0fundingpairs,8CEXvenues,noHLinvisiblefilters; DDpending tokenized. Fresh publicHyperliquidmetaAndAssetCtxs acrossall10builderDEXes finds **io:GPRO active**, funding.0005899426/hour,mark1.7855,mid1.7907,OI454653.6,volume947351.62. Sourcealreadyrecognizesstocktickers anddoespricecorroboration in src/spreadarb/api_discovery/sources.py~1030. Need tracewhy nativeio:GPRO notpresent incurrentSpreads andwhy stockFundingrowsineligible; separateidentity/collectorcoverage fromFundingeligibility. No blindalias/guardweakening. BNCSTOCK requires separate nativeidentity androutecheck. Evidencehyperliquid-gpro-bnc-native.json. CUA tab29UA/tab30GPRO maycloseafterturn.


## Corrective release LIVE — 2026-09-06 07:19 UTC

**b4230a7 /272beeec7e0e9f49 is now LIVE.** Guard clear before helper and immediately before recreation. Deployment session45823 terminalexit0; health200, both app/collector source+data digests match, oomfalse/running/restart0. Evidence corrective-release-deploy.txt. No caps changed. Backup unit separately syntax-verified and installed after PID0 check, daemon-reloaded only; effective TimeoutStartUSec2h, SHA25644bf4298826351d5b2f3136692d3da68a421b5977f8f8eb2d4c3d690eb0630b8. No backup started. Next normaltimer freshly12:18:27UTC. Failed06:49run remains failed.

Previous combined observer499780 is **terminal**, inactive/PID0/Resultsuccess/exit0 and finishedmarker1788678770.8109705 (~07:12:50). Frozen combined-observer-complete.jsonl + combined-analysis-complete.json:473host/7195.36s,42coverage/6928.67s,10gens,priced64083–202974 vs201569baseline, **coverage FAILED**. No sampledOOM/restart/unhealthy/endpointfailures; /free max22.561s,health7.572s. Appanon3188183040/current3750916096,collectoranon3879731200/current4294447104. This does not justify proposed lowercaps.

**NEW SOLE OBSERVER** spreadboard-stability-corrective-20260906.service, **PID561510**, started07:16:39UTC,2h/RuntimeMax7500s, expectedfinish09:16:39. Host-side standardstability_soak.py, remote /opt/spreadboard/runtime/stability/20260906-corrective/samples.jsonl. Preserve it; no secondobserver/restart/profiling children. No releasewaiter queued. Automation remainspaused. Old combined observer must not be restarted.

Initial actual UI ICX /markets?q=ICX: current headline basis correct; expanded WhiteBIT Futures->Kucoin Spot current funding-.030%day/leg+.0100%every8h, calculator payload current-.03 distinctfrom settled1d+.205881. Open calculator preserved separate horizons:1d Settled1d+$2.06;3d Currentrateprojection-$0.90 at$1000perleg. This verifies initial rendered sign/basis distinction, not a natural live signflip. Evidence corrective-release-ui-initial.json; CUA tab27 maycloseafterturn. Candidate automated cases coverexpiry/recovery/signflip. Next: ordinary afterrelease coverage/RAM/CPU/latency acrossgenerations/navigation, broadercurrentUI/UAchecks, defensiblecaps thenfinal48h andnormalbackups. GoalACTIVE.


## Bounded backup candidate — 2026-09-06 06:58 UTC

Latest **b4230a7 /272beeec7e0e9f49**, undeployed, includes all preceding fixes. Backup rclone calls use timeout5m/connections2, default request pacing4/sec/burst1 (preserve operator values); probe timeout360s. Non-rclone commands unchanged. Unit TimeoutStartSec2h prevents infinite stall across six-hour schedule. Retention7daily/4weekly/3monthly, check1/20 and failure propagation unchanged; no automatic mutation retry added. Installed restic0.16.4 advertises options; default opening timeout1m, production service currently timeoutinfinity. Official docs https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html and https://rclone.org/docs/ . Shared project quota may still fail; do not claim fixed until normal firings pass.

Actual run_backup path test fails originalsource;12 focused pass; final **2710passed149.82s exit0**,Ruff no new516/unchanged517baseline exit0. Two preliminary full runs explicitly interrupted exit130 before wrapper/test correction; final clean run supersedes them. Tracked generated data restored, source frozen after final gates. Manifest pins new source and backupunit hash. Guarded deployment helper does NOT install the backup unit: after sole observer finishes and backup remains terminal, apply/verify unit separately with daemon-reload and no manualbackup start. No production changes made yet.

Fresh06:58:14 observer499780 active/noexit; preserve to~07:12:50. Backup remains failed06:49 snapshotb691030d/forgettimeout after Drivequota. Next normal timer12:18:45 at prior freshcheck. Goal active; no waiter/capchanges/automation revival.


## Current headline fixed; normal backup failed — 2026-09-06 06:52 UTC

Latest candidate **f260666 / c8b10e2fa4585bcd**, undeployed, includes all prior fixes. Collapsed Spreads headline now labels its Now-selected value with current-rate basis and retains live hooks through unavailability. Previously legacy settled provenance could label current funding as settled and disable updates/recovery. Two new tests fail original;73 focused pass; full **2708 passed146.98s exit0**, Ruff no new516/unchanged517baseline exit0. Source frozen and generated data restored. Manifest and runbook updated; no release waiter or cap change.

Fresh06:51:35 sole observer499780 remains active; preserve through expected07:12:50. Normal backup532835 is now terminal FAILED:06:21:16–06:49:00, Result=exit-code, ExecMainStatus1. Log confirms snapshot **b691030d saved**, followed by `restic forget --prune` repository-open rclone timeout after repeated Drive shared-project quota403/429 errors. Thus the complete run failed; do not count snapshot creation alone as successful backup. Service peak2.0G/swap98.8M. The later /var/crash read-only error is secondary exception reporting, not backup root cause. Evidence output/stability-20260906/backup-0621-terminal.txt. No manual retry/prune/config change performed. Next investigate bounded backup timeout/retry strategy while preserving observer and protected workers, then guarded corrective release and normal-load acceptance.


## Net edge initial funding corrected — 2026-09-06 06:48 UTC

Latest candidate **c2192bf**, source/data digest **eb169789768c17d5**, NOT deployed. Includes all earlier candidate fixes. `render_net_edge_button` previously preferred settled 24-hour history when filling `current_funding_24h_pct`; only a later stream tick corrected it. It now uses the same current-only selection as the headline. No settled-history fallback when current funding is unavailable. Four regressions (negative, zero, projected fallback, unavailable) fail old code; all 25 focused tests pass. Full **2,706 passed in 158.56s, actual exit 0**; Ruff no new findings (516 against unchanged 517 baseline), actual exit 0. Test-generated tracked data restored; no source edits after gates.

Release manifest/runbook updated to the new pin; old manifest archived locally. No release waiter, deployment or cap changes. Observer499780 and backup532835 freshly nonterminal at 06:44:51 UTC. Preserve the same jobs. Still required: completed baseline, protected corrective release, live funding/coverage validation, defensible caps and final 48-hour/backups evidence.


## Release verification runbook — 2026-09-06 06:41 UTC

Start with `docs/operations/2026-09-06-candidate-release-verification.md`. It separates release success from coverage, UI coherence, safe-cap and final 48-hour acceptance. The local candidate manifest freezes source/data digest and build/helper hashes. No source changes, release or new waiter in this checkpoint.

Fresh 06:38:07 UTC: observer PID 499780 remains active/running; backup PID 532835 remains activating/start, with no exit timestamp. Neither is terminal. Candidate digest rechecked `07c8589ad5765ecf`. Preserve both jobs; recurring automation remains paused. Historical checkpoints below are not instructions to restart old waiters.

## Streaming index writer candidate — 2026-09-06 06:34 UTC

**Latest local9ae48fb/digest07c8589ad5765ecf, NOT deployed**, includesfc8e2f4installation,2746e4efundingUI,6ab7440navigationmemory,7b7f904cache. Default live-index writer now serializes/hashes one route at a time and atomically replaces pointer only afterfsync. Samebytes/checksum/order, supplied-encoded path retained, previousindex readable onfailure. Full2702passed140.73s exit0;Ruffno new516/unchanged517baseline exit0;16focusedpass;originalwriter2fail3pass. Datarestored/sourcefrozenaftergates. Details2026-09-06-streaming-index-writer.md.

Realcollectorpeak06:25:11 anon3879731200bytes withoutwebsocket; index2254924KiB/discovery643724KiB/supervisor504084KiB RSS, hostavailable552960KiB. Current index512479951bytes. Thus proposedcollector3.5GiB remainsunsafe. Synthetic208911-route512997577bytewriter comparison matchedSHA256exactly:incremental tracedpeak541884881->3521252bytes;tracedtime1.197->3.259s. This is encoding allocation only, not deliveredRSSsaving; do not subtractitfromrealpeak tochoosecaps.

Fresh06:33:39:soleobserver499780active;normalbackup532835activating/noExecMainExitTimestamp, notyetcomplete. Preserveuntilobserverexpected07:12:50;no newobserver/waiter/restart. Livef047ccf/capsunchanged,automationPAUSED. CoveragehourstillFAILEDbecausegen5drop64083; retainedasbaseline. Nextcompleteobservation/backup,phaseandcapdecision,guardedfixrelease,thenordinarycoverage/memory/UIverificationandfinal48h.

## Install reconciliation measured locally — 2026-09-06 06:25 UTC

Latest candidate remains **fc8e2f4**, source digest **593e42d43bb2a75d**, NOT deployed; no release waiter. Full2697tests/Ruff gates unchanged. Recurring automation freshly confirmed PAUSED. Production observer499780 and normalbackup532835 were freshly active06:22:34; preserve both, do not count backup's interim Result=success as completion.

Synthetic offline208421-route Mac replay verifies all208421complete observations in every case. Untraced install:normal1.571s,forced concurrent-refresh2.099s; longestreaderlock0.757s in race versus0.000040s normal. Traced incremental peak allocation:normal39232952bytes,race58807196bytes,delta19574244bytes; retained27699196vs31226252bytes. Allocation excludes setup; profiler slows the tracedcase to11.19s, so use untraced timing. Not productionRSS/headroom or attribution of06:03coveragefailure. Rare reconciliation can stallreaders, requiring after-release latency observation, but the boundedlocalcost is now measured. Evidencecandidate-install-profile-manifest.json andinstall-profile-*.json; allprofileprocesses terminalexit0, no production profiling.

Release preparation: retainfailedbaseline as evidence rather than callingitgreen. Wait for soleobserverterminalsuccess/finishedmarker, inspect completephase/coverage and backup, then make explicitcapdecision. No cap reduction justified yet. Afix release may use a documented failingbaseline; never reuse oldwaiters whose sourcepins or passpredicates differ. Freshbothprotected-worker guards remain mandatory before guardedhelper/recreation. No deploymentqueued now.

## Generation-install candidate — 2026-09-06 06:22 UTC

Latest local **fc8e2f4**, undeployed, includes2746e4e/6ab7440/7b7f904. Reconciles a price refresh completed during structural preparation before install publishes; retains all chosen quote tuple fields instead of truncating exact price/index evidence. Three original-source failures; isolated original-install-only mutant still yields priced0instead1 with fixed tuplemerge, proving the race separately.25focusedpass;full2697passed155.32s exit0;Ruffno new516against unchanged517baseline exit0. Tracked data restored;source unchanged aftergates. See2026-09-06-generation-install-coherence.md andinstall-coherence-*.txt.

Production06:03 generation5coverage drop remains a FAILEDhour gate. This reproducible race is not yet causal attribution for all of that drop; parsing/refresh delays and real quote expiry remain alternative contributors. New rare reconciliation allocates anothermap underreaderlock: benchmark/observe latency andRAM afterrelease. Do not weakenfreshness or fake coverage.

Livef047ccf unchanged;no releasewaiter/capchanges;automationPAUSED. Soleobserver499780 freshlyactive06:20:42, preserveuntil07:12:50. Normalbackup started06:21:16, freshlyactivating/start PID532835 at06:21:31; NOT terminalsuccess despiteResult=success/ExecMainStatus0 duringstart. Poll sameunit/PID;no manualbackup. Next: finish baseline,reviewfailedcoverage/capdecision,guardedrelease andordinaryvalidation;full48h stillunproved.

## Coverage gate failure and tested coherence fix — 2026-09-06 06:14 UTC

**Priority: investigate the generation-install coverage drop before release/cap changes.** Frozen combined-observer-hour-check.jsonl spans3638.85s,240hostsamples,31coverageover3618.64s. Priced coverage FAILED±10%:201856/gen4 at06:01:05,64083/gen5 at06:03:06,202127/samegen5 at06:05:06. Route_count remained208421 during the drop/recovery; funding_only jumped144329, refresh_seconds0 at the drop. This points toward generation install/first price overlay but is not yet causal proof. No container change/restart/OOM/unhealthy/endpointfailure. Do not claim one-hour success from issues=[]: coverage.within_10pct andone_hour_log_proof are false. Evidence combined-comparison-hour-check.json and combined-coverage-drop.json. Preserve observer499780 to07:12:50; no duplicate/restart.

**Latest local candidate2746e4e, NOT deployed**, includes6ab7440memory exclusions and7b7f904cache revalidation. Fixes confirmed Spreads funding-coherence defect: every board stream derives current funding/leg metadata from one snapshot; row/group live direction/rate/cadence hooks, expiry/recovery, Net edge dataset and already-open exact-route calculator update together. Settled windows and quote/user state preserved. Full2694passed174.13s exit0;27focusedpass3.32s; six original-source/JS failures, five unaffected casespass; Ruff no new516against unchanged517baseline exit0. Tracked generated data restored; no product edits after gates. Docs2026-09-06-spreads-funding-coherence.md.

Live remainsf047ccf; no queued waiter/capchanges; automationPAUSED. Hour memory:appanon3035693056/current3446505472;collectoranon3204747264/current4294447104. CPU.859/1.936cores. /free13samplesmax14.906s;health31max5.251s. Index36samples,nonewithwebsocket. These observations do not establish safe lower caps or final48h. Nextnormalbackup06:20:56UTC, latestcompleted01:15:36success.

## New Spreads stream coherence defect — 2026-09-06 06:05 UTC

**Next correctness fix: expanded Spreads funding updates are not coherent.** Current ICX page WhiteBIT Futures -> Kraken Futures displayed live +3.225% while static text still said pay, leg rates +0.0100% / -0.0805%, and Net edge retained -1.962087%. Fresh Kraken06:03:52 PF_ICXUSD rate=0.000020114131200275quote/base and mark0.0148081074 confirm the rate changed positive; the old metadata, not necessarily the live numeric sign, is wrong. _board_stream_rows only reads coherent funding snapshot/legs when funding_only is set; render_market_group_route omits live hooks for explanation/legs/cadence. Fix stream payload plus all dependent displayed values/actions together, retaining exact settled-window semantics and route identity. Add actual DOM event behavior tests, including sign flip/unavailable and net-edge data. No fix yet. Evidence ua-purr-icx-ui-0603.json and icx-native-kraken-ui-audit.json.

UA05:58:59 guest futures15leaders:12saved exact counterparts after explicit Kucoin -> Kucoin Futures adapter-name normalization. Copied generation saved05:43:03, sha256db8efa8d2a8f96b7c06b53827c5e0a3dd1b6222eedc116429ed35df739f74689; not same-time UI proof. ICX OKX->WhiteBIT missing from saved generation but present in current expanded UI (+0.933%day, -0.1505%/4h and+0.0100%/8h; DDidentity warning). BMNRSTOCK remains earlier researched case, not freshly recertified here.

UA PURRSTOCK Bitget->Hyperliquid shows12.483vs0.1203 and-99.04%basis. Fresh Bitget ticker12.487, native PURRUSDT contract isRwaYES; official listing identifies stock underlying Hyperliquid Strategies Inc. Hyperliquid native core PURR mark0.12033 corresponds to token PURR; official docs distinguish token. This is an identity mismatch, not a missing valid opportunity to add. Our current PURRSTOCK UI has6other routes,0Fundingpairs,noHyperliquid. Sources https://www.bitget.com/support/articles/12560603893437 and https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm/hypercore-less-than-greater-than-hyperevm-transfers ; native JSON files purr-native-{bitget,hyperliquid}.json.

Production observer499780 freshlyactive05:57:26. Backup freshly success (00:19:49–01:15:36 exit0), nexttimer06:20:56UTC. Preserve observer until07:12:50. Livef047ccf; local6ab7440 stillundeployed; no waiter/capchanges; automationPAUSED. Browser navigation timeouts for T/ICX were followed by reading the SAME completed tabs, not duplicate requests. No production diagnostic child or orders/messages.

## Navigation callers candidate — 2026-09-06 05:57 UTC

Latest local candidate **6ab7440**, not deployed, includes history exclusion 0b534e5 and cache revalidation 7b7f904. Live remains f047ccf. No release waiter queued; caps unchanged; automation PAUSED; preserve sole observer499780 until expected07:12:50UTC and freshly check terminal state before acting.

The independent FundingCatalogPublisher now receives the actual collector RefreshLoop and takes the heavy lock plus websocket pause around navigation after catalogue publication completes. The bulk-funding scheduler pauses via its route publisher's refresh loop inside its existing heavy lock and after route-priority checks. Both restore on failure. No cadence/headroom/retry/source semantics changed; bulk quotes continue. Reviewed history/post-discovery paths also exclude websocket; actual production peak/freshness still needs proof after release.

Final suite **2690 passed139.33s exit0**; Ruff no new516against unchanged517baseline exit0. Four new cases fail original caller methods. An initial full run overlapped a formatting edit and failed11 source-inspection checks (2679passed); it is rejected and retained as evidence. The clean final run supersedes it. Tracked generated data restored; no source edits after final gates. See docs/operations/2026-09-06-navigation-callers-memory.md.

Frozen current-production pre-hour log:164host/2480.87seconds,21coverage/fourgenerations,201162–201998priced, no sampled failures/gaps. Index21samples, none withwebsocket. Collectoranonpeak3204747264bytes, total4294258688; appanon2826043392. Only41.35minutes, not an hour/savings/48h proof. Next: finish existing observation and backup, compare ordinary phases, decide safe caps then guarded release; do not reuse stale waiter/source pins.

## Evidence-navigation candidate — 2026-09-06 05:48 UTC

**Live remains f047ccf. Latest local candidate is 0b534e5**, including same-key cache fix 7b7f904. It is not deployed and no release waiter is queued. Sole observer 499780 was freshly active at 05:48:19 UTC; preserve to expected 07:12:50 UTC. Caps unchanged; recurring automation PAUSED.

MarketEvidenceLoop previously resumed websocket after the evidence child but before the funding-navigation child, while retaining the heavy lock. The candidate retains the pause through navigation and restores it on success, failed evidence, evidence exception and navigation exception. Other navigation callers remain a separate follow-up. Bulk quote collection, cadence, funding semantics and headroom guard are unchanged. The longer fast-lane pause requires production freshness validation after release.

Validation: 57 focused tests passed (2.77s), four new behavior cases included. Original-source mutant fails two exclusion cases and passes the two evidence-failure cases, exit 1. Full suite 2,686 passed (154.46s), actual exit 0; Ruff no new findings, 516 known versus unchanged 517 baseline, actual exit 0. Tracked generated test data restored. No source edits after gates. Evidence and review: docs/operations/2026-09-06-evidence-navigation-memory.md and output/stability-20260906/evidence-navigation-*.txt.

Next: review FundingCatalogPublisher and _schedule_funding_navigation lock/refresh-loop wiring; finish the existing normal observation, compare phases and normal backup outcome, then choose safe caps and guarded release. The measured navigation/websocket overlap alone does not identify which caller launched that production child. No whole-service overlap elimination or delivered memory-saving claim yet.

## Ordinary-load checkpoint — 2026-09-06 05:43 UTC

Live remains f047ccf; candidate 7b7f904 is not deployed and no release waiter is queued. The sole observer, spreadboard-stability-combined-20260906.service (PID 499780), was freshly active at 05:41 UTC. Preserve it until its expected 07:12:50 UTC completion. The recurring automation is confirmed PAUSED.

Frozen combined-observer-0030.jsonl contains 116 host samples over 1,751.67 seconds (29.2 minutes, despite the rounded filename), 15 coverage samples, 201,162–201,699 priced routes and two generations. No sampled OOM, restart, unhealthy state, endpoint failure or gap flag. This is not an hour proof. Six /free requests peaked at 14.906 seconds; 15 health requests peaked at 5.045 seconds.

Collector anonymous memory peaked at 3,204,747,264 bytes at 05:28:05, with websocket and funding-navigation workers present. Index was present in 15 samples, all without websocket; this supports the intended exclusion but cannot rule out brief unsampled overlap. Source inspection shows _schedule_funding_navigation takes the heavy lock without websocket pause; other navigation entry paths also require review before any change. App anonymous peak was 2,794,663,936 bytes. Mean CPU was 0.837 app / 2.036 collector cores. These short-window peaks are not a causal saving versus the prior two-hour baseline.

Collector sampled total memory reached 4,293,664,768 bytes (99.97% of its 4 GiB limit). A separate host-side cgroup read at 05:43:02 reports memory.events max=2561, oom=0, oom_kill=0: the limit caused reclaim attempts, not an OOM. At that later instant, file cache was 1,140,932,608 bytes, predominantly inactive; anonymous memory was 1,019,781,120 bytes. Do not interpret total-memory pressure as all unreclaimable anonymous memory or infer safe lower limits from this one read. Caps remain unchanged pending the full phase comparison.

Evidence under output/stability-20260906/: combined-comparison-0030.json, combined-collector-peaks-0030.json, collector-cgroup-0545.json (embedded timestamp is 05:43:02, filename is only a label). No production diagnostic child, restart, cap or source mutation was performed.

## Cache-revalidation candidate — 2026-09-06 05:30 UTC

**Live remains f047ccf / bc8129844c597cd1. Candidate 7b7f904 / f276ddefc946e561 is NOT deployed.** No release waiter is queued. Sole production observer499780 (`spreadboard-stability-combined-20260906.service`) was freshly active; preserve it to about07:12:50UTC. Recurring automation staysPAUSED.

Local reproduction found that a20second-old projection was discarded after its10second short TTL for an unchanged request key, but reused if only the price-file key changed. The candidate preserves that completed projection within the unchanged900second structural TTL. Foreground reuse still applies current values and requests revalidation; background lookup remains a miss without removing concurrent readers' fallback. A new background projection replaces membership. Empty payloads, structurally expired entries and incompatible structural/query signatures remain excluded, and serving does not extend the timestamp or cache bound.

Final gates:2,682tests passed139.30s exit0; Ruff no new516against unchanged517baseline exit0;23focused tests pass. Original lookup source fails both new foreground cases and passes3safety/rebuild cases. One initial mutant run exposed test-fixture in-flight state leakage under a frozen clock; that local process was stopped and fixture isolation corrected before final gates. Generated tracked test data restored. No product source changes after gates. Evidence `same-key-full-final.txt`, `same-key-original-source-final.txt`, `exact-key-expiry-{before,after}.json`; details `docs/operations/2026-09-06-same-key-cache-revalidation.md`.

This is a proven redundant-rebuild path, not causal attribution for the specific31.933s production request or delivered HTTP latency improvement. Retaining a fallback during rebuild can affect peak allocation; validate ordinary RAM/CPU/coverage and latency after any later release. Do not deploy during the current memory observation. Review its complete phase comparison and safe-cap decision first.

First15minute deployed-memory log:60host/8coverage samples,201341–201569priced,2generations, no sampledOOM/restart/unhealthy/endpoint failures. Appanonpeak2,794,663,936bytes; collectoranon2,858,545,152. CPU.865/2.195cores; `/free`3samples max14.906s; health8samples max2.47s. Different duration/phase mix means these smaller peaks are NOT a proved saving versus the prior2h log. `combined-observer-current.jsonl`, `combined-observer-analysis.json`, `combined-comparison-initial.json`. Current release still needs full-hour/ordinary publication evidence, safe limits, further backups andfinal48h.

## Prior checkpoint details

## Deployed combined release — 2026-09-06 05:17 UTC

**Live app and collector are f047ccf, digest bc8129844c597cd1.** Sole combined waiter PID93680/session39465 finished exit0 at05:11:54UTC. Both guards cleared; both container source digests matched; health200. Containers started05:10:48UTC and were freshly healthy, restarts0/OOMfalse. Do not restart the terminal waiter. Former selection-only waiter89383/session7455 remains terminal143. No deployment queued; caps unchanged; recurring automationPAUSED.

The previous observer434080 finished successfully. Frozen `release-observer-complete-before-combined.jsonl`/analysis:474host samples over7197.82s,42coverage samples over6922.19s,11generations,198399–200890priced. No sampledOOM/restart/unhealthy or endpoint failures/gaps. Baseline appanon3,164,200,960/collectoranon4,053,626,880bytes; CPU.812/1.989cores. `/free`max31.933s, health10.018s. Successful kernel read05:09:35 has no OOM records since03:08:53. This proves the prior two-hour baseline, not current-release savings orfinal48h.

**NEW SOLE observer:** `spreadboard-stability-combined-20260906.service`, PID**499780**, started**05:12:50UTC**,2h untilabout**07:12:50UTC**, RuntimeMax7500s. Prior running/activating stability units were checked absent before start. Output `/opt/spreadboard/runtime/stability/20260906-combined/samples.jsonl`. Freshactive/running. Same15shost/120shealthfirsthourthen300s/free300s/backup300s cadence. Do not duplicate; no diagnostic child load inside its ordinary window. Compare against frozen prior baseline using localcompare_release_memory.py. Post-release public health05:12:22 ready/gen1,201423priced/207748routes,1249tokens; not an hourproof.

Actual signed-in UI after deployment: OPENAI36routes/25onfirstpage, GateFutures→HyperliquidFutures~6.6% indicative, nativeio:OAI in chart link, DD/depth labels intact, eligibleFunding0. Funding849tokens/23862livepairs, projected versus settled windows distinct. `combined-release-ui.json` and `combined-release-health.json`. Page-level observation does not prove every native route or execution readiness.

The unusually large T current funding headline (~47.56%day projection) is corroborated by nativeKrakenPF_TUSD data: fundingRate-.00009182quote/base, mark.00462894917 implies-1.9836%/hour at current notional. Nativeinstrument tradeabletrue, contractSize1, baseT, `maxRelativeFundingRate=.02`, tradfifalse; the older generic0.5% cap cannot be applied to all instruments. fundingRatePrediction is a separate, much smaller positive value and must not silently replace current funding. Snapshot `kraken-t-native-funding.json`; no forecast/realized-return/entry claim. T settled windows remainedblank on the renderedpage.

Full release gates2677passed146.44s exit0; Ruff no new516against517baseline exit0; six new behavioural cases fail bypass mutants. No source changes since gates. GoalACTIVE: measure ordinary after-release RAM/CPU/phase overlap and coverage, resolve latency/safe caps, subsequent normal backup firings and final48h. No trades/messages/spend/force/guardweakening.

## Earlier release checkpoints (historical)

## Combined memory candidate and sole waiter — 2026-09-06 04:59 UTC

Candidate **f047ccf**, digest **bc8129844c597cd1**, includes selection cleanup2a8a03b. NOT deployed. It also pauses the optional websocket worker inside the heavy lock during post-discovery publication, with finally-resume. Actual peak/log correlation: discovery completed03:52:39, index finished03:56:28, collector anon peak03:56:04 had websocket959232KiB plus index1877432KiB. Ordinary publisher already paused it; this post-discovery call path did not. This is a measured overlap mechanism, not yet delivered RAM savings. Full2677tests passed146.44s exit0; Ruff no new516against517baseline exit0; four new publication-path cases fail a no-op pause mutant, and two selection cases fail cleanup bypass. No source edits after gates; test-generated tracked data restored.

Former release waiter PID89383/session7455 was intentionally stopped before edits, terminal exit143, no deploy. Do not restart it. NEW SOLE bounded release waiter **PID93680 / tool session39465**, `output/stability-20260906/wait_combined_release.py`, started04:58:44UTC,2hbound. It holds the same singleton lock and pins the combined source/helper. Fresh poll confirms observer434080 still running; preserve to finished around05:09UTC. The waiter requires successful terminal service+finished log, baseline analysis/noissues/onehourcoverage, and both protected-worker guards before guarded recreation. Events `combined-release-wait.jsonl`; deployment output `combined-release-deploy.txt`; frozen baseline will be `release-observer-complete-before-combined.jsonl`. No combined deployment yet. Source must remain unchanged while it waits.

Live remains **fb814c3 /3d0f3437aa390c87**. Latest99min baseline:391host/38coverage samples,9generations,198399–200503priced, no sampledOOM/restart/unhealthy/endpointfailures. `/free` max31.933s, so latency remains open independently of200status. Physical8,326,938,624bytes versus9,059,696,640declared limits. Collector target3.5GiB is295,530,496bytes below anon peak; hypothetical cleanup alone stillleaves187,708,396bytes beforemargin. App3GiB target has only57,024,512anon-only headroom. Do not lower caps or treat local allocation savings as production totals.

Next: poll SAME session39465; inspect actual terminal deployment/digests/endpoints if it ships, then observe ordinary RAM/CPU/coverage across publication and evidence cycles. Keep goalactive until safe caps, final48h and further normal backup firings are proved. No trades/messages/spend/force/guardweakening; recurring automation remainsPAUSED. Detailed evidence: `docs/operations/2026-09-06-post-discovery-memory.md`.

## Earlier checkpoints (historical)

Checkpoint: 2026-09-06 04:38 UTC. The goal remains active. This is a continuation handover, not a completion claim.

### Selection-cache candidate — 2026-09-06 04:38 UTC

Candidate **2a8a03b**, NOT deployed. Websocket selection now releases parsed row/response caches after the full lane batch, on its selection thread, including failure. Selected keys, saved-position priority,160cap/300srefresh floor and website-process caches remain unchanged. Offline30,504-row snapshot replay freed107,822,100bytes through the actual cleanup API; this is not whole-worker RSS/cap evidence. Full suite2673passed150.14s exit0; Ruff no new516against517baseline exit0; both new cases fail cleanup-bypass mutant exit1. No source edits after gates. Source/test changes committed; test-generated tracked data restored.

Live source remainsfb814c3 /3d0f3437aa390c87. Sole observer434080 freshly active, both protected workers clear at last probe; preserve until terminal around05:08:53UTC, then refresh guards before any deployment. No deploy waiter queued or cap changes. See collector-retention/docs/operations/2026-09-06-selection-cache-cleanup.md. Next: finish ordinary baseline, guarded release, compare normal RAM/CPU/coverage, continue necessary memory work andfinal48h/backups. Goalactive/recurringautomationpaused.

## Previous measured result

One-hour priced-coverage gate passed; the current copied log spans approximately79min with no sampled OOM/restart/unhealthy or endpoint failures. Collector ordinary anonymous memory peaked at3.775GiB, so its proposed3.5GiB cap is unsafe at present. Caps remain unchanged. The finite observer is still the sole observation job, expected end05:08:53UTC; refresh its state before acting.

Local catalogue sharing reduced paired-client retained Python allocations by67.8MB Gate and27.7MB Bybit, without symbol-count loss. This is not a deployed fix or production RAM saving. Read `docs/operations/2026-09-06-catalogue-memory-investigation.md` for exact evidence, dynamic-listing risks and the competing route-selection cache hypothesis. Candidate2a8a03b now implements selection-cache cleanup; catalogue sharing remains unimplemented. No deployment queued. Preserve the normal observation window.

### Sole selection release waiter — 2026-09-06 04:42 UTC

One bounded local waiter is ACTIVE: tool session7455, `output/stability-20260906/wait_selection_release.py`, started04:41:36UTC with a2h bound. It holds the existing guarded-release-wait.lock, so no second release waiter can run. Fresh poll recorded observer_running PID434080. Do not restart/duplicate or edit candidate source while it waits. Previous waiter59028/session78374 remains terminal and is not reused.

The waiter pins source digest74ceb35494a9c6d4 (candidate2a8a03b) and the deploy helper hash. It requires observer inactive/MainPID0/Resultsuccess AND a finished log marker, freezes and analyzes the complete baseline, requires one-hour coverage/no issue flags, checks both protected workers twice before calling the helper, which checks both again immediately before recreation. Failed/unknown observer completion stops without deployment; transient read errors retry within the bound. Seven completion predicate checks passed. Events `selection-release-wait.jsonl`; future deployment output `selection-release-deploy.txt`. No deployment has occurred at this checkpoint. Current live sourcefb814c3/digest3d0f3437aa390c87. Recurring automation remainsPAUSED.

Next action: poll the SAME session7455, preserve observer434080 to terminal (~05:09UTC), inspect actual deploy exit/digests/endpoints if it ships, then measure normal candidate RAM/CPU/coverage. Do not treat waiter timeout as deployment or success. Goal remains active; safe caps, subsequent backups andfinal48h remain outstanding.

## Start here

- Work only in `/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-collector-retention`, branch `codex/collector-retention-20260906`.
- Read `docs/operations/2026-09-06-acceptance-matrix.md` for verified evidence and remaining gates. Detailed chronology is in `docs/operations/2026-09-05-stability-review.md` and `2026-09-05-ua-comparison.md`.
- Read root `REMINDERS.md`, vault current state and project rules before mutations. Refresh volatile state; historical checkpoints are not live evidence.
- The separate `tmp/spreadboard-funding-current-truth` checkout is not the release checkout. Preserve unrelated work there.
- SSH: `ssh -i ~/.ssh/spreadboard_digitalocean root@178.128.126.204`. Server source `/opt/spreadboard/app`; runtime `/opt/spreadboard/runtime`; container Python `/app/.venv/bin/python`.

## Release state — deployed

The sole waiter PID59028/session78374 completed exit0 at03:00:26UTC. Do not restart it. Live app and collector source is **fb814c3 / 3d0f3437aa390c87**; both protected-worker checks cleared and both source digests matched. `output/stability-20260906/held-release-deploy.txt` is the terminal evidence.

CAP production probe now preserves HTX futures long/Kraken spot short with correctpositivecarry. Signed-in CAP/Funding pages populated; OPENAI first page includes Binance/Bitget→Hyperliquid around6.1% indicative, withio:OAI source identity andDDwarnings. No positive-spread route is hidden solely for negative funding in this observed case.

The old counts observer is terminal. The sole active observer is `spreadboard-stability-release-20260906.service`, PID434080, started03:08:53UTC for2h, output `/opt/spreadboard/runtime/stability/20260906-release/samples.jsonl`. Do not duplicate it. This measures ordinary releasedmemory/CPU/coverage before the cap decision; final48hacceptance remains open. See `output/stability-20260906/checkpoint-deployed.md` and the acceptance matrix.

## What the deployed release changes

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

1. Observe the same PID434080 and collect ordinary reload/coverage evidence. Do not run duplicate observers or diagnostic child profiles unnoticed inside that window.
2. Compare against pre-held-release-observer.jsonl, pre-held-release-summary.json and pre-held-release-cache-counters.jsonl. Collector measured anon3.775GiB exceeds its proposed3.5GiB cap; memory work is required before lowering it. Current3584/4096/768/192MiB caps remain unchanged.
3. Decide safecaps from measuredheadroom, then establish the final uninterrupted48h and30-sample/hour evidence. No forced recoveryrepeatinsidecleanwindow.
4. Verify subsequent normal backup timer firings. Latest success00:19:49–01:15:36; nexttimer previously06:20:56UTC. Do not countolder successes acrosslaterfailure.
5. Keep goal open until every original acceptance gate is proved. Candidateallocationexperimentisnot deliveredmemoryproof. No further deploymentiscurrentlyqueued.

## Boundaries

No trades, borrowing, repayment, transfer, conversion, withdrawal, messages, spend, cap/subscription increases, weaker freshness/identity/95% accuracy/settlement gates, or force deployment. Ourbit remains excluded. Public relevance is positive spread OR positive funding, with needed alternatives and history preserved.

The recurring `finish-spreadboard-stability-acceptance` automation remains **PAUSED**. A continuation is not authorization to reenable it. Do not duplicate workers, observers or deployment waiters. No subagents. Never print secrets or complete process arguments/configuration.

If source changes become necessary after the current waiter is terminal, rerun the full gates with actual exit codes before any later deployment:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --with pytest --with pytest-asyncio python -m pytest tests/ -q
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --with ruff python scripts/ruff_ratchet.py
```

Raw evidence is in `output/stability-20260906/`. The previous handover is archived as `2026-09-05-stability-cardinality-history.md`; its operational directions are superseded.

## Additional Spot category comparison — 2026-09-06 04:46 UTC

A fresh guest inspection of UA's Spot tab found15visible spot-to-spot leaders. The exact category is deliberately retired from SpreadBoard: `api_spreads.RETIRED_ROUTE_KINDS` contains SPOT andDEX-SPOT, and the2026-08-28continuous-stream handover explicitly preserves that product decision. Spot market books remain necessary for the retained futures/spot routes, charts, portfolio marks and token-price alerts.

Nine displayed routes used Binance Alpha (SIREN,MITO,MOG,SPX,CHIP,ONDO,POWER,CFG,MORPHO), and two used Ourbit (SHROOM,BULLA). The remaining four were UPC MexcSpot→BitgetSpot, FONE GateSpot→MexcSpot, LUNC KucoinSpot→BinanceSpot andNPC MexcSpot→GateSpot. Their venues are configured but the route family is retired. These overlapping scope reasons explain the guest leaders; they do not establish a missing retained futures/funding route. Do not restore spot-to-spot permutations in response to this comparison or present them as funding opportunities.

Actual signed-in SpreadBoard `/markets?q=UPC` showed no rows and only Futures-Futures/Futures-Spot/Futures-DEX/Allroutes categories, consistent with policy. This is a category-scope check, not a native token-identity, transfer-rail or executable-arbitrage audit. UA's Spot-Dex/Futures-Dex tabs were visibly premium-disabled; no bypass attempted. Evidence `output/stability-20260906/ua-spot-family-comparison.json`. This15-row sample is separate from earlier samples and must not be summed into an exhaustive coverage claim.

## Phase comparison preparation — 2026-09-06 05:05 UTC

The frozen99minute baseline (`release-observer-99min.jsonl`) contains67index samples:10overlapped websocket and peaked at4,053,626,880collector anon bytes;57without websocket peaked at3,173,052,416.146evidence samples peaked at3,329,355,776. These are sampled phase maxima, not a causal/additive delivered saving, and15s sampling can miss brief overlap.

Local `output/stability-20260906/compare_release_memory.py BEFORE AFTER` reports both original observation analyses, phase presence/counts/peaks and anon/CPU deltas. It never claims acceptance; unequal duration/coverage/phases/load require review. A same-log comparison produced zero deltas; five missing-counter/process checks passed and retained unknown values instead of zero savings. Candidate digestbc8129844c597cd1 remains unchanged. Sole combined waiter93680/session39465 remains active; no deployment at this checkpoint. Preserve it and observer434080 until terminal.

## Post-release funding generation audit — 2026-09-06 05:37 UTC

Copied the published complete funding file to the Mac and audited locally; no diagnostic child was added to the production observation. Embedded saved_at is **2026-09-06 05:26:40.191552UTC**;51,049,746bytes; SHA256`5bd9aa25ee230f1d7c5ea11d57d346b1d16e3de32dff94036e5c5b79f291e40e`. The local copy mtime is download time, not source generation time.

**222,522 routes independently recomputed, zero arithmetic mismatches, duplicate exact leg pairs, self-pairs, Ourbit entries, missing complete rate inputs or unavailable funding.** Families:80,740Futures→Futures;70,868Futures→Spot;70,914Spot→Futures. No retired Spot→Spot family.5,328token blocks,1,062nonempty.94,538positive daily carry,94,489negative,33,495zero. The sign counts do not establish public eligibility: positive basis can coexist with negative carry, and required alternatives/history must not be blindly pruned. Counts are not unique-token counts or a reason to reintroduce retired lanes.

Evidence `funding-snapshot-arithmetic-post-release.json`, `funding-copy-metadata.json`, and the copied `funding-replay/complete_funding_catalog.json`. This verifies the stored snapshot's arithmetic and exact identities as keys, not native economic identity, present quote freshness, settlement completeness or enterability. Current live overlays are separate. The audit completes fresh post-memory-release regression evidence without changing source or limits.

Live remainsf047ccf; localcandidate7b7f904/2682tests remains undeployed, no waiter. Sole observer499780 freshly active at this turn's check; preserve until07:12:50UTC. First15min raw data/analysis now frozen as`combined-observer-0015.jsonl` and`combined-observer-analysis-0015.json`. Goalactive, heartbeatpaused; full ordinary memory/caps/latency/backups/48h gates remain.
