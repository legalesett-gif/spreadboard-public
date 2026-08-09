"""Small SMTP boundary for account-recovery mail.

Only configuration presence is exposed through ``status``.  Credentials stay
in environment variables and are never included in errors or application
responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
import os
import smtplib
import ssl


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    use_ssl: bool
    starttls: bool

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender and (not self.username or self.password))


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
    )


def status() -> dict[str, object]:
    settings = config()
    return {
        "provider": "smtp",
        "configured": settings.configured,
        "recovery_ready": settings.configured,
    }


def send_password_reset(*, recipient: str, display_name: str, reset_url: str) -> None:
    settings = config()
    if not settings.configured:
        raise RuntimeError("email_delivery_not_configured")

    message = EmailMessage()
    message["Subject"] = "Reset your SpreadBoard password"
    message["From"] = settings.sender
    message["To"] = recipient
    name = str(display_name or "there").strip() or "there"
    message.set_content(
        f"Hello {name},\n\n"
        "Use this single-use link within two hours to choose a new SpreadBoard password:\n\n"
        f"{reset_url}\n\n"
        "If you did not request this, ignore this email. Your current password remains unchanged.\n"
    )

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
