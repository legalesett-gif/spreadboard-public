"""Small SMTP boundary for account-recovery mail.

Only configuration presence is exposed through ``status``.  Credentials stay
in environment variables and are never included in errors or application
responses.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    use_ssl: bool
    starttls: bool
    resend_api_key: str
    resend_api_url: str

    @property
    def configured(self) -> bool:
        smtp_ready = bool(self.host and (not self.username or self.password))
        return bool(self.sender and (self.resend_api_key or smtp_ready))

    @property
    def provider(self) -> str:
        return "resend_api" if self.resend_api_key else "smtp"


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def config() -> MailConfig:
    use_ssl = _flag("SPREADBOARD_SMTP_SSL")
    try:
        port = int(os.environ.get("SPREADBOARD_SMTP_PORT", "465" if use_ssl else "587"))
    except ValueError:
        port = 465 if use_ssl else 587
    return MailConfig(
        host=os.environ.get("SPREADBOARD_SMTP_HOST", "").strip(),
        port=max(1, min(65535, port)),
        username=os.environ.get("SPREADBOARD_SMTP_USERNAME", "").strip(),
        password=os.environ.get("SPREADBOARD_SMTP_PASSWORD", ""),
        sender=os.environ.get("SPREADBOARD_SMTP_FROM", "").strip(),
        use_ssl=use_ssl,
        starttls=_flag("SPREADBOARD_SMTP_STARTTLS", default=not use_ssl),
        resend_api_key=os.environ.get("SPREADBOARD_RESEND_API_KEY", ""),
        resend_api_url=os.environ.get(
            "SPREADBOARD_RESEND_API_URL", "https://api.resend.com/emails"
        ).strip(),
    )


def status() -> dict[str, object]:
    settings = config()
    return {
        "provider": settings.provider,
        "configured": settings.configured,
        "recovery_ready": settings.configured,
    }


def _send_text(*, settings: MailConfig, recipient: str, subject: str, body: str) -> str:
    """Deliver through HTTPS when configured, with SMTP as the portable fallback.

    Returns the provider's message id where there is one. Discarding it left no
    way to answer "was this actually sent?" after the fact -- the only evidence
    was that nothing raised, which is exactly what a silently-filtered email
    looks like too.
    """
    if settings.resend_api_key:
        payload = json.dumps(
            {
                "from": settings.sender,
                "to": [recipient],
                "subject": subject,
                "text": body,
            }
        ).encode("utf-8")
        request = Request(
            settings.resend_api_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "SpreadBoard/1.0",
            },
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            if not 200 <= int(response.status) < 300:
                raise RuntimeError("email_delivery_failed")
            # The id is evidence, not a precondition: a response we cannot
            # parse must never turn a delivered email into a failed one.
            reader = getattr(response, "read", None)
            if reader is None:
                return ""
            try:
                return str((json.loads(reader().decode("utf-8")) or {}).get("id") or "")
            except (AttributeError, ValueError, UnicodeDecodeError):
                return ""

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.sender
    message["To"] = recipient
    message.set_content(body)

    context = ssl.create_default_context()
    client_context = (
        smtplib.SMTP_SSL(settings.host, settings.port, timeout=10, context=context)
        if settings.use_ssl
        else smtplib.SMTP(settings.host, settings.port, timeout=10)
    )
    with client_context as client:
        if settings.starttls and not settings.use_ssl:
            client.starttls(context=context)
        if settings.username:
            client.login(settings.username, settings.password)
        client.send_message(message)
    return ""


def send_password_reset(*, recipient: str, display_name: str, reset_url: str) -> None:
    settings = config()
    if not settings.configured:
        raise RuntimeError("email_delivery_not_configured")

    name = str(display_name or "there").strip() or "there"
    body = (
        f"Hello {name},\n\n"
        "Use this single-use link within two hours to choose a new SpreadBoard password:\n\n"
        f"{reset_url}\n\n"
        "If you did not request this, ignore this email. Your current password remains unchanged.\n"
    )
    _send_text(
        settings=settings,
        recipient=recipient,
        subject="Reset your SpreadBoard password",
        body=body,
    )


def send_subscription_notice(
    *, recipient: str, display_name: str, subject: str, body: str, action_url: str
) -> str:
    """Send one non-marketing membership lifecycle notice."""
    settings = config()
    if not settings.configured:
        raise RuntimeError("email_delivery_not_configured")

    name = str(display_name or "there").strip() or "there"
    text = (
        f"Hello {name},\n\n{str(body).strip()}\n\n"
        f"Manage your membership:\n{action_url}\n\n"
        "This is a service notice about your prepaid SpreadBoard access.\n"
    )
    return _send_text(
        settings=settings,
        recipient=recipient,
        subject=str(subject or "SpreadBoard membership update")[:180],
        body=text,
    )
