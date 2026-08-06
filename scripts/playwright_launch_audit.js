async page => {
  const pages = [
    ["markets-all", "/markets"],
    ["markets-futures", "/markets?kind=FUTURES"],
    ["markets-futures-spot", "/markets?kind=FUTURES-SPOT-PAIR"],
    ["markets-spot", "/markets?kind=SPOT"],
    ["markets-futures-dex", "/markets?kind=DEX-FUTURES"],
    ["markets-spot-dex", "/markets?kind=DEX-SPOT"],
    ["funding-futures", "/funding?farm=futures-futures"],
    ["funding-spot", "/funding?farm=futures-spot"],
    ["funding-dex", "/funding?farm=futures-dex"],
    ["fair", "/fair"],
    ["charts", "/charts"],
    ["intel", "/intel"],
    ["watchlist", "/watchlist"],
    ["portfolio", "/account"],
    ["alerts", "/alerts"],
    ["pricing", "/pricing"],
    ["guide", "/guide"],
    ["free", "/free"],
    ["terms", "/terms"],
    ["privacy", "/privacy"],
    ["refunds", "/refunds"],
    ["triage", "/triage"],
    ["signals", "/signals"],
    ["community", "/community"],
    ["playbook", "/playbook"],
    ["learn", "/learn"],
  ];
  const errors = [];
  page.on("pageerror", error => errors.push(`pageerror:${error.message}`));
  page.on("console", message => {
    if (message.type() === "error") errors.push(`console:${message.text()}`);
  });
  const results = [];
  for (const [name, path] of pages) {
    const before = errors.length;
    const started = Date.now();
    let response;
    let navigationError = null;
    try {
      response = await page.goto(`https://spreadarbitrage.ink${path}`, {
        waitUntil: "domcontentloaded",
        timeout: 90000,
      });
      await page.waitForTimeout(500);
    } catch (error) {
      navigationError = `${error.name}:${error.message}`;
    }
    const body = await page.locator("body").innerText().catch(() => "");
    const h1 = await page.locator("h1").first().innerText().catch(() => "");
    const missing = ["undefined", "NaN", "[object Object]"].filter(value => body.includes(value));
    results.push({
      name,
      path,
      status: response ? response.status() : null,
      ms: Date.now() - started,
      title: await page.title(),
      h1,
      bodyChars: body.length,
      links: await page.locator("a").count(),
      buttons: await page.locator("button").count(),
      inputs: await page.locator("input,select,textarea").count(),
      missing,
      navigationError,
      errors: errors.slice(before),
    });
    if (["markets-all", "funding-futures", "fair", "charts", "portfolio", "alerts"].includes(name)) {
      await page.screenshot({
        path: `output/playwright/production-${name}-2026-08-06.png`,
        fullPage: true,
      });
    }
  }
  return results;
}
