"""Funding must show and alert on the same evidence it ranks.

Production's ``Now`` tab ranked ONG from its normalized ``+3.3012%`` current
24-hour carry, while the row showed the older ``+2.371% settled 24h`` value.
The summary therefore advertised the live leader but the row hid the number
which made it the leader.  Historical tabs have the opposite contract: they
rank and display the selected settled window, never relabel a last-live rate as
current.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spreadboard import alerts, server


def _route(**updates: object) -> dict[str, object]:
    route: dict[str, object] = {
        "token": "ONG",
        "route_key": "ONG|Mexc|Futures|Bybit|Futures",
        "route_kind": "FUTURES",
        "long_venue": "Mexc",
        "long_market_type": "Futures",
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "funding_daily_pct": 3.3012,
        "funding_projected_24h_pct": 3.5436,
        "funding_24h_pct": 2.371,
        "executable_spread_pct": 1.2414,
        "depth_weighted_spread_pct": 1.15,
        "age_min": 0.2,
        "radar_windows": {"1d": 2.371, "7d": 9.06, "30d": 14.2},
    }
    route.update(updates)
    return route


def _group(**updates: object) -> dict[str, object]:
    route = _route(**updates)
    return {
        "token": "ONG",
        "token_name": "Ontology Gas",
        "best_route": route,
        "best_funding_route": route,
        "routes": [route],
        "route_count": 1,
    }


def test_now_group_shows_the_normalized_live_rank_even_when_settled_24h_exists() -> None:
    html = server.render_funding_token_group(_group())

    assert "+3.301%" in html
    assert "24h at current rate" in html
    assert "+2.371%" not in html


def test_now_child_row_shows_the_current_projection_too() -> None:
    html = server.render_funding_pair(_route())

    assert "+3.301%" in html
    assert "at current rate" in html
    assert "+2.371%" not in html


def test_a_historical_tab_shows_the_exact_window_it_ranked() -> None:
    html = server.render_funding_token_group(_group(), selected_window="7d")

    assert "+9.060%" in html
    assert "settled 7d" in html
    assert "+3.301%" not in html


def test_the_page_passes_its_selected_window_into_every_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = _group()
    group["best_funding_window_pct"] = 9.06
    monkeypatch.setattr(
        server,
        "api_market_spreads",
        lambda *_args, **_kwargs: {
            "ok": True,
            "groups": [group],
            "summary": {"matching_rows": 1},
            "source_health": {"canonical_api": {"status": "fresh", "age_min": 0.2}},
        },
    )
    monkeypatch.setattr(server, "_historical_funding_groups", lambda *args, **kwargs: [group])
    monkeypatch.setattr(
        server,
        "funding_history_health",
        lambda: {
            "attempted_leg_count": 1,
            "catalog_leg_count": 1,
            "classified_leg_count": 1,
            "pending_leg_count": 0,
            "retryable_error_leg_count": 0,
        },
    )

    html = server.render_funding_page(
        Path("board.json"), {}, {"rank": ["7d"], "farm": ["futures-futures"]}
    )

    assert "Settled 7d" in html
    assert "+9.060%" in html
    assert "+3.301%" not in html


def test_funding_alert_draft_and_worker_use_the_current_projection() -> None:
    route = _route()
    html = server.render_alert_draft_button(route, alert_type="funding")

    assert 'data-current-value="3.3012"' in html
    assert alerts._rule_value(route, "funding_24h_pct") == 3.3012


def test_token_funding_alert_uses_the_best_current_projection() -> None:
    metrics = alerts.token_metrics([_route(), _route(funding_projected_24h_pct=1.0)])

    assert metrics["ONG"]["token_funding_24h_pct"] == 3.3012


def test_settled_only_fallback_is_never_labelled_live() -> None:
    route = _route(
        funding_daily_pct=None,
        funding_projected_24h_pct=None,
        funding_24h_pct=2.371,
        funding_rank_basis="settled_public_events",
    )

    html = server.render_funding_pair(route)

    assert "+2.371%" not in html
    assert "funding unavailable" in html
    assert "data-live-funding" not in html


def test_markets_child_row_shows_the_same_live_value_as_its_group() -> None:
    route = _route(funding_rank_basis="projected_current_rate")

    html = server.render_market_group_route(route)

    assert "+3.301%" in html
    assert "24h at current rate" in html
    assert "+2.371%" not in html


@pytest.mark.parametrize(
    ("ok", "status", "seconds"),
    [(False, "warming", 5), (True, "fresh", 300)],
)
def test_funding_recovers_quickly_only_while_its_generation_is_warming(
    monkeypatch: pytest.MonkeyPatch,
    ok: bool,
    status: str,
    seconds: int,
) -> None:
    """A cold Funding shell must not strand the member for five minutes.

    The in-place refresher already preserves scroll and never reloads the
    document.  Funding should use its five-second recovery cadence only until
    a current generation arrives, then adopt the normal five-minute cadence.
    """
    monkeypatch.setattr(
        server,
        "api_market_spreads",
        lambda *_args, **_kwargs: {
            "ok": ok,
            "groups": [],
            "summary": {},
            "source_health": {"canonical_api": {"status": status}},
        },
    )
    monkeypatch.setattr(
        server,
        "funding_history_health",
        lambda: {
            "attempted_leg_count": 0,
            "catalog_leg_count": 0,
            "classified_leg_count": 0,
            "pending_leg_count": 0,
            "retryable_error_leg_count": 0,
        },
    )
    monkeypatch.setattr(
        server.bulk_quotes,
        "funding_health",
        lambda: {"status": "fresh", "p95_age_seconds": 30.0},
    )

    html = server.render_funding_page(Path("board.json"), {}, {})

    assert f'class="funding-page terminal-page" data-refresh="{seconds}"' in html
    assert "location.reload()" not in html
