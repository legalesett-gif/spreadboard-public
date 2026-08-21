from __future__ import annotations

from pathlib import Path

from spreadboard import server


def _market_payload() -> dict:
    return {
        "ok": True,
        "summary": {
            "matching_tokens": 0,
            "matching_rows": 0,
            "funding_rows": 0,
            "max_depth_weighted_spread_pct": None,
            "max_abs_funding_24h_pct": None,
        },
        "groups": [],
        "rows": [],
        "top_edges": [],
        "top_funding": [],
        "source_health": {"canonical_api": {"status": "fresh", "age_min": 0.1}},
        "pagination": {"returned_rows": 0, "total_rows": 0, "limit": 25, "offset": 0},
        "filters": {},
    }


def test_shell_contains_the_reconciled_editorial_terminal_system() -> None:
    html = server.shell("Design check", "markets", "<p>content</p>")

    assert "Editorial market-terminal refresh" in server.APP_CSS
    assert "--terminal-bg:#07110f" in server.APP_CSS
    assert ".brand-mark::before,.brand-mark::after { display:none; }" in server.APP_CSS
    assert ".token-route-ledger-head,.funding-ledger-head" in server.APP_CSS
    assert "border-radius:0" in server.APP_CSS
    # The page must actually load that stylesheet.
    assert "/assets/app.css" in html


def test_markets_and_funding_have_explicit_ledger_headers(monkeypatch) -> None:
    payload = _market_payload()
    monkeypatch.setattr(server, "api_market_spreads", lambda *_args, **_kwargs: payload)
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

    markets = server.render_markets_page(Path("missing.jsonl"), {}, {})
    funding = server.render_funding_page(Path("missing.jsonl"), {}, {})

    assert 'class="token-route-ledger-head"' in markets
    assert "Matched spread" in markets and "Best-route funding" in markets
    assert 'class="funding-ledger-head"' in funding
    assert "Settled windows" in funding and "Entry basis" in funding


def test_chart_copy_names_the_two_directions_without_implying_an_exit_fill() -> None:
    html = server.render_live_spread_chart("GUA|Long|Short", [], "1h")

    assert "In % · open" in html
    assert "Out % · close" in html
    assert "marked reverse direction" in html
    assert "Open ask → bid" not in html
    assert "actual exit" not in html.lower()


def test_public_auth_and_membership_surfaces_use_the_flat_layout() -> None:
    login = server.render_login_page({})
    register = server.render_register_page()
    pricing = server.render_pricing_page()

    for page in (login, register):
        assert ".login-shell" in page
        assert "border-width:1px 0" in page
        assert "box-shadow:none" in page
    assert ".pricing-tier" in pricing
    assert "background:transparent" in pricing
    assert "border-radius:2px" in pricing


def test_playbook_cards_use_theme_tokens_instead_of_light_only_surfaces() -> None:
    """Mobile dark mode made the playbook effectively unreadable in production.

    The later editorial theme changes global text to pale colours, but these
    older cards and their answer boxes kept hard-coded white backgrounds.  The
    component must consume the same terminal surface/text tokens in both modes.
    """

    card_rule = server.APP_CSS.split(".playbook-card {", 1)[1].split("}", 1)[0]
    answer_rule = server.APP_CSS.split(".playbook-answer {", 1)[1].split("}", 1)[0]

    assert "background: var(--terminal-panel)" in card_rule
    assert "border: 1px solid var(--terminal-line)" in card_rule
    assert "color: var(--terminal-text)" in card_rule
    assert "background: var(--terminal-row)" in answer_rule
    assert "border: 1px solid var(--terminal-line)" in answer_rule
    assert "color: var(--terminal-text)" in answer_rule
    assert "background: white" not in answer_rule
