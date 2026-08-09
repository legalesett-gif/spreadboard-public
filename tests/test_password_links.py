"""Setting a password from a one-time link.

There was no recovery path at all: no reset, no forgot-password, and no mail
sender. A member who lost their password was locked out permanently, and a new
member could only be created by an admin typing a password -- which means the
admin knows it.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading

import pytest

from spreadboard import accounts


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
    return [u for u in accounts.list_users(db_path=db) if u["email"] == "anatolij@example.com"][0]["id"]


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
    ):
        monkeypatch.delenv(name, raising=False)
    page = server.render_forgot_password_page()
    assert "Email recovery is temporarily unavailable" in page
    assert "Send reset link" in page
    assert " disabled" in page


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
        assert len(sent) == 1
        assert sent[0]["recipient"] == "recover@example.test"
        token = sent[0]["reset_url"].split("token=", 1)[1]
        assert accounts.password_token_status(token, db_path=db_path)["purpose"] == "reset"
    finally:
        client.close()
        app.shutdown()
        app.server_close()
        server.SpreadBoardHandler._login_attempts.clear()
