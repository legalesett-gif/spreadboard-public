const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const base = 'http://127.0.0.1:8309';
const out = __dirname;

async function measure(page) {
  return page.evaluate(() => ({
    viewport: { width: innerWidth, height: innerHeight },
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    title: document.title,
  }));
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
  const qrResponses = [];
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('response', response => {
    if (response.url().includes('/api/billing/crypto/qr/')) {
      qrResponses.push({ url: response.url(), status: response.status(), type: response.headers()['content-type'] });
    }
  });

  await page.goto(base + '/login', { waitUntil: 'networkidle' });
  await page.locator('input[name="email"]').fill(process.env.LOCAL_AUDIT_EMAIL);
  await page.locator('input[name="password"]').fill(process.env.LOCAL_AUDIT_PASSWORD);
  await Promise.all([
    page.waitForURL(url => !url.pathname.endsWith('/login')),
    page.locator('button[type="submit"]').click(),
  ]);

  const checks = {};
  await page.goto(base + '/pricing', { waitUntil: 'networkidle' });
  checks.desktopPricing = await measure(page);
  await page.screenshot({ path: path.join(out, 'desktop-pricing.png'), fullPage: true });

  await page.goto(base + '/telegram', { waitUntil: 'networkidle' });
  checks.desktopTelegram = await measure(page);
  await page.screenshot({ path: path.join(out, 'desktop-telegram.png'), fullPage: true });

  await page.goto(base + '/account?section=members', { waitUntil: 'networkidle' });
  checks.desktopAdmin = await measure(page);
  checks.adminIdentity = await page.evaluate(async () => {
    const response = await fetch('/api/session');
    const payload = await response.json();
    return {
      status: response.status,
      email: payload.user && payload.user.email,
      role: payload.user && payload.user.role,
      active: payload.user && payload.user.subscription_active,
    };
  });
  await page.screenshot({ path: path.join(out, 'desktop-admin.png'), fullPage: true });

  await page.goto(base + '/subscription?tier=scanner', { waitUntil: 'networkidle' });
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
    tokenButtons: [...document.querySelectorAll('[data-crypto-token-picker] button')].map(button => button.textContent.trim()),
  }));
  await page.screenshot({ path: path.join(out, 'desktop-crypto-usdc.png'), fullPage: true });

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
  await page.goto(base + '/pricing', { waitUntil: 'networkidle' });
  checks.mobilePricing = await measure(page);
  await page.screenshot({ path: path.join(out, 'mobile-pricing.png'), fullPage: true });

  await page.goto(base + '/subscription?tier=research_pro', { waitUntil: 'networkidle' });
  await page.locator('[data-subscription-consent]').check();
  await page.locator('[data-crypto-tier="research_pro"][data-crypto-period="30"]').click();
  await page.locator('[data-crypto-invoice]').waitFor({ state: 'visible' });
  await page.waitForFunction(() => {
    const image = document.querySelector('[data-crypto-qr]');
    return image && image.complete && image.naturalWidth > 0;
  });
  checks.mobileInvoice = await measure(page);
  await page.screenshot({ path: path.join(out, 'mobile-crypto-research.png'), fullPage: true });

  const audit = { checks, qrResponses, consoleErrors, pageErrors };
  fs.writeFileSync(path.join(out, 'audit.json'), JSON.stringify(audit, null, 2));
  process.stdout.write(JSON.stringify(audit, null, 2));
  await browser.close();
})().catch(error => {
  process.stderr.write(error.stack + '\n');
  process.exit(1);
});
