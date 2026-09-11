"""A linked trial must unlock the real HTTP gates, including Research Pro."""
import http.client
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from spreadboard import accounts
from spreadboard.server import SpreadBoardHandler, SpreadBoardServer


def test_registered_member_link_trial_and_expiry_through_same_http_session(tmp_path, monkeypatch):
    monkeypatch.setenv('SPREADBOARD_AUTH_REQUIRED', '1')
    monkeypatch.delenv('SPREADBOARD_ADMIN_EMAIL', raising=False)
    monkeypatch.delenv('SPREADBOARD_ADMIN_PASSWORD', raising=False)
    db = tmp_path/'accounts.sqlite3'
    server = SpreadBoardServer(('127.0.0.1', 0), SpreadBoardHandler,
                              board_path=tmp_path/'missing.jsonl', config={}, accounts_path=db)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    try:
        client.request('POST', '/api/register', json.dumps({
            'email': 'http-trial@example.test', 'display_name': 'HTTP Trial',
            'password': 'isolated-trial-test-password',
        }), {'Content-Type': 'application/json'})
        response = client.getresponse()
        assert response.status == 201
        cookie = response.getheader('Set-Cookie')
        response.read()
        headers = {'Cookie': cookie}

        def access():
            codes = []
            for path in ('/funding', '/charts', '/intel', '/playbook'):
                client.request('HEAD', path, headers=headers)
                response = client.getresponse()
                codes.append(response.status)
                response.read()
            return codes

        assert access() == [402]*4
        with sqlite3.connect(db) as con:
            uid = con.execute('SELECT id FROM users WHERE email=?', ('http-trial@example.test',)).fetchone()[0]
        token = accounts.create_telegram_link_token(uid, db_path=db)
        # Simulate the verified private Telegram callback locally. No message
        # or real identity is used; the production binding transaction is real.
        linked = accounts.bind_telegram_chat(token, 987654321, db_path=db)
        assert linked.subscription_status == 'trialing'
        assert access() == [200]*4
        with sqlite3.connect(db) as con:
            con.execute('UPDATE users SET subscription_expires_at=? WHERE id=?',
                        ((datetime.now(UTC)-timedelta(seconds=1)).isoformat(), uid))
        assert access() == [402]*4
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        SpreadBoardHandler._login_attempts.clear()
