const fs = require('fs');
const path = require('path');
const { chromium } = require('/Users/sviatoslav/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const baseURL = 'https://spreadarbitrage.ink';
const email = process.env.SPREADBOARD_AUDIT_EMAIL;
const sessionToken = process.env.SPREADBOARD_AUDIT_SESSION;
const outputDir = __dirname;

if (!email || !sessionToken) throw new Error('audit credentials are unavailable');

async function authenticate(context) {
  await context.addCookies([{
    name: 'spreadboard_session',
    value: sessionToken,
    domain: 'spreadarbitrage.ink',
    path: '/',
    httpOnly: true,
    secure: true,
    sameSite: 'Lax',
  }]);
}

async function fetchJson(page, pathname) {
  const result = await page.evaluate(async url => {
    const started = performance.now();
    const response = await fetch(url, { credentials: 'same-origin' });
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    return { status: response.status, elapsedMs: Math.round(performance.now() - started), payload };
  }, pathname);
  if (result.status !== 200) throw new Error(`${pathname} returned ${result.status}`);
  return result;
}

function routeRows(payload) {
  return (payload.groups || []).flatMap(group => group.routes || []);
}

async function auditViewport(browser, name, viewport, isMobile) {
  const context = await browser.newContext({ viewport, isMobile, colorScheme: 'dark' });
  await authenticate(context);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(`page:${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error') errors.push(`console:${message.text()}`);
  });
  page.on('requestfailed', request => {
    const failure = request.failure()?.errorText || 'failed';
    if (request.url().includes('/api/stream/board') && failure.includes('ERR_ABORTED')) return;
    errors.push(`request:${request.url()}:${failure}`);
  });

  const marketsResponse = await page.goto(`${baseURL}/markets`, {
    waitUntil: 'domcontentloaded',
    timeout: 90000,
  });
  const marketsText = await page.locator('body').innerText();
  const marketResult = {
    status: marketsResponse?.status(),
    researchOnly: /research only/i.test(marketsText),
    verifiedOnly: /verified only/i.test(marketsText),
    matchedEvidence: /matched \$500 vwap/i.test(marketsText),
    indicativeEvidence: /indicative/i.test(marketsText),
    overflowPx: await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - innerWidth)),
  };
  await page.screenshot({ path: path.join(outputDir, `${name}-markets.png`), fullPage: true });

  const fundingResponse = await page.goto(`${baseURL}/funding?farm=futures-futures&rank=now&q=SIREN`, {
    waitUntil: 'domcontentloaded',
    timeout: 90000,
  });
  const group = page.locator('details.funding-token-group').first();
  await group.waitFor({ state: 'attached', timeout: 30000 });
  await group.evaluate(element => { element.open = true; });
  const fundingResult = await group.evaluate(element => {
    const groupLabels = [...element.querySelectorAll('.funding-realised .funding-window em')].map(node => node.textContent.trim());
    const pairLabels = [...element.querySelectorAll('.funding-pair-returns .funding-window em')].map(node => node.textContent.trim());
    const pairValues = [...element.querySelectorAll('.funding-pair-returns .funding-window strong')].map(node => node.textContent.trim());
    return {
      groupLabels,
      pairLabels,
      pairValues,
      researchOnly: /research only/i.test(element.innerText),
      evidenceText: [...element.querySelectorAll('.spread-evidence-label')].map(node => node.textContent.trim()),
    };
  });
  fundingResult.status = fundingResponse?.status();
  fundingResult.overflowPx = await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - innerWidth));
  await page.screenshot({ path: path.join(outputDir, `${name}-funding-siren-expanded.png`), fullPage: true });

  await context.close();
  return { name, markets: marketResult, funding: fundingResult, errors };
}

(async () => {
  fs.mkdirSync(outputDir, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  try {
    const desktop = await auditViewport(browser, 'desktop', { width: 1440, height: 1000 }, false);
    const mobile = await auditViewport(browser, 'mobile', { width: 390, height: 844 }, true);
    const apiContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await authenticate(apiContext);
    const apiPage = await apiContext.newPage();
    await apiPage.goto(`${baseURL}/markets`, { waitUntil: 'domcontentloaded', timeout: 90000 });
    const apiWindows = {};
    for (const windowName of ['now', '1d', '7d', '30d']) {
      const response = await fetchJson(apiPage, `/api/spreads?kind=FUTURES&funding_only=1&funding_window=${windowName}&limit=20`);
      const rows = routeRows(response.payload);
      const metadata = response.payload.funding_catalog || {};
      const values = (response.payload.groups || []).map(group => Number(
        windowName === 'now' ? group.best_funding_24h_pct : group.best_funding_window_pct
      ));
      apiWindows[windowName] = {
        groups: (response.payload.groups || []).length,
        routes: rows.length,
        elapsedMs: response.elapsedMs,
        valueKind: metadata.window_value_kind,
        nowIndependent: metadata.now_is_independent,
        descending: values.every((value, index) => Number.isFinite(value) && value > 0 && (!index || value <= values[index - 1] + 1e-12)),
      };
    }
    await apiContext.close();

    const expectedGroup = ['Now projected', '24h settled', '7d settled', '30d settled'];
    const expectedPair = ['Now', '24h', '7d', '30d'];
    const results = [desktop, mobile];
    const failures = results.flatMap(result => {
      const issues = [];
      if (result.markets.status !== 200 || result.markets.researchOnly || result.markets.verifiedOnly) issues.push(`${result.name}: markets is not unified`);
      if (!result.markets.matchedEvidence || !result.markets.indicativeEvidence) issues.push(`${result.name}: evidence labels missing`);
      if (result.markets.overflowPx > 2 || result.funding.overflowPx > 2) issues.push(`${result.name}: horizontal overflow`);
      if (result.funding.status !== 200 || result.funding.researchOnly) issues.push(`${result.name}: funding is not unified`);
      if (!expectedGroup.every(label => result.funding.groupLabels.includes(label))) issues.push(`${result.name}: group windows incomplete`);
      if (!expectedPair.every(label => result.funding.pairLabels.includes(label))) issues.push(`${result.name}: pair windows incomplete`);
      return issues.concat(result.errors);
    });
    for (const [windowName, evidence] of Object.entries(apiWindows)) {
      const expectedKind = windowName === 'now' ? 'current_rate_projected_24h' : 'aggregate_exact_settlements';
      if (!evidence.groups || !evidence.routes || !evidence.descending || evidence.valueKind !== expectedKind || evidence.nowIndependent !== true) {
        failures.push(`${windowName}: API ranking/evidence contract failed`);
      }
    }
    const report = { auditedAt: new Date().toISOString(), revision: 'c8f131e', results, apiWindows, failures };
    fs.writeFileSync(path.join(outputDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
    console.log(JSON.stringify(report));
    if (failures.length) process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
