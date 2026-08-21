# SpreadBoard — full page-by-page audit, fix and verify

You are taking over a site-wide audit of SpreadBoard, a crypto spread-arbitrage
SaaS that is about to launch. Another agent (Claude) did part of this work and
stopped partway. Your job is to **review its work critically, correct what it
got wrong, and then finish the audit across every page it never reached.**

Do not trust anything in this document that you can check yourself. Several
confident claims in this session turned out to be wrong. Verify first.

---

## 0. Current exact continuation point — Codex update 2026-08-21

Read this section and the live ledger at
`output/audit/2026-08-20-page-by-page-ledger.md` before doing anything else.
The original sections below are retained as the starting brief; where they
conflict with this checkpoint or the ledger, this checkpoint is newer.

### Work completed and deployed

- The six named commits in §3 were rechecked. `/markets` and the legacy
  `/arbitrage` alias have now received the full desktop/mobile and light/dark
  pass, including filters, reset, presets, Copy View, Net Edge, alerts,
  pagination, JSON, expanded rows and layout.
- Removed query-only stale-generation serving. `_MARKET_STALE_CACHE`,
  `allow_stale` and the stale rebuild path no longer exist. A changed snapshot
  produces its current generation or an honest `warming` result; no older
  structural generation is served as current.
- Added exact fast-quote route deltas for expanded catalogue pairs outside the
  bounded resident WebSocket set. Matching includes token, both venues, market
  types and symbols and keeps the existing quote-age boundary.
- Fixed a production-only DOM fan-out: the outer token group and its leader
  child share a route key, and the SSE updater traversed the entire `<details>`
  subtree, replacing every child spread with the leader value. Group updates
  are now scoped to the direct `<summary>`; children update independently.
- Named proof: before the fix, OPENAI `Bitget → Ourbit` and other expanded rows
  all became `+3.27%` although their exact values were near `+0.13%`–`+0.19%`.
  After deployment, 14 OPENAI rows held 12 distinct values; every row present
  in the same current SSE event matched the DOM within display rounding and
  the browser console had zero errors or warnings.
- Fixed the GUA Pro Table funding omission: settled 24h is preferred, otherwise
  the current-rate 24h projection is shown and labelled. Named `Ourbit → Gate`
  no longer shows a dash when the projection is known.
- Fixed Pro Table overflow and moved the market-wide side panels below the
  full-width table. At 1440px and 375px both body and main fit exactly with no
  horizontal overflow; 1d/7d/30d and action controls remain visible.
- In-place refresh now preserves live controls and unsaved input while merging
  newly available exchange options into the preserved select. The exact cold
  recovery test restored 1 → 21 options and retained `GUA UNSAVED`.
- DEX empty states now report the actual OKX provider rejection rather than
  claiming there are no routes. Filtered KPIs no longer inherit unrelated
  global values. Exact economic duplicates are deduplicated.
- `/playbook` is now fully audited. Its hard-coded light card and answer
  surfaces made dark mode unreadable; a failing-test-first change moved the
  component onto terminal theme tokens. Production desktop/mobile light/dark
  screenshots, computed contrast, source/API truth, every unique content link,
  Pause/Resume, theme control, console and network checks now pass.
- `/profile` is now fully audited. Known empty-book money totals are exact
  zeroes, all account mutations refresh in place without losing document
  state, overlapping refreshes are newest-wins, Pushover and Web Push feedback
  is truthful, and delete-confirmation errors are human-readable. A disposable
  GUA position exercised create/edit/alerts/close validation/chart/delete and
  DEX catalogue coverage; the position, alert rules, test notification,
  Telegram link token and all other audit artifacts were then proven absent.
- A live-test mistake is documented rather than hidden: the first settings
  response patch called `.public_dict()` on a dict and returned `500`. It was
  immediately hotfixed, covered by an endpoint-level test, redeployed and
  reverified with a reversible display-name save and no document reload.
- `/intel` is now fully audited. The page had ignored the always-on subscriber
  webhook and instead depended on an empty Mac/Telethon bridge file, linked the
  wrong bot, cached through new lookups for up to 15 minutes, overflowed to
  470px at a 375px viewport, and let the fixed refresh pill cover its mobile
  empty state. It now reads the privacy-safe server-side subscriber-attention
  feed, invalidates on new events, uses the configured subscription-bot link
  and copy, wraps long identifiers, and places the refresh control after page
  content on mobile. The current empty state is honest: the real feed is stale,
  and no Telegram message was fabricated to populate it.
- `/triage` is now fully audited. Its dark mode used white cards with pale text,
  its nested `<main>` created invalid landmarks, its source copy still claimed
  a local Telegram/file dependency, and the global fixed refresh control covered
  queue cards on desktop. Triage now uses shared theme tokens, one main landmark,
  and explicit always-on subscriber-source copy. The refresh control is in the
  header's normal layout on every auto-refresh page, so it stays accessible
  without covering data at desktop or mobile widths.
- `/signals` is now fully audited. Its five lanes used hard-coded light
  surfaces in dark mode, it ignored the only event kind produced by the
  always-on subscriber webhook (`chat_signal`), its copy still described a
  local listener, and its default six-hour window had no selectable/current
  control. It now renders an explicit Subscriber Lookups lane without
  inventing numeric metrics, links tokens safely, uses shared theme tokens,
  identifies the privacy-safe subscriber source accurately, and exposes
  1h/6h/12h/48h controls with the selected window announced to assistive
  technology. The real source remains stale, so the production empty lanes
  were preserved rather than populated synthetically.
- `/community` is now fully audited. Its scoreboard, ledger and discussion
  cards were unreadable in dark mode, the page nested a second main landmark,
  its source copy still described a retired local topic-brief pipeline, and the
  API's evidence window had no visible selector. Shared theme tokens, one main
  landmark, accurate privacy/source copy and 1h/6h/12h/48h selected controls are
  deployed. A populated fixture additionally caught and fixed whole-unit
  rounding of reported PnL; two decimals are now preserved. Genuine production
  call/result/question rows remain empty because the source is stale, not
  because synthetic evidence was inserted.
- The revision marker was proven insufficient as deployment evidence. Despite
  naming the current checkpoint, production still held older copies of
  `fair_price.py`, `fast_quotes.py` and `venue_funding_history.py`. The three
  modules were synchronized to the persisted host, web and collector, both
  services were restarted healthy, and a new read-only release verifier now
  compares every `spreadboard/**/*.py` file across all three production
  targets. It passes with 51/51 files and no changed, missing or unexpected
  modules.
- The drift caused two real regressions. Fair Price again admitted unknown
  Kraken Futures turnover: named ANTHROPICX showed `-32.06%` although its
  current turnover was about `$42`. Production now renders 34 cards with zero
  unknown/below-floor rows and no ANTHROPICX. The core quote path also lacked
  `33f5410`; after repair an exact thin reverse-book fixture retains the needed
  ask-side quote when the unused bid VWAP is null, and the live BTW chart
  streams exact entry, `$500` matched and exit values with a clean console.
- `/learn` is now fully audited. Its old cards misnamed the canonical collector
  as local, blurred funding carry with spread convergence, omitted Spot-Spot
  execution friction, and overstated the read-only boundary. Seven precise
  evidence cards now cover route direction, separate opportunities,
  volatility/margin limits, freshness, DEX provider/identity truth and the
  exchange-trading boundary. Methodology, Status and Guide actions, both
  themes and both viewports pass; the final odd card spans the desktop grid.

### Current deployment and verification

- The code and handoff are checkpointed on branch `codex/crypto-billing`.
  Confirm the exact immutable hash with `git log -1 --oneline`; do not accept
  the production `.deployed_revision` marker without the complete source-sync
  verifier below.
- Production `app-app-1` and `app-collector-1` are healthy. The unchanged
  `spreadboard/portfolio.py` SHA-256 remains
  `666e3986c1cefee1c11a4ca365f5999e6895f5d0106f9c8ce5d867a84cc10867`;
  current server and Intel hashes are below.
- Latest source-check commit: **`2ee93e8`**. Run
  `.venv/bin/python scripts/verify_production_source_sync.py`; the last fresh
  result matched all 51 package modules on the persisted host, app and
  collector with no drift.
- Latest audited-page code commits: **`c8a2ad5`** and **`b908165`** for Learn.
- Full suite: **1,478 passed**, one pre-existing unknown `asyncio_mode` warning.
  Ruff ratchet: **0 new findings, 532 known**.
- Warm signed-in timings after the restart were about 0.63s for `/markets`,
  0.85s for `/arbitrage?kind=FUTURES`, 0.42s for `/account`, and 0.29–0.34s
  for repeated 25-group API requests. The first default `/markets` generation
  after restart took 15.5s; this is a real remaining cold-start UX issue, not
  permission to restore stale serving.
- Screenshots added: `arbitrage-{desktop,mobile}-{light,dark}.png` plus the
  final Markets Pro Table files under `output/playwright/2026-08-20-page-audit/`;
  `/profile` position and settings evidence is under
  `output/playwright/2026-08-21-page-audit/`. The same directory now contains
  `/intel` desktop/mobile light/dark evidence and the final non-overlapping
  mobile capture, plus `/triage`, `/signals`, `/community` and `/learn`
  before/final desktop/mobile light/dark evidence.

### Fresh ML gate — do not train or activate

The production ranking/outcome worker was run through `/app/.venv/bin/python`.
Selected method is still exactly
`deterministic_dual_opportunity_evidence_v5`: 25,357 observations, 17,344
labeled 24h outcomes, 1,832 routes and 6.75 labeled days. Both class-balance
gates and leakage pass and the 24h-embargoed chronological split is valid.
Data readiness still fails on 0%/80% exact lifecycle-cost completeness and
6.75/30 days; no candidate exists, activation is false and the deterministic
fallback remains active. Do not mix the 5,832 v4 rows or weaken these gates.

### External live failure still open

OKX DEX currently rejects catalogue access on chains 1, 56, 137, 42161 and
8453 with an API-key/region entitlement error. Both verified DEX lanes remain
zero after 1,034 under-covered completed cycles. Credentials are present; this
needs provider/account entitlement repair and then sustained-cycle proof. Do
not portray the zero lanes as evidence no DEX opportunity exists.

### Resume exactly here

1. Continue §8 with the remaining static routes, starting at `/proof`.
2. Then complete
   `/executor`, `/status`, `/telegram`, `/guide`,
   `/methodology`, `/pricing`, `/subscription`, `/partner`, `/free`, all auth
   and legal routes, and the previously partial Funding/Rankings/Fair/Charts/
   Portfolio/Watchlist passes. Do not mark a page complete after a batch smoke
   test; exercise its controls under both themes and both viewports.
3. Finish dynamic `/token/*`, `/pair/*` and `/r/*` routes and the authenticated
   Account flows.
4. Replace the first uncached JSON `warming` response with an explicit
   preparing/export UX without serving stale data. Characterize and improve
   the 15.5s post-restart cold default Markets generation without weakening
   freshness.
5. Re-run the full suite and Ruff ratchet, deploy, and reverify production.

After the checkpoint commit the only intentional uncommitted files are the
generated historical-cache JSON, `data/token_rankings.json`, and Playwright
screenshots. Preserve them; do not discard or overwrite them. No trade,
transfer, Telegram group message or secret disclosure occurred.

---

## 1. The operator's standing instruction (this is the spec)

Verbatim, repeated several times:

> "Inspect each page, ensure that dark mode is enabled and working everywhere.
> Ensure that everything is working as intended. Confirm to me after that you
> have inspected and analysed and where needed fixed and spotted all the
> possible issues. Reverify that this was actually done and that you have
> actually spotted the issues and fixed these. Ensure that every function, on
> each tap, is working correctly and is visually showing correct. Be diligent.
> GO step by step, Page -> Analysis -> issue spotting -> fixing -> ensuring
> that issue is fixed and every function is working as intended and confirm
> that there are no breaks -> next page. Ensure that none of the pages are
> missed. Do not stop until you fully completed the goal!"

So: **one page at a time, all the way through, with evidence at each step.**
Breadth matters as much as depth — a page skipped is a failure even if the
pages you did cover are perfect.

---

## 2. Environment

| Thing | Value |
|---|---|
| Repo | `/Users/sviatoslav/Desktop/Spread Arbitrage/tmp/spreadboard-public-release-clean` |
| Python | `.venv/bin/python` (3.13). **Do not use system python3** — it is 3.9 and fails on `datetime.UTC`, which looks like a product bug and is not |
| Tests | `.venv/bin/python -m pytest -q` — **1,418 passing** as of commit `33f5410`. No `--timeout` flag available |
| Lint | `uvx ruff check <files>` (ruff is not in the venv) |
| Lint policy | Ratchet, not zero. `ruff-baseline.txt` is TSV `file<TAB>rule<TAB>count`. You must add **no new (file, rule) findings above baseline**. Compare counts programmatically; do not eyeball |
| Production | DigitalOcean droplet `178.128.126.204`, key `~/.ssh/spreadboard_digitalocean` |
| Domain | `spreadarbitrage.ink` (Caddy terminates TLS, reverse-proxies `app:8200` with `flush_interval -1` — that is what makes SSE work) |
| Containers | `app-app-1`, `app-collector-1`, `app-accounting-worker-1`, `app-caddy-1` |
| Data | `/app/runtime` in-container (mounted from `/opt/spreadboard/runtime`). `/app/data` is baked-in seed only — stale files there mean nothing |

**Deploying one file:**
```bash
scp -i ~/.ssh/spreadboard_digitalocean spreadboard/FILE.py root@178.128.126.204:/tmp/FILE.py
ssh -i ~/.ssh/spreadboard_digitalocean root@178.128.126.204 '
  for c in app-app-1 app-collector-1; do docker cp /tmp/FILE.py $c:/app/spreadboard/FILE.py; done
  docker restart app-app-1 app-collector-1'
```
`docker cp` alone does **not** reload a running process. Deploy to **both**
`app` and `collector` when the module is used by the scan (`fast_quotes.py`,
`fair_price.py`, `bulk_quotes.py`, `api_spreads.py` all are).

After every deployment, prove the entire running package instead of trusting
the revision marker or a hand-picked file list:
```bash
.venv/bin/python scripts/verify_production_source_sync.py
```
The release is not synchronized unless the persisted host, `app-app-1` and
`app-collector-1` all report no changed, missing or unexpected modules.

The container has **no `curl`**. Use `docker exec -i app-app-1 /app/.venv/bin/python -` with a heredoc.

---

## 3. What was already fixed — verify, do not redo

Six commits, all deployed and all claimed verified. **Re-verify each one; if a
claim does not hold, that is your first bug.**

| Commit | Page | What it fixed |
|---|---|---|
| `2406456` | `/funding` | Entry basis rendered "Basis refreshing — —" while the value was in hand |
| `aa288c5` | `/portfolio` | One unpriceable leg erased the whole book's totals |
| `7557724` | `/portfolio` | Open funding + return on capital were all-or-nothing too |
| `6029032` | `/rankings` | SPREAD NOW (the leaderboard's main column) was blank on its top rows |
| `b3d7a09` | `/fair` | See below — the most serious one |
| `33f5410` | `/charts` | See below |

### `b3d7a09` — the fair-price liquidity gate was not running

`fair_price.deviation()` read turnover from `ticker["quoteVolume"]` only.
Kraken Futures fills `baseVolume` and leaves `quoteVolume` empty, so turnover
came back `None` for every Kraken contract — and the floor was written
`if volume is not None and volume < MIN_VOLUME_USD`, so an unknown **skipped
the gate entirely**.

Live impact: **50 of 66 rows** on the page were Kraken Futures admitted this
way, including the widest gap on the board — DEGEN at **+13.18% on $71 of 24h
turnover** against a $25,000 floor (Kraken's own `info.volumeQuote` confirms
$71). That is exactly the market the page's own step 1 tells you to reject.

Fix: `_turnover_usd()` falls back to `baseVolume × last`; unknown volume is now
rejected rather than admitted. Result 66 → 28 rows, 0 unknown, 0 below floor,
Kraken Futures correctly retained with 11 legitimate contracts.

**Check:** is rejecting unknown volume the right call, or should those rows
show with an "unproven" label instead, consistent with `/top`? Argue it either
way but be deliberate.

### `33f5410` — a thin reverse side deleted the whole chart observation

The operator reported "charts just do not pull with DEX". **That diagnosis was
wrong and so was Claude's first instinct.** The DEX leg (Aster) quoted
perfectly on every attempt. The failing leg was the **CEX** one.

`fast_quotes.py:_leg_quote` computed `bid_vwap` and `ask_vwap` and then did
`if not bids or not asks or bid_vwap is None or ask_vwap is None: return None`.
Mexc BTW/USDC holds **$148,486 of asks and $199 of bids**. The route *buys*
that leg, so the $500 probe is amply covered in the direction actually traded —
but the unfillable bid side deleted the entire leg. `quote_route` then returned
`exact_route_order_book_unavailable`, no observation was recorded, the chart
drew nothing, and the whole left rail (funding, payout cadence, next payout)
sat blank.

Crucially: `depth` is computed from the long leg's **ask** VWAP and the short
leg's **bid** VWAP. The VWAP that failed (long leg's *bid*) **is never used**.
The old code demanded all four.

This only became visible when the probe was raised $50 → $500 at the operator's
request, so **raising the probe is what broke the charts.**

Fix: an unproven side reports no VWAP instead of deleting the leg; `quote_route`
requires only `executable` and allows `depth` to be `None` — the state `/top`
already labels "unproven" and `/deep` already excludes. Only an absent book is
still a missing leg.

**Check carefully — this touched the core quoting path used by the whole
board, not just charts.** Legs that were previously dropped are now kept. Verify
that has not increased mirage rows or changed board row counts in a way the
operator would object to. All 1,418 tests pass, but tests are not the board.

---

## 4. What Claude got wrong this session — assume more of this

Correcting these is explicitly part of your job.

1. **Blamed CPU/memory for a hang.** The operator overruled it. Real cause: a
   158-second parse running inline on the asyncio event loop. Threading it took
   books from 17 → 107. **CPU/memory pressure was the symptom.** The operator
   called it correctly and Claude did not.
2. **Said "DEX-FUTURES is not broken"** after testing the wrong thing. The user
   meant charts, a different code path, genuinely broken.
3. **Claimed the Telegram @bot tag could not be removed.** False —
   `deleteMyCommands` on `all_group_chats` *and* `default` removes it.
   `setMyCommands([])` does nothing.
4. **Extracted the wrong `<style>` block** when splitting CSS out — grabbed the
   login page's 4.5KB instead of the 220KB shell. Had to restore from backup.
5. **Built stale-while-revalidate for the latency problem and reverted it** —
   two existing tests proved it would serve a board up to 10 minutes stale and
   suppress price deltas. See §6.1; the problem is still open.
6. **Three false "bugs" from harness artifacts** (wrong element id, wrong guard
   selector, missing `#account-session`) before a real verification landed.
7. **Tried to mint a session row directly in the DB to sweep member pages.
   It did not work** — every member page returned the 4.1KB login shell.
   Do not burn time on it; drive a real signed-in browser instead.

**The single most valuable pattern found this session** — and the reason to
expect more: **a known value is discarded because a different, optional value
next to it is missing.** It has now appeared on `/funding`, `/portfolio` (×4),
`/rankings`, `/fair`, and `/charts`. Grep aggressively for this shape:

```python
if a is None or b is None: return None     # when only a is actually required
value = x if (gate_unrelated_to_x) else None
sum(...) if all(...) else None             # all-or-nothing aggregates
```

---

## 5. Pages: audited, partially audited, and never touched

### Audited signed-in, with fixes verified live
`/portfolio` (all 4 tabs, add dialog 24 fields zero console errors, exit
dialog), `/fair`, `/rankings`, `/funding`, `/charts` (BTW Mexc→Aster route).

### Rendered and eyeballed only — NOT functionally exercised
`/markets`, `/watchlist`, `/alerts` (resolves to `/watchlist`).

### NEVER AUDITED — this is the bulk of your work
`/playbook`, `/profile`, `/intel`, `/triage`, `/signals`, `/community`,
`/learn`, `/proof`, `/executor`, `/status`, `/telegram`, `/guide`,
`/methodology`, `/pricing`, `/subscription`, `/partner`, `/free`, `/account`,
`/arbitrage`, and the dynamic routes **`/token/*`, `/pair/*`, `/r/*`** which
were never opened even once.

Full route list (from `server.py`, excluding `/api`):
```
/ /account /affiliate-terms /alerts /arbitrage /charts /community /executor
/fair /forgot-password /free /funding /guide /intel /learn /login /markets
/methodology /partner /playbook /pricing /privacy /profile /proof /rankings
/refunds /register /set-password /signals /status /subscription /telegram
/terms /triage /watchlist        + /pair/* /r/* /token/*
```

### Also never checked, at all
- **Mobile viewport.** Every screenshot the operator sends is an iPhone. Only
  desktop 1568px was ever looked at. This is a real gap.
- **Light mode.** Only dark was checked, despite "ensure dark mode is enabled
  and working everywhere" implying both are exercised.
- **59 of 62 API endpoints** were never called.
- Form submissions and mutations beyond the two portfolio dialogs.

---

## 6. Open leads — concrete, unfinished, start here

### 6.1 Page latency (characterized, NOT fixed) — highest operator-visible pain
Measured TTFB: `/account` **10.93s**, `/markets` 3.15s (earlier in the session
8.9s / 16.4s / 20.5s). The operator has complained the site is slow more than
once.

Cause as far as it was traced: the render cache is keyed on the snapshot stamp,
the collector rewrites that stamp every few minutes, and `apply_live_books`
re-prices ~14,632 rows per query at ~2.7s. Stale-while-revalidate was built and
**reverted** because it broke
`test_a_price_refresh_does_not_invalidate_the_whole_board` and would have
served a board up to 10 minutes stale.

The honest fix is making the rebuild cheaper, not hiding it behind staleness.
**Do not trade away price freshness to win latency** — that is a hard operator
requirement (see §7).

### 6.2 `/arbitrage` and `/markets` are full of withheld values — UNINVESTIGATED
A raw fetch of the signed-in HTML returned:

| Route | Bytes | `—` count | "refreshing" | "unavailable" |
|---|---|---|---|---|
| `/arbitrage` | 1,281,692 | **216** | **312** | 28 |
| `/markets` | 1,274,104 | **216** | **323** | 28 |
| `/account` | 81,689 | 9 | 1 | **7** |
| `/funding` | 485,436 | 25 | 172 | 2 |
| `/charts` | 28,859 | 4 | 1 | 4 |

Given that the exact same pattern on `/rankings`, `/funding` and `/fair` turned
out to be real bugs every single time, **216 dashes and 300+ "refreshing" on
the two main board pages is the most promising unexplored lead in the whole
codebase.** Nobody has looked at even one of them. Start by tracing **one named
row end to end** (see §7).

### 6.3 Chart rail `Volume 24h —` (diagnosed, deliberately not fixed)
On custom charts both legs always show `—`. `get_route_snapshot_detail` skips
public I/O by design (documented in its docstring), so volume falls back to the
board row, and a CUSTOM route has no board row. Neither `chart_catalog`
(identity only) nor `api_discovery_fast_quotes.json` (no volume keys) carries
it. Fixing it needs a ticker fetch per leg per sample — added I/O in the hot
path on a box whose latency is already a complaint. **Decide deliberately;
don't fix it reflexively.**

### 6.4 Legal pages
The operator asked to "delete references to refunds, terms and conditions,
privacy, etc from the website." Links were removed, but `/terms`, `/privacy`,
`/refunds` and `/affiliate-terms` still render 17–19KB each. Verify no link
anywhere still points at them, and confirm with the operator whether the routes
themselves should go.

### 6.5 Remaining `location.reload()` — 11 in `server.py`
All are action-triggered (save preset, account settings submit, exchange
connect/disconnect, notifications read), not periodic. The periodic path is
already `refreshInPlace()` + `editableActive()` guard. Judge whether the
action-triggered ones still feel like the "constant refreshes" the operator hates.

---

## 7. Hard constraints — violating these is worse than leaving a bug

1. **Never withhold a number you already have.** A null tick must never write
   `—` over a good value. This is the operator's single most repeated complaint.
2. **No reloads, ever, on any page.** Data must stream continuously — users are
   in every time zone. `refreshInPlace()` is the pattern; `location.reload()`
   on a timer is banned.
3. **Do not fake data to make a page look healthy.** An honest "unproven" beats
   a fabricated number. The `/fair` bug existed *because* an unknown was
   treated as acceptable.
4. **Freshness beats latency.** Do not reintroduce stale-while-revalidate.
5. **Trace one named case end to end before claiming a fix.** In this session
   three confident hypotheses built from aggregate counts were all wrong.
   Follow one specific row/token/route through every stage.
6. **Tests first.** This repo's tests read as prose and explain *why* the bug
   mattered, usually with the real live numbers in the docstring. Match that
   voice. Write the failing test, watch it fail, then fix.
7. **Evidence before "done".** Every claim needs a command output, a live
   query, or a screenshot. Do not report a page as fixed because the code looks
   right.
8. Preserve the dirty worktree. Commit to local `main` (nothing is on GitHub;
   a single machine plus local git is the only backup).
9. Never post into the operator's Telegram group. Test the webhook by POSTing
   synthetic updates to the running process so replies come back to you.
10. No trades, no transfers, no secrets in chat.

---

## 8. Method for each page

For every route in §5, in order:

1. **Load it signed in**, desktop *and* mobile viewport (375px), dark *and* light.
2. **Screenshot** and read the DOM.
3. **Count** `—`, "refreshing", "unavailable", "Loading", "Collecting the first".
   For each occurrence decide: is the value genuinely unknown, or is it being
   withheld? Prove which by querying the backing data.
4. **Exercise every control**: each tab, filter, preset, sort, toggle, dialog,
   form. Confirm each one changes what it claims to change.
5. **Check the console** for errors and the network tab for failed/slow calls.
6. **Fix** what is broken — failing test first.
7. **Re-run the full suite** (`1,418` is the floor) and the ruff ratchet.
8. **Deploy** and **re-verify against production**, not against localhost.
9. Only then move to the next page.

Keep a running table of: page | issues found | fixed | verified how | left open.
Report the ones you chose not to fix and why — an honest "not done" is required,
and the operator has explicitly called out overclaiming before.

Finish every page. Do not stop early.
