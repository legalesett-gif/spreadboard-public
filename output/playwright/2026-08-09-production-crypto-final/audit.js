const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const base = 'https://spreadarbitrage.ink';
const out = __dirname;

async function measure(page) {
  return page.evaluate(() => ({
    viewport: { width: innerWidth, height: innerHeight },
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    title: document.title,
    heading: document.querySelector('h1')?.textContent?.trim() || '',
  }));
}

async function goto(page, route) {
  const response = await page.goto(base + route, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForTimeout(800);
  return response && response.status();
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.BROWSER_EXECUTABLE || undefined,
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  const failedResponses = [];
  const qrResponses = [];
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push({ url: page.url(), text: message.text() });
  });
  page.on('pageerror', error => pageErrors.push({ url: page.url(), text: error.message }));
  page.on('response', response => {
    if (response.status() >= 400) failedResponses.push({ url: response.url(), status: response.status() });
    if (response.url().includes('/api/billing/crypto/qr/')) {
      qrResponses.push({ url: response.url(), status: response.status(), type: response.headers()['content-type'] });
    }
  });

  const checks = {};
  checks.loginPageStatus = await goto(page, '/login');
  await page.locator('input[name="email"]').fill(process.env.PROD_AUDIT_EMAIL);
  await page.locator('input[name="password"]').fill(process.env.PROD_AUDIT_PASSWORD);
  await Promise.all([
    page.waitForURL(url => !url.pathname.endsWith('/login'), { timeout: 30000 }),
    page.locator('button[type="submit"]').click(),
  ]);
  checks.session = await page.evaluate(async () => {
    const response = await fetch('/api/session');
    const payload = await response.json();
    return {
      status: response.status,
      email: payload.user && payload.user.email,
      role: payload.user && payload.user.role,
      active: payload.user && payload.user.subscription_active,
      tier: payload.user && payload.user.subscription_tier,
    };
  });

  checks.pricingStatus = await goto(page, '/pricing');
  checks.desktopPricing = await measure(page);
  checks.pricingCopy = await page.locator('body').innerText().then(text => ({
    cryptoFirst: text.includes('pay once in USDC or USDT on Arbitrum'),
    noAutoRenewal: text.includes('No card, no automatic renewal'),
    onboarding: text.includes('What you get — and how to start'),
  }));
  await page.screenshot({ path: path.join(out, 'desktop-pricing.png'), fullPage: true });

  checks.telegramStatus = await goto(page, '/telegram');
  checks.desktopTelegram = await measure(page);
  checks.telegramCopy = await page.locator('body').innerText().then(text => ({
    privateForumConnected: text.includes('Private subscriber forum connected'),
    publicBroadcastOptional: text.includes('Optional public broadcast not configured'),
    directTop: text.includes('works in a direct chat with the bot'),
  }));
  await page.screenshot({ path: path.join(out, 'desktop-telegram.png'), fullPage: true });

  checks.accountStatus = await goto(page, '/account?section=members');
  checks.desktopAccount = await measure(page);
  await page.screenshot({ path: path.join(out, 'desktop-account.png'), fullPage: false });

  checks.marketsStatus = await goto(page, '/markets?view=table');
  await page.waitForTimeout(2500);
  checks.desktopMarkets = await measure(page);
  checks.usd1Visible = (await page.locator('select[name="quote"] option[value="USD1"]').count()) === 1;
  await page.screenshot({ path: path.join(out, 'desktop-markets.png'), fullPage: false });

  checks.scannerSubscriptionStatus = await goto(page, '/subscription?tier=scanner');
  await page.locator('[data-subscription-consent]').check();
  await page.locator('[data-crypto-tier="scanner"][data-crypto-period="30"]').click();
  await page.locator('[data-crypto-invoice]').waitFor({ state: 'visible' });
  await page.waitForFunction(() => {
    const image = document.querySelector('[data-crypto-qr]');
    return image && image.complete && image.naturalWidth > 0;
  });
  checks.desktopInvoice = await measure(page);
  checks.usdc = await page.evaluate(() => ({
    amount: document.querySelector('[data-crypto-amount]').textContent.trim(),
    contract: document.querySelector('[data-crypto-contract]').textContent.trim(),
    wallet: document.querySelector('[data-crypto-wallet-link]').getAttribute('href'),
    qr: document.querySelector('[data-crypto-qr]').getAttribute('src'),
  }));
  await page.locator('[data-crypto-checkout]').screenshot({ path: path.join(out, 'desktop-crypto-usdc-panel.png') });
  await page.screenshot({ path: path.join(out, 'desktop-crypto-usdc-viewport.png'), fullPage: false });

  await page.getByRole('button', { name: 'Pay with USDT' }).click();
  await page.waitForFunction(() => document.querySelector('[data-crypto-contract]').textContent.includes('fd086'));
  await page.waitForFunction(() => {
    const image = document.querySelector('[data-crypto-qr]');
    return image && image.complete && image.naturalWidth > 0;
  });
  checks.usdt = await page.evaluate(() => ({
    amount: document.querySelector('[data-crypto-amount]').textContent.trim(),
    contract: document.querySelector('[data-crypto-contract]').textContent.trim(),
    wallet: document.querySelector('[data-crypto-wallet-link]').getAttribute('href'),
    qr: document.querySelector('[data-crypto-qr]').getAttribute('src'),
  }));

  await page.setViewportSize({ width: 390, height: 844 });
  checks.mobilePricingStatus = await goto(page, '/pricing');
  checks.mobilePricing = await measure(page);
  await page.screenshot({ path: path.join(out, 'mobile-pricing.png'), fullPage: true });

  checks.mobileTelegramStatus = await goto(page, '/telegram');
  checks.mobileTelegram = await measure(page);
  await page.screenshot({ path: path.join(out, 'mobile-telegram.png'), fullPage: true });

  checks.researchSubscriptionStatus = await goto(page, '/subscription?tier=research_pro');
  await page.locator('[data-subscription-consent]').check();
  await page.locator('[data-crypto-tier="research_pro"][data-crypto-period="30"]').click();
  await page.locator('[data-crypto-invoice]').waitFor({ state: 'visible' });
  await page.waitForFunction(() => {
    const image = document.querySelector('[data-crypto-qr]');
    return image && image.complete && image.naturalWidth > 0;
  });
  checks.mobileInvoice = await measure(page);
  checks.research = await page.evaluate(() => ({
    amount: document.querySelector('[data-crypto-amount]').textContent.trim(),
    contract: document.querySelector('[data-crypto-contract]').textContent.trim(),
    wallet: document.querySelector('[data-crypto-wallet-link]').getAttribute('href'),
    qr: document.querySelector('[data-crypto-qr]').getAttribute('src'),
  }));
  await page.locator('[data-crypto-checkout]').screenshot({ path: path.join(out, 'mobile-crypto-research-panel.png') });
  await page.screenshot({ path: path.join(out, 'mobile-crypto-research-viewport.png'), fullPage: false });

  checks.mobileMarketsStatus = await goto(page, '/markets?view=table');
  await page.waitForTimeout(1800);
  checks.mobileMarkets = await measure(page);
  await page.screenshot({ path: path.join(out, 'mobile-markets.png'), fullPage: false });

  const invoiceIds = [checks.usdc.qr, checks.research.qr].map(value => Number(value.match(/qr\/(\d+)/)[1]));
  const audit = { checks, invoiceIds, qrResponses, consoleErrors, pageErrors, failedResponses };
  fs.writeFileSync(path.join(out, 'audit.json'), JSON.stringify(audit, null, 2));
  process.stdout.write(JSON.stringify(audit, null, 2));
  await browser.close();
})().catch(error => {
  process.stderr.write(error.stack + '\n');
  process.exit(1);
});
