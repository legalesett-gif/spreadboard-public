#!/usr/bin/env node

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const baseUrl = String(process.argv[2] || "http://127.0.0.1:8200").replace(/\/$/, "");
const outputDir = process.argv[3] || "output/playwright/product-audit";

const pages = [
  ["markets-table", "/markets?view=table"],
  ["markets-tokenized", "/markets?asset_class=tokenized"],
  ["telegram", "/telegram"],
  ["methodology", "/methodology"],
  ["proof", "/proof"],
  ["pricing", "/pricing"],
  ["subscription-scanner", "/subscription?tier=scanner"],
  ["executor", "/executor"],
];

const viewports = [
  ["desktop", { width: 1440, height: 1000 }],
  ["mobile", { width: 390, height: 844 }],
];

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
    for (const [viewportName, viewport] of viewports) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const browserErrors = [];
      page.on("pageerror", error => browserErrors.push(`pageerror:${error.message}`));
      page.on("console", message => {
        if (message.type() === "error") browserErrors.push(`console:${message.text()}`);
      });

      for (const [name, pathname] of pages) {
        browserErrors.length = 0;
        const started = Date.now();
        let response = null;
        let navigationError = null;
        try {
          response = await page.goto(`${baseUrl}${pathname}`, {
            waitUntil: "domcontentloaded",
            timeout: 90_000,
          });
          await page.waitForTimeout(350);
        } catch (error) {
          navigationError = `${error.name}:${error.message}`;
        }
        const audit = await page.evaluate(() => {
          const body = document.body;
          const root = document.documentElement;
          const text = body?.innerText || "";
          const rects = [...document.querySelectorAll("body *")]
            .map(element => ({ element, rect: element.getBoundingClientRect() }))
            .filter(({ rect }) => rect.width > 0 && rect.height > 0);
          const clipped = rects
            .filter(({ element, rect }) => {
              const style = getComputedStyle(element);
              let ancestor = element.parentElement;
              while (ancestor) {
                const ancestorStyle = getComputedStyle(ancestor);
                if (["auto", "scroll"].includes(ancestorStyle.overflowX)) return false;
                ancestor = ancestor.parentElement;
              }
              if (["fixed", "absolute"].includes(style.position)) return false;
              if (["auto", "scroll"].includes(style.overflowX)) return false;
              return rect.right > innerWidth + 2 || rect.left < -2;
            })
            .slice(0, 10)
            .map(({ element, rect }) => ({
              tag: element.tagName,
              className: String(element.className || "").slice(0, 100),
              left: Math.round(rect.left),
              right: Math.round(rect.right),
            }));
          return {
            title: document.title,
            h1: document.querySelector("h1")?.innerText || "",
            bodyChars: text.length,
            invalidText: ["undefined", "NaN", "[object Object]"].filter(item => text.includes(item)),
            bodyOverflowPx: Math.max(0, Math.round(root.scrollWidth - innerWidth)),
            clipped,
            links: document.querySelectorAll("a").length,
            buttons: document.querySelectorAll("button").length,
            inputs: document.querySelectorAll("input, select, textarea").length,
          };
        });
        const screenshot = path.join(outputDir, `${viewportName}-${name}.png`);
        await page.screenshot({ path: screenshot, fullPage: true });
        results.push({
          viewport: viewportName,
          name,
          pathname,
          status: response?.status() || null,
          elapsedMs: Date.now() - started,
          navigationError,
          browserErrors: [...browserErrors],
          screenshot,
          ...audit,
        });
      }
      await context.close();
    }
  } finally {
    await browser.close();
  }
  const failures = results.filter(result =>
    result.status !== 200 ||
    result.navigationError ||
    result.browserErrors.length ||
    result.invalidText.length ||
    result.bodyOverflowPx > 2 ||
    result.clipped.length
  );
  process.stdout.write(`${JSON.stringify({ baseUrl, results, failures }, null, 2)}\n`);
  if (failures.length) process.exitCode = 1;
}

main().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
