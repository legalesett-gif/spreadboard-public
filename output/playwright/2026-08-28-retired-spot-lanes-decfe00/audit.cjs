const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { chromium } = require('/Users/sviatoslav/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const base = 'https://spreadarbitrage.ink';
const email = 'route-lane-audit-decfe00@invalid.local';
const password = execFileSync('security', [
  'find-generic-password', '-a', 'route-lane-audit',
  '-s', 'SPREADBOARD_AUDIT/retired-spot-lanes-decfe00', '-w',
], { encoding: 'utf8' }).trim();
const output = __dirname;

async function login(page) {
  const response = await page.goto(`${base}/login`, {
    waitUntil: 'domcontentloaded', timeout: 90000,
  });
  if (!response || response.status() !== 200) throw new Error('login page unavailable');
  const result = await page.evaluate(async credentials => {
    const loginResponse = await fetch('/api/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(credentials),
    });
    return { status: loginResponse.status, payload: await loginResponse.json() };
  }, { email, password });
  if (result.status !== 200 || !result.payload.ok) throw new Error('audit login failed');
}

async function fetchJson(page, pathname) {
  return page.evaluate(async url => {
    const started = performance.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 90000);
    try {
      const response = await fetch(url, {
        credentials: 'same-origin', signal: controller.signal,
      });
      let payload = {};
      try { payload = await response.json(); } catch (_) {}
      return {
        status: response.status,
        elapsedMs: Math.round(performance.now() - started),
        payload,
      };
    } finally {
      clearTimeout(timeout);
    }
  }, pathname);
}

function rows(payload) {
  return (payload.groups || []).flatMap(group => group.routes || []);
}

async function auditViewport(browser, name, viewport, isMobile) {
  const context = await browser.newContext({ viewport, isMobile, colorScheme: 'dark' });
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
  await login(page);

  const pages = {};
  for (const [key, pathname] of [
    ['markets', '/markets'], ['charts', '/charts'], ['guide', '/guide'],
  ]) {
    const started = Date.now();
    const response = await page.goto(`${base}${pathname}`, {
      waitUntil: 'domcontentloaded', timeout: 90000,
    });
    const text = await page.locator('body').innerText();
    pages[key] = {
      status: response?.status(),
      elapsedMs: Date.now() - started,
      spotSpotLabel: /Spot[- /]Spot/i.test(text),
      spotDexLabel: /Spot[- /]DEX/i.test(text),
      futuresFuturesLabel: /Futures[- /]Futures/i.test(text),
      futuresSpotLabel: /Futures[- /]Spot/i.test(text),
      futuresDexLabel: /Futures[- /]DEX/i.test(text),
      overflowPx: await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - innerWidth)),
    };
    await page.screenshot({ path: path.join(output, `${name}-${key}.png`), fullPage: true });
  }
  await context.close();
  return { name, pages, errors };
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  try {
    const desktop = await auditViewport(browser, 'desktop', { width: 1440, height: 1000 }, false);
    const mobile = await auditViewport(browser, 'mobile', { width: 390, height: 844 }, true);

    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    await login(page);
    const laneKinds = ['FUTURES', 'FUTURES-SPOT-PAIR', 'DEX-FUTURES', 'SPOT', 'DEX-SPOT'];
    const lanes = {};
    for (const kind of laneKinds) {
      const result = await fetchJson(page, `/api/spreads?kind=${encodeURIComponent(kind)}&limit=25`);
      lanes[kind] = {
        status: result.status,
        elapsedMs: result.elapsedMs,
        groups: (result.payload.groups || []).length,
        routes: rows(result.payload).length,
        coverageMode: result.payload.coverage_mode || null,
      };
    }
    const references = {};
    for (const token of ['ANTHROPIC', 'ONE', 'SPCX', 'SHEIN', 'ONG', 'EDEN', 'XCU']) {
      const result = await fetchJson(page, `/api/spreads?q=${token}&limit=50`);
      references[token] = {
        status: result.status,
        elapsedMs: result.elapsedMs,
        groups: (result.payload.groups || []).length,
        routes: rows(result.payload).length,
        routeKinds: [...new Set(rows(result.payload).map(row => row.route_kind))].sort(),
      };
    }
    await context.close();

    const results = [desktop, mobile];
    const failures = [];
    for (const result of results) {
      for (const [pageName, evidence] of Object.entries(result.pages)) {
        if (evidence.status !== 200) failures.push(`${result.name}:${pageName}:http`);
        if (evidence.spotSpotLabel || evidence.spotDexLabel) failures.push(`${result.name}:${pageName}:retired-label`);
        if (evidence.overflowPx > 2) failures.push(`${result.name}:${pageName}:overflow`);
      }
      failures.push(...result.errors);
    }
    for (const kind of ['FUTURES', 'FUTURES-SPOT-PAIR']) {
      if (lanes[kind].status !== 200 || lanes[kind].routes < 1) failures.push(`${kind}:empty`);
    }
    for (const kind of ['SPOT', 'DEX-SPOT']) {
      if (lanes[kind].status !== 200 || lanes[kind].routes !== 0) failures.push(`${kind}:not-retired`);
    }
    if (lanes['DEX-FUTURES'].status !== 200) failures.push('DEX-FUTURES:http');

    const report = {
      auditedAt: new Date().toISOString(), revision: 'decfe00',
      results, lanes, references, failures,
    };
    fs.writeFileSync(path.join(output, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
    console.log(JSON.stringify(report));
    if (failures.length) process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
