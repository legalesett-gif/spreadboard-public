"""Legal routes stay directly usable without reappearing in site navigation."""

from __future__ import annotations

import re

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
    """The previous muted prose was 4.424:1 and the accent action 1.705:1."""

    css = server.APP_CSS

    assert "--legal-muted:#596a64;" in css
    assert "--legal-link:#08796d;" in css
    assert ":root[data-theme=\"dark\"] .legal-page" in css
    assert ".legal-page>header p,.legal-sections p { color:var(--legal-muted);" in css
    assert ".legal-page nav a,.legal-contact a {" in css
    assert "color:var(--legal-link);" in css


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
