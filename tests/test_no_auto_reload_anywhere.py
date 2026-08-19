"""No page reloads itself. Every one of them refreshes in place instead.

`shell()` puts the auto-refresh script on every page, and any page carrying
data-refresh counted down and called location.reload(): rankings and signals
every 120s, intel and triage every 180s, markets every 300s. A member reading a
table, mid-scroll or mid-thought, had the document replaced under them.

The operator's rule is explicit: "Everything should be working exactly as
intended, without any reloads on any of the pages."

Refreshing is still necessary -- these pages are not all stream-fed -- so the
tick now fetches the same URL and swaps the main content out of the parsed
response. Same freshness, no reload, and the scroll position survives because
nothing is torn down.
"""

from __future__ import annotations

import inspect

from spreadboard import server


def _script() -> str:
    return server.render_auto_refresh_script()


def test_the_tick_never_reloads_the_document() -> None:
    assert "location.reload()" not in _script()


def test_the_tick_refreshes_in_place_instead() -> None:
    source = _script()

    assert "DOMParser" in source
    assert "fetch(" in source


def test_no_page_shell_forces_a_reload() -> None:
    """data-refresh-force existed only to defeat the pause toggle."""
    from pathlib import Path

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert 'data-refresh-force="1"' not in source


def test_typing_still_defers_the_refresh() -> None:
    """Swapping content under an open form would lose what was typed."""
    assert "editableActive()" in _script()


def test_the_shell_still_installs_it() -> None:
    """Removing the reload must not remove the freshness."""
    assert "render_auto_refresh_script" in inspect.getsource(server.shell)
