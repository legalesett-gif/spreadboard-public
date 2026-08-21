from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

from spreadboard import accounts, server


def _running_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[server.SpreadBoardServer, threading.Thread, str]:
    password = "correct-horse-battery-staple"
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("SPREADBOARD_ADMIN_PASSWORD", password)
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=tmp_path / "missing.jsonl",
        config={},
        accounts_path=tmp_path / "accounts.sqlite3",
    )
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    return app, thread, password


def test_login_page_has_safe_progressive_enhancement_and_theme() -> None:
    html = server.render_login_page({"next": ["/account"]})

    assert '<form id="loginForm" action="/api/login" method="post">' in html
    assert '<input type="hidden" name="next" value="/account">' in html
    assert 'maxlength="254"' in html
    assert 'maxlength="1024"' in html
    assert "auth-theme-toggle" in html
    assert "data-theme" in html
    assert "try{localStorage.setItem" in html.replace(" ", "")


def test_login_next_path_cannot_break_out_of_the_inline_script() -> None:
    attack = "/</script><script>document.body.dataset.compromised='yes'</script>"
    html = server.render_login_page({"next": [attack]})

    assert attack not in html
    assert "</script><script>document.body.dataset.compromised" not in html
    assert 'name="next" value="/account">' in html


@pytest.mark.parametrize(
    "unsafe",
    (
        "https://attacker.example/",
        "//attacker.example/",
        "/\\attacker.example/",
        "/\x00/account",
    ),
)
def test_login_rejects_nonlocal_or_ambiguous_next_paths(unsafe: str) -> None:
    html = server.render_login_page({"next": [unsafe]})

    assert 'name="next" value="/account">' in html


def test_unknown_account_still_performs_password_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    calls: list[tuple[str, str]] = []
    original = accounts.verify_password

    def recording_verify(password: str, encoded: str) -> bool:
        calls.append((password, encoded))
        return original(password, encoded)

    monkeypatch.setattr(accounts, "verify_password", recording_verify)
    with pytest.raises(ValueError, match="invalid_credentials"):
        accounts.login("unknown@example.test", "not-the-password", db_path=db_path)

    assert len(calls) == 1
    assert calls[0][1].startswith("scrypt$")


def test_html_login_never_places_credentials_in_a_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, password = _running_server(tmp_path, monkeypatch)
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        body = urlencode(
            {
                "email": "admin@example.test",
                "password": "wrong-password-value",
                "next": "/account",
            }
        )
        client.request(
            "POST",
            "/api/login",
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{app.server_port}",
                "Host": f"127.0.0.1:{app.server_port}",
            },
        )
        response = client.getresponse()
        response.read()
        location = response.getheader("Location") or ""

        assert response.status == 303
        assert location == "/login?next=%2Faccount&error=invalid"
        assert "admin" not in location
        assert "password" not in location

        success = urlencode(
            {
                "email": "admin@example.test",
                "password": password,
                "next": "/account",
            }
        )
        client.request(
            "POST",
            "/api/login",
            body=success,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{app.server_port}",
                "Host": f"127.0.0.1:{app.server_port}",
            },
        )
        response = client.getresponse()
        response.read()
        cookie = response.getheader("Set-Cookie") or ""

        assert response.status == 303
        assert response.getheader("Location") == "/account"
        assert accounts.SESSION_COOKIE in cookie
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie

        client.request("GET", "/login?next=%2Faccount", headers={"Cookie": cookie})
        response = client.getresponse()
        response.read()
        assert response.status == 303
        assert response.getheader("Location") == "/account"
    finally:
        client.close()
        app.shutdown()
        app.server_close()
        thread.join(timeout=5)


def test_cross_site_html_login_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, _password = _running_server(tmp_path, monkeypatch)
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    try:
        client.request(
            "POST",
            "/api/login",
            body=urlencode(
                {"email": "admin@example.test", "password": "attacker-controlled"}
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
        assert response.getheader("Set-Cookie") is None
    finally:
        client.close()
        app.shutdown()
        app.server_close()
        thread.join(timeout=5)
