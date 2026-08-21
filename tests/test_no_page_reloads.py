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

    assert "location.reload()" not in source
    assert "window.location.reload()" not in source


def test_every_account_mutation_has_an_in_place_refresh_path() -> None:
    """Settings, notifications, Telegram and exchange credentials are account UI too.

    Position edits had been corrected, but the remaining actions still tore
    down the document.  Keep a single server-rendered partial-refresh path and
    stable delegated controls for nodes that the refresh replaces.
    """

    source = _account_script()

    assert "refreshAccountView" in source
    assert "refreshAccountSetting" in source
    for selector in (
        "[data-position-new]",
        "[data-notifications-read]",
        "[data-telegram-action]",
    ):
        assert f"event.target.closest('{selector}')" in source
    assert "window.spreadboardRefreshAccountView=refreshAccountView" in source


def test_overlapping_account_refreshes_cannot_restore_an_older_snapshot() -> None:
    """The listed-market probe can finish after a user mutation refresh.

    Only the newest request may replace account panels; otherwise an older GET
    can visually undo a just-saved correction even though the database is right.
    """

    source = _account_script()

    assert "accountRefreshVersion" in source
    assert "requestVersion!==accountRefreshVersion" in source
    assert "data-account-live-status" in source


def test_async_notification_buttons_keep_a_stable_button_reference() -> None:
    """DOM event.currentTarget is cleared as soon as an async listener yields."""

    source = _account_script()
    pushover = source.split("[data-pushover-test]", 1)[1].split(
        "const webPush", 1
    )[0]
    browser_push = source.split("[data-web-push-enable]", 1)[1].split(
        "[data-web-push-disable]", 1
    )[0]

    for handler in (pushover, browser_push):
        assert "const button=event.currentTarget" in handler
        assert "finally{button.disabled=false;}" in handler
        assert "finally{event.currentTarget.disabled=false;}" not in handler
    assert "pushover_user_not_configured" in source


def test_listed_market_recovery_refreshes_the_account_in_place() -> None:
    source = server.render_portfolio_market_refresh_script(
        [
            {
                "id": 7,
                "status": "open",
                "market_listing_status": "listed",
                "quote_status": "refreshing",
            }
        ]
    )

    assert "location.reload()" not in source
    assert "spreadboardRefreshAccountView" in source


def test_position_delete_confirmation_error_is_human_readable() -> None:
    """A typo at a destructive confirmation must not expose an API code."""

    source = _account_script()

    assert "position_delete_confirmation_mismatch" in source
    assert "The confirmation did not match" in source


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
