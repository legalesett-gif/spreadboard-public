"""Small Stripe Billing adapter with strict webhook verification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class BillingError(RuntimeError):
    """A safe, user-facing billing failure."""


@dataclass(frozen=True)
class StripeConfig:
    secret_key: str
    webhook_secret: str
    price_id: str
    scanner_price_id: str
    research_pro_price_id: str
    public_url: str
    plan_label: str

    @property
    def mode(self) -> str:
        if self.secret_key.startswith(("sk_live_", "rk_live_")):
            return "live"
        if self.secret_key.startswith(("sk_test_", "rk_test_")):
            return "test"
        return "unknown"

    @property
    def provider_ready(self) -> bool:
        allow_test = os.environ.get("SPREADBOARD_ALLOW_TEST_BILLING", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        return bool(
            self.secret_key
            and self.public_url
            and (self.mode == "live" or (self.mode == "test" and allow_test))
        )

    @property
    def checkout_ready(self) -> bool:
        return self.provider_ready and bool(self.research_pro_price_id)

    def tier_checkout_ready(self, tier: str) -> bool:
        price_id = self.scanner_price_id if tier == "scanner" else self.research_pro_price_id
        return self.provider_ready and bool(price_id)

    def tier_price_id(self, tier: str) -> str:
        if tier not in {"scanner", "research_pro"}:
            raise BillingError("unknown_subscription_tier")
        price_id = self.scanner_price_id if tier == "scanner" else self.research_pro_price_id
        if not price_id:
            raise BillingError("tier_checkout_not_configured")
        return price_id

    @property
    def webhook_ready(self) -> bool:
        return bool(self.webhook_secret)


def config() -> StripeConfig:
    public_url = os.environ.get("SPREADBOARD_PUBLIC_URL", "").strip().rstrip("/")
    if public_url and urlparse(public_url).scheme not in {"https", "http"}:
        public_url = ""
    legacy_price = os.environ.get("SPREADBOARD_STRIPE_PRICE_ID", "").strip()
    research_price = os.environ.get("SPREADBOARD_STRIPE_RESEARCH_PRO_PRICE_ID", "").strip() or legacy_price
    return StripeConfig(
        secret_key=os.environ.get("SPREADBOARD_STRIPE_SECRET_KEY", "").strip(),
        webhook_secret=os.environ.get("SPREADBOARD_STRIPE_WEBHOOK_SECRET", "").strip(),
        price_id=research_price,
        scanner_price_id=os.environ.get("SPREADBOARD_STRIPE_SCANNER_PRICE_ID", "").strip(),
        research_pro_price_id=research_price,
        public_url=public_url,
        plan_label=os.environ.get("SPREADBOARD_SUBSCRIPTION_LABEL", "Monthly membership").strip()
        or "Monthly membership",
    )


def status() -> dict[str, Any]:
    value = config()
    return {
        "provider": "stripe",
        "mode": value.mode,
        "checkout_ready": value.checkout_ready,
        "webhook_ready": value.webhook_ready,
        "configured": value.checkout_ready and value.webhook_ready,
        "plan_label": value.plan_label,
        "tiers": {
            "scanner": {"checkout_ready": value.tier_checkout_ready("scanner"), "monthly_cents": 4_900},
            "research_pro": {"checkout_ready": value.tier_checkout_ready("research_pro"), "monthly_cents": 18_000},
        },
        "providers": {
            "stripe": {
                "mode": value.mode,
                "checkout_ready": value.checkout_ready,
                "webhook_ready": value.webhook_ready,
                "recurring": True,
            },
            "whitepay": {
                "checkout_ready": False,
                "webhook_ready": False,
                "recurring": False,
                "status": "merchant_onboarding_required",
            },
        },
    }


def create_checkout_session(user: Any, *, tier: str = "research_pro") -> str:
    value = config()
    if not value.tier_checkout_ready(tier):
        raise BillingError("billing_not_configured")
    price_id = value.tier_price_id(tier)
    params: dict[str, str] = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": f"{value.public_url}/account?billing=success",
        "cancel_url": f"{value.public_url}/subscription?billing=cancelled",
        "client_reference_id": str(user.id),
        "subscription_data[metadata][spreadboard_user_id]": str(user.id),
        "subscription_data[metadata][spreadboard_tier]": tier,
        "metadata[spreadboard_user_id]": str(user.id),
        "metadata[spreadboard_tier]": tier,
        "allow_promotion_codes": "true",
    }
    if getattr(user, "billing_customer_id", None):
        params["customer"] = user.billing_customer_id
    else:
        params["customer_email"] = user.email
    result = _stripe_post(
        "/v1/checkout/sessions",
        params,
        idempotency_key=f"spreadboard-checkout-{user.id}-{tier}-{int(time.time() // 86400)}",
    )
    return _trusted_stripe_url(result.get("url"), {"checkout.stripe.com"})


def create_portal_session(user: Any) -> str:
    value = config()
    customer = getattr(user, "billing_customer_id", None)
    if not value.checkout_ready or not customer:
        raise BillingError("billing_portal_unavailable")
    result = _stripe_post(
        "/v1/billing_portal/sessions",
        {"customer": customer, "return_url": f"{value.public_url}/account"},
        idempotency_key=f"spreadboard-portal-{user.id}-{int(time.time() // 30)}",
    )
    return _trusted_stripe_url(result.get("url"), {"billing.stripe.com"})


def verify_webhook(payload: bytes, signature_header: str, *, now: float | None = None) -> dict[str, Any]:
    secret = config().webhook_secret
    if not secret:
        raise BillingError("billing_webhook_not_configured")
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, separator, value = item.strip().partition("=")
        if separator:
            parts.setdefault(key, []).append(value)
    try:
        timestamp = int((parts.get("t") or [""])[0])
    except ValueError as exc:
        raise BillingError("invalid_webhook_signature") from exc
    if abs((now if now is not None else time.time()) - timestamp) > 300:
        raise BillingError("expired_webhook_signature")
    signed = str(timestamp).encode("ascii") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
        raise BillingError("invalid_webhook_signature")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BillingError("invalid_webhook_payload") from exc
    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise BillingError("invalid_webhook_payload")
    return event


def _stripe_post(path: str, params: dict[str, str], *, idempotency_key: str) -> dict[str, Any]:
    value = config()
    request = Request(
        "https://api.stripe.com" + path,
        data=urlencode(params).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {value.secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed Stripe origin
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise BillingError("billing_provider_unavailable") from exc
    if not isinstance(result, dict):
        raise BillingError("invalid_billing_provider_response")
    return result


def _trusted_stripe_url(value: Any, hosts: set[str]) -> str:
    url = str(value or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in hosts:
        raise BillingError("invalid_billing_redirect")
    return url
