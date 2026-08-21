"""Legal routes stay directly usable without reappearing in site navigation."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from spreadboard import server


def test_every_legal_document_has_one_main_landmark() -> None:
    """The shared shell already owns the document's only main landmark."""

    for page in ("terms", "privacy", "refunds", "affiliate"):
        html = server.render_legal_page(page)

        assert len(re.findall(r"<main(?:\s|>)", html)) == 1, page
        assert '<div class="legal-sections">' in html
        assert re.search(r'<main>\s*<section class="legal-page">', html)


def test_legal_copy_and_actions_use_contrast_safe_light_tokens() -> None:
    """Every small legal label must clear AA, not only prose and actions.

    The light Privacy kicker measured 3.655:1 even after the earlier Terms
    audit repaired the muted prose and contact-link colours.
    """

    css = server.APP_CSS

    assert "--legal-muted:#596a64;" in css
    assert "--legal-link:#08796d;" in css
    assert ":root[data-theme=\"dark\"] .legal-page" in css
    assert ".legal-page>header p,.legal-sections p { color:var(--legal-muted);" in css
    assert ".legal-page nav a,.legal-contact a {" in css
    assert "color:var(--legal-link);" in css
    assert ".legal-page .page-kicker { color:var(--legal-link); }" in css


def test_privacy_notice_covers_the_data_the_product_actually_collects() -> None:
    """A privacy notice must describe optional browser push and why data is used.

    The product stores a Web Push endpoint, browser key material and a user
    agent, but the previous notice mentioned only Telegram and Pushover.  It
    also listed fields without explaining lawful purposes, optionality, rights,
    objection, complaints or automated decision-making.
    """

    html = server.render_legal_page("privacy")

    for heading in (
        "Who is responsible",
        "How and why we use data",
        "Required and optional data",
        "Sharing and international processing",
        "Retention",
        "Automated decisions",
        "Your privacy rights",
        "Right to object and withdraw",
        "Complaints and contact",
    ):
        assert f"<h2>{heading}</h2>" in html
    assert "Web Push subscription endpoint" in html
    assert "performance of our contract" in html
    assert "legitimate interests" in html
    assert "data portability" in html
    assert "right to object" in html
    assert "supervisory authority" in html
    assert "solely automated processing or profiling" in html
    assert f"Version {server.TERMS_VERSION}" in html


def test_privacy_controller_details_are_configurable_and_escaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The controller identity needs owner-supplied legal details, never a guess."""

    monkeypatch.setenv("SPREADBOARD_DATA_CONTROLLER_NAME", "Example & Company")
    monkeypatch.setenv("SPREADBOARD_DATA_CONTROLLER_ADDRESS", "1 <Main> Street")

    html = server.render_legal_page("privacy")

    assert "Example &amp; Company" in html
    assert "1 &lt;Main&gt; Street" in html
    assert "Example & Company" not in html
    assert "1 <Main> Street" not in html


def test_legal_contact_methods_are_safe_working_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPREADBOARD_SUPPORT_EMAIL", "help@example.test")
    monkeypatch.setenv("SPREADBOARD_SUPPORT_URL", "https://t.me/example_support")

    html = server.render_legal_page("terms")

    assert 'href="mailto:help@example.test"' in html
    assert 'href="https://t.me/example_support"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_legal_contact_configuration_cannot_create_an_unsafe_href(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPREADBOARD_SUPPORT_EMAIL", "not an email")
    monkeypatch.setenv("SPREADBOARD_SUPPORT_URL", "javascript:alert(1)")

    html = server.render_legal_page("terms")

    assert 'href="mailto:not an email"' not in html
    assert 'href="javascript:' not in html
    assert "not an email" in html
    assert "javascript:alert(1)" in html


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "https://user:secret@example.test/help",
        "https://example.test\\@elsewhere.invalid/help",
        "https://example.test/help\nelsewhere",
    ),
)
def test_legal_support_url_rejects_ambiguous_https_configuration(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_url: str,
) -> None:
    monkeypatch.setenv("SPREADBOARD_SUPPORT_URL", unsafe_url)

    html = server.render_legal_page("terms")

    assert f'href="{unsafe_url}"' not in html
    assert unsafe_url in html


def test_authenticated_legal_shell_finishes_and_verifies_logout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirecting at fetch headers aborted the JSON body and hid logout errors."""

    user = SimpleNamespace(
        display_name="Audit",
        is_admin=True,
        csrf_token="csrf",
        entitlement_tier="research_pro",
    )
    monkeypatch.setattr(server.accounts, "current_user", lambda: user)

    html = server.render_legal_page("terms")

    assert 'class="logout-status" role="status" aria-live="polite"' in html
    assert "const response=await fetch('/api/logout'" in html
    assert "const payload=await response.json().catch(()=>({}));" in html
    assert "if(!response.ok||payload.ok!==true)" in html
    assert "Sign out failed. Try again." in html


def test_authenticated_mobile_keeps_a_bounded_logout_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phones had no sign-out control, and a stalled request disabled it forever."""

    user = SimpleNamespace(
        display_name="Audit",
        is_admin=True,
        csrf_token="csrf",
        entitlement_tier="research_pro",
    )
    monkeypatch.setattr(server.accounts, "current_user", lambda: user)

    html = server.render_legal_page("terms")

    assert ".account-chip { display:none; }" in server.APP_CSS
    assert ".logout-status:not(:empty)" in server.APP_CSS
    assert ".account-chip,.logout-status,.logout-button { display:none; }" not in server.APP_CSS
    assert "const controller=new AbortController()" in html
    assert "signal:controller.signal" in html
    assert "setTimeout(()=>controller.abort(),15000)" in html
