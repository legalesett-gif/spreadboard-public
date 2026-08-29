const fs = require('fs');
const path = require('path');
const { chromium } = require('/Users/sviatoslav/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const baseURL = 'https://spreadarbitrage.ink';
const email = 'spreadboard-audit-06505f8@invalid.local';
const password = process.env.SPREADBOARD_AUDIT_PASSWORD;
const outputDir = __dirname;
if (!password) throw new Error('missing audit password');

function rows(payload) {
  return (payload.groups || []).flatMap(group => group.routes || []);
}

async function login(page) {
  const response = await page.goto(`${baseURL}/login?next=%2Ffunding`, {
    waitUntil: 'domcontentloaded', timeout: 60000,
  });
  if (!response || response.status() !== 200) throw new Error('login unavailable');
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(password);
  await Promise.all([
    page.waitForURL(url => !url.pathname.startsWith('/login'), { timeout: 30000 }),
    page.locator('#loginForm button[type="submit"]').click(),
  ]);
}

async function fetchFunding(page, windowName) {
  return page.evaluate(async windowName => {
    const started = performance.now();
    const response = await fetch(`/api/spreads?kind=FUTURES&funding_only=1&funding_window=${windowName}&limit=20`, {
      credentials: 'same-origin',
    });
    return {
      status: response.status,
      elapsedMs: Math.round(performance.now() - started),
      payload: await response.json(),
    };
  }, windowName);
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
    const message = failure ? failure.errorText : 'failed';
    if (!request.url().includes('/api/stream/board') || !message.includes('ERR_ABORTED')) {
      errors.push(`request:${request.url()}:${message}`);
    }
  });
  await login(page);

  const funding = {};
  for (const windowName of ['now', '1d', '7d', '30d']) {
    const result = await fetchFunding(page, windowName);
    if (result.status !== 200) throw new Error(`${windowName} API status ${result.status}`);
    const found = rows(result.payload);
    if (!found.length) throw new Error(`${windowName} API returned zero routes`);
    const catalog = result.payload.funding_catalog || {};
    const expectedKind = windowName === 'now' ? 'current_rate_projected_24h' : 'aggregate_exact_settlements';
    if (catalog.window_value_kind !== expectedKind) throw new Error(`${windowName} value kind mismatch`);
    if (catalog.now_is_independent !== true) throw new Error(`${windowName} mixed live and history`);
    const malformed = found.filter(route => {
      const windows = route.settled_funding_windows;
      return !windows || !Object.prototype.hasOwnProperty.call(windows, '1d')
        || !Object.prototype.hasOwnProperty.call(windows, '7d')
        || !Object.prototype.hasOwnProperty.call(windows, '30d');
    });
    if (malformed.length) throw new Error(`${windowName} has ${malformed.length} routes without all window keys`);
    if (windowName !== 'now') {
      const incompleteSelected = found.filter(route => route.settled_funding_windows[windowName] == null);
      if (incompleteSelected.length) throw new Error(`${windowName} has ${incompleteSelected.length} selected-window blanks`);
    }
    funding[windowName] = {
      elapsedMs: result.elapsedMs,
      routeCount: found.length,
      tokenCount: (result.payload.groups || []).length,
      valueKind: catalog.window_value_kind,
      windowSample: found.slice(0, 2).map(route => ({
        token: route.token,
        windows: route.settled_funding_windows,
      })),
    };
  }

  const started = Date.now();
  const response = await page.goto(`${baseURL}/funding?funding_window=7d`, {
    waitUntil: 'domcontentloaded', timeout: 60000,
  });
  const pageElapsedMs = Date.now() - started;
  if (!response || response.status() !== 200) throw new Error('Funding page unavailable');
  const firstGroup = page.locator('details.funding-token-group').first();
  await firstGroup.scrollIntoViewIfNeeded();
  const summaryText = await firstGroup.locator('summary').innerText();
  console.log(JSON.stringify({ name, summaryText }));
  const summaryLower = summaryText.toLowerCase();
  for (const label of ['Now projected', '24h settled', '7d settled', '30d settled']) {
    if (!summaryLower.includes(label.toLowerCase())) throw new Error(`summary missing ${label}`);
  }
  await firstGroup.locator('summary').click();
  const firstPairText = await firstGroup.locator('.funding-pair-row').first().innerText();
  const firstPairLower = firstPairText.toLowerCase();
  for (const label of ['Now', '24h', '7d', '30d']) {
    if (!firstPairLower.includes(label.toLowerCase())) throw new Error(`expanded pair missing ${label}`);
  }
  const overflowPx = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  await page.screenshot({ path: path.join(outputDir, `${name}-funding-7d.png`), fullPage: true });
  await context.close();
  return {
    name,
    funding,
    page: { status: response.status(), elapsedMs: pageElapsedMs, overflowPx, summaryText, firstPairText },
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
