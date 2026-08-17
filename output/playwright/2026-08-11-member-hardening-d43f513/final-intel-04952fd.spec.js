const fs = require('fs');
const { test, expect } = require('@playwright/test');

const credentials = JSON.parse(fs.readFileSync(process.env.SPREADBOARD_QA_CREDENTIALS, 'utf8'));
test.use({
  launchOptions: {
    executablePath: '/Users/sviatoslav/Library/Caches/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-mac-arm64/chrome-headless-shell',
  },
});

async function login(page) {
  await page.goto('https://spreadarbitrage.ink/login', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="email"]').fill(credentials.email);
  await page.locator('input[name="password"]').fill(credentials.password);
  await page.locator('#loginForm button[type="submit"]').click();
  await page.waitForURL(url => !url.pathname.startsWith('/login'));
}

test('desktop Intel first-use state', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page);
  await page.goto('https://spreadarbitrage.ink/intel', { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('heading', { name: 'Intel activates from the next bot lookup' })).toBeVisible();
  await expect(page.getByText('Latest Brief')).toHaveCount(0);
  await page.screenshot({
    path: 'output/playwright/2026-08-11-member-hardening-d43f513/final-04952fd-desktop-intel.png',
    fullPage: true,
  });
});

test('mobile Intel first-use state', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto('https://spreadarbitrage.ink/intel', { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('heading', { name: 'Intel activates from the next bot lookup' })).toBeVisible();
  await page.screenshot({
    path: 'output/playwright/2026-08-11-member-hardening-d43f513/final-04952fd-mobile-intel.png',
    fullPage: true,
  });
});
