const fs = require('fs');
const path = require('path');
const { chromium } = require('/Users/sviatoslav/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const base = 'https://spreadarbitrage.ink';
const email = 'spreadboard-audit-23e4a45@invalid.local';
const password = process.env.SPREADBOARD_AUDIT_PASSWORD;
const output = __dirname;
if (!password) throw new Error('missing audit password');

function routes(payload) {
  return (payload.groups || []).flatMap(group => group.routes || []);
}

async function fetchJson(page, pathname) {
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const result = await page.evaluate(async url => {
      const started = performance.now();
      const response = await fetch(url, { credentials: 'same-origin' });
      let payload = {};
      try { payload = await response.json(); } catch (_) {}
      return { status: response.status, elapsedMs: Math.round(performance.now() - started), payload };
    }, pathname);
    if (result.status === 200 && !String(result.payload.coverage_mode || '').includes('warming')) return result;
    await page.waitForTimeout(2500);
  }
  throw new Error(`API unavailable: ${pathname}`);
}

async function login(page) {
  const response = await page.goto(`${base}/login?next=%2Fmarkets`, { waitUntil: 'domcontentloaded', timeout: 90000 });
  if (!response || response.status() !== 200) throw new Error('login unavailable');
  const result = await page.evaluate(async credentials => {
    const loginResponse = await fetch('/api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(credentials),
    });
    return { status: loginResponse.status, payload: await loginResponse.json() };
  }, { email, password });
  if (result.status !== 200 || !result.payload.ok) throw new Error('audit login failed');
  await page.goto(`${base}/markets`, { waitUntil: 'domcontentloaded', timeout: 90000 });
}

async function auditViewport(browser, name, viewport, isMobile, doApi) {
  const context = await browser.newContext({ viewport, isMobile, colorScheme: 'dark' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(`page:${error.message}`));
  page.on('console', message => { if (message.type() === 'error') errors.push(`console:${message.text()}`); });
  page.on('requestfailed', request => {
    const failure = request.failure()?.errorText || 'failed';
    // Navigating between audited pages intentionally closes the previous SSE
    // board stream; that abort is not a product request failure.
    if (request.url().includes('/api/stream/board') && failure.includes('ERR_ABORTED')) return;
    errors.push(`request:${request.url()}:${failure}`);
  });
  await login(page);

  const familyKinds = {
    futuresFutures: 'FUTURES',
    futuresSpot: 'FUTURES-SPOT-PAIR',
    spotSpot: 'SPOT',
    futuresDex: 'DEX-FUTURES',
  };
  const families = {};
  const stability = [];
  if (doApi) {
    for (let pass = 0; pass < 4; pass += 1) {
      const sample = {};
      for (const [familyName, kind] of Object.entries(familyKinds)) {
        const result = await fetchJson(page, `/api/spreads?kind=${encodeURIComponent(kind)}&limit=20`);
        const found = routes(result.payload);
        if (!found.length) {
          throw new Error(`${familyName} has no routes: ${JSON.stringify({
            ok: result.payload.ok,
            mode: result.payload.mode,
            coverageMode: result.payload.coverage_mode,
            summary: result.payload.summary,
            filters: result.payload.filters,
            catalog: result.payload.funding_catalog,
          })}`);
        }
        const cexLane = kind !== 'DEX-FUTURES';
        const misclassified = found.filter(row => {
          const venues = [String(row.long_venue || ''), String(row.short_venue || '')];
          return cexLane
            ? venues.some(venue => venue.includes('DEX'))
            : !venues.some(venue => venue.startsWith('OKX DEX'));
        });
        if (misclassified.length) throw new Error(`${familyName} contains misclassified DEX routes`);
        const evidence = { elapsedMs: result.elapsedMs, routes: found.length, tokens: (result.payload.groups || []).length };
        families[familyName] = evidence;
        sample[familyName] = evidence;
      }
      stability.push(sample);
      if (pass < 3) await page.waitForTimeout(20000);
    }
  }

  const funding = {};
  for (const windowName of doApi ? ['now', '1d', '7d', '30d'] : []) {
    const result = await fetchJson(page, `/api/spreads?kind=FUTURES&funding_only=1&funding_window=${windowName}&limit=20`);
    const groups = result.payload.groups || [];
    const catalog = result.payload.funding_catalog || {};
    const values = groups.map(group => Number(windowName === 'now' ? group.best_funding_24h_pct : group.best_funding_window_pct));
    if (!groups.length || values.some(value => !Number.isFinite(value) || value <= 0)) throw new Error(`${windowName} contains blank/non-positive ranked values`);
    if (values.some((value, index) => index && value > values[index - 1] + 1e-12)) throw new Error(`${windowName} rank is not descending`);
    if (catalog.now_is_independent !== true) throw new Error(`${windowName} mixes Now and history`);
    const expectedKind = windowName === 'now' ? 'current_rate_projected_24h' : 'aggregate_exact_settlements';
    if (catalog.window_value_kind !== expectedKind) throw new Error(`${windowName} has wrong value kind`);
    funding[windowName] = { elapsedMs: result.elapsedMs, tokens: groups.length, valueKind: catalog.window_value_kind };
  }

  const marketsStarted = Date.now();
  const marketsResponse = await page.goto(`${base}/markets`, { waitUntil: 'domcontentloaded', timeout: 90000 });
  const marketsText = await page.locator('body').innerText();
  const marketsTextLower = marketsText.toLowerCase();
  const markets = {
    status: marketsResponse?.status(),
    elapsedMs: Date.now() - marketsStarted,
    researchOnly: marketsText.includes('Research only'),
    matchedEvidence: marketsTextLower.includes('matched $500 vwap'),
    indicativeEvidence: marketsTextLower.includes('indicative top-book quote'),
    overflow: await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - innerWidth)),
  };
  await page.screenshot({ path: path.join(output, `${name}-markets.png`), fullPage: true });

  const fundingStarted = Date.now();
  const fundingResponse = await page.goto(`${base}/funding?farm=futures-futures&rank=now`, { waitUntil: 'domcontentloaded', timeout: 90000 });
  const firstGroup = page.locator('details.funding-token-group').first();
  await firstGroup.waitFor({ state: 'attached', timeout: 30000 });
  await firstGroup.evaluate(element => { element.open = true; });
  const expanded = await firstGroup.evaluate(element => {
    const labels = [...element.querySelectorAll('.funding-realised .funding-window em')].map(item => item.textContent.trim());
    const pairLabels = [...element.querySelectorAll('.funding-pair-returns .funding-window em')].map(item => item.textContent.trim());
    const values = [...element.querySelectorAll('.funding-pair-returns .funding-window strong')].map(item => item.textContent.trim());
    return { labels, pairLabels, values, text: element.innerText };
  });
  const fundingPage = {
    status: fundingResponse?.status(),
    elapsedMs: Date.now() - fundingStarted,
    labels: expanded.labels,
    pairLabels: expanded.pairLabels,
    pairValues: expanded.values,
    hasFourGroupWindows: ['Now projected', '24h settled', '7d settled', '30d settled'].every(label => expanded.labels.includes(label)),
    hasFourPairWindows: ['Now', '24h', '7d', '30d'].every(label => expanded.pairLabels.includes(label)),
    researchOnly: expanded.text.includes('Research only'),
    overflow: await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - innerWidth)),
  };
  await page.screenshot({ path: path.join(output, `${name}-funding-expanded.png`), fullPage: true });

  await context.close();
  return { name, families, stability, funding, markets, fundingPage, errors };
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  try {
    const results = [
      await auditViewport(browser, 'desktop', { width: 1440, height: 1000 }, false, true),
      await auditViewport(browser, 'mobile', { width: 390, height: 844 }, true, false),
    ];
    const failures = results.filter(result =>
      result.errors.length || result.markets.status !== 200 || result.markets.researchOnly ||
      !result.markets.matchedEvidence || !result.markets.indicativeEvidence || result.markets.overflow > 2 ||
      result.fundingPage.status !== 200 || !result.fundingPage.hasFourGroupWindows ||
      !result.fundingPage.hasFourPairWindows || result.fundingPage.researchOnly || result.fundingPage.overflow > 2
    );
    const report = { auditedAt: new Date().toISOString(), results, failures };
    fs.writeFileSync(path.join(output, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
    console.log(JSON.stringify(report));
    if (failures.length) process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
