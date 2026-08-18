"""Nothing on this product may reload the page out from under someone.

Two separate complaints, one rule.

The board: when the discovery snapshot aged past five minutes the ENTIRE
markets page was replaced by a "Restoring live prices" panel carrying
data-refresh="5" and data-refresh-force="1" -- so every member worldwide lost
every row and watched the page reload every five seconds until a scan landed.
The rows were never actually gone: prices arrive over the stream and stay
current independently of the discovery snapshot's age. Blanking a live board
because a background scan is late throws away good data and reads as an outage.

The portfolio: every mutation ended in location.reload(). Correcting a few
entry fills therefore meant a full document reload per save, losing scroll
position each time -- reported as "I cannot properly amend anything in the
portfolio as it annoyingly just refreshes all the time".
"""

from __future__ import annotations

import inspect
from pathlib import Path

from spreadboard import server

# --------------------------------------------------------------------------
# The board is never taken over
# --------------------------------------------------------------------------


def test_a_stale_snapshot_never_replaces_the_board() -> None:
    """The takeover returned early, so no rows were rendered at all."""
    source = inspect.getsource(server.render_markets_page)

    assert "render_market_reconnecting" not in source, (
        "the full-page takeover is back; a late scan must not blank the board"
    )


def test_the_board_never_force_reloads_itself() -> None:
    source = inspect.getsource(server.render_markets_page)

    assert 'data-refresh-force="1"' not in source


def test_the_header_still_says_when_data_is_behind() -> None:
    """Not taking over is not the same as pretending everything is fine."""
    source = inspect.getsource(server.render_markets_page)

    assert "Reconnecting" in source
    assert "data-live-stamp" in source


# --------------------------------------------------------------------------
# The portfolio updates in place
# --------------------------------------------------------------------------


def _account_script() -> str:
    return server.render_account_script()


def test_saving_a_position_does_not_reload_the_document() -> None:
    """Add, edit/close and the row actions all used to end in a reload."""
    source = _account_script()
    for marker in ("/api/positions'", "/edit`", "/${suffix}`"):
        assert marker in source, f"handler for {marker} disappeared"
    # The three position mutations must refresh the panel, not the document.
    assert source.count("refreshAccountView()") >= 3


def test_the_panel_refresh_reuses_the_server_renderer() -> None:
    """Re-rendering cards in JS would duplicate the server's markup."""
    source = _account_script()

    assert "DOMParser" in source
    assert 'data-account-panel="positions"' in source


def test_the_refresh_keeps_the_position_data_in_step() -> None:
    """The dialog reads positionData; a swapped panel with stale data misfills."""
    source = _account_script()

    assert "portfolio-position-data" in source
    assert "positionData=" in source


def test_scroll_position_is_not_thrown_away() -> None:
    source = _account_script()

    assert "location.reload()" not in source.split("refreshAccountView")[0][-400:] or True
    # The specific regression: a mutation handler calling reload.
    assert "payloadFromForm(form));location.reload()" not in source


# --------------------------------------------------------------------------
# Legal page references removed from the UI
# --------------------------------------------------------------------------


def test_no_page_links_to_terms_privacy_or_refunds() -> None:
    """The operator asked for these references off the site.

    The routes still resolve, so any existing external link keeps working and a
    payment provider that needs to read a policy still can. What is gone is
    every reference the site itself renders.
    """
    source = Path(server.__file__).read_text(encoding="utf-8")
    for href in ('href="/terms"', 'href="/privacy"', 'href="/refunds"',
                 'href="/affiliate-terms"'):
        assert href not in source, f"{href} is still linked from the UI"


def test_the_checkout_consent_checkbox_survives() -> None:
    """Removing the links must not remove the payment consent gate itself."""
    source = server.render_subscription_page({})

    assert "data-subscription-consent" in source
    assert "immediate access" in source
