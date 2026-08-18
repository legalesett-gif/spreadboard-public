"""Recording an exit has to be findable, not merely present.

The operator reported "there is no way to close a position. So, I cannot input
the values at which I have closed my position, to calculate the PnL". The
control existed and worked -- verified end to end in a browser against the real
page structure: the dialog opens titled "Record completed journal position",
sets status to closed, prefills closed_at, and shows required "Long exit fill"
and "Short exit fill" inputs.

It was the third of five controls in a footer at the bottom of a tall card,
styled identically to "Delete entry", and on a phone those five stack vertically
below every metric and both legs. A capability nobody can find is indistinguishable
from one that does not exist.

So: it leads the row, it is the only primary-styled action there, and it says what
it is for rather than just "Close position".
"""

from __future__ import annotations

import inspect

from spreadboard import server


def _footer(status: str = "open") -> str:
    html = server.render_position_card({
        "id": 1, "token": "ESPORTS", "status": status,
        "long_venue": "OKX DEX 56", "short_venue": "Gate",
        "long_market_type": "Spot", "short_market_type": "Futures",
        "long_quantity": 100.0, "long_entry_price": 2.0,
        "short_quantity": 100.0, "short_entry_price": 2.05,
        "opened_at": "2026-08-08T21:37:49Z",
    })
    start = html.index("<footer>")
    return html[start:html.index("</footer>", start)]


def test_an_open_position_offers_the_exit_control() -> None:
    assert 'data-position-action="close"' in _footer()


def test_the_exit_control_comes_before_the_other_actions() -> None:
    """It was third of five and read as just another secondary button."""
    footer = _footer()
    close = footer.index('data-position-action="close"')
    for other in ("data-position-edit", 'data-position-action="alert"',
                  'data-position-action="delete"'):
        assert close < footer.index(other), f"{other} still precedes the exit control"


def test_the_exit_control_is_the_primary_action_on_the_card() -> None:
    """Identical styling to "Delete entry" is what made it invisible."""
    footer = _footer()
    segment = footer[footer.index('data-position-action="close"') - 120:]
    assert "primary" in segment[:200]


def test_it_says_what_it_is_for() -> None:
    """"Close position" reads as dismissing a card, not recording a fill."""
    footer = _footer()
    assert "exit" in footer.casefold()


def test_a_closed_position_does_not_offer_it_again() -> None:
    assert 'data-position-action="close"' not in _footer(status="closed")


def test_the_dialog_still_requires_both_exit_fills() -> None:
    """The PnL is meaningless without both legs."""
    source = inspect.getsource(server.render_account_script)
    assert "field.required=closed&&field.name!=='exit_fees_usd'" in source
    dialog = server.render_position_edit_dialog()
    for field in ("long_exit_price", "short_exit_price", "closed_at", "exit_fees_usd"):
        assert field in dialog
