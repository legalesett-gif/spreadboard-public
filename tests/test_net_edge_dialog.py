"""The Markets Net edge calculator must read fields from its form.

Production threw ``Cannot read properties of undefined (reading 'notional')``
on the first click.  ``elements`` belongs to the form, not to the surrounding
``HTMLDialogElement``; syntax checks cannot catch that DOM contract error.
"""

from __future__ import annotations

from spreadboard import server


def test_net_edge_calculator_reads_named_fields_from_the_form() -> None:
    source = server.NET_EDGE_SCRIPT

    assert "const form = dialog.querySelector('form');" in source
    assert "form?.elements[name]?.value" in source
    assert "dialog.elements[name]" not in source


def test_net_edge_dialog_resolves_the_probe_label_in_dynamic_copy() -> None:
    """Production exposed the Python placeholder literally after opening the
    calculator because the standalone JavaScript constant was never formatted.
    """

    html = server.render_net_edge_dialog()

    assert "{PROBE_LABEL}" not in html
    assert server.PROBE_LABEL in html


def test_missing_settled_window_is_not_coerced_to_zero_funding() -> None:
    """JSON null becomes numeric zero in JavaScript. A missing 7d window must
    remain unavailable, not report $0.00 from a fictitious settled history.
    """

    source = server.NET_EDGE_SCRIPT

    assert "const exactAvailable = exact !== null" in source
    assert "const funding = exactAvailable" in source
    assert "textContent = exactAvailable" in source


def test_missing_current_rate_is_not_coerced_to_zero_funding() -> None:
    source = server.NET_EDGE_SCRIPT

    assert "const dailyAvailable" in source
    assert "current_funding_24h_pct !== null" in source


def test_missing_matched_edge_is_not_used_as_a_zero_percent_opening() -> None:
    source = server.NET_EDGE_SCRIPT

    assert "const boardAvailable" in source
    assert "const exactQuoteAvailable" in source
    assert "const exactEdgeAvailable" in source
    assert "Number(route.board_matched_edge_pct || 0)" not in source


def test_exact_size_response_with_null_matched_spread_is_not_coerced_to_zero() -> None:
    """Production returned ``ok: true`` with ``matched_spread_pct: null`` for
    an OSMO route whose sell book could not fill $1,000.  ``Number(null)``
    turned that failed depth proof into a fictitious exact 0.000% quote.
    """

    source = server.NET_EDGE_SCRIPT

    assert "const matchedAvailable = data.matched_spread_pct !== null" in source
    assert "if (!matchedAvailable) throw new Error('exact_route_target_depth_unavailable')" in source
    assert "route.exact_matched_edge_pct = Number(data.matched_spread_pct);" in source


def test_calculator_distinguishes_no_futures_leg_from_unknown_funding() -> None:
    button = server.render_net_edge_button(
        {
            "token": "ZIL",
            "route_key": "ZIL|Binance|Spot|Mexc|Spot",
            "long_market_type": "Spot",
            "short_market_type": "Spot",
            "depth_weighted_spread_pct": 2.7,
        }
    )

    assert "has_futures_leg" in button
    assert "false" in button
    assert "route.has_futures_leg === false" in server.NET_EDGE_SCRIPT


def test_known_opening_and_costs_are_not_blank_when_funding_is_unknown() -> None:
    """The calculator repeated the partial-total bug fixed in Portfolio: it
    held a usable pre-funding net but erased it because one component was null.
    """

    html = server.render_net_edge_dialog()

    assert "data-net-total-label" in html
    assert "Net before funding" in html
