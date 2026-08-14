from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from spreadboard import (
    accounts,
    fast_quotes,
    live_book_cache,
    portfolio,
    portfolio_funding,
    position_markets,
    server,
)
from scripts.websocket_book_worker import (
    _desired_legs,
    _install_ccxt_client_reset_compat,
)


def _catalogue() -> dict:
    return {
        "markets": [
            {
                "token": "ESPORTS",
                "venue": "OKX DEX 56",
                "market_type": "Spot",
                "symbol": "ESPORTS",
                "dex_chain": "56",
                "dex_contract": "0xf39e4b21c84e737df08e2c3b32541d856f508e48",
            },
            {
                "token": "ESPORTS",
                "venue": "Gate",
                "market_type": "Futures",
                "symbol": "ESPORTS/USDT:USDT",
                "contract_size": 100.0,
            },
        ]
    }


def _dex_position() -> dict:
    return {
        "id": 7,
        "status": "open",
        "route_key": "ESPORTS|OKX DEX 56|DEX|Gate|Futures",
        "token": "ESPORTS",
        "long_venue": "OKX DEX 56",
        "long_market_type": "DEX",
        "long_symbol": "ESPORTS",
        "long_quantity": 1000,
        "long_entry_price": 0.015,
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_symbol": "ESPORTS/USDT:USDT",
        "short_quantity": 1000,
        "short_entry_price": 0.016,
        "entry_fees_usd": 0,
        "capital_usd": 100,
        "opened_at": "2026-08-08T22:37:00Z",
        "funding_cashflows": [],
        "alert_rules": [],
    }


def test_saved_dex_label_resolves_to_exact_catalogue_spot_adapter() -> None:
    position = _dex_position()
    current = {
        "route_key": "ESPORTS|OKX DEX 56|Spot|Gate|Futures",
        "token": "ESPORTS",
        "long_venue": "OKX DEX 56",
        "long_market_type": "Spot",
        # Older canonical DEX rows omit this field; identity is in the catalogue.
        "long_market_symbol": None,
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": "ESPORTS/USDT:USDT",
        "long_bid": 0.0151,
        "long_ask": 0.0152,
        "short_bid": 0.0155,
        "short_ask": 0.0156,
    }

    result = position_markets.resolve_position_route(position, [current], catalogue=_catalogue())

    assert result["listing_status"] == "listed"
    assert result["current_row"] is current
    assert result["history_route_key"] == ("ESPORTS|OKX DEX 56|Spot|Gate|Futures")
    assert result["chart_route_key"].startswith("CUSTOM:")
    route = result["canonical_route"]
    assert route["long_market_type"] == "Spot"
    assert route["dex_chain"] == "56"
    assert route["dex_contract"] == "0xf39e4b21c84e737df08e2c3b32541d856f508e48"


def test_position_match_never_substitutes_a_different_saved_symbol() -> None:
    position = {
        **_dex_position(),
        "token": "GUA",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_symbol": "GUA/USDT",
        "short_symbol": "GUA/USDT:USDT",
    }
    wrong = {
        "token": "GUA",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_market_symbol": "1000GUA/USDT",
        "short_venue": "Gate",
        "short_market_type": "Futures",
        "short_market_symbol": "GUA/USDT:USDT",
    }
    catalogue = {
        "markets": [
            {"token": "GUA", "venue": "Mexc", "market_type": "Spot", "symbol": "GUA/USDT"},
            {"token": "GUA", "venue": "Gate", "market_type": "Futures", "symbol": "GUA/USDT:USDT"},
        ]
    }

    result = position_markets.resolve_position_route(position, [wrong], catalogue=catalogue)

    assert result["listing_status"] == "listed"
    assert result["current_row"] is None


def test_listed_position_is_refreshing_not_claimed_market_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(portfolio, "_history_quote", lambda _key: None)
    result = portfolio._hydrate_position(
        _dex_position(),
        [],
        books={},
        funding_legs={},
        catalogue=_catalogue(),
    )

    assert result["market_listing_status"] == "listed"
    assert result["quote_status"] == "refreshing"
    assert result["quote_refresh_needed"] is True
    assert result["chart_route_key"].startswith("CUSTOM:")
    assert "Markets listed · refreshing" in server.render_position_card(result)


def test_reference_history_does_not_mark_a_full_position(monkeypatch) -> None:
    now_us = int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000)
    monkeypatch.setattr(
        portfolio,
        "_history_quote",
        lambda _key: {
            "long_bid": 0.0151,
            "long_ask": 0.0152,
            "short_bid": 0.0155,
            "short_ask": 0.0156,
            "long_quote_ts_us": now_us,
            "short_quote_ts_us": now_us,
            "quote_ts_us": now_us,
            "position_quote_source": "live_chart_exact_route",
        },
    )

    result = portfolio._hydrate_position(
        _dex_position(),
        [],
        books={},
        funding_legs={},
        catalogue=_catalogue(),
    )

    assert result["quote_status"] == "refreshing"
    assert result["long_mark_price"] is None
    assert result["short_mark_price"] is None
    assert result["price_pnl_usd"] is None


def test_position_marks_use_reference_prices_without_exit_impact() -> None:
    position = {
        **_dex_position(),
        "user_id": 9,
        "entry_fees_usd": 1.0,
        "borrow_costs_usd": 2.0,
        "gas_costs_usd": 3.0,
        "transfer_costs_usd": 4.0,
        "slippage_costs_usd": 5.0,
    }
    now = datetime.now(tz=timezone.utc)
    now_us = int(now.timestamp() * 1_000_000)
    snapshot = {
        "schema": portfolio_funding.SCHEMA,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "positions": {
            "9:7": {
                "status": "ok",
                "position_fingerprint": portfolio_funding.position_fingerprint(position),
                "amount_usd": "2.5",
                "event_count": 2,
                "synced_at": now.isoformat().replace("+00:00", "Z"),
                "marks": {
                    "long": {
                        "status": "ok",
                        "source": "dexscreener_exact_contract_pool",
                        "basis": "dex_pool_reference",
                        "quantity": "1000",
                        "price_usd": "0.0148",
                        "quoted_at": now.isoformat().replace("+00:00", "Z"),
                    }
                },
            }
        },
    }
    books = {
        "Gate|Futures|ESPORTS/USDT:USDT": live_book_cache.CachedBook(
            bids=[[0.0157, 5.0], [0.0156, 5.0]],
            asks=[[0.0158, 4.0], [0.0159, 6.0]],
            quote_ts_us=now_us,
        )
    }

    result = portfolio._hydrate_position(
        position,
        [],
        books=books,
        funding_legs={},
        catalogue=_catalogue(),
        funding_snapshot=snapshot,
    )

    assert result["quote_status"] == "live"
    assert result["long_mark_price"] == pytest.approx(0.0148)
    assert result["short_mark_price"] == pytest.approx(0.01575)
    assert result["current_marked_spread_pct"] == pytest.approx((0.01575 / 0.0148 - 1) * 100)
    assert result["current_exit_spread_pct"] == result["current_marked_spread_pct"]
    assert result["funding_income_usd"] == pytest.approx(2.5)
    assert result["fees_usd"] == pytest.approx(1.0)
    assert result["other_costs_usd"] == pytest.approx(9.0)
    assert result["total_costs_usd"] == pytest.approx(10.0)
    assert result["slippage_costs_usd"] == pytest.approx(5.0)
    assert result["slippage_included_in_fills"] is True
    assert result["total_pnl_usd"] == pytest.approx(result["price_pnl_usd"] + 2.5 - 10.0)
    assert result["quote_source"] == ("dexscreener_exact_contract_pool+resident_book_midpoint")
    assert result["long_mark_basis"] == "dex_pool_reference"
    assert result["short_mark_basis"] == "bid_ask_midpoint"


def test_reference_book_uses_best_bid_and_ask_without_walking_depth() -> None:
    levels = [[10.0, 2.0], [11.0, 3.0]]

    assert portfolio._best_price(levels) == pytest.approx(10.0)
    assert portfolio._midpoint(10.0, 11.0) == pytest.approx(10.5)
    assert portfolio._midpoint(11.0, 10.0) is None


def test_closed_position_labels_stored_exit_fills() -> None:
    position = {
        **_dex_position(),
        "status": "closed",
        "closed_at": "2026-08-12T00:05:00Z",
        "long_exit_price": 0.015,
        "short_exit_price": 0.016,
    }

    result = portfolio._hydrate_position(position, [], funding_snapshot={})

    assert result["long_mark_basis"] == "stored_exit"
    assert result["short_mark_basis"] == "stored_exit"


def test_position_chart_link_uses_exact_custom_route_and_since_entry() -> None:
    position = {
        **_dex_position(),
        "quote_status": "refreshing",
        "market_listing_status": "listed",
        "chart_route_key": "CUSTOM:opaque",
        "current_funding": {},
    }

    html = server.render_position_card(position)
    href = html.split('href="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    query = parse_qs(urlparse(href).query)

    assert query["route_key"] == ["CUSTOM:opaque"]
    assert query["window"] == ["position"]
    assert query["opened_at"] == ["2026-08-08T22:37:00Z"]
    assert "Chart since entry" in html


def test_since_entry_window_uses_exact_open_timestamp_and_caps_at_30_days() -> None:
    opened = datetime.now(tz=timezone.utc) - timedelta(days=3, hours=2)
    since_us = server._position_opened_us(opened.isoformat())
    config = server.position_chart_window_config(since_us)

    assert 74 <= float(config["hours"]) <= 75
    assert config["label"] == "Since entry"
    very_old = server._position_opened_us("2020-01-01T00:00:00Z")
    assert 719 <= float(server.position_chart_window_config(very_old)["hours"]) <= 720


def test_portfolio_schedules_shared_exact_route_without_blocking(monkeypatch) -> None:
    scheduled: list[str] = []
    user = SimpleNamespace()
    monkeypatch.setattr(
        server.portfolio,
        "portfolio_snapshot",
        lambda *_args, **_kwargs: {
            "positions": [
                {
                    "status": "open",
                    "quote_refresh_needed": True,
                    "canonical_route": {"route_key": "CUSTOM:exact"},
                }
            ]
        },
    )
    monkeypatch.setattr(
        server,
        "_schedule_chart_route_refresh",
        lambda row: scheduled.append(row["route_key"]) or {"status": "warming"},
    )

    payload = server.api_portfolio(user, Path("board.json"), Path("accounts.sqlite3"))

    assert scheduled == ["CUSTOM:exact"]
    assert payload["positions"][0]["position_quote_refresh"]["status"] == "warming"


def test_portfolio_does_not_resample_a_position_already_live(monkeypatch) -> None:
    scheduled: list[str] = []
    monkeypatch.setattr(
        server.portfolio,
        "portfolio_snapshot",
        lambda *_args, **_kwargs: {
            "positions": [
                {
                    "status": "open",
                    "quote_refresh_needed": False,
                    "canonical_route": {"route_key": "CUSTOM:live"},
                }
            ]
        },
    )
    monkeypatch.setattr(
        server,
        "_schedule_chart_route_refresh",
        lambda row: scheduled.append(row["route_key"]),
    )

    server.api_portfolio(SimpleNamespace(), Path("board.json"), Path("accounts.sqlite3"))

    assert scheduled == []


def test_background_position_alert_worker_warms_an_off_board_exact_route(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="off-board-alert@example.test",
        display_name="Off Board Alert",
        password="off-board-alert-password",
        subscription_status="active",
        subscription_days=30,
        db_path=db_path,
    )
    position = accounts.create_position(user["id"], _dex_position(), db_path=db_path)
    accounts.add_alert_rule(
        user["id"],
        position["id"],
        {"metric": "exit_spread_pct", "operator": "gte", "threshold": 0},
        db_path=db_path,
    )
    scheduled: list[str] = []
    monkeypatch.setattr(portfolio.api_spreads, "load_spreads", lambda **_kwargs: {"rows": []})
    monkeypatch.setattr(portfolio, "_live_books", lambda: {})
    monkeypatch.setattr(portfolio, "_history_quote", lambda _key: None)
    monkeypatch.setattr(portfolio.bulk_quotes, "load_funding", lambda: {})
    monkeypatch.setattr(portfolio.chart_catalog, "load", _catalogue)
    worker = portfolio.PositionAlertWorker(
        board_path=tmp_path / "board.json",
        accounts_path=db_path,
        quote_scheduler=lambda row: scheduled.append(row["route_key"]),
    )

    summary = worker.check_once()

    assert summary["positions"] == 1
    assert len(scheduled) == 1
    assert scheduled[0].startswith("CUSTOM:")


def test_background_position_alert_worker_uses_exact_portfolio_snapshot(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="exact-alert@example.test",
        display_name="Exact Alert",
        password="exact-alert-password",
        subscription_status="active",
        subscription_days=30,
        db_path=db_path,
    )
    position = accounts.create_position(user["id"], _dex_position(), db_path=db_path)
    accounts.add_alert_rule(
        user["id"],
        position["id"],
        {"metric": "funding_usd", "operator": "gte", "threshold": 1},
        db_path=db_path,
    )
    exact_snapshot = {"schema": portfolio_funding.SCHEMA, "positions": {}}
    seen_snapshots: list[dict] = []

    monkeypatch.setattr(portfolio.api_spreads, "load_spreads", lambda **_kwargs: {"rows": []})
    monkeypatch.setattr(portfolio, "_live_books", lambda: {})
    monkeypatch.setattr(portfolio.bulk_quotes, "load_funding", lambda: {})
    monkeypatch.setattr(portfolio.chart_catalog, "load", _catalogue)
    monkeypatch.setattr(portfolio.portfolio_funding, "load", lambda: exact_snapshot)

    def hydrate(raw, _rows, **kwargs):
        seen_snapshots.append(kwargs["funding_snapshot"])
        return {
            **raw,
            "funding_income_usd": 2.0,
            "quote_refresh_needed": False,
        }

    monkeypatch.setattr(portfolio, "_hydrate_position", hydrate)
    worker = portfolio.PositionAlertWorker(
        board_path=tmp_path / "board.json",
        accounts_path=db_path,
    )

    summary = worker.check_once()

    assert seen_snapshots == [exact_snapshot]
    assert summary == {"users": 1, "positions": 1, "notifications": 1}


def test_open_position_books_take_priority_over_ranked_board(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "accounts.sqlite3"
    accounts.initialize(db_path)
    user = accounts.create_user(
        email="position-books@example.test",
        display_name="Position Books",
        password="position-books-password",
        subscription_status="active",
        subscription_days=30,
        db_path=db_path,
    )
    accounts.create_position(
        user["id"],
        {
            "token": "GUA",
            "long_venue": "Mexc",
            "long_market_type": "Spot",
            "long_symbol": "GUA/USDT",
            "long_quantity": 1,
            "long_entry_price": 1,
            "short_venue": "Gate",
            "short_market_type": "Futures",
            "short_symbol": "GUA/USDT:USDT",
            "short_quantity": 1,
            "short_entry_price": 1,
        },
        db_path=db_path,
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}")
    monkeypatch.setattr(
        "scripts.websocket_book_worker._board_legs",
        lambda *_args, **_kwargs: [("Binance", "Futures", "BTC/USDT:USDT")],
    )

    legs = _desired_legs(snapshot, limit=2, accounts_path=db_path)

    assert legs == {
        ("Mexc", "Spot", "GUA/USDT"),
        ("Gate", "Futures", "GUA/USDT:USDT"),
    }


def test_two_dex_legs_keep_their_own_chain_and_contract_identity() -> None:
    row = {
        "long_venue": "OKX DEX 56",
        "short_venue": "OKX DEX 501",
        "dex_chain": "56",
        "dex_contract": "0xlegacy-top-level",
        "notes": {
            "identity": {
                "long": {"chain_id": "56", "token_address": "0xlong"},
                "short": {"chain_id": "501", "token_address": "short-mint"},
            }
        },
    }

    assert fast_quotes._dex_chain_contract(row, side="long") == ("56", "0xlong")
    assert fast_quotes._dex_chain_contract(row, side="short") == ("501", "short-mint")


def test_ccxt_websocket_reset_compat_rejects_pending_futures_once() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.rejected: Exception | None = None

        def reject(self, error: Exception) -> Exception:
            self.rejected = error
            return error

    error = ConnectionError("closing transport")

    assert _install_ccxt_client_reset_compat(FakeClient) is True
    client = FakeClient()
    assert client.reset(error) is error
    assert client.rejected is error
    assert _install_ccxt_client_reset_compat(FakeClient) is False
