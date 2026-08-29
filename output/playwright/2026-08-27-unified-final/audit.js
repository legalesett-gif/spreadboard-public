"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const base = "https://spreadarbitrage.ink";
const session = process.env.SPREADBOARD_AUDIT_SESSION;
const exactPair = process.env.SPREADBOARD_AUDIT_PAIR || "";
const output = __dirname;

async function inspect(page, viewport) {
  const errors = [];
  page.on("pageerror", error => errors.push(`page:${error.message}`));
  page.on("console", message => {
    if (message.type() === "error") errors.push(`console:${message.text()}`);
  });
  await page.context().addCookies([{
    name: "spreadboard_session",
    value: session,
    url: base,
    httpOnly: true,
    secure: true,
    sameSite: "Lax",
  }]);
  const started = Date.now();
  const response = await page.goto(`${base}/funding?farm=futures-futures&rank=now&q=SIREN`, {
    waitUntil: "domcontentloaded",
    timeout: 90_000,
  });
  await page.waitForTimeout(700);
  const funding = await page.evaluate(() => {
    const text = document.body.innerText;
    const links = [...document.querySelectorAll('a[href^="/pair/"]')];
    const sirenPair = links.find(link => {
      const row = link.closest("article, tr, section, div");
      return (row?.innerText || link.innerText).includes("SIREN");
    }) || links[0];
    return {
      bodyChars: text.length,
      researchOnly: text.includes("Research only"),
      windows: ["Now", "24h", "7d", "30d"].filter(label => text.includes(label)),
      exactValues: ["+0.16%", "+0.85%", "+0.10%"].filter(value => text.includes(value)),
      invalid: ["undefined", "NaN", "[object Object]"].filter(value => text.includes(value)),
      overflow: Math.max(0, document.documentElement.scrollWidth - innerWidth),
      pairHref: sirenPair?.getAttribute("href") || null,
    };
  });
  await page.screenshot({ path: path.join(output, `${viewport}-funding-siren.png`), fullPage: true });
  let pair = null;
  if (exactPair || funding.pairHref) {
    const pairResponse = await page.goto(`${base}${exactPair || funding.pairHref}`, {
      waitUntil: "domcontentloaded",
      timeout: 90_000,
    });
    await page.waitForTimeout(500);
    pair = await page.evaluate(() => {
      const text = document.body.innerText;
      return {
        statusText: document.querySelector("h1")?.innerText || "",
        routeReturns: text.includes("Route returns"),
        windows: ["Now", "24h", "7d", "30d"].filter(label => text.includes(label)),
        exactValues: ["+0.16%", "+0.85%", "+0.10%"].filter(value => text.includes(value)),
        invalid: ["undefined", "NaN", "[object Object]"].filter(value => text.includes(value)),
        overflow: Math.max(0, document.documentElement.scrollWidth - innerWidth),
      };
    });
    pair.httpStatus = pairResponse.status();
    await page.screenshot({ path: path.join(output, `${viewport}-pair-detail.png`), fullPage: true });
  }
  return {
    viewport,
    status: response.status(),
    elapsedMs: Date.now() - started,
    funding,
    pair,
    errors,
  };
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
  });
  const results = [];
  try {
    for (const [name, viewport] of [["desktop", { width: 1440, height: 1000 }], ["mobile", { width: 390, height: 844 }]]) {
      const context = await browser.newContext({ viewport, colorScheme: "dark" });
      results.push(await inspect(await context.newPage(), name));
      await context.close();
    }
  } finally {
    await browser.close();
  }
  const failures = results.filter(result =>
    result.status !== 200 || result.errors.length || result.funding.researchOnly ||
    result.funding.windows.length !== 4 || result.funding.invalid.length || result.funding.overflow > 2 ||
    !result.pair || result.pair.httpStatus !== 200 || !result.pair.routeReturns ||
    result.pair.windows.length !== 4 || result.pair.exactValues.length !== 3 ||
    result.pair.invalid.length || result.pair.overflow > 2
  );
  console.log(JSON.stringify({ results, failures }, null, 2));
  if (failures.length) process.exitCode = 1;
})();
