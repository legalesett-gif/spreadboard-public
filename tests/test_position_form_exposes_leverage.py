"""Leverage was stored but never askable, so it could never be set.

The owner runs SKHX at 5x on the short. Without a field, the position kept
`long_leverage`/`short_leverage` NULL, `capital_committed_usd` reported the full
$6,002 of notional as locked capital, and no amount of backend correctness could
fix a value the form never collects.

Per leg, because a spot long is 1x while the perp short beside it may not be.
"""

from __future__ import annotations

from spreadboard import server


def test_the_add_form_asks_for_leverage_on_both_legs() -> None:
    html = server.render_position_dialog()

    assert 'name="long_leverage"' in html
    assert 'name="short_leverage"' in html


def test_the_correction_form_asks_too() -> None:
    """An existing position predates the column and can only be fixed here."""

    html = server.render_position_edit_dialog()

    assert 'name="long_leverage"' in html
    assert 'name="short_leverage"' in html


def test_leverage_defaults_to_blank_not_one() -> None:
    """A prefilled 1x is a claim the member did not make.

    Blank means "not stated", which capital_metrics already treats as unlevered.
    """

    for html in (server.render_position_dialog(), server.render_position_edit_dialog()):
        marker = 'name="short_leverage"'
        field = html[html.index(marker) : html.index(marker) + 200]
        assert 'value="1"' not in field


def test_editing_a_position_prefills_its_recorded_leverage() -> None:
    """Without this the correction form opens blank and silently clears the
    value on save, which is worse than never having had the field."""

    data = server.editable_position_data(
        {"token": "SKHX", "capital_usd": 100.0, "long_leverage": None, "short_leverage": 5.0}
    )

    assert "short_leverage" in data
    assert data["short_leverage"] == 5.0
    assert "long_leverage" in data
