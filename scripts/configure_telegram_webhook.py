#!/usr/bin/env python3
"""Register the production Telegram webhook using secrets from the environment."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen


def telegram_call(token: str, method: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise SystemExit(f"Telegram rejected {method}")
    return result


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
    telegram_call(token, "setWebhook", json.loads(payload))
    telegram_call(
        token,
        "setMyCommands",
        {
            "scope": {"type": "all_private_chats"},
            "commands": [
                {"command": "subscribe", "description": "Open secure membership checkout"},
                {"command": "mysubscription", "description": "Check membership status"},
                {"command": "access", "description": "Request subscriber group access"},
            ],
        },
    )
    telegram_call(
        token,
        "setMyCommands",
        {
            "scope": {"type": "all_group_chats"},
            "commands": [
                {"command": "setupgroup", "description": "Connect this subscriber group"}
            ],
        },
    )
    telegram_call(
        token,
        "setMyDescription",
        {
            "description": (
                "Link your SpreadBoard account, manage membership, and request "
                "access to the subscriber community. Payments stay on secure checkout pages."
            )
        },
    )
    telegram_call(
        token,
        "setMyShortDescription",
        {"short_description": "SpreadBoard membership and subscriber access."},
    )
    print("Telegram webhook, command menus, and profile text configured successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
