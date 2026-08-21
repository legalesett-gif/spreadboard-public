from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from spreadboard import accounts, server


@pytest.fixture()
def password_db(tmp_path: Path) -> tuple[Path, int]:
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    user = accounts.create_user(
        email="set-password@example.test",
        display_name="Set Password",
        password="the-original-password",
        subscription_status="inactive",
        db_path=path,
    )
    return path, int(user["id"])


def test_set_password_uses_the_progressive_auth_shell(
    password_db: tuple[Path, int],
) -> None:
    db_path, user_id = password_db
    token = accounts.create_password_token(user_id, purpose="invite", db_path=db_path)

    page = server.render_set_password_page(
        {"token": [token], "next": ["/partner"]}, db_path
    )

    assert page.count("<main") == 1
    assert 'id="themeToggle"' in page
    assert '<noscript><style>.auth-theme-toggle{display:none}' in page
    assert 'action="/api/set-password"' in page
    assert 'method="post"' in page
    assert 'name="next" value="/partner"' in page
    assert page.count('maxlength="1024"') == 2
    assert 'aria-live="polite"' in page
    assert "site-nav" not in page

    invalid = server.render_set_password_page({}, db_path)
    assert invalid.count("<main") == 1
    assert 'href="/forgot-password"' in invalid
    assert 'href="/login"' in invalid


def test_set_password_rejects_script_breakout_in_next(
    password_db: tuple[Path, int],
) -> None:
    db_path, user_id = password_db
    token = accounts.create_password_token(user_id, db_path=db_path)
    marker = "codexSetPasswordBreakout"

    page = server.render_set_password_page(
        {"token": [token], "next": [f"/</script><script>window.{marker}=1</script>"]},
        db_path,
    )

    assert marker not in page
    assert 'name="next" value="/"' in page


def test_plain_html_password_setup_is_origin_bound_and_never_leaks_passwords(
    password_db: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, user_id = password_db
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    server.SpreadBoardHandler._login_attempts.clear()
    _user, old_session = accounts.login(
        "set-password@example.test",
        "the-original-password",
        db_path=db_path,
    )
    token = accounts.create_password_token(user_id, purpose="invite", db_path=db_path)
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=db_path.parent / "missing.jsonl",
        config={},
        accounts_path=db_path,
    )
    threading.Thread(target=app.serve_forever, daemon=True).start()
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    origin = f"http://127.0.0.1:{app.server_port}"

    def post(payload: dict[str, str], request_origin: str) -> tuple[int, str, dict]:
        client.request(
            "POST",
            "/api/set-password",
            body=urlencode(payload),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
                "Origin": request_origin,
            },
        )
        response = client.getresponse()
        raw = response.read()
        parsed = json.loads(raw) if raw else {}
        return response.status, response.getheader("Location") or "", parsed

    try:
        payload = {
            "token": token,
            "next": "/partner",
            "password": "the-new-password-value",
            "confirm": "the-new-password-value",
        }
        status, location, body = post(payload, "https://attacker.example")
        assert (status, body) == (403, {"ok": False, "error": "invalid_request_origin"})
        assert location == ""
        assert accounts.password_token_status(token, db_path=db_path) is not None

        mismatch = {**payload, "confirm": "a-different-password"}
        status, location, body = post(mismatch, origin)
        assert status == 303
        assert body == {}
        mismatch_query = parse_qs(urlparse(location).query)
        assert mismatch_query["error"] == ["mismatch"]
        assert "password" not in mismatch_query
        assert "confirm" not in mismatch_query
        assert accounts.password_token_status(token, db_path=db_path) is not None

        short = {**payload, "password": "short", "confirm": "short"}
        status, location, body = post(short, origin)
        assert status == 303
        assert body == {}
        short_query = parse_qs(urlparse(location).query)
        assert short_query["error"] == ["password"]
        assert "password" not in short_query
        assert "confirm" not in short_query
        assert accounts.password_token_status(token, db_path=db_path) is not None

        status, location, body = post(payload, origin)
        assert (status, location, body) == (303, "/login?next=%2Fpartner", {})
        assert accounts.password_token_status(token, db_path=db_path) is None
        assert accounts.user_for_session(old_session, db_path) is None
        assert accounts.login(
            "set-password@example.test", "the-new-password-value", db_path=db_path
        )
    finally:
        client.close()
        app.shutdown()
        app.server_close()
        server.SpreadBoardHandler._login_attempts.clear()


def test_password_token_is_claimed_under_an_explicit_write_lock(
    password_db: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime SQL trace guards the single-use token against a replay race."""
    db_path, user_id = password_db
    token = accounts.create_password_token(user_id, db_path=db_path)
    statements: list[str] = []
    original_connect = accounts._connect

    def traced_connect(path):
        connection = original_connect(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(accounts, "_connect", traced_connect)

    assert accounts.consume_password_token(
        token, "a-concurrency-safe-password", db_path=db_path
    )
    normalized = [" ".join(statement.upper().split()) for statement in statements]
    begin = next(index for index, statement in enumerate(normalized) if statement == "BEGIN IMMEDIATE")
    select = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("SELECT * FROM PASSWORD_TOKENS")
    )
    assert begin < select
    assert any(
        statement.startswith("UPDATE PASSWORD_TOKENS SET USED_AT")
        and "USED_AT IS NULL" in statement
        for statement in normalized
    )


def test_caddy_preserves_the_application_no_referrer_policy() -> None:
    caddyfile = (Path(__file__).resolve().parents[1] / "Caddyfile").read_text()

    assert 'Referrer-Policy "no-referrer"' in caddyfile
    assert 'Referrer-Policy "strict-origin-when-cross-origin"' not in caddyfile
