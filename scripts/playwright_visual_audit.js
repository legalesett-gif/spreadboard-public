#!/usr/bin/env node

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const mode = process.argv[2] || "production";
const outputDir = process.argv[3] || `output/playwright/${mode}-visual-audit`;
const productionBase = String(process.env.SPREADBOARD_AUDIT_BASE_URL || "https://spreadarbitrage.ink").replace(/\/$/, "");
const requestedPages = new Set(
  String(process.env.SPREADBOARD_AUDIT_PAGES || "")
    .split(",")
    .map(value => value.trim())
    .filter(Boolean),
);

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
  ["methodology", `${productionBase}/methodology`],
  ["status", `${productionBase}/status`],
  ["login", `${productionBase}/login`],
  ["forgot-password", `${productionBase}/forgot-password`],
];
const memberPages = [
  ["markets", `${productionBase}/markets`],
  ["funding", `${productionBase}/funding`],
  ["rankings", `${productionBase}/rankings`],
  ["charts", `${productionBase}/charts`],
  ["fair", `${productionBase}/fair`],
  ["intel", `${productionBase}/intel`],
  ["watchlist", `${productionBase}/watchlist`],
  ["alerts", `${productionBase}/alerts`],
  ["account", `${productionBase}/account`],
  ["account-settings", `${productionBase}/account?audit=settings#settings`],
  ["account-members", `${productionBase}/account?audit=members#members`],
  ["subscription", `${productionBase}/subscription`],
];

async function signIn(page) {
  const session = process.env.SPREADBOARD_AUDIT_SESSION || "";
  if (session) {
    await page.context().addCookies([{
      name: "spreadboard_session",
      value: session,
      url: productionBase,
      httpOnly: true,
      secure: true,
      sameSite: "Lax",
    }]);
    const response = await page.goto(`${productionBase}/api/session`, {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });
    return response?.ok() || false;
  }
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

async function exerciseMemberState(page, name) {
  if (name === "watchlist") {
    await page.getByLabel("Account mode").selectOption("isolated");
    await page.getByLabel("Futures notional, USD").fill("10000");
    await page.getByLabel("Chosen leverage").fill("3");
    await page.getByLabel("Exact maintenance margin, %").fill("1");
    await page.getByLabel("Public 24h stress move, %").fill("20");
    await page.getByLabel("Collateral allocated to this position, USD").fill("4000");
    await page.getByRole("button", { name: "Calculate stress margin" }).click();
    await page.waitForFunction(() =>
      document.querySelector("#marginPlanResult header strong")?.textContent?.includes("Within entered stress"),
      null,
      { timeout: 30_000 },
    );
    return await page.evaluate(() => ({
      marginPlannerVisible: Boolean(document.querySelector("#margin-planner")),
      marginVerdict: document.querySelector("#marginPlanResult header strong")?.textContent || "",
      transientCopyVisible: document.querySelector("#margin-planner")?.textContent?.includes("not stored") || false,
    }));
  }
  if (name === "charts") {
    const token = page.locator("[data-chart-token]");
    await token.fill("GUA");
    await token.dispatchEvent("input");
    await page.waitForFunction(() =>
      [...document.querySelectorAll("[data-chart-long] option")]
        .some(option => option.textContent.includes("OKX DEX 56")), null, { timeout: 30_000 });
    const chosen = await page.evaluate(() => {
      const long = document.querySelector("[data-chart-long]");
      const short = document.querySelector("[data-chart-short]");
      const dex = [...long.options].find(option => option.textContent.includes("OKX DEX 56"));
      const future = [...short.options].find(option => option.textContent.includes("Futures"));
      if (!dex || !future) return { token: "GUA", dexLongAvailable: Boolean(dex), futureShortAvailable: Boolean(future) };
      long.value = dex.value;
      long.dispatchEvent(new Event("change", { bubbles: true }));
      short.value = future.value;
      short.dispatchEvent(new Event("change", { bubbles: true }));
      return {
        token: "GUA",
        dexLongAvailable: true,
        futureShortAvailable: true,
        routeReady: !document.querySelector("[data-chart-create]").disabled,
      };
    });
    await page.waitForTimeout(250);
    return chosen;
  }
  if (name === "account") {
    await page.locator("[data-position-new]").click();
    const token = page.locator("[data-position-form] input[name=token]");
    await token.fill("GUA");
    await token.dispatchEvent("input");
    await page.waitForFunction(() =>
      [...document.querySelectorAll("[data-position-long-leg] option")]
        .some(option => option.textContent.includes("OKX DEX 56")), null, { timeout: 30_000 });
    const chosen = await page.evaluate(() => {
      const select = document.querySelector("[data-position-long-leg]");
      const dialog = document.querySelector("[data-position-dialog]");
      const dex = [...select.options].find(option => option.textContent.includes("OKX DEX 56"));
      if (dex) {
        select.value = dex.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      const modalScrollable = dialog.scrollHeight > dialog.clientHeight;
      dialog.scrollTop = dialog.scrollHeight;
      const submit = dialog.querySelector('button[type="submit"]');
      const dialogRect = dialog.getBoundingClientRect();
      const submitRect = submit.getBoundingClientRect();
      return {
        token: "GUA",
        dexLongAvailable: Boolean(dex),
        individualMarketCount: Math.max(0, select.options.length - 1),
        modalScrollable,
        saveButtonReachable: submitRect.top >= dialogRect.top && submitRect.bottom <= dialogRect.bottom,
      };
    });
    await page.waitForTimeout(250);
    return chosen;
  }
  return null;
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
      const pages = mode === "reference"
        ? referencePages.filter(([name]) => !requestedPages.size || requestedPages.has(name))
        : [
            ...publicPages.filter(([name]) => !requestedPages.size || requestedPages.has(name)),
            ...(
              !requestedPages.size || memberPages.some(([name]) => requestedPages.has(name))
                ? [["__sign_in__", ""]]
                : []
            ),
            ...memberPages.filter(([name]) => !requestedPages.size || requestedPages.has(name)),
          ];
      let signedIn = false;
      for (const [name, url] of pages) {
        if (name === "__sign_in__") {
          signedIn = await signIn(page);
          continue;
        }
        if (memberPages.some(([memberName]) => memberName === name) && !signedIn) continue;
        errors.length = 0;
        const started = Date.now();
        let response = null;
        let navigationError = null;
        let interactiveState = null;
        try {
          response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 90_000 });
          await page.waitForTimeout(mode === "reference" ? 2_500 : 700);
          if (mode !== "reference") interactiveState = await exerciseMemberState(page, name);
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
          interactiveState,
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
