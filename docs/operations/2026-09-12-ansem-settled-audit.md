# ANSEM settled funding complaint: evidence, fixes, limits

## Scope and evidence

User clarified that the complaint concerns **all settled values on the Funding
page**, not Portfolio cash funding. This audit therefore traces public settlement
events → venue history cache → exact route subtraction → Funding API/HTML.
Preserve `now` as a labelled projection and 24h/7d/30d as payment-event sums.

Production before this change: `f898e78`; local branch also contains Claude's
documentation commits through `a433d44`. His Kucoin and backup changes are retained.

Fresh direct native HTTP reads, 12 September 2026 around 22:35–22:43 UTC,
independently matched all **13 available** ANSEM venue/window totals:

| Futures venue (short receives positive) | Settled 24h % | Settled 7d % | Settled 30d % |
|---|---:|---:|---:|
| Hyperliquid `para:ANSEM` | 0.33492917 | 3.73368008 | unavailable |
| MEXC `ANSEM_USDT` | 0.0866 | 0.7404 | 2.6924 |
| Aster `ANSEMUSDT` | 0.001434 | 0.950359 | -0.035725 |
| Gate `ANSEM_USDT` | 0.03 | 0.21 | unavailable |
| BingX `ANSEM-USDT` | 0.0906 | 0.7533 | 2.2038 |

These are summed rates on one futures-leg notional, not realized account USDT,
margin ROI, or future expected income. Pair carry is short minus long; spot
contributes zero. Thus MEXC **spot** → Hyperliquid is 3.73368008% over 7d,
whereas MEXC **futures** → Hyperliquid is 2.99328008%.

Hyperliquid returned 439 hourly events, only about 18.3 days: their 16.59822291%
partial sum must not be called a 30-day return. Gate returned 68 four-hour events
covering about 11.3 days: its 0.34% partial sum also is not a 30-day return.
BitMart remained a provider HTTP error and was not certified by this audit.

Authenticated production HTTP checks returned matching canonical ANSEM values
for all four period filters. HTML Futures–Spot Now/7d and Futures–Futures 7d
returned in 3.20/1.92/4.70 seconds and showed the expected rounded values.
Disposable audit users and sessions were removed in finally blocks. No secrets
were printed and no exchange/accounting/Telegram mutations were made.

## Confirmed defects and repair

1. Native `Hyperliquid|para:ANSEM` had `symbol_not_indexed` while canonical
   `Hyperliquid|PARA-ANSEM/USDC:USDC` held correct history. Resolve native names
   only against a unique existing full contract symbol; preserve namespace and
   never guess collateral or borrow another ANSEM contract. Apply to collection,
   current route windows, last-complete values and history-status explanations.
2. Silent pages hid the Resume button but inherited the global saved pause.
   This could freeze historical cells indefinitely beside updating live prices.
   Silent pages now ignore that invisible inherited pause; visible pause controls
   on other pages retain their behavior.
3. Failed refreshes and redirects to login were silently ignored. Add a visible
   out-of-date/retry or session-expired/sign-in notice; stop the live stream on
   confirmed session expiry. Bound a stalled fetch to 30 seconds. Refresh an
   inactive tab on return without reloading the document or discarding active
   edits.

Safari's existing SpreadBoard tab displayed an old market document containing
retired HTX/Phemex rows. Clicking Funding redirected to Sign in. This verifies
an expired browser session, **not** the exact prior ANSEM discrepancy: no user
screenshot/URL of that discrepancy was supplied. Do not claim all prior reports
were just stale browsers, or that all venues are now numerically certified.

## User clarification: younger contracts belong in the long-period ranking

On 13 September (London), the user clarified that a contract younger than30days
should show the amount since release rather than a blank. Public history alone
does not prove the listing date, so the UI says **since first verified settlement**
and **18.3d available**, not "30-day return" or an invented release date.

- `funding_available_history.py` reads the existing read-only exact settlement
  ledger, with bounded compact-series and interval-validation caches. No new
  provider calls or writes run in a page request. Full-age missing/gapped
  archives are rejected before an unnecessary ledger read.
- A shorter period must pass the same cadence, interior-gap, current-tail and
  provider-schedule expiry checks. Fewer than4events cannot prove cadence.
- Futures-futures uses the intersection of both legs' verified histories and
  one common end time. It never subtracts18days of one leg from30of the other.
- `settled_funding_windows` stays strict; `settled_funding_available` carries
  actual duration, first settlement, as-of, per-leg event counts and net total.
  Member-facing ranking uses full totals when valid, otherwise the disclosed
  shorter total; deterministic/ML full-window readers are unchanged.
- Background navigation freezes the same display metadata as its ranking.
  Expanded details revalidate current freshness. Now remains a live estimate;
  available periods are not extrapolated.

Fresh read-only execution against production ledger around23:10 UTC:
Hyperliquid ANSEM440events =16.63613333% over18.2928days; Gate68events =0.34%
over11.2942days. MEXC full30d2.6924% unchanged. Three-route cold check took0.358s.

## Verification and boundaries

- Full suite:2995passed. Final retired/unindexed-ledger exclusion plus the
  focused integrity matrix:169passed. Ruff0new (500known); diff check clean.
  Refresh tests execute production JS with saved pause, failure, login redirect
  and tab return; available-history tests cover shared-leg dates, gaps, expiry,
  negative values, full-period preservation and API/navigation/HTML parity.
- Read-only100-short-history benchmark:0.187s,99eligible; bounded256series /
  4096period caches. Release and post-deploy evidence recorded at closeout.
- A single current native comparison is a sample, not universal correctness.
  Broader source reconciliation, historical rollover lag, startup latency and
  long-running collection/backup acceptance remain separate outstanding work.
- Strict full30d remains unavailable for shorter history; the separately labelled
  available-period value is now intentionally included in the display/ranking.
  No model, trades, notifications or subscriber accounting enabled.

## Release evidence

Source `8282617` committed/pushed and deployed once to app+collector with the
discovery guard clear at both boundaries. Source fingerprint `f5be5e7a72b1d7a9`
matches both containers; health200, restart counters0, OOMfalse. Preserves the
later Claude documentation commits, including `2e9cae3`. Runtime/test artifacts
were neither committed nor shipped.

Authenticated steady-state ANSEM API checks for30d/Now/24h/7d returned in
0.624/0.405/1.030/0.481seconds. Every response contains strict24h0.34770695%,
7d3.70983312%, strict30dnull, and separate30d-available16.63613333% /440events /
18.3067days. Funding30d HTML Futures-Spot/Futures-Futures returned200 in4.304 /
0.420seconds (99/101KB), contained9/7 labelled shorter-period cells and no
invalid numeric text. New session-expiry notice code present. Both disposable
audit users/sessions removed; DBquick_check=ok, foreign-key violations0.

The first immediate post-restart Now request timed out at60seconds. One bounded
retry then passed all checks above. Existing cold-start/catalogue warm-up latency
is NOT solved by this release. At23:26UTC the last valid navigation generation
was still the pre-release one; targeted/current API and HTML are updated, but
publication of the new all-token background ranking still needs its first
collector generation. No extra restart or unsupervised heavy worker was forced.

Bounded closeout check at23:36UTC: route-index publication completed and the
supervised token-ranking worker was active. Funding navigation still retained
the old policy (ANSEM rank318 /2.6924% full-history MEXC leader, rather than the
new16.6361% shorter-history Hyperliquid leader). **Do not call global-ranking
acceptance complete.** Targeted `/funding?farm=futures-spot&rank=30d&q=ANSEM`
already passes. Next safe check: verify the first new-policy supervised
navigation generation, all12lanes and ANSEM's new best value. Do not add an
uncoordinated worker: host has7.9GiB RAM, collector/app already use severalGiB.
