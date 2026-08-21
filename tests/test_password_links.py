"""Setting a password from a one-time link.

There was no recovery path at all: no reset, no forgot-password, and no mail
sender. A member who lost their password was locked out permanently, and a new
member could only be created by an admin typing a password -- which means the
admin knows it.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from spreadboard import accounts, server


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    accounts.create_user(
        email="anatolij@example.com",
        display_name="Anatolij",
        password="a-long-enough-password",
        db_path=path,
    )
    return path


def _user_id(db: Path) -> int:
    return next(
        u for u in accounts.list_users(db_path=db) if u["email"] == "anatolij@example.com"
    )["id"]


def test_a_link_lets_someone_set_their_own_password(db: Path) -> None:
    user_id = _user_id(db)
    token = accounts.create_password_token(user_id, db_path=db)

    assert accounts.password_token_status(token, db_path=db)["display_name"] == "Anatolij"
    assert accounts.consume_password_token(token, "brand-new-password-1", db_path=db) is not None

    # The new password works, the old one does not.
    assert accounts.login("anatolij@example.com", "brand-new-password-1", db_path=db)
    with pytest.raises(ValueError):
        accounts.login("anatolij@example.com", "a-long-enough-password", db_path=db)


def test_a_link_works_exactly_once(db: Path) -> None:
    token = accounts.create_password_token(_user_id(db), db_path=db)

    assert accounts.consume_password_token(token, "first-password-here", db_path=db) is not None
    assert accounts.consume_password_token(token, "second-password-here", db_path=db) is None
    assert accounts.password_token_status(token, db_path=db) is None


def test_minting_a_new_link_retires_the_old_one(db: Path) -> None:
    user_id = _user_id(db)
    first = accounts.create_password_token(user_id, db_path=db)
    second = accounts.create_password_token(user_id, db_path=db)

    assert accounts.password_token_status(first, db_path=db) is None
    assert accounts.password_token_status(second, db_path=db) is not None


def test_an_expired_link_is_refused(db: Path) -> None:
    import sqlite3

    user_id = _user_id(db)
    token = accounts.create_password_token(user_id, db_path=db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE password_tokens SET expires_at = ? WHERE user_id = ?",
            ("2000-01-01T00:00:00+00:00", user_id),
        )
        connection.commit()
    finally:
        connection.close()

    assert accounts.password_token_status(token, db_path=db) is None
    assert accounts.consume_password_token(token, "another-password-here", db_path=db) is None


def test_spending_a_link_ends_every_existing_session(db: Path) -> None:
    """If the link was needed because the password leaked, old sessions must go."""
    user_id = _user_id(db)
    _user, token_value = accounts.login(
        "anatolij@example.com", "a-long-enough-password", db_path=db
    )
    assert accounts.user_for_session(token_value, db) is not None

    accounts.consume_password_token(
        accounts.create_password_token(user_id, db_path=db), "replacement-password", db_path=db
    )

    assert accounts.user_for_session(token_value, db) is None


def test_a_short_password_is_refused_before_anything_is_written(db: Path) -> None:
    user_id = _user_id(db)
    token = accounts.create_password_token(user_id, db_path=db)

    with pytest.raises(ValueError):
        accounts.consume_password_token(token, "short", db_path=db)

    # The link survives a rejected attempt, so the person can try again.
    assert accounts.password_token_status(token, db_path=db) is not None


def test_a_garbage_token_is_simply_invalid(db: Path) -> None:
    assert accounts.password_token_status("nonsense", db_path=db) is None
    assert accounts.consume_password_token("nonsense", "a-valid-password-x", db_path=db) is None
    assert accounts.consume_password_token("", "a-valid-password-x", db_path=db) is None


def test_the_raw_token_is_never_stored(db: Path) -> None:
    import sqlite3

    token = accounts.create_password_token(_user_id(db), db_path=db)
    connection = sqlite3.connect(db)
    try:
        stored = [r[0] for r in connection.execute("SELECT token_hash FROM password_tokens")]
    finally:
        connection.close()

    assert stored and token not in stored


def test_invited_admin_chooses_their_own_password(db: Path) -> None:
    created, token = accounts.create_invited_user(
        email="alex@spreadarbitrage.ink",
        display_name="Alex",
        role="admin",
        db_path=db,
    )

    assert created["is_admin"] is True
    assert created["subscription_active"] is True
    assert accounts.password_token_status(token, db_path=db)["display_name"] == "Alex"
    assert accounts.consume_password_token(
        token, "alex-chooses-this-password", db_path=db
    ) is not None
    assert accounts.login(
        "alex@spreadarbitrage.ink", "alex-chooses-this-password", db_path=db
    )


def test_password_setup_page_uses_the_product_form_layout(db: Path) -> None:
    from spreadboard import server

    token = accounts.create_password_token(_user_id(db), purpose="invite", db_path=db)
    page = server.render_set_password_page({"token": [token]}, db)

    assert 'data-password-setup' in page
    assert '.set-password-panel form' in page


def test_partner_password_setup_returns_to_the_partner_cabinet(db: Path) -> None:
    token = accounts.create_password_token(_user_id(db), purpose="invite", db_path=db)

    page = server.render_set_password_page(
        {"token": [token], "next": ["/partner"]}, db
    )

    assert 'window.location.assign("/login?next=%2Fpartner")' in page
    assert '@media(max-width:560px)' in page


def test_the_page_and_endpoints_are_reachable_without_a_session() -> None:
    """The person using the link cannot sign in yet -- that is the point."""
    import inspect

    from spreadboard import server

    gate = inspect.getsource(server.SpreadBoardHandler._authorize)
    assert '"/set-password"' in gate
    assert '"/api/set-password"' in gate
    assert '"/forgot-password"' in gate
    assert '"/api/request-password-reset"' in gate


def test_email_lookup_is_case_insensitive_and_returns_no_profile(db: Path) -> None:
    assert accounts.user_id_for_email("ANATOLIJ@EXAMPLE.COM", db_path=db) == _user_id(db)
    assert accounts.user_id_for_email("missing@example.com", db_path=db) is None


def test_forgot_password_page_fails_closed_until_email_is_configured(monkeypatch) -> None:
    from spreadboard import server

    for name in (
        "SPREADBOARD_SMTP_HOST", "SPREADBOARD_SMTP_USERNAME",
        "SPREADBOARD_SMTP_PASSWORD", "SPREADBOARD_SMTP_FROM",
        "SPREADBOARD_RESEND_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    page = server.render_forgot_password_page()
    assert "Email recovery is temporarily unavailable" in page
    assert "Send reset link" in page
    assert " disabled" in page


def test_forgot_password_page_supports_both_themes_and_never_falls_back_to_get(
    monkeypatch,
) -> None:
    """A script failure must not put an account email into URL history."""
    monkeypatch.setenv("SPREADBOARD_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SPREADBOARD_SMTP_FROM", "support@example.test")

    page = server.render_forgot_password_page()

    assert 'data-theme="dark"' in page or "dataset.theme" in page
    assert 'id="themeToggle"' in page
    assert 'aria-label="Toggle light and dark mode"' in page
    assert ':root[data-theme="dark"]' in page
    assert 'action="/api/request-password-reset"' in page
    assert 'method="post"' in page
    assert "try{localStorage.setItem" in page
    assert "recovery_temporarily_busy" in page
    assert "owner must finish SMTP setup" not in page


def test_plain_html_reset_form_returns_to_a_generic_confirmation(
    tmp_path, monkeypatch
) -> None:
    """Progressive enhancement must work without exposing the address in a URL."""
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SPREADBOARD_SMTP_FROM", "support@example.test")
    db_path = tmp_path / "accounts.sqlite3"
    monkeypatch.setattr(server, "_deliver_password_reset", lambda *_args, **_kwargs: None)
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
        client.request(
            "POST",
            "/api/request-password-reset",
            body=urlencode({"email": "recover@example.test"}),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
            },
        )
        response = client.getresponse()
        response.read()
        assert response.status == 303
        assert response.getheader("Location") == "/forgot-password?requested=1"
        assert "recover" not in str(response.getheader("Location"))
        page = server.render_forgot_password_page(requested=True)
        assert "If that account exists, a reset link has been sent." in page
        assert 'role="status"' in page
    finally:
        client.close()
        app.shutdown()
        app.server_close()


def test_reset_rate_limit_uses_the_caddy_forwarded_client_ip(tmp_path, monkeypatch) -> None:
    """Ten visitors behind Caddy must not consume one global reset allowance."""
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SPREADBOARD_SMTP_FROM", "support@example.test")
    db_path = tmp_path / "accounts.sqlite3"
    calls = []
    monkeypatch.setattr(
        server,
        "_deliver_password_reset",
        lambda email, **_kwargs: calls.append(email),
    )
    server.SpreadBoardHandler._login_attempts.clear()
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
        for index in range(10):
            client.request(
                "POST",
                "/api/request-password-reset",
                body=json.dumps({"email": f"first-{index}@example.test"}),
                headers={
                    "Content-Type": "application/json",
                    "X-Forwarded-For": "198.51.100.10",
                },
            )
            response = client.getresponse()
            assert response.status == 202
            response.read()
        client.request(
            "POST",
            "/api/request-password-reset",
            body=json.dumps({"email": "other-visitor@example.test"}),
            headers={
                "Content-Type": "application/json",
                "X-Forwarded-For": "198.51.100.11",
            },
        )
        response = client.getresponse()
        assert response.status == 202
        response.read()
        deadline = time.monotonic() + 2
        while len(calls) < 11 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) == 11
    finally:
        client.close()
        app.shutdown()
        app.server_close()
        server.SpreadBoardHandler._login_attempts.clear()


def test_forwarded_ip_is_trusted_only_from_a_private_reverse_proxy() -> None:
    assert server.proxy_client_ip("172.20.0.5", "198.51.100.7") == "198.51.100.7"
    assert server.proxy_client_ip("127.0.0.1", "198.51.100.8") == "198.51.100.8"
    assert server.proxy_client_ip("172.20.0.5", "1.1.1.1, 198.51.100.9") == (
        "198.51.100.9"
    )
    assert server.proxy_client_ip("8.8.8.8", "1.1.1.1") == "8.8.8.8"
    assert server.proxy_client_ip("172.20.0.5", "not-an-ip") == "172.20.0.5"


def test_password_reset_executor_has_a_hard_pending_bound(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_PASSWORD_RESET_WORKERS", "1")
    monkeypatch.setenv("SPREADBOARD_PASSWORD_RESET_MAX_PENDING", "2")
    started = threading.Event()
    release = threading.Event()

    def blocked_delivery(*_args, **_kwargs):
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(server, "_deliver_password_reset", blocked_delivery)
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=tmp_path / "accounts.sqlite3",
    )
    try:
        assert app.submit_password_reset("first@example.test") is True
        assert started.wait(2)
        assert app.submit_password_reset("second@example.test") is True
        assert app.submit_password_reset("overflow@example.test") is False
    finally:
        release.set()
        app.server_close()


def test_password_reset_queue_overflow_is_visible_but_account_agnostic(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SPREADBOARD_SMTP_FROM", "support@example.test")
    monkeypatch.setenv("SPREADBOARD_PASSWORD_RESET_WORKERS", "1")
    monkeypatch.setenv("SPREADBOARD_PASSWORD_RESET_MAX_PENDING", "1")
    started = threading.Event()
    release = threading.Event()

    def blocked_delivery(*_args, **_kwargs):
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(server, "_deliver_password_reset", blocked_delivery)
    server.SpreadBoardHandler._login_attempts.clear()
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=tmp_path / "accounts.sqlite3",
    )
    threading.Thread(target=app.serve_forever, daemon=True).start()
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        assert app.submit_password_reset("occupy@example.test") is True
        assert started.wait(2)
        client.request(
            "POST",
            "/api/request-password-reset",
            body=json.dumps({"email": "any-account@example.test"}),
            headers={"Content-Type": "application/json"},
        )
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 503
        assert payload == {"ok": False, "error": "recovery_temporarily_busy"}
    finally:
        client.close()
        release.set()
        app.shutdown()
        app.server_close()
        server.SpreadBoardHandler._login_attempts.clear()


def test_reset_response_does_not_wait_for_delivery_and_keeps_provider_evidence(
    tmp_path, monkeypatch
) -> None:
    """Known accounts must not be enumerable from the outbound-mail latency."""
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SPREADBOARD_SMTP_FROM", "support@example.test")
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    accounts.create_user(
        email="recover@example.test",
        display_name="Recover",
        password="original-password-long",
        db_path=db_path,
    )
    delivery_started = threading.Event()
    release_delivery = threading.Event()

    def slow_delivery(**_kwargs):
        delivery_started.set()
        assert release_delivery.wait(5)
        return "resend-password-reset-123"

    monkeypatch.setattr(server.mailer, "send_password_reset", slow_delivery)
    server.SpreadBoardHandler._login_attempts.clear()
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=db_path,
    )
    result = {}

    def request_reset() -> None:
        client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
        try:
            client.request(
                "POST",
                "/api/request-password-reset",
                body=json.dumps({"email": "recover@example.test"}),
                headers={"Content-Type": "application/json"},
            )
            response = client.getresponse()
            result.update(status=response.status, payload=json.loads(response.read()))
        finally:
            client.close()

    threading.Thread(target=app.serve_forever, daemon=True).start()
    request_thread = threading.Thread(target=request_reset)
    request_thread.start()
    try:
        assert delivery_started.wait(2)
        request_thread.join(0.2)
        assert not request_thread.is_alive(), "the generic HTTP response waited on email delivery"
        assert result == {
            "status": 202,
            "payload": {
                "ok": True,
                "message": "If that account exists, a reset link will be sent.",
            },
        }
    finally:
        release_delivery.set()
        request_thread.join(5)
        app.shutdown()
        app.server_close()
        server.SpreadBoardHandler._login_attempts.clear()

    deadline = time.monotonic() + 2
    evidence = accounts.password_reset_delivery_health(db_path=db_path)
    while evidence.get("status") != "sent" and time.monotonic() < deadline:
        time.sleep(0.01)
        evidence = accounts.password_reset_delivery_health(db_path=db_path)
    assert evidence == {
        "status": "sent",
        "provider": "smtp",
        "requested_at": evidence["requested_at"],
        "finished_at": evidence["finished_at"],
        "error_type": "",
    }


def test_password_recovery_status_requires_delivery_evidence_and_reports_latest_failure(
    db: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        server.mailer,
        "status",
        lambda: {"configured": True, "provider": "resend_api"},
    )

    assert server.password_recovery_status(db)["status"] == "setup_needed"

    delivery_id = accounts.queue_password_reset_delivery(
        _user_id(db), provider="resend_api", db_path=db
    )
    accounts.finish_password_reset_delivery(
        delivery_id,
        delivered=True,
        message_id="provider-message-id",
        db_path=db,
    )
    assert server.password_recovery_status(db) == {
        "status": "operational",
        "detail": "Password-recovery provider accepted the latest message",
        "last_delivery_at": server.password_recovery_status(db)["last_delivery_at"],
    }

    failed_id = accounts.queue_password_reset_delivery(
        _user_id(db), provider="resend_api", db_path=db
    )
    accounts.finish_password_reset_delivery(
        failed_id,
        delivered=False,
        error_type="HTTPError",
        db_path=db,
    )
    public = server.password_recovery_status(db)
    assert public["status"] == "degraded"
    assert public["detail"] == "Latest password-recovery delivery failed"
    assert "provider-message-id" not in json.dumps(public)


def test_password_recovery_status_fails_closed_for_stalled_or_stale_evidence(
    db: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        server.mailer,
        "status",
        lambda: {"configured": True, "provider": "resend_api"},
    )
    user_id = _user_id(db)
    queued = accounts.queue_password_reset_delivery(
        user_id, provider="resend_api", db_path=db
    )
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE password_reset_deliveries SET requested_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", queued),
        )
        connection.commit()
    finally:
        connection.close()
    stalled = server.password_recovery_status(db)
    assert stalled["status"] == "degraded"
    assert stalled["detail"] == "Latest password-recovery delivery stalled"

    sent = accounts.queue_password_reset_delivery(
        user_id, provider="resend_api", db_path=db
    )
    accounts.finish_password_reset_delivery(
        sent, delivered=True, message_id="accepted-id", db_path=db
    )
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE password_reset_deliveries SET finished_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", sent),
        )
        connection.commit()
    finally:
        connection.close()
    stale = server.password_recovery_status(db)
    assert stale["status"] == "setup_needed"
    assert stale["detail"] == "Latest password-recovery provider acceptance is stale"


def test_password_recovery_status_does_not_reuse_evidence_from_an_old_provider(
    db: Path, monkeypatch
) -> None:
    delivery_id = accounts.queue_password_reset_delivery(
        _user_id(db), provider="smtp", db_path=db
    )
    accounts.finish_password_reset_delivery(
        delivery_id, delivered=True, db_path=db
    )
    monkeypatch.setattr(
        server.mailer,
        "status",
        lambda: {"configured": True, "provider": "resend_api"},
    )

    status = server.password_recovery_status(db)
    assert status["status"] == "setup_needed"
    assert status["detail"] == "Current password-recovery provider is unverified"


def test_status_page_shows_when_recovery_was_last_accepted() -> None:
    page = server.render_status_page(
        {
            "checked_at": "2026-08-21T19:05:00+00:00",
            "components": {
                "email_recovery": {
                    "status": "operational",
                    "detail": "Password-recovery provider accepted the latest message",
                    "last_delivery_at": "2026-08-21T19:00:00+00:00",
                }
            },
        }
    )
    assert "Password-recovery provider accepted the latest message" in page
    assert "21 Aug 2026 · 19:00 UTC" in page


def test_reset_request_is_generic_and_sends_a_single_use_link(tmp_path, monkeypatch) -> None:
    from spreadboard import mailer, server

    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SPREADBOARD_SMTP_FROM", "support@example.test")
    monkeypatch.setenv("SPREADBOARD_PUBLIC_URL", "https://spreadboard.example")
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    accounts.create_user(
        email="recover@example.test", display_name="Recover",
        password="original-password-long", subscription_status="inactive", db_path=db_path,
    )
    sent = []
    monkeypatch.setattr(mailer, "send_password_reset", lambda **kwargs: sent.append(kwargs))
    server.SpreadBoardHandler._login_attempts.clear()
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0), server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl", config={}, accounts_path=db_path,
    )
    threading.Thread(target=app.serve_forever, daemon=True).start()
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        responses = []
        for email in ("missing@example.test", "recover@example.test"):
            client.request(
                "POST", "/api/request-password-reset",
                body=json.dumps({"email": email}), headers={"Content-Type": "application/json"},
            )
            response = client.getresponse()
            responses.append((response.status, json.loads(response.read())))
        assert responses[0] == responses[1]
        assert responses[0][0] == 202
        deadline = time.monotonic() + 2
        while len(sent) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(sent) == 1
        assert sent[0]["recipient"] == "recover@example.test"
        token = sent[0]["reset_url"].split("token=", 1)[1]
        assert accounts.password_token_status(token, db_path=db_path)["purpose"] == "reset"
    finally:
        client.close()
        app.shutdown()
        app.server_close()
        server.SpreadBoardHandler._login_attempts.clear()


def test_resend_https_delivery_is_preferred_over_smtp(monkeypatch) -> None:
    from spreadboard import mailer

    monkeypatch.setenv("SPREADBOARD_SMTP_HOST", "smtp.resend.com")
    monkeypatch.setenv("SPREADBOARD_SMTP_FROM", "SpreadBoard <support@example.test>")
    monkeypatch.setenv("SPREADBOARD_RESEND_API_KEY", "re_test_only_not_a_real_secret")
    sent = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, *, timeout):
        sent.append((request, timeout))
        return Response()

    monkeypatch.setattr(mailer, "urlopen", fake_urlopen)
    mailer.send_password_reset(
        recipient="alex@example.test",
        display_name="Alex",
        reset_url="https://spreadboard.example/set-password?token=one-use",
    )

    assert mailer.status()["provider"] == "resend_api"
    assert len(sent) == 1
    request, timeout = sent[0]
    assert request.full_url == "https://api.resend.com/emails"
    assert timeout == 10
    assert request.get_header("Authorization") == "Bearer re_test_only_not_a_real_secret"
    payload = json.loads(request.data)
    assert payload["to"] == ["alex@example.test"]
    assert payload["from"] == "SpreadBoard <support@example.test>"
    assert payload["subject"] == "Reset your SpreadBoard password"
    assert parse_qs(urlparse(payload["text"].splitlines()[4]).query)["token"] == ["one-use"]
