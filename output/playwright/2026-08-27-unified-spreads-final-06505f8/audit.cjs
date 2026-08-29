const fs = require('fs');
const path = require('path');
const { chromium } = require('/Users/sviatoslav/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const baseURL = 'https://spreadarbitrage.ink';
const email = 'spreadboard-audit-06505f8@invalid.local';
const password = process.env.SPREADBOARD_AUDIT_PASSWORD;
const outputDir = __dirname;

if (!password) throw new Error('missing audit password');

function routes(payload) {
  return (payload.groups || []).flatMap(group => group.routes || []);
}

async function fetchJson(page, pathname) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const result = await page.evaluate(async url => {
      const started = performance.now();
      const response = await fetch(url, { credentials: 'same-origin' });
      return {
        status: response.status,
        elapsedMs: Math.round(performance.now() - started),
        payload: await response.json(),
      };
    }, pathname);
    if (result.status === 200 && !(result.payload.coverage_mode || '').includes('warming')) {
      return result;
    }
    await page.waitForTimeout(3000);
  }
  throw new Error(`API remained unavailable: ${pathname}`);
}

async function login(page) {
  const response = await page.goto(`${baseURL}/login?next=%2Fmarkets`, {
    waitUntil: 'domcontentloaded',
    timeout: 60000,
  });
  if (!response || response.status() !== 200) throw new Error('login page unavailable');
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(password);
  await Promise.all([
    page.waitForURL(url => !url.pathname.startsWith('/login'), { timeout: 30000 }),
    page.locator('#loginForm button[type="submit"]').click(),
  ]);
}

async function auditViewport(browser, name, viewport, isMobile = false) {
  const context = await browser.newContext({ viewport, isMobile });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(`pageerror:${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error') errors.push(`console:${message.text()}`);
  });
  page.on('requestfailed', request => {
    const failure = request.failure();
    errors.push(`request:${request.url()}:${failure ? failure.errorText : 'failed'}`);
  });
  await login(page);

  const familyPaths = {
    futures_futures: '/api/spreads?kind=FUTURES&limit=20',
    futures_spot: '/api/spreads?kind=FUTURES-SPOT-PAIR&limit=20',
    spot_spot: '/api/spreads?kind=SPOT&limit=20',
    futures_dex: '/api/spreads?kind=DEX-FUTURES&limit=20',
    spot_dex: '/api/spreads?kind=DEX-SPOT&limit=20',
  };
  const families = {};
  for (const [family, pathname] of Object.entries(familyPaths)) {
    const result = await fetchJson(page, pathname);
    const found = routes(result.payload);
    if (!found.length) throw new Error(`${family} returned zero routes`);
    const cexOnly = !family.includes('dex');
    const foreignDex = found.filter(row => {
      const venues = [String(row.long_venue || ''), String(row.short_venue || '')];
      if (cexOnly) return venues.some(venue => venue.includes('DEX'));
      return !venues.some(venue => venue.startsWith('OKX DEX'));
    });
    if (foreignDex.length) throw new Error(`${family} contains misclassified DEX routes`);
    families[family] = {
      elapsedMs: result.elapsedMs,
      routeCount: found.length,
      tokenCount: (result.payload.groups || []).length,
      summary: result.payload.summary || {},
      sample: found.slice(0, 2).map(row => ({
        token: row.token,
        route_kind: row.route_kind,
        long: `${row.long_venue} ${row.long_market_type}`,
        short: `${row.short_venue} ${row.short_market_type}`,
        spread: row.depth_weighted_spread_pct ?? row.executable_spread_pct,
      })),
    };
  }

  const catalogue = {};
  for (const token of ['OPENAI', 'ANTHROPIC', 'SPCX', 'GUA']) {
    const result = await fetchJson(page, `/api/catalog-pairs?token=${encodeURIComponent(token)}&limit=2000`);
    catalogue[token] = {
      elapsedMs: result.elapsedMs,
      routeCount: (result.payload.routes || []).length,
      catalogMarketCount: result.payload.catalog_market_count || 0,
      freshMarketCount: result.payload.fresh_market_count || 0,
      routeKinds: [...new Set((result.payload.routes || []).map(row => row.route_kind))].sort(),
    };
  }

  const funding = {};
  for (const windowName of ['now', '1d', '7d', '30d']) {
    const result = await fetchJson(
      page,
      `/api/spreads?kind=FUTURES&funding_only=1&funding_window=${windowName}&limit=20`,
    );
    const found = routes(result.payload);
    const catalog = result.payload.funding_catalog || {};
    if (windowName !== 'now' && catalog.window_value_kind !== 'aggregate_exact_settlements') {
      throw new Error(`${windowName} did not use exact settlements`);
    }
    if (catalog.now_is_independent !== true) throw new Error(`${windowName} mixed Now with history`);
    funding[windowName] = {
      elapsedMs: result.elapsedMs,
      routeCount: found.length,
      tokenCount: (result.payload.groups || []).length,
      valueKind: catalog.window_value_kind,
      nowIndependent: catalog.now_is_independent,
      completeWindowLegCount: catalog.complete_window_leg_count,
    };
  }

  const marketsStarted = Date.now();
  const marketsResponse = await page.goto(`${baseURL}/markets`, {
    waitUntil: 'domcontentloaded',
    timeout: 60000,
  });
  const marketsElapsedMs = Date.now() - marketsStarted;
  const marketsText = await page.locator('body').innerText();
  if (marketsText.includes('Research only')) throw new Error('retired Research only lane is visible');
  const marketsOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  await page.screenshot({ path: path.join(outputDir, `${name}-markets.png`), fullPage: true });

  const fundingStarted = Date.now();
  const fundingResponse = await page.goto(`${baseURL}/funding?funding_window=7d`, {
    waitUntil: 'domcontentloaded',
    timeout: 60000,
  });
  const fundingElapsedMs = Date.now() - fundingStarted;
  const fundingOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  await page.screenshot({ path: path.join(outputDir, `${name}-funding-7d.png`), fullPage: true });

  await context.close();
  return {
    name,
    families,
    catalogue,
    funding,
    pages: {
      markets: { status: marketsResponse && marketsResponse.status(), elapsedMs: marketsElapsedMs, overflowPx: marketsOverflow },
      funding7d: { status: fundingResponse && fundingResponse.status(), elapsedMs: fundingElapsedMs, overflowPx: fundingOverflow },
    },
    errors,
  };
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  try {
    const report = {
      auditedAt: new Date().toISOString(),
      desktop: await auditViewport(browser, 'desktop', { width: 1440, height: 1000 }),
      mobile: await auditViewport(browser, 'mobile', { width: 390, height: 844 }, true),
    };
    fs.writeFileSync(path.join(outputDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
