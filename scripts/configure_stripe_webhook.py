#!/usr/bin/env python3
"""Move the existing Stripe webhook to the configured public URL."""

from __future__ import annotations

import json
import os
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


STRIPE_API = "https://api.stripe.com/v1"
WEBHOOK_PATH = "/api/billing/webhook"


def stripe_request(path: str, *, data: dict[str, str] | None = None) -> dict:
    secret_key = os.environ.get("SPREADBOARD_STRIPE_SECRET_KEY", "").strip()
    if not secret_key:
        raise SystemExit("Stripe secret key is required")
    request = Request(
        STRIPE_API + path,
        data=urlencode(data).encode("utf-8") if data is not None else None,
        headers={"Authorization": f"Bearer {secret_key}"},
        method="POST" if data is not None else "GET",
    )
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise SystemExit("Stripe returned an invalid response")
    return result


def main() -> int:
    public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip().rstrip("/")
    parsed = urlparse(public_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("An HTTPS public URL is required")
    target_url = public_url + WEBHOOK_PATH
    endpoints = stripe_request("/webhook_endpoints?limit=100").get("data") or []
    matches = [
        item for item in endpoints
        if isinstance(item, dict) and urlparse(str(item.get("url") or "")).path == WEBHOOK_PATH
    ]
    if len(matches) != 1:
        raise SystemExit(f"Expected one SpreadBoard webhook endpoint, found {len(matches)}")
    endpoint_id = str(matches[0].get("id") or "")
    if not endpoint_id.startswith("we_"):
        raise SystemExit("Stripe returned an invalid webhook endpoint ID")
    stripe_request(f"/webhook_endpoints/{endpoint_id}", data={"url": target_url})
    print(f"Stripe webhook configured at {target_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
