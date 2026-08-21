"""Subscription UI must expose only actions the billing boundary will accept.

The production defect behind these tests let an active Research Pro member
select Scanner in the query string.  The page then called Scanner the selected
plan and rendered a live payment button even though ``create_invoice`` correctly
rejects that mid-term relabel.  A billing page must not advertise a mutation its
own server refuses.
"""

from __future__ import annotations

import pytest

from spreadboard import accounts, server


@pytest.fixture(autouse=True)
def _configured_crypto_and_signed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SPREADBOARD_CRYPTO_RECEIVING_ADDRESS",
        "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937",
    )
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    accounts.set_current_user(None)
    yield
    accounts.set_current_user(None)


def _member(
    *,
    status: str = "active",
    tier: str = "research_pro",
    billing_customer_id: str | None = None,
    cancel_at_period_end: bool = False,
    role: str = "member",
) -> accounts.User:
    return accounts.User(
        id=70,
        email="member@example.test",
        display_name="Member",
        role=role,
        subscription_status=status,
        subscription_expires_at="2099-08-28T15:01:58Z" if status == "active" else None,
        monthly_capital_usd=None,
        subscription_tier=tier,
        billing_customer_id=billing_customer_id,
        billing_subscription_id="sub_example" if billing_customer_id else None,
        subscription_cancel_at_period_end=cancel_at_period_end,
        csrf_token="csrf",
    )


def test_active_prepaid_member_query_cannot_relabel_the_current_plan() -> None:
    accounts.set_current_user(_member())

    html = server.render_subscription_page({"tier": ["scanner"]})

    assert '<span class="sub-badge">Current plan</span>' in html
    assert '<strong>Research Pro</strong>' in html
    assert 'class="sub-tier-choice selected" href="/subscription?tier=research_pro"' not in html
    assert 'href="/subscription?tier=scanner"' not in html
    assert "Tier changes become available after 28 August 2099" in html


def test_crypto_checkout_renders_only_the_selected_or_current_tier() -> None:
    inactive = _member(status="inactive", tier="free")
    accounts.set_current_user(inactive)

    scanner = server.render_subscription_page({"tier": ["scanner"]})
    assert scanner.count('data-crypto-tier="scanner"') == 3
    assert 'data-crypto-tier="research_pro"' not in scanner
    assert "$49.00" in scanner and "$490.00" in scanner
    assert "$1,365.00" not in scanner

    accounts.set_current_user(_member())
    active = server.render_subscription_page({"tier": ["scanner"]})
    assert active.count('data-crypto-tier="research_pro"') == 3
    assert 'data-crypto-tier="scanner"' not in active


def test_active_prepaid_copy_names_self_service_same_tier_renewal() -> None:
    accounts.set_current_user(_member())

    html = server.render_subscription_page()

    assert "Same-tier renewals extend your access from 28 August 2099" in html
    assert "Contact support to change it" not in html
    assert "30-day reference price" in html


def test_billing_managed_cancellation_is_not_called_automatic_renewal() -> None:
    accounts.set_current_user(
        _member(billing_customer_id="cus_example", cancel_at_period_end=True)
    )

    html = server.render_subscription_page()

    assert "Cancellation is scheduled; access continues until 28 August 2099." in html
    assert "Renews automatically" not in html
    assert "Next payment" not in html
    assert "Recurring billing" in html
    assert "longer terms" not in html
    assert 'data-billing-action="portal"' in html
    assert "data-crypto-checkout" not in html
    assert "data-subscription-consent" not in html


def test_administrator_has_no_customer_purchase_controls() -> None:
    accounts.set_current_user(_member(role="admin"))

    html = server.render_subscription_page({"tier": ["scanner"]})

    assert "Administrator access has no billing term." in html
    assert "data-crypto-checkout" not in html
    assert "data-subscription-consent" not in html
    assert 'href="/subscription?tier=scanner"' not in html
    assert "longer terms" not in html


def test_signed_out_render_has_no_dead_invoice_controls() -> None:
    html = server.render_subscription_page()

    assert "Sign in to subscribe" in html
    assert "data-crypto-checkout" not in html
    assert "data-subscription-consent" not in html


def test_crypto_copy_only_reports_success_after_clipboard_resolves() -> None:
    script = server.render_crypto_checkout_script()

    assert "await navigator.clipboard.writeText(v)" in script
    assert "Copy failed" in script
    assert "navigator.clipboard&&navigator.clipboard.writeText(v);" not in script


def test_invoice_polling_does_not_swallow_repeated_status_failures() -> None:
    script = server.render_crypto_checkout_script()

    assert "pollFailures" in script
    assert "Confirmation status is temporarily unavailable in this browser." in script
    assert "clearInterval(poll); poll=null;" in script
    assert ".catch(function(){});" not in script


def test_subscription_uses_contrast_safe_light_muted_copy() -> None:
    accounts.set_current_user(_member())

    html = server.render_subscription_page()

    assert "--sub-muted:#596a64" in html
    assert ".sub-page .terminal-heading p" in html


def test_consent_error_matches_the_visible_acknowledgement() -> None:
    accounts.set_current_user(_member(status="inactive", tier="free"))

    html = server.render_subscription_page()

    assert "Please acknowledge immediate digital access before continuing." in html
    assert "Please accept the Terms and Refund Policy first." not in html
