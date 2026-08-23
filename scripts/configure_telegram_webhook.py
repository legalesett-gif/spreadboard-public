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
            "allowed_updates": ["message", "chat_join_request", "my_chat_member", "callback_query", "inline_query"],
            # Deployments must not discard messages that arrived during a
            # container restart. Telegram will redeliver them to this URL.
            "drop_pending_updates": False,
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
                {"command": "start", "description": "What this bot can do"},
                {"command": "subscribe", "description": "Open secure membership checkout"},
                {"command": "mysubscription", "description": "Check membership status"},
                {"command": "access", "description": "Request subscriber group access"},
                {"command": "top", "description": "Public live research preview"},
                {"command": "token", "description": "Spread view for a token"},
                {"command": "spread", "description": "Spread across all parsed venues"},
                {"command": "funding", "description": "Funding rate and APR per route"},
                {"command": "radar", "description": "Retained 24h / 7d / 30d funding leaders"},
                {"command": "transfer", "description": "Deposit / withdrawal rails per venue"},
                {"command": "deep", "description": "Only routes that proved the probe size"},
                {"command": "carry", "description": "Best paired carry per day"},
                {"command": "depth", "description": "Can you get size in at the probe"},
                {"command": "calc", "description": "Split capital across both legs"},
                {"command": "help", "description": "Every shortcut, e.g. GUA/f"},
                {"command": "status", "description": "How fresh the data is"},
            ],
        },
    )
    # Telegram writes "/funding@spreadarbitragesubscription_bot" when a member
    # picks a command in a supergroup. The parser accepts that exact form, so
    # the group can keep a discoverable menu without losing tagged requests.
    # Account, payment and group-setup actions remain private-chat only.
    telegram_call(
        token,
        "setMyCommands",
        {
            "scope": {"type": "all_group_chats"},
            "commands": [
                {"command": "top", "description": "Widest spreads now"},
                {"command": "spread", "description": "Spread board or add a token"},
                {"command": "funding", "description": "Best carry or add a token"},
                {"command": "radar", "description": "24h / 7d / 30d funding leaders"},
                {"command": "deep", "description": "Routes that proved the probe size"},
                {"command": "carry", "description": "Best positive paired carry"},
                {"command": "token", "description": "Spread view: /token GUA"},
                {"command": "depth", "description": "Depth view: /depth GUA"},
                {"command": "transfer", "description": "Rails view: /transfer GUA"},
                {"command": "calc", "description": "Size capital: /calc GUA 5000"},
                {"command": "help", "description": "Every command and shortcut"},
                {"command": "status", "description": "Spread and funding freshness"},
            ],
        },
    )
    # Keep the default scope empty. Telegram should resolve explicitly to the
    # private or group menu above, never leak one audience's actions to another.
    telegram_call(token, "deleteMyCommands", {"scope": {"type": "default"}})
    telegram_call(
        token,
        "setMyDescription",
        {
            "description": (
                "Live spreads, funding and depth from SpreadBoard, answered in "
                "chat.\n\n"
                "Ask by token: GUA/ for the spread, GUA/f funding, GUA/d depth, "
                "GUA/t transfer rails, GUA/c 5000 to size a position.\n"
                "Ask the board: /top widest now, /deep only what proved the "
                "probe size, /carry best paired carry. /help shows everything.\n\n"
                "Also here to link your account, manage membership and request "
                "subscriber access. Payments stay on secure checkout pages."
            )
        },
    )
    telegram_call(
        token,
        "setMyShortDescription",
        {
            "short_description": (
                "Live spreads, funding and depth in chat. Type GUA/ or /top. "
                "Membership and subscriber access too."
            )
        },
    )
    print("Telegram webhook, command menus, and profile text configured successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
