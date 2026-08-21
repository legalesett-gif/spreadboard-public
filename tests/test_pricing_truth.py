"""Pricing must not sell broader or more immediate coverage than production has."""

from spreadboard import accounts, server


def test_pricing_names_evidence_limits_and_every_prepaid_tier() -> None:
    html = server.render_pricing_page()

    assert "Current market evidence, clearly labelled." in html
    assert "Every spread, live." not in html
    assert "22 exchanges plus OKX DEX" not in html
    assert "Live, not polled" not in html
    assert "never ticker matching" not in html
    assert "priority support" not in html
    assert "unavailable sources stay labelled" in html
    assert "Scanner prepaid terms" in html
    assert "$135 billed once" in html
    assert "$490 billed once" in html
    assert "Research Pro prepaid terms" in html
    assert "$375 billed once" in html
    assert "$1,365 billed once" in html


def test_active_member_is_not_offered_an_impossible_mid_term_tier_change() -> None:
    class User:
        display_name = "Active member"
        csrf_token = "csrf"
        subscription_active = True
        subscription_tier = "research_pro"
        is_admin = False

    accounts.set_current_user(User())
    try:
        html = server.render_pricing_page()
    finally:
        accounts.set_current_user(None)

    assert "Open current plan" in html
    assert "Available after current term" in html
    assert "Choose Scanner" not in html
