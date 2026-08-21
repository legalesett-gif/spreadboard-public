"""Public registration must not trade account integrity for convenience.

Registration is both an authentication boundary and the start of affiliate and
billing state.  A browser without JavaScript must never put credentials in the
URL, and a failure after the user insert must not leave an account the owner
cannot safely retry.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

from spreadboard import accounts, affiliates, server


def _running_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[server.SpreadBoardServer, threading.Thread, Path]:
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SPREADBOARD_ADMIN_PASSWORD", raising=False)
    server.SpreadBoardHandler._login_attempts.clear()
    db_path = tmp_path / "accounts.sqlite3"
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=db_path,
    )
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    return app, thread, db_path


def _close(
    app: server.SpreadBoardServer,
    thread: threading.Thread,
    client: http.client.HTTPConnection,
) -> None:
    client.close()
    app.shutdown()
    app.server_close()
    thread.join(timeout=5)
    server.SpreadBoardHandler._login_attempts.clear()


def _origin_headers(app: server.SpreadBoardServer) -> dict[str, str]:
    authority = f"127.0.0.1:{app.server_port}"
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": f"http://{authority}",
        "Host": authority,
    }


def test_register_page_has_safe_progressive_enhancement_and_theme() -> None:
    html = server.render_register_page({})

    assert '<form id="registerForm" action="/api/register" method="post">' in html
    assert 'name="display_name" maxlength="100"' in html
    assert 'name="email" type="email" maxlength="254"' in html
    assert 'name="password" type="password" minlength="12" maxlength="1024"' in html
    assert "auth-theme-toggle" in html
    assert "data-theme" in html
    assert "try{localStorage.setItem" in html.replace(" ", "")
    assert "<noscript><style>.auth-theme-toggle{display:none}</style></noscript>" in html


def test_no_javascript_registration_posts_credentials_and_redirects_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, db_path = _running_server(tmp_path, monkeypatch)
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        client.request(
            "POST",
            "/api/register",
            body=urlencode(
                {
                    "display_name": "No JavaScript Member",
                    "email": "no-js@example.test",
                    "password": "no-javascript-password",
                }
            ),
            headers=_origin_headers(app),
        )
        response = client.getresponse()
        response.read()
        set_cookies = response.getheaders()

        assert response.status == 303
        assert response.getheader("Location") == "/subscription"
        assert "email" not in response.getheader("Location")
        assert "password" not in response.getheader("Location")
        session_cookie = next(
            value for key, value in set_cookies
            if key.casefold() == "set-cookie" and accounts.SESSION_COOKIE in value
        )
        assert "HttpOnly" in session_cookie and "SameSite=Lax" in session_cookie

        client.request("GET", "/register", headers={"Cookie": session_cookie})
        response = client.getresponse()
        response.read()
        assert response.status == 303
        assert response.getheader("Location") == "/subscription"

        user_id = accounts.user_id_for_email("no-js@example.test", db_path=db_path)
        assert user_id is not None
        accounts.update_subscription(
            user_id,
            status="active",
            expires_at="2099-01-01T00:00:00Z",
            tier="research_pro",
            db_path=db_path,
        )
        client.request("GET", "/register", headers={"Cookie": session_cookie})
        response = client.getresponse()
        response.read()
        assert response.status == 303
        assert response.getheader("Location") == "/account"
    finally:
        _close(app, thread, client)


def test_cross_site_html_registration_is_rejected_without_creating_a_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, db_path = _running_server(tmp_path, monkeypatch)
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        client.request(
            "POST",
            "/api/register",
            body=urlencode(
                {
                    "display_name": "Cross Site",
                    "email": "cross-site@example.test",
                    "password": "cross-site-password",
                }
            ),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://attacker.example",
                "Host": f"127.0.0.1:{app.server_port}",
            },
        )
        response = client.getresponse()
        payload = json.loads(response.read())

        assert response.status == 403
        assert payload == {"error": "invalid_request_origin", "ok": False}
        assert accounts.user_id_for_email("cross-site@example.test", db_path=db_path) is None
    finally:
        _close(app, thread, client)


def test_registration_recognizes_case_insensitive_json_media_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, db_path = _running_server(tmp_path, monkeypatch)
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        client.request(
            "POST",
            "/api/register",
            body=json.dumps(
                {
                    "display_name": "   ",
                    "email": "media-type@example.test",
                    "password": "media-type-password",
                }
            ),
            headers={"Content-Type": "Application/JSON; Charset=UTF-8"},
        )
        response = client.getresponse()
        payload = json.loads(response.read())

        assert response.status == 400
        assert payload == {"error": "invalid_display_name", "ok": False}
        assert accounts.user_id_for_email("media-type@example.test", db_path=db_path) is None
    finally:
        _close(app, thread, client)


def test_registration_validation_is_bounded_and_duplicate_is_generic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, db_path = _running_server(tmp_path, monkeypatch)
    accounts.create_user(
        email="existing@example.test",
        display_name="Existing",
        password="existing-account-password",
        subscription_status="inactive",
        db_path=db_path,
    )
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        for body, expected in (
            (
                {"display_name": "   ", "email": "new@example.test", "password": "valid-password-value"},
                "invalid_display_name",
            ),
            (
                {"display_name": "Member", "email": "new@example.test", "password": "x" * 1025},
                "password_must_be_at_most_1024_characters",
            ),
            (
                {"display_name": "Existing", "email": "existing@example.test", "password": "existing-account-password"},
                "registration_not_available",
            ),
        ):
            client.request(
                "POST",
                "/api/register",
                body=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            response = client.getresponse()
            payload = json.loads(response.read())
            assert response.status == 400
            assert payload == {"error": expected, "ok": False}
            assert "registered" not in json.dumps(payload).casefold()
        assert accounts.user_id_for_email("new@example.test", db_path=db_path) is None

        client.request(
            "POST",
            "/api/register",
            body=urlencode(
                {
                    "display_name": "   ",
                    "email": "form@example.test",
                    "password": "form-password-value",
                }
            ),
            headers=_origin_headers(app),
        )
        response = client.getresponse()
        response.read()
        assert response.status == 303
        assert response.getheader("Location") == "/register?error=name"
        assert "form%40example" not in response.getheader("Location")
        assert "password" not in response.getheader("Location")
    finally:
        _close(app, thread, client)


def test_failure_after_referral_mutation_rolls_back_user_attribution_click_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, db_path = _running_server(tmp_path, monkeypatch)
    partner_user = accounts.create_user(
        email="rollback-partner@example.test",
        display_name="Rollback Partner",
        password="rollback-partner-password",
        subscription_status="inactive",
        db_path=db_path,
    )
    affiliates.create_partner(
        int(partner_user["id"]),
        slug="rollback-partner",
        display_name="Rollback Partner",
        db_path=db_path,
    )
    _partner, referral_token = affiliates.create_click("rollback-partner", db_path=db_path)
    original_attachment = affiliates.attach_registration_in_transaction

    def fail_after_attachment(*args, **kwargs):
        original_attachment(*args, **kwargs)
        raise RuntimeError("private-database-detail")

    monkeypatch.setattr(
        affiliates,
        "attach_registration_in_transaction",
        fail_after_attachment,
    )
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        client.request(
            "POST",
            "/api/register",
            body=json.dumps(
                {
                    "display_name": "Rollback Member",
                    "email": "rollback@example.test",
                    "password": "rollback-member-password",
                }
            ),
            headers={
                "Content-Type": "application/json",
                "Cookie": f"{affiliates.REFERRAL_COOKIE}={referral_token}",
            },
        )
        response = client.getresponse()
        raw = response.read().decode("utf-8")

        assert response.status == 503
        assert json.loads(raw) == {"error": "registration_temporarily_unavailable", "ok": False}
        assert "private-database-detail" not in raw
        assert accounts.user_id_for_email("rollback@example.test", db_path=db_path) is None
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM affiliate_attributions"
            ).fetchone()[0] == 0
            click = connection.execute(
                "SELECT registered_user_id, registered_at FROM affiliate_clicks"
            ).fetchone()
            assert click == (None, None)
    finally:
        _close(app, thread, client)


def test_registration_attaches_referral_once_and_clears_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, db_path = _running_server(tmp_path, monkeypatch)
    partner_user = accounts.create_user(
        email="partner@example.test",
        display_name="Partner",
        password="partner-account-password",
        subscription_status="inactive",
        db_path=db_path,
    )
    affiliates.create_partner(
        int(partner_user["id"]),
        slug="audit-partner",
        display_name="Audit Partner",
        db_path=db_path,
    )
    _partner, referral_token = affiliates.create_click("audit-partner", db_path=db_path)
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        client.request(
            "POST",
            "/api/register",
            body=json.dumps(
                {
                    "display_name": "Referred Member",
                    "email": "referred@example.test",
                    "password": "referred-member-password",
                }
            ),
            headers={
                "Content-Type": "application/json",
                "Cookie": f"{affiliates.REFERRAL_COOKIE}={referral_token}",
            },
        )
        response = client.getresponse()
        payload = json.loads(response.read())
        cookies = [
            value for key, value in response.getheaders() if key.casefold() == "set-cookie"
        ]

        assert response.status == 201
        assert payload["next"] == "/subscription"
        assert any(accounts.SESSION_COOKIE in value for value in cookies)
        assert any(
            affiliates.REFERRAL_COOKIE in value and "Max-Age=0" in value
            for value in cookies
        )
        user_id = accounts.user_id_for_email("referred@example.test", db_path=db_path)

        client.request(
            "POST",
            "/api/register",
            body=json.dumps(
                {
                    "display_name": "Second Referred Member",
                    "email": "referred-second@example.test",
                    "password": "referred-second-password",
                }
            ),
            headers={
                "Content-Type": "application/json",
                "Cookie": f"{affiliates.REFERRAL_COOKIE}={referral_token}",
            },
        )
        second_response = client.getresponse()
        second_response.read()
        second_user_id = accounts.user_id_for_email(
            "referred-second@example.test", db_path=db_path
        )

        assert second_response.status == 201
        assert second_user_id is not None
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM affiliate_attributions",
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM affiliate_attributions WHERE user_id = ?",
                (second_user_id,),
            ).fetchone()[0] == 0
            click = connection.execute(
                "SELECT registered_user_id FROM affiliate_clicks"
            ).fetchone()
            assert click == (user_id,)
    finally:
        _close(app, thread, client)
