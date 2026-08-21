"""Pricing must not sell broader or more immediate coverage than production has."""

import http.client
import threading

from spreadboard import accounts, affiliates, server


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


def test_light_pricing_copy_uses_a_contrast_safe_local_muted_colour() -> None:
    html = server.render_pricing_page()

    assert "--pricing-muted:#596a64" in html
    assert ".pricing-page .reason p { color:var(--pricing-muted); }" in html


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


def test_referral_banner_requires_a_valid_first_party_click_cookie(tmp_path) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    partner_user = accounts.create_user(
        email="partner@example.test",
        display_name="Partner",
        password="correct horse battery staple",
        db_path=db_path,
    )
    affiliates.create_partner(
        int(partner_user["id"]),
        slug="evidence-channel",
        display_name="Evidence Channel",
        db_path=db_path,
    )
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=db_path,
    )
    threading.Thread(target=app.serve_forever, daemon=True).start()
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        client.request("GET", "/pricing?referred=1")
        response = client.getresponse()
        assert response.status == 200
        assert "Your channel discount is saved" not in response.read().decode()

        client.request("GET", "/r/evidence-channel")
        response = client.getresponse()
        assert response.status == 303
        assert response.getheader("Location") == "/pricing?referred=1"
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        response.read()

        client.request("GET", "/pricing?referred=1", headers={"Cookie": cookie})
        response = client.getresponse()
        assert response.status == 200
        assert "Your channel discount is saved" in response.read().decode()
    finally:
        client.close()
        app.shutdown()
        app.server_close()
