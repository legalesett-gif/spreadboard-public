"""The membership pages say what you get, not an essay about it.

The pricing page carried fourteen paragraphs of prose; a member deciding
whether to pay reads the ticks and the price.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from spreadboard import accounts, crypto_billing, server


@pytest.fixture(autouse=True)
def _signed_out() -> None:
    accounts.set_current_user(None)


def _visible_prose(html: str) -> list[str]:
    """Paragraphs a reader has to wade through.

    Feature ticks are deliberately one line each and are the concise format, so
    they do not count; this measures the running prose that was the problem.
    """
    body = re.sub(r"<style>.*?</style>", " ", html, flags=re.S)
    body = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", body, flags=re.S)
    cleaned = [" ".join(re.sub(r"<[^>]+>", " ", item).split()) for item in paragraphs]
    return [line for line in cleaned if len(line) > 45]


def test_the_pricing_page_is_short() -> None:
    html = server.render_pricing_page()
    prose = _visible_prose(html)

    # It used to run to eighteen blocks of copy.
    assert len(prose) <= 8, "\n".join(prose)
    assert max((len(line) for line in prose), default=0) <= 220


def test_the_pricing_page_still_says_the_essentials() -> None:
    html = server.render_pricing_page()

    assert "Current market evidence, clearly labelled." in html
    assert "What you get" in html
    assert "Why membership" in html
    # Every feature tick is present.
    for feature in server.MEMBERSHIP_FEATURES:
        assert feature.split(" — ")[0][:30] in html


def test_the_terms_come_from_the_prices_actually_charged() -> None:
    """Written out by hand they drift the first time a price changes."""
    terms = server.membership_terms()

    assert [term["days"] for term in terms] == sorted(crypto_billing.PERIODS)
    for term in terms:
        assert term["total"] == pytest.approx(crypto_billing.PERIODS[term["days"]] / 100.0)
    # The shortest term is the reference, so it can never show a saving.
    assert terms[0]["saving_pct"] == 0
    # A longer term must not cost more per month than the shortest.
    assert all(term["per_month"] <= terms[0]["per_month"] + 1e-9 for term in terms)


def test_every_term_is_offered_on_both_pages() -> None:
    pricing = server.render_pricing_page()
    subscription = server.render_subscription_page()

    for term in server.membership_terms():
        assert term["label"] in pricing
        assert term["label"] in subscription


def test_the_subscription_page_keeps_its_consent_and_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concise must not mean dropping what is legally required or functional."""
    monkeypatch.setenv(
        "SPREADBOARD_CRYPTO_RECEIVING_ADDRESS",
        "0xe45cedb238f0a90f111a283eb5f67f7e4d80b937",
    )
    monkeypatch.setenv("SPREADBOARD_CRYPTO_RPC_URL", "https://example.invalid/rpc")
    accounts.set_current_user(
        accounts.User(
            id=1,
            email="buyer@example.test",
            display_name="Buyer",
            role="member",
            subscription_status="inactive",
            subscription_expires_at=None,
            monthly_capital_usd=None,
            subscription_tier="free",
            billing_customer_id=None,
            billing_subscription_id=None,
            subscription_cancel_at_period_end=False,
            csrf_token="csrf",
        )
    )
    html = server.render_subscription_page()

    assert "data-subscription-consent" in html
    assert "statutory cancellation right" in html
    # The operator asked for the policy references off the site (2026-08-19).
    # The consent gate itself stays: it is what records that the buyer asked for
    # immediate access, which is the part that actually matters at checkout.
    assert 'href="/terms"' not in html and 'href="/refunds"' not in html
    assert "data-billing-error" in html
    assert "account-session" in html


def test_directly_managed_access_is_not_described_as_auto_renewing() -> None:
    class _User:
        id = 1
        display_name = "Member"
        email = "member@example.com"
        role = "member"
        is_admin = False
        subscription_active = True
        subscription_status = "active"
        subscription_tier = "research_pro"
        csrf_token = "csrf"
        subscription_expires_at = "2026-08-18T00:00:00+00:00"
        billing_customer_id = None

    accounts.set_current_user(_User())
    try:
        html = server.render_subscription_page()
    finally:
        accounts.set_current_user(None)

    assert "Prepaid Research Pro access is active through 18 August 2026." in html
    assert "Same-tier renewals extend your access from 18 August 2026." in html
    assert "Access until" in html
    assert "Renews automatically" not in html
    assert "Subscribe ·" not in html


def test_the_subscription_page_leads_with_the_plan_facts() -> None:
    html = server.render_subscription_page()

    assert "30-day reference price" in html
    assert "Billing cycle" in html
    prose = _visible_prose(html)
    assert len(prose) <= 6, "\n".join(prose)


def test_legal_pages_describe_the_live_prepaid_crypto_path() -> None:
    terms = server.render_legal_page("terms")
    privacy = server.render_legal_page("privacy")
    refunds = server.render_legal_page("refunds")

    assert "does not renew automatically" in terms
    assert "wallet private key or seed phrase" in privacy
    assert "public transaction hash" in refunds
    assert "Stripe processes" not in privacy
    assert "recurring billing through the account billing portal" not in refunds


def test_a_renewal_date_reads_as_a_date() -> None:
    assert server.fmt_renewal_date("2026-08-18T00:00:00+00:00") == "18 August 2026"
    assert server.fmt_renewal_date(None) == "—"
    assert server.fmt_renewal_date("not a date") == "—"


def test_the_watchlist_follows_the_theme_and_is_legible() -> None:
    """It was hardcoded light-mode -- white cards on a dark page -- at 11px.

    Every colour came from a literal (#dedede, white, #666) rather than the
    theme variables the rest of the site uses, so dark mode did not reach it at
    all, and #666 on a dark background is unreadable.
    """
    import re

    from pathlib import Path as _Path

    from spreadboard import accounts, server

    class _User:
        id = 1
        display_name = "Alex"
        email = "a@b"
        role = "admin"
        is_admin = True
        subscription_active = True
        subscription_status = "active"
        csrf_token = "t"
        subscription_expires_at = None
        billing_customer_id = None
        billing_subscription_id = None
        subscription_cancel_at_period_end = False
        monthly_capital_usd = None

    accounts.set_current_user(_User())
    try:
        html = server.render_watchlist_page(_Path("data/spreadboard.json"), {}, {})
    finally:
        accounts.set_current_user(None)

    # The stylesheet is linked rather than inlined now, so the rules live there.
    assert "/assets/app.css" in html
    rules = re.findall(r"(\.watch[a-z-]*[^{]*\{[^}]*\})", server.APP_CSS)
    assert rules, "no watchlist rules rendered"

    hardcoded = [r for r in rules if re.search(r"#[0-9a-fA-F]{3,6}\b", r) or ": white" in r]
    assert not hardcoded, f"{len(hardcoded)} watchlist rules still hardcode a colour"

    sizes = {int(m) for r in rules for m in re.findall(r"font-size:\s*(\d+)px", r)}
    assert sizes, "no font sizes found"
    assert min(sizes) >= 13, f"smallest watchlist type is {min(sizes)}px"

    # Tap targets big enough to hit.
    heights = {int(m) for r in rules for m in re.findall(r"min-height:\s*(\d+)px", r)}
    assert all(h >= 38 for h in heights if h < 100), f"small tap target: {sorted(heights)}"


def test_saved_alert_cards_follow_dark_and_light_theme_tokens() -> None:
    """Saved alert cards were white with pale text in dark mode."""

    import re

    css = "\n".join(re.findall(r"\.member-alert[^\{]*\{[^}]*\}", server.APP_CSS))

    for literal in ("#dedede", "#fff", "#6a6a6a", "#666", "#555", "#d5d5d5", "#f7f7f7"):
        assert literal not in css
    assert "background: var(--terminal-panel)" in css
    assert "color: var(--terminal-text)" in css
    assert ".member-alert-card.paused { opacity: 1;" in css


def test_pro_table_keeps_execution_evidence_and_route_actions() -> None:
    html = server.render_pro_market_table([{
        "token": "SIREN", "route_key": "SIREN|OKX DEX|Spot|Gate|Futures",
        "long_venue": "OKX DEX", "long_market_type": "Spot", "long_price": 0.03,
        "short_venue": "Gate", "short_market_type": "Futures", "short_price": 0.031,
        "depth_weighted_spread_pct": 3.2, "executable_spread_pct": 3.5,
        "funding_24h_pct": 0.4, "depth_usd": 2500,
        "long_deposit_enabled": True, "long_withdraw_enabled": True,
        "short_deposit_enabled": True, "short_withdraw_enabled": True,
    }])
    for expected in ("Pro Table", "Matched edge", "Funding 24h", "SIREN", "Details", "Chart"):
        assert expected in html


def test_persistence_score_uses_realised_windows_not_current_apr(monkeypatch) -> None:
    monkeypatch.setattr(
        server.venue_funding_history, "route_windows",
        lambda row: {"1d": 0.2, "7d": 1.1, "30d": 3.0},
    )
    result = server.funding_persistence({"route_key": "X"})
    assert result["status"] == "persistent"
    assert result["observed_windows"] == 3


def test_net_edge_button_carries_matched_spread_and_realised_funding(monkeypatch) -> None:
    monkeypatch.setattr(
        server.venue_funding_history, "route_windows",
        lambda row: {"1d": 0.25, "7d": 1.5, "30d": 4.0},
    )
    html = server.render_net_edge_button({
        "token": "CASHCAT", "route_key": "cashcat-route",
        "depth_weighted_spread_pct": 1.2, "funding_24h_pct": 0.25,
    })
    assert "Net edge" in html
    assert "1.2" in html and "1d" in html and "0.25" in html


def test_public_methodology_and_proof_label_evidence_honestly() -> None:
    methodology = server.render_methodology_page()
    proof = server.render_proof_page()
    for expected in ("Matched-size VWAP", "Settled funding", "Identity", "Unknown stays unknown"):
        assert expected in methodology
    for expected in (
        "Current audit in progress",
        "10 / 35",
        "4 / 4",
        "per closed route",
        "51 / 51",
        "1,478",
        "Archived audit · 7 August 2026",
        "638",
        "12 / 12",
        "Modeled example",
        "Losing example",
        "Basis convergence captured",
    ):
        assert expected in proof
    assert "Matched opening edge" not in proof
    assert "Latest verified checkpoint" not in proof
    assert '<a href="/api/health">Machine-readable live health →</a>' in proof
    assert '<details class="archive-audit">' in proof
    assert ".audit-caveat" in server.APP_CSS
    assert ".audit-caveat {" in server.APP_CSS
    assert "color:var(--terminal-text);" in server.APP_CSS.split(".audit-caveat {", 1)[1].split(
        "}", 1
    )[0]
    assert ".archive-audit summary span { color:var(--terminal-text); }" in server.APP_CSS
    assert "guaranteed" not in (methodology + proof).lower()
