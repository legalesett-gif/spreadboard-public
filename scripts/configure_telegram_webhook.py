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
    telegram_call(
        token,
        "setMyCommands",
        {
            "scope": {"type": "all_group_chats"},
            # Deliberately empty. Telegram writes
            # "/funding@spreadarbitragesubscription_bot" into the message when a
            # member picks a command from the "/" popup in a supergroup, and no
            # API setting changes how the client writes it. What IS controllable
            # is whether the popup has anything to offer: register nothing here
            # and it cannot offer, and therefore cannot tag, anything.
            #
            # Nothing but the popup is lost. Registration drives that menu only
            # -- privacy mode is off, so every group message reaches the bot
            # regardless, and a hand-typed "/funding" is answered exactly as
            # before. Discovery moves to /help, the profile text, and the
            # buttons under each answer.
            "commands": [
            ],
        },
    )
    # Telegram resolves a group's menu down the scope chain, so an empty
    # all_group_chats falls back to the default scope and the popup reappears
    # -- carrying the same "@botname" insertion. Clearing default closes that
    # door. Private chats are unaffected: all_private_chats is set explicitly
    # above and takes precedence over default for a DM.
    telegram_call(
        token,
        "setMyCommands",
        {"scope": {"type": "default"}, "commands": []},
    )
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
