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


def test_in_place_refresh_preserves_live_interactive_islands() -> None:
    """Replacing a parsed subtree destroys listeners on every new control.

    Pages may opt a stable interactive island out of the structural swap.  The
    refresh must transplant the already-live node into the fetched shell before
    replacing the surrounding data.  This preserves listeners and unsaved form
    state without executing fetched scripts a second time.
    """
    source = _script()

    assert "[data-refresh-preserve]" in source
    assert "currentIsland.replaceWith" not in source
    assert "nextIsland.replaceWith(currentIsland)" in source


def test_markets_preserve_filters_and_the_live_calculator() -> None:
    page_source = inspect.getsource(server.render_markets_page)
    filter_source = inspect.getsource(server.render_market_filter_bar)
    preset_source = inspect.getsource(server.render_filter_preset_panel)

    assert '<form class="market-filter-form" data-refresh-preserve="markets-filters"' in filter_source
    assert 'data-refresh-preserve="markets-presets"' in preset_source
    # Counts, tabs and active-filter chips are server evidence and must refresh;
    # preserving the whole panel froze a cold query's zeros beside live rows.
    assert (
        '<section class="market-filter-panel terminal-filter-panel" '
        'data-refresh-preserve' not in filter_source
    )
    assert 'data-refresh-preserve="markets-net-edge"' in server.render_net_edge_dialog()
    # The fetched route rows are new, so the share handler must delegate from
    # the stable document rather than remain attached to the discarded button.
    assert "document.addEventListener('click'" in page_source
    assert "[data-share-market]" in page_source


def test_preserved_market_filter_learns_exchange_options_after_warmup() -> None:
    """A cold shell can render before the catalogue knows its venues.

    The preserved form must keep any unsaved member input, but it must not keep
    an empty exchange select forever after the surrounding board recovers.
    Dynamic options are merged into the live select before its island is
    transplanted into the fetched shell.
    """
    source = _script()
    filter_source = inspect.getsource(server.render_market_filter_bar)

    assert "[data-refresh-options]" in source
    assert "existingSelect.append(option.cloneNode(true))" in source
    assert 'select name="exchange" data-refresh-options' in filter_source


def test_no_page_shell_forces_a_reload() -> None:
    """data-refresh-force existed only to defeat the pause toggle."""
    from pathlib import Path

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert 'data-refresh-force="1"' not in source


def test_typing_still_defers_the_refresh() -> None:
    """Swapping content under an open form would lose what was typed."""
    assert "editableActive()" in _script()


def test_a_recovered_board_adopts_the_slower_structural_refresh_interval() -> None:
    """A board opened during reconnect used to fetch its 1.2MB page forever.

    The first shell carried ``data-refresh=5``.  Once production recovered,
    the in-place response correctly carried ``data-refresh=300`` but only the
    element was replaced: the interval remained the original immutable five
    seconds.  SSE was already streaming prices, so every member kept rebuilding
    the whole board twelve times a minute for no freshness benefit.
    """
    source = _script()

    assert "let seconds" in source
    assert "seconds = refreshSeconds(next)" in source


def test_a_slow_structural_refresh_cannot_overlap_the_next_tick() -> None:
    """A five-second reconnect tick must not stack expensive page rebuilds."""
    source = _script()

    assert "let refreshPending = false" in source
    assert "if (refreshPending) return" in source
    assert "refreshPending = true" in source
    assert "refreshPending = false" in source


def test_the_shell_still_installs_it() -> None:
    """Removing the reload must not remove the freshness."""
    assert "render_auto_refresh_script" in inspect.getsource(server.shell)


def test_saving_a_market_preset_updates_the_chip_list_without_reloading() -> None:
    """The Markets preset form used to tear down the entire 1.2 MB board
    after a successful save even though the API returns the created preset.
    """

    source = server.FILTER_PRESET_SCRIPT

    assert "location.reload()" not in source
    assert "addPresetChip(data.preset)" in source
    assert "document.createElement('span')" in source


def test_preset_save_keeps_the_form_reference_across_the_await() -> None:
    """DOM event ``currentTarget`` is cleared once an async listener yields.

    The preset was successfully created, but the success path then tried to
    call ``reset`` through that cleared event field and reported a false error.
    Capture the form before the network request, just as the share button keeps
    its own stable button reference.
    """

    source = server.FILTER_PRESET_SCRIPT

    assert "const form = event.currentTarget;" in source
    assert "new FormData(form)" in source
    assert "form.reset();" in source
    after_request = source.split("await request('/api/filter-presets'", 1)[1]
    assert "event.currentTarget" not in after_request
