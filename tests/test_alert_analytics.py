"""Why an alert did not arrive is currently invisible.

`check_once` returns three totals and throws everything else away, so a rule
that never fires looks identical whether the condition was not met, the route
had no quote, or Pushover rejected the push. These record the reason alongside
the totals, and how long a triggered alert took from condition to delivery.

The three-key return of `check_once` is deliberately left alone -- several
suites assert it by equality -- so the detail rides on the worker instead.
"""

from __future__ import annotations

import json

from cryptography.fernet import Fernet

from spreadboard import accounts, alerts

ROUTE = "COTI|FUTURES|Gate|Futures|Bybit|Futures"


def _member(db, monkeypatch, *, pushover: bool = True, active: bool = True):
    monkeypatch.setenv("SPREADBOARD_FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    accounts.initialize(db)
    user = accounts.create_user(
        email="analytics@example.test",
        display_name="Analytics",
        password="strong-analytics-password",
        subscription_status="active" if active else "inactive",
        db_path=db,
    )
    if pushover:
        accounts.save_notification_preferences(
            user["id"],
            {"pushover_user_key": "k" * 30, "pushover_enabled": True},
            db_path=db,
        )
    return user


def _rule(db, user, *, threshold: float = 5) -> None:
    accounts.add_market_alert_rule(
        user["id"],
        {
            "route_key": ROUTE,
            "symbol": "COTI",
            "type": "token_spread",
            "direction": "above",
            "threshold": threshold,
            "stability_seconds": 0,
        },
        db_path=db,
    )


def _row(spread: float) -> dict:
    return {
        "route_key": ROUTE,
        "symbol": "COTI",
        "long_venue": "Gate",
        "short_venue": "Bybit",
        "open_spread_pct": spread,
        "age_min": 0.1,
    }


def _worker(tmp_path, db) -> alerts.UserMarketAlertWorker:
    return alerts.UserMarketAlertWorker(
        board_path=tmp_path / "board.json", accounts_path=db, poll_seconds=5
    )


def test_check_once_still_returns_only_its_three_totals(tmp_path, monkeypatch) -> None:
    """Existing suites assert this by equality; analytics must not widen it."""
    db = tmp_path / "accounts.sqlite3"
    user = _member(db, monkeypatch)
    _rule(db, user)
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setattr(
        alerts.api_spreads, "load_spreads", lambda **_k: {"rows": [_row(6.0)]}
    )
    monkeypatch.setattr(alerts, "send_pushover_message", lambda **_k: {"ok": True})

    result = _worker(tmp_path, db).check_once()

    assert set(result) == {"evaluated", "triggered", "delivered"}


def test_a_route_with_no_quote_is_counted_not_silently_dropped(tmp_path, monkeypatch) -> None:
    db = tmp_path / "accounts.sqlite3"
    user = _member(db, monkeypatch)
    _rule(db, user)
    monkeypatch.setattr(alerts.api_spreads, "load_spreads", lambda **_k: {"rows": []})
    monkeypatch.setattr(alerts, "_quote_custom_alert_route", lambda _route: None)

    worker = _worker(tmp_path, db)
    worker.check_once()

    assert worker.last_run["skipped"]["no_value"] == 1
    assert worker.last_run["evaluated"] == 0


def test_an_inactive_subscriber_is_counted_as_skipped(tmp_path, monkeypatch) -> None:
    db = tmp_path / "accounts.sqlite3"
    user = _member(db, monkeypatch, active=False)
    _rule(db, user)
    accounts.update_subscription(user["id"], status="inactive", expires_at=None, db_path=db)
    monkeypatch.setattr(
        alerts.api_spreads, "load_spreads", lambda **_k: {"rows": [_row(6.0)]}
    )

    worker = _worker(tmp_path, db)
    worker.check_once()

    assert worker.last_run["skipped"]["inactive_subscriber"] == 1


def test_an_unmet_condition_is_distinguished_from_a_missing_quote(tmp_path, monkeypatch) -> None:
    db = tmp_path / "accounts.sqlite3"
    user = _member(db, monkeypatch)
    _rule(db, user, threshold=99)
    monkeypatch.setattr(
        alerts.api_spreads, "load_spreads", lambda **_k: {"rows": [_row(6.0)]}
    )

    worker = _worker(tmp_path, db)
    worker.check_once()

    assert worker.last_run["evaluated"] == 1
    assert worker.last_run["triggered"] == 0
    assert worker.last_run["skipped"]["condition_not_met"] == 1
    assert worker.last_run["skipped"]["no_value"] == 0


def test_an_unconfigured_pushover_token_is_named_as_the_reason(tmp_path, monkeypatch) -> None:
    """The live deployment has no app token, so every push silently vanishes."""
    db = tmp_path / "accounts.sqlite3"
    user = _member(db, monkeypatch)
    _rule(db, user)
    monkeypatch.delenv("SPREADBOARD_PUSHOVER_APP_TOKEN", raising=False)
    monkeypatch.setattr(
        alerts.api_spreads, "load_spreads", lambda **_k: {"rows": [_row(6.0)]}
    )

    worker = _worker(tmp_path, db)
    worker.check_once()

    assert worker.last_run["triggered"] == 1
    assert worker.last_run["rejected"]["pushover_unconfigured"] == 1


def test_a_rejected_push_records_its_reason(tmp_path, monkeypatch) -> None:
    db = tmp_path / "accounts.sqlite3"
    user = _member(db, monkeypatch)
    _rule(db, user)
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setattr(
        alerts.api_spreads, "load_spreads", lambda **_k: {"rows": [_row(6.0)]}
    )
    monkeypatch.setattr(
        alerts,
        "send_pushover_message",
        lambda **_k: {"ok": False, "status": 429},
    )

    worker = _worker(tmp_path, db)
    worker.check_once()

    assert worker.last_run["rejected"]["pushover_http_429"] == 1


def test_latency_from_condition_to_delivery_is_measured(tmp_path, monkeypatch) -> None:
    db = tmp_path / "accounts.sqlite3"
    user = _member(db, monkeypatch)
    _rule(db, user)
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setattr(
        alerts.api_spreads, "load_spreads", lambda **_k: {"rows": [_row(6.0)]}
    )
    monkeypatch.setattr(alerts, "send_pushover_message", lambda **_k: {"ok": True})

    worker = _worker(tmp_path, db)
    worker.check_once()

    latency = worker.last_run["latency_seconds"]
    assert latency["samples"] == 1
    assert latency["max"] >= 0
    assert latency["p50"] >= 0


def test_the_status_snapshot_is_written_where_operators_look(tmp_path, monkeypatch) -> None:
    db = tmp_path / "accounts.sqlite3"
    user = _member(db, monkeypatch)
    _rule(db, user)
    monkeypatch.setattr(alerts, "STATUS_PATH", tmp_path / "alert_worker_status.json")
    monkeypatch.setenv("SPREADBOARD_PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setattr(
        alerts.api_spreads, "load_spreads", lambda **_k: {"rows": [_row(6.0)]}
    )
    monkeypatch.setattr(alerts, "send_pushover_message", lambda **_k: {"ok": True})

    worker = _worker(tmp_path, db)
    worker.check_once()
    worker.write_status()

    written = json.loads((tmp_path / "alert_worker_status.json").read_text(encoding="utf-8"))
    assert written["triggered"] == 1
    assert written["generated_at"].endswith("+00:00") or written["generated_at"].endswith("Z")
    assert "skipped" in written and "rejected" in written


def test_the_service_starts_the_alert_and_web_push_workers() -> None:
    """Both deliver notifications and neither reports its own absence.

    The chain watcher had exactly this failure once: wired in server.py's CLI
    main(), which production does not run, so nothing detected a payment.
    """
    import inspect

    from scripts import run_spreadboard_service as service

    main = inspect.getsource(service.main)
    assert "web_push_worker.start()" in main
    assert "market_alert_worker.start()" in main
