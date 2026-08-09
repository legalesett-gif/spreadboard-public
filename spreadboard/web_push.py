"""Account-owned Web Push delivery for in-app alert notifications."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any

from spreadboard import accounts


def status() -> dict[str, Any]:
    public_key = os.environ.get("SPREADBOARD_VAPID_PUBLIC_KEY", "").strip()
    private_key = os.environ.get("SPREADBOARD_VAPID_PRIVATE_KEY", "").strip()
    subject = os.environ.get("SPREADBOARD_VAPID_SUBJECT", "").strip()
    return {
        "configured": bool(public_key and private_key and subject),
        "public_key": public_key if public_key and private_key and subject else None,
        "subject_configured": bool(subject),
    }


def send(subscription: dict[str, Any], notification: dict[str, Any]) -> dict[str, Any]:
    config = status()
    if not config["configured"]:
        return {"ok": False, "permanent": False, "error": "web_push_not_configured"}
    payload = json.dumps(
        {
            "title": str(notification.get("title") or "SpreadBoard alert")[:160],
            "body": str(notification.get("body") or "")[:1000],
            "url": str(notification.get("url") or "/account")[:500],
            "tag": f"spreadboard-{notification.get('notification_id') or 'alert'}",
        },
        separators=(",", ":"),
    )
    try:
        from pywebpush import WebPushException, webpush
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "permanent": False, "error": type(exc).__name__}
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=payload,
            vapid_private_key=os.environ["SPREADBOARD_VAPID_PRIVATE_KEY"].strip(),
            vapid_claims={"sub": os.environ["SPREADBOARD_VAPID_SUBJECT"].strip()},
            ttl=300,
            timeout=15,
        )
        return {"ok": True, "permanent": False, "error": None}
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        code = int(getattr(response, "status_code", 0) or 0)
        return {
            "ok": False,
            "permanent": code in {404, 410},
            "error": f"web_push_http_{code or 'error'}",
        }
    except Exception as exc:  # noqa: BLE001 - delivery failure cannot stop alert evaluation.
        return {"ok": False, "permanent": False, "error": type(exc).__name__}


class Worker:
    """Flush notification/subscription pairs with bounded retries."""

    def __init__(
        self,
        *,
        accounts_path: Path | str = accounts.DEFAULT_DB_PATH,
        poll_seconds: float = 5.0,
    ) -> None:
        self.accounts_path = Path(accounts_path)
        self.poll_seconds = max(3.0, float(poll_seconds))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="spreadboard-web-push", daemon=True)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        if status()["configured"] and not self.running:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.running:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        self._stop.wait(3.0)
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001
                print(f"spreadboard-web-push: {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(self.poll_seconds)

    def check_once(self) -> dict[str, int]:
        if not status()["configured"]:
            return {"pending": 0, "delivered": 0, "failed": 0}
        pending = accounts.pending_web_push_deliveries(
            db_path=self.accounts_path, limit=100
        )
        delivered = failed = 0
        for item in pending:
            result = send(
                item,
                {
                    **item,
                    "url": "/account",
                },
            )
            state = (
                "success"
                if result["ok"]
                else "permanent_failure"
                if result.get("permanent")
                else "pending"
            )
            accounts.record_web_push_delivery(
                int(item["notification_id"]),
                int(item["subscription_id"]),
                status=state,
                error=str(result.get("error") or ""),
                db_path=self.accounts_path,
            )
            delivered += int(bool(result["ok"]))
            failed += int(not result["ok"])
        return {"pending": len(pending), "delivered": delivered, "failed": failed}
