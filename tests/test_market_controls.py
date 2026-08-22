"""Interactive Markets controls must survive their asynchronous work."""

from __future__ import annotations

from pathlib import Path

from spreadboard import server


def test_copy_view_link_keeps_the_button_across_the_clipboard_await() -> None:
    """``Event.currentTarget`` becomes null after the handler yields.

    Production copied the URL, then threw while trying to write "Link copied"
    because the async continuation read ``event.currentTarget`` again.
    """

    source = Path(server.__file__).read_text(encoding="utf-8")

    assert (
        "const button=event.currentTarget;" in source
        or "const button=event.target.closest('[data-share-market]');" in source
    )
    assert "button.textContent='Link copied'" in source
    assert "event.currentTarget.textContent='Link copied'" not in source


def test_json_export_prepares_then_downloads_instead_of_opening_raw_warming_data() -> None:
    page_source = Path(server.__file__).read_text(encoding="utf-8")
    script = server.render_json_export_script()

    assert 'data-market-export' in page_source
    assert 'data-market-export-status' in page_source
    assert "Preparing current JSON" in script
    assert "payload.status === 'warming'" in script
    assert "URL.createObjectURL" in script
    assert "anchor.download" in script
    assert "for (let attempt = 1; attempt <= 12; attempt += 1)" in script
    assert "location.assign" not in script


def test_dex_market_empty_state_names_a_zero_row_provider_failure() -> None:
    health = {
        "status": "fresh",
        "dex_spot_source": {
            "status": "partial",
            "rows": 0,
            "blockers": ["partial_source_errors"],
            "errors": [
                "catalogue:1:okx_dex_tokens:Your API key or regions have no access to current services"
            ],
        },
    }

    rendered = server.render_market_lane_empty("DEX-FUTURES", health)

    assert "provider access was rejected" in rendered
    assert "not evidence that no DEX routes exist" in rendered
    assert "No live API routes match" not in rendered


def test_dex_market_empty_state_distinguishes_a_successful_empty_cycle() -> None:
    rendered = server.render_market_lane_empty(
        "DEX-SPOT",
        {
            "status": "fresh",
            "dex_spot_source": {"status": "ok", "rows": 0, "errors": []},
        },
    )

    assert "quoting completed" in rendered
    assert "no verified Spot-DEX route matched" in rendered


def test_funding_dex_empty_state_does_not_call_a_failed_cycle_successful() -> None:
    rendered = server.render_funding_farm_empty(
        "futures-dex",
        {
            "status": "fresh",
            "dex_spot_source": {
                "status": "partial",
                "rows": 0,
                "blockers": ["partial_source_errors"],
                "errors": ["catalogue:1:okx_dex_tokens:no access to current services"],
            },
        },
    )

    assert "provider access was rejected" in rendered
    assert "quoting ran but no DEX route matched" not in rendered


def test_filtered_page_labels_global_sidebar_rankings_as_market_wide() -> None:
    rendered = server.render_market_lane(
        "Top Arbitrage Edges", [], "edge", market_wide=True
    )

    assert "Market-wide Arbitrage Edges" in rendered
    assert "Across all current lanes" in rendered


def test_pro_table_uses_the_full_desktop_width_for_settled_windows() -> None:
    source = Path(server.__file__).read_text(encoding="utf-8")

    assert "pro-table-layout" in source
    assert ".pro-table-layout { grid-template-columns:minmax(0,1fr); }" in server.APP_CSS
    assert ".pro-table-layout .market-side" in server.APP_CSS
    assert ".pro-market-table td > small" in server.APP_CSS
    assert "white-space:normal" in server.APP_CSS
    assert ".pro-actions { min-width:118px; white-space:normal; }" in server.APP_CSS


def test_pro_table_shows_a_known_current_projection_when_history_is_pending() -> None:
    """No settled window is not the same as no current funding evidence."""
    row = {
        "token": "GUA",
        "route_key": "GUA|Ourbit|Futures|Gate|Futures",
        "long_venue": "Ourbit",
        "long_market_type": "Futures",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "funding_24h_pct": None,
        "funding_projected_24h_pct": 0.6108,
        "short_funding_interval_hours": 4,
    }

    rendered = server.render_pro_market_table([row])

    assert server.funding_24h_value(row) == 0.6108
    assert "+0.611%" in rendered
    assert "24h at current rate" in rendered
    assert "History pending" in rendered


def test_settled_funding_remains_preferred_to_the_current_projection() -> None:
    row = {"funding_24h_pct": 0.4152, "funding_projected_24h_pct": 0.6258}

    assert server.funding_24h_value(row) == 0.4152
    assert server.funding_24h_basis(row) == "settled 24h"
