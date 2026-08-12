from __future__ import annotations

from datetime import datetime, timezone

from scripts import sync_portfolio_funding
from spreadboard import portfolio_funding


def position(**overrides):
    row = {
        "id": 8,
        "user_id": 9,
        "token": "BTW",
        "status": "open",
        "long_venue": "Mexc",
        "long_market_type": "Spot",
        "long_symbol": "BTW/USDT",
        "long_quantity": 13530,
        "long_entry_price": 0.2017316364,
        "short_venue": "Aster",
        "short_market_type": "Futures",
        "short_symbol": "BTW/USDT:USDT",
        "short_quantity": 13530,
        "short_entry_price": 0.2026227273,
        "opened_at": "2026-08-11T23:19:58Z",
        "closed_at": None,
        "funding_cashflows": [],
    }
    row.update(overrides)
    return row


def test_exact_funding_requires_a_fresh_matching_position() -> None:
    row = position()
    generated = "2026-08-12T00:05:00Z"
    snapshot = {
        "schema": portfolio_funding.SCHEMA,
        "generated_at": generated,
        "positions": {
            "9:8": {
                "status": "ok",
                "position_fingerprint": portfolio_funding.position_fingerprint(row),
                "amount_usd": "0.49385144",
                "event_count": 1,
                "synced_at": generated,
            }
        },
    }
    now = datetime(2026, 8, 12, 0, 6, tzinfo=timezone.utc).timestamp()
    exact = portfolio_funding.exact_funding(row, snapshot, now=now)
    assert exact["known"] is True
    assert exact["amount_usd"] == 0.49385144
    assert exact["status"] == "exact"

    changed = portfolio_funding.exact_funding({**row, "short_entry_price": 0.2}, snapshot, now=now)
    assert changed["known"] is False
    assert changed["status"] == "position_changed"

    stale = portfolio_funding.exact_funding(row, snapshot, now=now + 901, max_age_seconds=900)
    assert stale["known"] is False
    assert stale["status"] == "stale"


def test_exact_mark_requires_matching_quantity_and_fresh_fingerprint() -> None:
    row = position(
        token="ESPORTS",
        long_venue="OKX DEX 56",
        long_market_type="DEX",
        long_symbol="ESPORTS",
        long_quantity=147464.913865,
    )
    generated = "2026-08-12T00:05:00Z"
    snapshot = {
        "schema": portfolio_funding.SCHEMA,
        "generated_at": generated,
        "positions": {
            "9:8": {
                "status": "ok",
                "position_fingerprint": portfolio_funding.position_fingerprint(row),
                "synced_at": generated,
                "marks": {
                    "long": {
                        "status": "ok",
                        "quantity": "147464.913865",
                        "price_usd": "0.01514",
                        "source": "paraswap_exact_sell_quote",
                        "quoted_at": generated,
                    }
                },
            }
        },
    }
    now = datetime(2026, 8, 12, 0, 6, tzinfo=timezone.utc).timestamp()

    marks = portfolio_funding.exact_marks(row, snapshot, now=now)
    assert marks["long"]["price_usd"] == 0.01514
    assert portfolio_funding.exact_marks({**row, "long_quantity": 1000}, snapshot, now=now) == {}


def test_private_sync_allocates_signed_events_to_position_window() -> None:
    row = position()
    seen = []

    def fetcher(venue, symbol, since_ms):
        seen.append((venue, symbol, since_ms))
        return [
            {
                "timestamp": sync_portfolio_funding.timestamp_ms("2026-08-11T23:00:00Z"),
                "amount": "9",
                "code": "USDT",
            },
            {
                "timestamp": sync_portfolio_funding.timestamp_ms("2026-08-12T00:00:00Z"),
                "amount": "0.49385144",
                "code": "USDT",
            },
        ]

    snapshot = sync_portfolio_funding.build_snapshot(
        [row], fetcher, generated_at="2026-08-12T00:05:00Z"
    )
    item = snapshot["positions"]["9:8"]
    assert seen[0][:2] == ("Aster", "BTW/USDT:USDT")
    assert item["status"] == "ok"
    assert item["amount_usd"] == "0.49385144"
    assert item["event_count"] == 1
    assert item["latest_event_at"] == "2026-08-12T00:00:00Z"


def test_overlapping_same_account_market_is_not_double_counted() -> None:
    first = position()
    second = position(id=9, opened_at="2026-08-11T23:30:00Z")
    snapshot = sync_portfolio_funding.build_snapshot(
        [first, second],
        lambda *_: [
            {
                "timestamp": sync_portfolio_funding.timestamp_ms("2026-08-12T00:00:00Z"),
                "amount": "1",
                "code": "USDT",
            }
        ],
        generated_at="2026-08-12T00:05:00Z",
    )
    assert snapshot["positions"]["9:8"]["status"] == "ambiguous_overlapping_position"
    assert snapshot["positions"]["9:9"]["status"] == "ambiguous_overlapping_position"


def test_sync_keeps_dex_mark_failure_separate_from_exact_funding() -> None:
    row = position(
        token="ESPORTS",
        long_venue="OKX DEX 56",
        long_market_type="DEX",
        long_symbol="ESPORTS",
        long_quantity=1000,
        _resolved_legs={
            "long": {
                "dex_chain": "56",
                "dex_contract": "0xf39e4b21c84e737df08e2c3b32541d856f508e48",
            }
        },
    )

    snapshot = sync_portfolio_funding.build_snapshot(
        [row],
        lambda *_: [],
        mark_fetcher=lambda *_: (_ for _ in ()).throw(RuntimeError("quote failed")),
        generated_at="2026-08-12T00:05:00Z",
    )

    item = snapshot["positions"]["9:8"]
    assert item["status"] == "ok"
    assert item["amount_usd"] == "0"
    assert item["marks"]["long"]["status"] == "quote_error:RuntimeError"


def test_quantity_vwap_is_full_size_and_contract_aware() -> None:
    levels = [["10", "2"], ["11", "3"]]

    assert sync_portfolio_funding.quantity_vwap(
        levels, sync_portfolio_funding.Decimal("40"), contract_size=sync_portfolio_funding.Decimal("10")
    ) == sync_portfolio_funding.Decimal("10.5")
    assert (
        sync_portfolio_funding.quantity_vwap(
            levels,
            sync_portfolio_funding.Decimal("60"),
            contract_size=sync_portfolio_funding.Decimal("10"),
        )
        is None
    )
