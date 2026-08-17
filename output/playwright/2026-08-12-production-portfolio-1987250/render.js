"use strict";

const path = require("node:path");
const { chromium } = require("playwright");

(async () => {
  const output = __dirname;
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  for (const [name, viewport] of [
    ["desktop", { width: 1440, height: 1000 }],
    ["mobile", { width: 390, height: 844 }],
  ]) {
    const page = await browser.newPage({ viewport });
    const errors = [];
    page.on("console", message => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`file://${path.join(output, "account.html")}`, {
      waitUntil: "domcontentloaded",
    });
    await page.screenshot({
      path: path.join(output, `${name}.png`),
      fullPage: true,
    });
    require("node:fs").writeFileSync(
      path.join(output, `${name}-errors.json`),
      JSON.stringify(errors, null, 2),
    );
    await page.close();
  }
  await browser.close();
})().catch(error => {
  console.error(error);
  process.exit(1);
});
