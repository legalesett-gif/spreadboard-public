#!/usr/bin/env python3
"""Register the production Telegram webhook using secrets from the environment."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen


def main() -> int:
    token = os.environ.get("SPREADBOARD_TELEGRAM_BOT_TOKEN", "").strip()
    secret = os.environ.get("SPREADBOARD_TELEGRAM_WEBHOOK_SECRET", "").strip()
    public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip().rstrip("/")
    if not token or not secret or not public_url.startswith("https://"):
        raise SystemExit("bot token, webhook secret, and HTTPS public URL are required")
    payload = json.dumps(
        {
            "url": f"{public_url}/api/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["message", "chat_join_request", "my_chat_member"],
            "drop_pending_updates": True,
        },
        separators=(",", ":"),
    ).encode()
    request = Request(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise SystemExit("Telegram rejected the webhook configuration")
    print("Telegram webhook configured successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
