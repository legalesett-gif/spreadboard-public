from __future__ import annotations

import json
import time

from spreadboard import market_core_backfill, market_history


def _row(
    route_key: str,
    token: str,
    timestamp_us: int,
    *,
    basis: float,
    funding: float,
    long_price: float,
    short_price: float,
    route_kind: str = "SPOT-FUTURES",
) -> dict:
    long_type = "DEX" if route_kind.startswith("DEX-") else "Spot"
    return {
        "route_key": route_key,
        "token": token,
        "route_kind": route_kind,
        "long_venue": "OKX DEX 56" if long_type == "DEX" else "Mexc",
        "long_market_type": long_type,
        "short_venue": "Bybit",
        "short_market_type": "Futures",
        "quote_ts_us": timestamp_us,
        "executable_spread_pct": basis,
        "depth_weighted_spread_pct": basis,
        "funding_daily_pct": funding,
        "target_notional_usd": 50,
        "notes": {
            "route_inputs": {
                "long": {"bid": long_price * 0.999, "ask": long_price},
                "short": {"bid": short_price, "ask": short_price * 1.001},
            },
            "funding": {"net_daily_pct": funding},
        },
    }


def test_market_core_backfill_is_versioned_cex_only_and_idempotent(tmp_path) -> None:
    history_db = tmp_path / "history.sqlite3"
    calibration_db = tmp_path / "calibration.sqlite3"
    now_us = int(time.time() * 1_000_000)
    end_day = now_us // market_core_backfill.DAY_US * market_core_backfill.DAY_US
    start_us = end_day - 5 * market_core_backfill.DAY_US
    rows = []
    for hour in range(5 * 24 + 1):
        timestamp = start_us + hour * market_core_backfill.HOUR_US
        rows.extend(
            [
                _row(
                    "A|Mexc|Spot|Bybit|Futures",
                    "A",
                    timestamp,
                    basis=2.0 - hour * 0.004,
                    funding=0.25,
                    long_price=100 + hour * 0.02,
                    short_price=102 + hour * 0.015,
                ),
                _row(
                    "B|Mexc|Spot|Bybit|Futures",
                    "B",
                    timestamp,
                    basis=-1.0 + hour * 0.002,
                    funding=-0.10 if hour % 9 == 0 else 0.15,
                    long_price=50 + hour * 0.01,
                    short_price=49.5 + hour * 0.012,
                ),
                _row(
                    "DEX|OKX DEX 56|DEX|Bybit|Futures",
                    "DEX",
                    timestamp,
                    basis=5.0,
                    funding=0.5,
                    long_price=10,
                    short_price=10.5,
                    route_kind="DEX-FUTURES",
                ),
            ]
        )
    assert market_history.record_snapshot(
        {"api_discovered_rows": rows}, db_path=history_db, retention_days=30
    ) == len(rows)

    first = market_core_backfill.backfill(
        db_path=calibration_db,
        history_db_path=history_db,
        daily_limit=10,
        max_days=10,
    )
    second = market_core_backfill.backfill(
        db_path=calibration_db,
        history_db_path=history_db,
        daily_limit=10,
        max_days=10,
    )

    assert first["observations_inserted"] >= 4
    assert first["outcomes_inserted"] == first["observations_inserted"]
    assert first["dex_rows"] == 0
    assert first["activation_eligible"] is False
    assert first["feature_leakage_scan"] == {"passed": True, "hits": []}
    assert second["days_processed"] == 0
    assert second["observations_inserted"] == 0
    connection = market_core_backfill.research_calibration._connect(calibration_db)
    try:
        rows = connection.execute(
            """SELECT method, feature_schema, historical_identity_status,
                      cost_scope, feature_json
               FROM market_core_observations ORDER BY id"""
        ).fetchall()
    finally:
        connection.close()
    assert rows
    assert {row["method"] for row in rows} == {market_core_backfill.METHOD}
    assert {row["feature_schema"] for row in rows} == {market_core_backfill.FEATURE_SCHEMA}
    assert {row["historical_identity_status"] for row in rows} == {
        "not_preserved_in_historical_source_cex_only"
    }
    assert {row["cost_scope"] for row in rows} == {"public_market_only_no_account_costs"}
    features = json.loads(rows[0]["feature_json"])
    assert features["historical_identity_present"] is False
    assert features["exact_account_costs_present"] is False
    assert features["long_realized_volatility_24h_pct"] is not None
    assert features["leg_return_correlation_24h"] is not None


def test_features_are_unchanged_when_only_post_observation_data_changes() -> None:
    observed = 100 * market_core_backfill.DAY_US
    past = [
        _row(
            "A|Mexc|Spot|Bybit|Futures",
            "A",
            observed - (24 - hour) * market_core_backfill.HOUR_US,
            basis=2.0 - hour * 0.01,
            funding=0.2,
            long_price=100 + hour * 0.1,
            short_price=102 + hour * 0.08,
        )
        for hour in range(24)
    ]
    ordinary_after = [
        _row(
            "A|Mexc|Spot|Bybit|Futures",
            "A",
            observed + hour * market_core_backfill.HOUR_US,
            basis=1.8 - hour * 0.02,
            funding=0.2,
            long_price=102.4 + hour * 0.02,
            short_price=103.9 - hour * 0.02,
        )
        for hour in range(1, 25)
    ]
    shocked_after = [
        {**row, "depth_weighted_spread_pct": 7.0, "funding_daily_pct": -1.0}
        for row in ordinary_after
    ]

    before = market_core_backfill._features(past, observed_day_us=observed, selection_score=2.0)
    unchanged = market_core_backfill._features(past, observed_day_us=observed, selection_score=2.0)
    ordinary = market_core_backfill._outcome(
        ordinary_after,
        observed_day_us=observed,
        entry_basis=before["entry_basis_pct"],
        entry_long_price=before["entry_long_price"],
        entry_short_price=before["entry_short_price"],
    )
    shocked = market_core_backfill._outcome(
        shocked_after,
        observed_day_us=observed,
        entry_basis=before["entry_basis_pct"],
        entry_long_price=before["entry_long_price"],
        entry_short_price=before["entry_short_price"],
    )

    assert before == unchanged
    assert ordinary["convergence_capture_pct"] != shocked["convergence_capture_pct"]
    assert ordinary["funding_positive_fraction"] != shocked["funding_positive_fraction"]
    forbidden = ("future", "outcome", "label", "end_basis", "convergence_capture")
    feature_keys = {str(key).casefold() for key in before}
    assert not any(token in key for token in forbidden for key in feature_keys)


def test_zero_depth_weighted_basis_is_not_replaced_by_top_book() -> None:
    assert (
        market_core_backfill._basis(
            {"depth_weighted_spread_pct": 0.0, "executable_spread_pct": 4.0}
        )
        == 0.0
    )
