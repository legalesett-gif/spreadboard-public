"""Alerts on a token's own price and funding, not on one venue pair.

The spread rules once read field names the board does not emit, so every rule
evaluated to None and silently never fired -- a member could set a threshold,
watch the board cross it, and never be told. These tests follow a rule from the
form to the notification so that cannot happen quietly again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spreadboard import accounts, alerts


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "accounts.sqlite3"
    accounts.initialize(path)
    return path


def _user(db: Path) -> int:
    accounts.create_user(
        email="member@example.com",
        display_name="Member",
        password="a-long-enough-password",
        db_path=db,
    )
    return accounts.list_users(db_path=db)[0]["id"]


BOARD = [
    # One asset quoted by four venues, and one venue printing nonsense.
    {"route_key": "DOGE|A|B", "token": "DOGE", "long_price": 0.20, "short_price": 0.201,
     "funding_24h_pct": 0.05, "long_venue": "Binance", "short_venue": "Gate"},
    {"route_key": "DOGE|C|D", "token": "DOGE", "long_price": 0.199, "short_price": 9.99,
     "funding_24h_pct": 0.12, "long_venue": "Mexc", "short_venue": "Bybit"},
    {"route_key": "PEPE|A|B", "token": "PEPE", "long_price": 0.000012,
     "funding_24h_pct": -0.30, "long_venue": "Gate", "short_venue": "Bitget"},
]


def test_the_token_price_is_the_median_so_one_bad_quote_cannot_move_it() -> None:
    """A single dislocated print has already become a headline on this board."""
    metrics = alerts.token_metrics(BOARD)

    # Legs are 0.20, 0.201, 0.199 and 9.99 -- the median ignores the outlier.
    assert metrics["DOGE"]["token_price"] == pytest.approx(0.2005, abs=1e-6)
    assert metrics["PEPE"]["token_price"] == pytest.approx(0.000012)


def test_token_funding_is_the_best_carry_available_on_the_asset() -> None:
    """That is the one a member would actually put on."""
    metrics = alerts.token_metrics(BOARD)

    assert metrics["DOGE"]["token_funding_24h_pct"] == pytest.approx(0.12)
    assert metrics["PEPE"]["token_funding_24h_pct"] == pytest.approx(-0.30)


def test_a_price_rule_is_stored_against_the_token_not_a_route(db: Path) -> None:
    user_id = _user(db)

    rule = accounts.add_market_alert_rule(
        user_id,
        {"type": "price", "symbol": "doge", "direction": "above", "threshold": 0.25},
        db_path=db,
    )

    assert rule["metric"] == "token_price"
    assert rule["route_key"] == "TOKEN:DOGE"
    assert rule["operator"] == "gte"
    assert accounts.token_from_alert_key(rule["route_key"]) == "DOGE"


def test_a_token_rule_needs_no_route(db: Path) -> None:
    """The whole point: a member should not have to pick a venue pair."""
    user_id = _user(db)

    rule = accounts.add_market_alert_rule(
        user_id,
        {"type": "token_funding", "symbol": "PEPE", "direction": "below",
         "threshold": -0.2},
        db_path=db,
    )

    assert rule["metric"] == "token_funding_24h_pct"
    assert rule["operator"] == "lte"

    # ...while a route rule still insists on one.
    with pytest.raises(ValueError):
        accounts.add_market_alert_rule(
            user_id,
            {"type": "token_spread", "symbol": "DOGE", "threshold": 5},
            db_path=db,
        )


def test_a_price_rule_fires_and_says_the_number(db: Path, monkeypatch) -> None:
    """Follows one rule from the form to the notification."""
    user_id = _user(db)
    accounts.update_subscription(
        user_id, status="active", expires_at="2099-01-01T00:00:00+00:00", db_path=db
    )
    accounts.add_market_alert_rule(
        user_id,
        {"type": "price", "symbol": "DOGE", "direction": "above", "threshold": 0.15},
        db_path=db,
    )

    monkeypatch.setattr(
        alerts.api_spreads, "load_spreads", lambda **_kwargs: {"rows": BOARD}
    )

    worker = alerts.UserMarketAlertWorker(
        board_path=Path("board.json"), accounts_path=db, poll_seconds=10
    )
    summary = worker.check_once()

    assert summary["evaluated"] >= 1
    notifications = accounts.list_notifications(user_id, db_path=db)
    assert notifications, "the rule crossed its threshold and told nobody"
    body = notifications[0]["body"]
    assert "DOGE" in body and "price" in body
    assert "0.2005" in body or "0.2" in body


def test_a_rule_that_has_not_crossed_stays_quiet(db: Path, monkeypatch) -> None:
    user_id = _user(db)
    accounts.update_subscription(
        user_id, status="active", expires_at="2099-01-01T00:00:00+00:00", db_path=db
    )
    accounts.add_market_alert_rule(
        user_id,
        {"type": "price", "symbol": "DOGE", "direction": "above", "threshold": 5.0},
        db_path=db,
    )

    monkeypatch.setattr(
        alerts.api_spreads, "load_spreads", lambda **_kwargs: {"rows": BOARD}
    )

    worker = alerts.UserMarketAlertWorker(
        board_path=Path("board.json"), accounts_path=db, poll_seconds=10
    )
    worker.check_once()

    assert not accounts.list_notifications(user_id, db_path=db)


def test_an_old_database_accepts_the_new_metrics(tmp_path: Path) -> None:
    """The metric column carried a CHECK, and SQLite cannot alter one in place.

    Without the rebuild a member would fill in the form and get a constraint
    error at insert time.
    """
    import sqlite3

    path = tmp_path / "old.sqlite3"
    accounts.initialize(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE market_alert_rules")
        connection.execute(
            """
            CREATE TABLE market_alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                route_key TEXT NOT NULL, symbol TEXT NOT NULL,
                metric TEXT NOT NULL CHECK (metric IN ('open_spread_pct', 'funding_24h_pct')),
                operator TEXT NOT NULL CHECK (operator IN ('lte', 'gte')),
                threshold REAL NOT NULL, stability_seconds INTEGER NOT NULL DEFAULT 10,
                enabled INTEGER NOT NULL DEFAULT 1, condition_since TEXT,
                last_condition_met INTEGER NOT NULL DEFAULT 0, last_triggered_at TEXT,
                last_value REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    user_id = _user(path)
    accounts.add_market_alert_rule(
        user_id,
        {"type": "token_spread", "symbol": "OLD", "route_key": "OLD|A|B", "threshold": 1},
        db_path=path,
    )

    # Re-running initialize performs the rebuild.
    accounts.initialize(path)

    rule = accounts.add_market_alert_rule(
        user_id,
        {"type": "price", "symbol": "DOGE", "threshold": 0.25},
        db_path=path,
    )
    assert rule["metric"] == "token_price"
    # ...and the rule stored before the rebuild survived it.
    assert any(r["symbol"] == "OLD" for r in accounts.list_market_alert_rules(user_id, db_path=path))


def test_the_alerts_page_offers_a_token_form() -> None:
    """Price was listed in the type filter but was a template that never fired."""
    source = Path("spreadboard/server.py").read_text(encoding="utf-8")

    assert 'id="tokenAlertForm"' in source
    assert '<option value="price">' in source
    assert '<option value="token_funding">' in source
    # It must post the token type through, not a route.
    assert "type: data.get('type')" in source


def test_a_bad_rule_is_a_bad_request_not_a_server_error() -> None:
    import inspect

    from spreadboard import server

    handler = inspect.getsource(server.SpreadBoardHandler.do_POST)
    block = handler.split('/api/market-alert-rules"', 1)[1][:600]
    assert "BAD_REQUEST" in block
