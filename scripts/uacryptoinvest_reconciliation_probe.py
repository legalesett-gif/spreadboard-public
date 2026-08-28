#!/usr/bin/env python3
"""Sample UACryptoInvest and submit a read-only exact-pair reconciliation.

This is an independent comparator, never a market-data source.  Its displayed
spread is retained only as an audit value; SpreadBoard continues to price every
route from direct venue books and keeps all identity/depth gates intact.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SOURCE_URL = "https://uacryptoinvest.com/arbitrage"
DEFAULT_ENDPOINT = "https://spreadarbitrage.ink/api/internal/reconciliation/uacryptoinvest"
TABS = {
    "Futures": ("Futures", "Futures"),
    "Spot-Futures": ("Spot", "Futures"),
}
SUPPORTED_TYPES = {"Spot", "Futures", "Dex"}
VENUE_ALIASES = {
    "Okx": "OKX",
    "OKx": "OKX",
    "okx": "OKX",
    "OkxDex": "OKX DEX",
    "OKXDEX": "OKX DEX",
}


def _chart_identity(href: str) -> tuple[str, str, str, str, str] | None:
    chart_id = (parse_qs(urlparse(href).query).get("charts") or [""])[0]
    parts = chart_id.split("~") if "~" in chart_id else chart_id.split("-")
    if len(parts) < 5 or parts[-3] not in SUPPORTED_TYPES or parts[-1] not in SUPPORTED_TYPES:
        return None
    return (
        "-".join(parts[:-4]).upper(),
        VENUE_ALIASES.get(parts[-4], parts[-4]),
        parts[-3],
        VENUE_ALIASES.get(parts[-2], parts[-2]),
        parts[-1],
    )


def _spread(text: str) -> float | None:
    percentages = re.findall(r"([+\-\u2212]?\d+(?:[.,]\d+)?)\s*%", text)
    if not percentages:
        return None
    try:
        return float(percentages[-1].replace("\u2212", "-").replace(",", "."))
    except ValueError:
        return None


def _wait_rows(page: Any, expected: tuple[str, str], timeout_seconds: float = 12.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    stable = 0
    previous = -1
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        raw = page.locator('a[href*="/charts?charts="]').evaluate_all(
            """links => links.map(link => ({
              href: link.getAttribute('href') || '',
              text: (link.closest('div[class*="arb-grid-"]') || link).innerText || ''
            }))"""
        )
        parsed: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for item in raw:
            identity = _chart_identity(str(item.get("href") or ""))
            if identity is None or (identity[2], identity[4]) != expected:
                continue
            if identity in seen:
                continue
            seen.add(identity)
            parsed.append(
                {
                    "token": identity[0],
                    "long_venue": identity[1],
                    "long_market_type": identity[2],
                    "short_venue": identity[3],
                    "short_market_type": identity[4],
                    "reference_spread_pct": _spread(str(item.get("text") or "")),
                }
            )
        rows = parsed
        if rows and len(rows) == previous:
            stable += 1
            if stable >= 2:
                return rows
        else:
            stable = 0
        previous = len(rows)
        time.sleep(0.4)
    return rows


def scrape() -> dict[str, Any]:
    observed = datetime.now(tz=UTC)
    sampled: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1320, "height": 2200})
        page = context.new_page()
        try:
            page.goto(SOURCE_URL, wait_until="commit", timeout=30_000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            for tab, expected in TABS.items():
                if tab != "Futures":
                    button = page.locator("button:visible").filter(
                        has_text=re.compile(rf"^\s*{re.escape(tab)}\s*$")
                    )
                    if button.count() == 0:
                        statuses.append({"tab": tab, "status": "unavailable", "rows": 0})
                        continue
                    button.first.click(timeout=5_000, force=True)
                rows = _wait_rows(page, expected)
                statuses.append({"tab": tab, "status": "ok" if rows else "empty", "rows": len(rows)})
                top_count = min(15, len(rows))
                tail = rows[top_count:]
                rng = random.Random(f"{observed.date().isoformat()}:{tab}")
                tail_sample = rng.sample(tail, k=min(10, len(tail)))
                selected = [*rows[:top_count], *tail_sample]
                for rank, row in enumerate(rows, start=1):
                    if row not in selected:
                        continue
                    sampled.append(
                        {
                            **row,
                            "source_rank": rank,
                            "sample_bucket": "top" if rank <= top_count else "tail",
                        }
                    )
        finally:
            context.close()
            browser.close()
    if any(status["status"] != "ok" for status in statuses) or not sampled:
        raise RuntimeError(f"reference_scrape_incomplete:{statuses}")
    return {
        "source": "uacryptoinvest.com",
        "source_url": SOURCE_URL,
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "tabs": statuses,
        "rows": sampled,
    }


def submit(payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = os.environ.get("SPREADBOARD_RECONCILIATION_URL", DEFAULT_ENDPOINT).strip()
    token = os.environ.get("SPREADBOARD_RECONCILIATION_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SPREADBOARD_RECONCILIATION_TOKEN is required")
    request = Request(
        endpoint,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "SpreadBoard-Reconciliation/1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"reconciliation_http_{exc.code}:{detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"reconciliation_unavailable:{type(exc).__name__}") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("invalid_reconciliation_response")
    return result


def main() -> int:
    try:
        payload = scrape()
        result = submit(payload)
    except Exception as exc:  # noqa: BLE001 - concise scheduled-job failure.
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:500]}"}))
        return 2
    summary = {
        "ok": bool(result.get("release_gate_passed")),
        "sample_count": result.get("sample_count"),
        "exact_pair_recall_pct": result.get("exact_pair_recall_pct"),
        "recall_drop_pp": result.get("recall_drop_pp"),
        "absence_count": result.get("absence_count"),
        "spread_investigation_count": result.get("spread_investigation_count"),
        "failures": result.get("failures") or [],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
