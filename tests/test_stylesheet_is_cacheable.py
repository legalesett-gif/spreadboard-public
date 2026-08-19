"""The stylesheet is served once and cached, not re-sent with every page.

215KB of identical CSS was inlined into every HTML response. On the markets
page that is 215KB of a 2.68MB document; on a small page it is most of the
document. It is re-parsed on every navigation and can never be cached, which is
the largest avoidable cost on a phone and the most direct cause of "the website
is laggy".

The CSS is static -- 220,055 characters with zero interpolations -- so it can be
a real file with a far-future cache lifetime and a version query that changes
when the CSS changes.
"""

from __future__ import annotations

import re

from spreadboard import server


def test_pages_link_the_stylesheet_instead_of_inlining_it() -> None:
    html = server.shell("Title", "markets", "<div></div>")

    assert 'rel="stylesheet"' in html
    assert "/assets/app.css" in html
    inline = sum(len(m) for m in re.findall(r"<style>(.*?)</style>", html, re.DOTALL))
    assert inline < 20000, f"{inline} bytes of CSS are still inline"


def test_the_stylesheet_carries_a_version_so_a_deploy_busts_the_cache() -> None:
    html = server.shell("Title", "markets", "<div></div>")

    assert re.search(r"/assets/app\.css\?v=[0-9a-f]{6,}", html), "no cache-busting version"


def test_the_stylesheet_body_is_the_css_the_pages_used_to_inline() -> None:
    assert len(server.APP_CSS) > 200000
    assert ":root" in server.APP_CSS
    assert "--terminal-bg" in server.APP_CSS
    # Un-doubled: it is no longer inside an f-string.
    assert "{{" not in server.APP_CSS


def test_dark_mode_still_lives_in_that_stylesheet() -> None:
    """Every page's theming comes from this one file now."""
    assert "color-scheme" in server.APP_CSS
    assert 'data-theme="dark"' in server.APP_CSS or "[data-theme" in server.APP_CSS


def test_the_theme_toggle_still_ships_inline() -> None:
    """It must run before paint, so it cannot wait on an external file."""
    html = server.shell("Title", "markets", "<div></div>")

    assert "data-theme" in html
