from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1] / "2026-08-13-ce0f9e6-alex-final"
ROOT.mkdir(parents=True, exist_ok=True)
BASE = "https://spreadarbitrage.ink"
TOKEN_POINTER = Path("/tmp/spreadboard-qa-session-path")


async def main() -> None:
    token_path = Path(TOKEN_POINTER.read_text().strip())
    token = token_path.read_text().strip()
    results: list[dict] = []
    failures: list[str] = []
    cases = [
        ("markets", "/markets"),
        ("funding", "/funding"),
        ("rankings", "/rankings"),
        ("charts", "/charts?token=GUA"),
        ("account", "/account"),
    ]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        for viewport, size in (
            ("desktop", {"width": 1440, "height": 1000}),
            ("mobile", {"width": 390, "height": 844}),
        ):
            context = await browser.new_context(viewport=size, device_scale_factor=1)
            await context.add_cookies(
                [{
                    "name": "spreadboard_session",
                    "value": token,
                    "domain": "spreadarbitrage.ink",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                }]
            )
            for name, path in cases:
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                page.on(
                    "console",
                    lambda msg, errors=errors: errors.append(f"console:{msg.text}")
                    if msg.type == "error"
                    else None,
                )
                started = time.perf_counter()
                response = await page.goto(BASE + path, wait_until="domcontentloaded", timeout=60_000)
                dom_ms = round((time.perf_counter() - started) * 1000, 1)
                await page.wait_for_timeout(2500)
                body = await page.locator("body").inner_text()
                h1 = await page.locator("h1").first.inner_text() if await page.locator("h1").count() else ""
                overflow = await page.evaluate(
                    "Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)"
                )
                lowered = body.lower()
                invalid = [needle for needle in ("traceback", "internal server error", "nan%", "infinity%") if needle in lowered]
                state: dict[str, object] = {}
                if name == "rankings":
                    headers = " ".join(await page.locator("th").all_inner_texts()).lower()
                    state = {
                        "rows": await page.locator("tbody tr").count(),
                        "has1d": "settled 24h" in headers,
                        "has7d": "settled 7d" in headers,
                        "has30d": "settled 30d" in headers,
                        "guard_markers_in_top": await page.locator("tbody tr").evaluate_all(
                            "rows => rows.slice(0, 10).filter(row => row.innerText.includes('?')).length"
                        ),
                    }
                elif name == "charts":
                    for _ in range(80):
                        if await page.locator("[data-chart-long] option").count() > 1:
                            break
                        await page.wait_for_timeout(250)
                    options = " ".join(await page.locator("option").all_inner_texts()).lower()
                    long_values = await page.locator("[data-chart-long] option").evaluate_all(
                        "options => options.slice(1).map(option => option.value)"
                    )
                    short_values = await page.locator("[data-chart-short] option").evaluate_all(
                        "options => options.slice(1).map(option => option.value)"
                    )
                    if long_values and short_values:
                        long_value = next((value for value in long_values if "okx dex" in value.lower()), long_values[0])
                        short_value = next((value for value in short_values if value != long_value and "futures" in value.lower()), "")
                        if not short_value:
                            short_value = next((value for value in short_values if value != long_value), "")
                        await page.locator("[data-chart-long]").select_option(long_value)
                        await page.locator("[data-chart-short]").select_option(short_value)
                    state = {
                        "gua": "gua" in lowered,
                        "dex": "dex" in options,
                        "futures": "futures" in options,
                        "ready": not await page.locator("[data-chart-create]").is_disabled(),
                    }
                elif name == "account":
                    state = {
                        "edit": "edit" in lowered,
                        "delete": "delete" in lowered,
                        "movement_pnl": "movement pnl" in lowered,
                        "settled_funding": "settled funding" in lowered,
                        "total_pnl": "total pnl" in lowered,
                    }
                screenshot = ROOT / f"{viewport}-{name}.png"
                await page.screenshot(path=str(screenshot), full_page=True)
                item = {
                    "viewport": viewport,
                    "name": name,
                    "status": response.status if response else None,
                    "final": page.url,
                    "dom_content_loaded_ms": dom_ms,
                    "errors": errors,
                    "state": state,
                    "screenshot": str(screenshot),
                    "title": await page.title(),
                    "h1": h1,
                    "overflow": overflow,
                    "invalid": invalid,
                    "chars": len(body),
                }
                results.append(item)
                if item["status"] != 200 or errors or overflow or invalid or "/login" in page.url:
                    failures.append(f"{viewport}:{name}:transport_or_render")
                if name == "rankings" and (
                    state["rows"] < 50 or not state["has1d"] or not state["has7d"] or not state["has30d"]
                ):
                    failures.append(f"{viewport}:{name}:ranking_columns")
                if name == "charts" and not all(state.values()):
                    failures.append(f"{viewport}:{name}:gua_route_picker")
                if name == "account" and not all(state.values()):
                    failures.append(f"{viewport}:{name}:portfolio_controls")
                await page.close()
            await context.close()
        await browser.close()
    report = {"generated_at": "2026-08-13", "revision": "ce0f9e6", "results": results, "failures": failures}
    (ROOT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
