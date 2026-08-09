#!/usr/bin/env node

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const mode = process.argv[2] || "production";
const outputDir = process.argv[3] || `output/playwright/${mode}-visual-audit`;
const productionBase = String(process.env.SPREADBOARD_AUDIT_BASE_URL || "https://spreadarbitrage.ink").replace(/\/$/, "");

const referencePages = [
  ["uainvest-scanner", "https://uainvest.com.ua/arbitrage"],
  ["uainvest-vip", "https://uainvest.com.ua/arbitrage/vip"],
  ["uacryptoinvest-scanner", "https://uacryptoinvest.com/arbitrage"],
];
const publicPages = [
  ["free", `${productionBase}/free`],
  ["pricing", `${productionBase}/pricing`],
  ["telegram", `${productionBase}/telegram`],
  ["guide", `${productionBase}/guide`],
  ["status", `${productionBase}/status`],
  ["login", `${productionBase}/login`],
  ["forgot-password", `${productionBase}/forgot-password`],
];
const memberPages = [
  ["markets", `${productionBase}/markets`],
  ["funding", `${productionBase}/funding`],
  ["charts", `${productionBase}/charts`],
  ["fair", `${productionBase}/fair`],
  ["alerts", `${productionBase}/alerts`],
  ["account", `${productionBase}/account`],
  ["subscription", `${productionBase}/subscription`],
];

async function signIn(page) {
  const email = process.env.SPREADBOARD_AUDIT_EMAIL || "";
  const password = process.env.SPREADBOARD_AUDIT_PASSWORD || "";
  if (!email || !password) return false;
  await page.goto(`${productionBase}/login`, { waitUntil: "domcontentloaded", timeout: 90_000 });
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(password);
  await Promise.all([
    page.waitForURL(url => !url.pathname.endsWith("/login"), { timeout: 30_000 }),
    page.locator('button[type="submit"]').click(),
  ]);
  return true;
}

async function main() {
  fs.mkdirSync(outputDir, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH }
      : {}),
  });
  const results = [];
  try {
    for (const [viewportName, viewport] of [
      ["desktop", { width: 1440, height: 1000 }],
      ["mobile", { width: 390, height: 844 }],
    ]) {
      const context = await browser.newContext({ viewport, colorScheme: "dark" });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(`pageerror:${error.message}`));
      page.on("console", message => {
        if (message.type() === "error") errors.push(`console:${message.text()}`);
      });
      const signedIn = mode === "production" ? await signIn(page) : false;
      const pages = mode === "reference"
        ? referencePages
        : [...publicPages, ...(signedIn ? memberPages : [])];
      for (const [name, url] of pages) {
        errors.length = 0;
        const started = Date.now();
        let response = null;
        let navigationError = null;
        try {
          response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 90_000 });
          await page.waitForTimeout(mode === "reference" ? 2_500 : 700);
        } catch (error) {
          navigationError = `${error.name}:${error.message}`;
        }
        const audit = await page.evaluate(() => {
          const text = document.body?.innerText || "";
          return {
            title: document.title,
            h1: document.querySelector("h1")?.innerText || "",
            bodyChars: text.length,
            invalidText: ["undefined", "NaN", "[object Object]"].filter(value => text.includes(value)),
            bodyOverflowPx: Math.max(0, Math.round(document.documentElement.scrollWidth - innerWidth)),
          };
        }).catch(() => ({ title: "", h1: "", bodyChars: 0, invalidText: [], bodyOverflowPx: 0 }));
        const screenshot = path.join(outputDir, `${viewportName}-${name}.png`);
        await page.screenshot({ path: screenshot, fullPage: true });
        results.push({
          viewport: viewportName,
          name,
          url,
          finalUrl: page.url(),
          status: response?.status() || null,
          elapsedMs: Date.now() - started,
          navigationError,
          browserErrors: [...errors],
          screenshot,
          ...audit,
        });
      }
      await context.close();
    }
  } finally {
    await browser.close();
  }
  const report = {
    mode,
    generatedAt: new Date().toISOString(),
    results,
    failures: results.filter(result =>
      !result.status || result.status >= 400 || result.navigationError ||
      result.browserErrors.length || result.invalidText.length || result.bodyOverflowPx > 2
    ),
  };
  fs.writeFileSync(path.join(outputDir, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  if (report.failures.length) process.exitCode = 1;
}

main().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
