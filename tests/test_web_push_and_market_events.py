from __future__ import annotations

import json

import pytest

from spreadboard import accounts, market_events, server, web_push


def _user(db_path, email="push@example.test"):
    return accounts.create_user(
        email=email,
        display_name="Push Member",
        password="strong-browser-push-password",
        subscription_status="active",
        db_path=db_path,
    )


def _subscription(endpoint="https://fcm.googleapis.com/fcm/send/opaque-capability"):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": "A" * 87, "auth": "B" * 22},
    }


def test_web_push_subscription_is_scoped_and_endpoint_allowlisted(tmp_path, monkeypatch):
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    first = _user(db_path)
    second = _user(db_path, "other@example.test")
    saved = accounts.save_web_push_subscription(first["id"], _subscription(), db_path=db_path)
    assert saved["user_id"] == first["id"]
    assert accounts.web_push_subscription_count(first["id"], db_path=db_path) == 1
    assert accounts.web_push_subscription_count(second["id"], db_path=db_path) == 0
    with pytest.raises(ValueError, match="owned_by_another_account"):
        accounts.save_web_push_subscription(second["id"], _subscription(), db_path=db_path)
    with pytest.raises(ValueError, match="invalid_web_push_endpoint"):
        accounts.save_web_push_subscription(
            first["id"], _subscription("https://127.0.0.1/internal"), db_path=db_path
        )


def test_web_push_worker_delivers_only_notifications_created_after_subscription(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = _user(db_path)
    accounts.create_notification(user["id"], title="Old", body="Before opt-in", db_path=db_path)
    accounts.save_web_push_subscription(user["id"], _subscription(), db_path=db_path)
    notification = accounts.create_notification(
        user["id"], title="Route alert", body="Fresh crossing", db_path=db_path
    )
    monkeypatch.setenv("SPREADBOARD_VAPID_PUBLIC_KEY", "public")
    monkeypatch.setenv("SPREADBOARD_VAPID_PRIVATE_KEY", "private")
    monkeypatch.setenv("SPREADBOARD_VAPID_SUBJECT", "mailto:alerts@example.test")
    sent = []
    monkeypatch.setattr(
        web_push,
        "send",
        lambda subscription, item: sent.append(item) or {"ok": True, "permanent": False, "error": None},
    )
    result = web_push.Worker(accounts_path=db_path).check_once()
    assert result == {"pending": 1, "delivered": 1, "failed": 0}
    assert sent[0]["notification_id"] == notification["id"]
    assert accounts.pending_web_push_deliveries(db_path=db_path) == []


def test_browser_test_requires_an_account_owned_subscription(tmp_path, monkeypatch):
    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = _user(db_path)

    with pytest.raises(ValueError, match="web_push_subscription_required"):
        server.queue_web_push_test(user["id"], accounts_path=db_path)

    assert accounts.list_notifications(user["id"], db_path=db_path) == []
    assert "Enable Web Push on this browser before queuing a test." in (
        server.render_account_script()
    )


def test_market_events_validate_sources_and_add_live_rail_blocks(tmp_path):
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "id": "gate-test-limit",
                        "type": "position_limit",
                        "severity": "watch",
                        "token": "TEST",
                        "venue": "Gate",
                        "title": "Position limit changed",
                        "detail": "Check the current tier before sizing.",
                        "source_label": "Gate notice",
                        "source_url": "https://www.gate.com/announcements/example",
                    },
                    {
                        "id": "bad-source",
                        "type": "maintenance",
                        "severity": "watch",
                        "token": "TEST",
                        "title": "Maintenance",
                        "source_url": "http://insecure.example.test/notice",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    events = market_events.events_for_route(
        {
            "token": "TEST",
            "long_venue": "Gate",
            "short_venue": "Mexc",
            "long_market_symbol": "TEST/USDT",
            "short_market_symbol": "TEST/USDT",
            "long_withdraw_enabled": False,
            "short_deposit_enabled": True,
        },
        path=path,
    )
    assert {event["type"] for event in events} == {
        "position_limit",
        "maintenance",
        "rail_closed",
    }
    assert next(event for event in events if event["id"] == "bad-source")["source_url"] is None
    assert next(event for event in events if event["type"] == "rail_closed")["severity"] == "block"


def test_market_event_overlays_and_browser_push_controls_are_visible(monkeypatch, tmp_path):
    row = {
        "market_events": [
            {
                "type": "delisting",
                "severity": "block",
                "title": "Trading ends soon",
                "source_label": "exchange notice",
                "source_url": "https://example.test/notice",
            }
        ]
    }
    assert "delisting" in server.render_market_event_badges(row)
    assert "Trading ends soon" in server.render_pair_market_events(row)

    monkeypatch.delenv("SPREADBOARD_ADMIN_EMAIL", raising=False)
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    created = _user(db_path)
    user = accounts.get_user_object(created["id"], db_path=db_path)
    monkeypatch.setenv("SPREADBOARD_VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("SPREADBOARD_VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("SPREADBOARD_VAPID_SUBJECT", "mailto:alerts@example.test")
    html = server.render_account_settings(user, db_path)
    assert "Browser Push" in html
    assert "Enable on this browser" in html
    assert 'data-vapid-key="public-key"' in html
    assert "private-key" not in html
