"""Deterministic funding, spread-entry and collateral-stress research.

The scores are deliberately not AI predictions.  Funding carry and basis
convergence are different opportunities, so each has its own score and
confidence.  Their economic interaction is explicit: expected funding affects
the spread score and evidence-backed convergence affects the funding score.
"""

from __future__ import annotations

from bisect import bisect_left
import math
import statistics
import time
from typing import Any


def evaluate(
    route: dict[str, Any] | None,
    *,
    windows: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    historical: bool = False,
) -> dict[str, Any]:
    """Score two route theses and estimate a conservative futures reserve."""
    if not isinstance(route, dict) or not route:
        unavailable_funding = _unavailable_opportunity("funding")
        unavailable_spread = _unavailable_opportunity("spread")
        return {
            "score": None,
            "confidence": 0,
            "label": "Insufficient data",
            "components": {},
            "funding_opportunity": unavailable_funding,
            "spread_opportunity": unavailable_spread,
            "route_economics": {
                "holding_horizon_hours": 24,
                "expected_funding_pct": None,
                "expected_convergence_capture_pct": None,
                "expected_gross_edge_pct": None,
                "known_costs_pct": None,
            },
            "planning_buffer_pct": None,
            "planning_buffer_label": "Collateral reserve unavailable",
            "risk_estimate": _unavailable_risk("no_route"),
            "reasons": ["No live or retained route is available for this token."],
            "method": "deterministic_dual_opportunity_evidence_v3",
        }

    windows = windows or {}
    risk = assess_route_risk(route, history=history, historical=historical)
    settled_daily = _first_number(
        route.get("funding_24h_pct"),
        windows.get("1d"),
    )
    projected_daily = _first_number(
        route.get("funding_projected_24h_pct"),
        route.get("funding_daily_pct"),
    )
    daily = settled_daily if settled_daily is not None else projected_daily
    carry = _clamp(max(0.0, daily or 0.0) * 40.0, 0.0, 20.0)

    persistence = 0.0
    persistence_detail = []
    for label, maximum, days in (("1d", 4.0, 1), ("7d", 6.0, 7), ("30d", 5.0, 30)):
        value = _number(windows.get(label))
        if value is None:
            persistence_detail.append(f"{label} unavailable")
            continue
        per_day = value / days
        if per_day > 0:
            persistence += maximum * _clamp(per_day / 0.25, 0.2, 1.0)
            persistence_detail.append(f"{label} positive")
        else:
            persistence_detail.append(f"{label} non-positive")
    funding_stability = risk.get("funding_stability") or {}
    positive_ratio = _number(funding_stability.get("positive_ratio"))
    if positive_ratio is not None and int(funding_stability.get("samples") or 0) >= 8:
        persistence *= 0.5 + 0.5 * positive_ratio
        persistence_detail.append(f"{positive_ratio * 100:.0f}% positive history samples")

    depth = _number(route.get("depth_usd"))
    volumes = [
        value
        for value in (
            _number(route.get("long_volume_24h_usd")),
            _number(route.get("short_volume_24h_usd")),
            _number(route.get("metadata_volume_24h_usd")),
        )
        if value is not None and value > 0
    ]
    min_volume = min(volumes) if volumes else None
    if depth is not None and depth > 0:
        liquidity = 15.0 * _log_scale(depth, low=50.0, high=250_000.0)
        liquidity_basis = f"matched depth ${depth:,.0f}"
    elif min_volume is not None:
        liquidity = 10.0 * _log_scale(min_volume, low=10_000.0, high=50_000_000.0)
        liquidity_basis = f"minimum leg volume ${min_volume:,.0f}"
    else:
        liquidity = 0.0
        liquidity_basis = "liquidity unavailable"

    freshness = str(route.get("freshness") or "").casefold()
    age = _number(route.get("age_min"))
    executable = _first_number(
        route.get("depth_weighted_spread_pct"),
        route.get("executable_spread_pct"),
        route.get("displayed_open_spread_pct"),
    )
    execution = 0.0
    if not historical and freshness in {"fresh", "live"}:
        execution += 6.0
    elif not historical and (age is None or age <= 10):
        execution += 4.0
    if executable is not None:
        execution += 5.0
    if route.get("long_market_symbol") and route.get("short_market_symbol"):
        execution += 4.0

    blockers = [str(item).casefold() for item in route.get("blockers") or []]
    unresolved_identity = bool(route.get("mirage_guarded") or route.get("identity_unresolved"))
    integrity = 0.0
    if not unresolved_identity:
        integrity += 6.0
    if not any("deposit" in item or "withdraw" in item or "deliver" in item for item in blockers):
        integrity += 5.0
    if not blockers:
        integrity += 4.0

    components = {
        "carry": _component(carry, 20, "Settled 24h carry where available; projection otherwise"),
        "persistence": _component(persistence, 15, ", ".join(persistence_detail)),
        "liquidity": _component(liquidity, 15, liquidity_basis),
        "execution": _component(execution, 15, "Freshness, executable spread and exact symbols"),
        "integrity": _component(integrity, 15, "Identity, transfer warnings and blockers"),
        "risk": _component(
            _risk_score(risk), 20, str(risk.get("summary") or "Risk evidence unavailable")
        ),
    }
    risk_points = _risk_score(risk)

    convergence = _convergence_evidence(history or [], current_basis=executable)
    expected_convergence = _number(convergence.get("expected_capture_pct"))
    expected_funding = daily
    expected_gross = (
        (expected_convergence or 0.0) + (expected_funding or 0.0)
        if expected_convergence is not None or expected_funding is not None
        else None
    )
    known_costs = _first_number(
        route.get("estimated_round_trip_cost_pct"),
        route.get("known_round_trip_cost_pct"),
    )

    stability_points = _funding_stability_score(funding_stability, maximum=15.0)
    spread_cross = 5.0 + _clamp((expected_convergence or 0.0) * 5.0, -5.0, 5.0)
    funding_cross = 7.5 + _clamp((daily or 0.0) * 15.0, -7.5, 7.5)
    convergence_points = _convergence_score(convergence, maximum=25.0)
    entry_edge = _clamp(max(0.0, executable or 0.0) / 1.5 * 25.0, 0.0, 25.0)

    funding_components = {
        "current_rate": _component(
            _clamp(max(0.0, daily or 0.0) * 50.0, 0.0, 25.0),
            25,
            "Settled 24h carry where available; current-rate projection otherwise",
        ),
        "persistence": _component(
            persistence / 15.0 * 20.0,
            20,
            ", ".join(persistence_detail),
        ),
        "stability": _component(
            stability_points,
            15,
            _funding_stability_detail(funding_stability),
        ),
        "spread_contribution": _component(
            spread_cross,
            10,
            _spread_cross_detail(convergence),
        ),
        "liquidity": _component(liquidity / 15.0 * 10.0, 10, liquidity_basis),
        "execution": _component(
            execution / 15.0 * 5.0,
            5,
            "Fresh quote, executable basis and exact symbols",
        ),
        "integrity": _component(
            integrity / 15.0 * 5.0,
            5,
            "Identity, transfer warnings and blockers",
        ),
        "risk": _component(
            risk_points / 20.0 * 10.0,
            10,
            str(risk.get("summary") or "Risk evidence unavailable"),
        ),
    }
    spread_components = {
        "entry_edge": _component(
            entry_edge,
            25,
            (
                f"Executable opening basis {executable:+.3f}% before fees"
                if executable is not None
                else "Executable opening basis unavailable"
            ),
        ),
        "convergence_history": _component(
            convergence_points,
            25,
            _convergence_detail(convergence),
        ),
        "funding_contribution": _component(
            funding_cross,
            15,
            (
                f"Expected 24h funding contribution {daily:+.3f}%"
                if daily is not None
                else "Funding contribution unavailable; treated as neutral"
            ),
        ),
        "liquidity": _component(liquidity / 15.0 * 10.0, 10, liquidity_basis),
        "execution": _component(
            execution / 15.0 * 10.0,
            10,
            "Fresh quote, executable basis and exact symbols",
        ),
        "integrity": _component(
            integrity / 15.0 * 5.0,
            5,
            "Identity, transfer warnings and blockers",
        ),
        "risk": _component(
            risk_points / 20.0 * 15.0,
            15,
            str(risk.get("summary") or "Risk evidence unavailable"),
        ),
    }
    funding_score = round(sum(item["value"] for item in funding_components.values()), 1)
    spread_score = round(sum(item["value"] for item in spread_components.values()), 1)

    evidence = 10.0 if daily is not None else 0.0
    evidence += sum(
        10.0 for label in ("1d", "7d", "30d") if _number(windows.get(label)) is not None
    )
    evidence += 10.0 if depth is not None or min_volume is not None else 0.0
    evidence += 10.0 if executable is not None else 0.0
    evidence += 10.0 if not unresolved_identity else 0.0
    evidence += float((risk.get("data_quality") or {}).get("confidence_points") or 0.0)
    evidence += 5.0 if settled_daily is not None else 0.0
    confidence = int(round(_clamp(evidence, 0.0, 100.0)))
    funding_confidence = _funding_confidence(
        daily=daily,
        settled_daily=settled_daily,
        windows=windows,
        funding_stability=funding_stability,
        depth=depth,
        min_volume=min_volume,
        unresolved_identity=unresolved_identity,
        executable=executable,
        exact_symbols=bool(route.get("long_market_symbol") and route.get("short_market_symbol")),
    )
    spread_confidence = _spread_confidence(
        executable=executable,
        convergence=convergence,
        risk=risk,
        depth=depth,
        min_volume=min_volume,
        unresolved_identity=unresolved_identity,
        historical=historical,
        exact_symbols=bool(route.get("long_market_symbol") and route.get("short_market_symbol")),
    )
    # Kept only for backwards-compatible API consumers.  Product surfaces use
    # the two thesis-specific scores below and never label this as one model score.
    score = round((funding_score + spread_score) / 2.0, 1)
    confidence = int(round((funding_confidence + spread_confidence) / 2.0))

    reserve = _number(risk.get("recommended_collateral_pct"))
    leverage = _number(risk.get("research_leverage_ceiling"))
    if reserve is None:
        reserve_label = "No futures collateral estimate; assess cash or borrow capacity separately"
    else:
        reserve_label = f"Rule-based collateral reserve: {reserve:.0f}% per futures leg" + (
            f" · research leverage ceiling {leverage:.2f}x" if leverage else ""
        )

    reasons = []
    if daily is not None:
        source = "settled" if settled_daily is not None else "current-rate projection"
        reasons.append(f"Net carry {daily:+.3f}% per 24h ({source}).")
    if risk.get("summary"):
        reasons.append(str(risk["summary"]))
    if executable is not None:
        reasons.append(
            f"Executable entry basis {executable:+.3f}%; "
            + _convergence_detail(convergence).rstrip(".")
            + "."
        )
    if _number(windows.get("7d")) is None:
        reasons.append("Seven-day settled funding history is not complete yet.")
    if unresolved_identity:
        reasons.append("Token identity is unresolved; verify the exact contract before transfer.")
    if historical:
        reasons.append("This is a retained radar route, not a current executable row.")
    if not reasons:
        reasons.append("Inspect the exact pair, score components and risk evidence before acting.")

    return {
        "score": score,
        "confidence": confidence,
        "label": "Two-factor route review",
        "components": components,
        "funding_opportunity": {
            "score": funding_score,
            "confidence": funding_confidence,
            "label": _opportunity_label("funding", funding_score, funding_confidence),
            "components": funding_components,
            "current_24h_pct": daily,
            "spread_contribution_pct": expected_convergence,
        },
        "spread_opportunity": {
            "score": spread_score,
            "confidence": spread_confidence,
            "label": _opportunity_label("spread", spread_score, spread_confidence),
            "components": spread_components,
            "entry_spread_pct": executable,
            "funding_contribution_24h_pct": daily,
            "convergence": convergence,
        },
        "route_economics": {
            "holding_horizon_hours": 24,
            "expected_funding_pct": expected_funding,
            "expected_convergence_capture_pct": expected_convergence,
            "expected_gross_edge_pct": expected_gross,
            "known_costs_pct": known_costs,
            "expected_net_edge_pct": (
                expected_gross - known_costs
                if expected_gross is not None and known_costs is not None
                else None
            ),
            "cost_status": "known" if known_costs is not None else "account_fee_and_exit_costs_required",
        },
        "planning_buffer_pct": reserve,
        "planning_buffer_label": reserve_label,
        "risk_estimate": risk,
        "reasons": reasons[:5],
        "method": "deterministic_dual_opportunity_evidence_v3",
        "disclaimer": (
            "Rule-based research evidence, not personalized advice, an AI prediction or a liquidation calculation. "
            "Gross edge excludes unknown account fees, borrow, gas and exit slippage. Exact leverage, maintenance "
            "tiers, account equity and other positions remain venue/account specific."
        ),
    }


def assess_route_risk(
    route: dict[str, Any],
    *,
    history: list[dict[str, Any]] | None,
    historical: bool = False,
) -> dict[str, Any]:
    """Estimate per-futures-leg collateral from empirical public history.

    The estimate uses adverse leg moves because a hedged route can still be
    liquidated on one venue before the profitable leg can be moved across.
    Basis widening, weak correlation, gaps, liquidity and evidence quality are
    additional—not replacement—risk controls.
    """
    rows = _clean_history(history or [])
    long_type = str(route.get("long_market_type") or "")
    short_type = str(route.get("short_market_type") or "")
    futures_sides = [
        side
        for side, market_type in (("long", long_type), ("short", short_type))
        if market_type.casefold() == "futures"
    ]
    timestamps = [int(row["quote_ts_us"]) for row in rows]
    span_hours = (timestamps[-1] - timestamps[0]) / 3_600_000_000.0 if len(timestamps) > 1 else 0.0
    intervals = _aligned_interval_returns(rows)
    daily = _rolling_24h_outcomes(rows)
    correlation = _correlation(
        [item["long_log_per_sqrt_hour"] for item in intervals if item.get("both")],
        [item["short_log_per_sqrt_hour"] for item in intervals if item.get("both")],
    )
    basis_adverse = [
        max(0.0, item["basis_change_pct_points"])
        for item in daily
        if item.get("basis_change_pct_points") is not None
    ]
    basis_p95 = _percentile(basis_adverse, 0.95)
    basis_max = max(basis_adverse) if basis_adverse else None
    basis_hourly = [
        item["basis_change_pct_points"]
        for item in intervals
        if item.get("basis_change_pct_points") is not None
    ]
    basis_sigma_24 = (
        statistics.pstdev(basis_hourly) * math.sqrt(24.0) if len(basis_hourly) >= 3 else None
    )

    funding_values = [
        value
        for row in rows
        if (value := _first_number(row.get("funding_daily_pct"), row.get("funding_24h_pct")))
        is not None
    ]
    sign_reversals = sum(
        (previous > 0 >= current) or (previous <= 0 < current)
        for previous, current in zip(funding_values, funding_values[1:])
    )
    funding_stability = {
        "samples": len(funding_values),
        "positive_ratio": (
            sum(value > 0 for value in funding_values) / len(funding_values)
            if funding_values
            else None
        ),
        "average_24h_pct": statistics.fmean(funding_values) if funding_values else None,
        "minimum_24h_pct": min(funding_values) if funding_values else None,
        "volatility_pct": statistics.pstdev(funding_values) if len(funding_values) >= 2 else None,
        "sign_reversals": sign_reversals,
    }
    data_quality = _risk_data_quality(rows, span_hours)
    if not futures_sides:
        return {
            "status": "not_applicable",
            "risk_level": "cash_or_borrow_risk",
            "recommended_collateral_pct": None,
            "research_leverage_ceiling": None,
            "futures_legs": {},
            "route_basis": {
                "adverse_24h_p95_pct_points": basis_p95,
                "adverse_24h_max_pct_points": basis_max,
                "volatility_24h_pct_points": basis_sigma_24,
            },
            "leg_return_correlation": correlation,
            "funding_stability": funding_stability,
            "data_quality": data_quality,
            "summary": "No futures leg: collateral reserve is not applicable; cash, borrow and transfer risk still apply.",
            "limitations": _risk_limitations(),
        }

    depth = _number(route.get("depth_usd"))
    liquidity_addon = (
        15.0 if depth is None else 10.0 if depth < 500 else 5.0 if depth < 2_500 else 2.0
    )
    history_addon = (
        20.0 if len(rows) < 24 else 12.0 if len(rows) < 72 else 6.0 if span_hours < 168 else 0.0
    )
    identity_addon = (
        15.0 if route.get("mirage_guarded") or route.get("identity_unresolved") else 0.0
    )
    historical_addon = 10.0 if historical else 0.0
    route_kind = str(route.get("route_kind") or "").upper()
    dex_addon = 5.0 if "DEX" in route_kind or "dex" in (long_type + short_type).casefold() else 0.0
    borrow_addon = 8.0 if short_type.casefold() == "spot" else 0.0
    basis_addon = min(12.0, max(0.0, _first_number(basis_p95, basis_sigma_24) or 0.0) * 0.5)
    correlation_addon = (
        8.0 if correlation is None else _clamp((0.85 - correlation) * 10.0, 0.0, 12.0)
    )
    common_addon = (
        10.0
        + liquidity_addon
        + history_addon
        + identity_addon
        + historical_addon
        + dex_addon
        + borrow_addon
        + basis_addon
        + correlation_addon
    )

    spread = abs(
        _first_number(
            route.get("depth_weighted_spread_pct"),
            route.get("executable_spread_pct"),
            route.get("displayed_open_spread_pct"),
        )
        or 0.0
    )
    fallback_shock = _clamp(max(15.0, spread * 1.5), 15.0, 35.0)
    leg_results: dict[str, dict[str, Any]] = {}
    reserves = []
    calibration_coverages = []
    for side in futures_sides:
        normalized_logs = [
            item[f"{side}_log_per_sqrt_hour"]
            for item in intervals
            if item.get(f"{side}_log_per_sqrt_hour") is not None
        ]
        sigma_24 = (
            statistics.pstdev(normalized_logs) * math.sqrt(24.0) * 100.0
            if len(normalized_logs) >= 3
            else None
        )
        recent_logs = normalized_logs[-72:]
        recent_sigma_24 = (
            statistics.pstdev(recent_logs) * math.sqrt(24.0) * 100.0
            if len(recent_logs) >= 12
            else None
        )
        parametric_99 = sigma_24 * 2.33 if sigma_24 is not None else None
        recent_parametric_99 = recent_sigma_24 * 2.33 if recent_sigma_24 is not None else None
        adverse = [
            item.get(f"{side}_adverse_pct")
            for item in daily
            if item.get(f"{side}_adverse_pct") is not None
        ]
        adverse = [float(item) for item in adverse]
        empirical_p95 = _percentile(adverse, 0.95)
        maximum = max(adverse) if adverse else None
        shock = max(
            fallback_shock if len(rows) < 24 else 0.0,
            parametric_99 or 0.0,
            recent_parametric_99 or 0.0,
            empirical_p95 or 0.0,
            (maximum or 0.0) * 0.85,
        )
        reserve = round(_clamp(shock + common_addon, 25.0, 100.0), 0)
        calibration = _rolling_calibration(adverse, reserve_addon_pct=common_addon)
        if calibration.get("coverage_pct") is not None:
            calibration_coverages.append(float(calibration["coverage_pct"]))
        leg_results[side] = {
            "adverse_24h_p95_pct": empirical_p95,
            "adverse_24h_max_pct": maximum,
            "realized_sigma_24h_pct": sigma_24,
            "recent_realized_sigma_24h_pct": recent_sigma_24,
            "parametric_99_pct": parametric_99,
            "recent_parametric_99_pct": recent_parametric_99,
            "stress_move_pct": shock,
            "recommended_collateral_pct": reserve,
            "calibration": calibration,
        }
        reserves.append(reserve)

    reserve = max(reserves)
    leverage = round(min(3.0, 100.0 / reserve), 2) if reserve > 0 else None
    risk_level = "lower" if reserve <= 40 else "elevated" if reserve <= 60 else "high"
    calibration_coverage = min(calibration_coverages) if calibration_coverages else None
    summary = (
        f"{risk_level.title()} collateral stress: {reserve:.0f}% reserve per futures leg"
        f"; {len(rows)} hourly history samples across {span_hours / 24:.1f}d"
    )
    if calibration_coverage is not None:
        summary += f"; rolling stress coverage {calibration_coverage:.0f}%"
    elif len(rows) < 72:
        summary += "; calibration pending more history"
    return {
        "status": "ok" if data_quality["grade"] in {"strong", "usable"} else "limited_data",
        "risk_level": risk_level,
        "recommended_collateral_pct": reserve,
        "research_leverage_ceiling": leverage,
        "futures_legs": leg_results,
        "route_basis": {
            "adverse_24h_p95_pct_points": basis_p95,
            "adverse_24h_max_pct_points": basis_max,
            "volatility_24h_pct_points": basis_sigma_24,
        },
        "leg_return_correlation": correlation,
        "funding_stability": funding_stability,
        "data_quality": data_quality,
        "add_ons_pct": {
            "maintenance_and_operations": 10.0,
            "liquidity": liquidity_addon,
            "history": history_addon,
            "identity": identity_addon,
            "historical_route": historical_addon,
            "dex_execution": dex_addon,
            "spot_borrow": borrow_addon,
            "basis": basis_addon,
            "correlation": correlation_addon,
        },
        "summary": summary,
        "limitations": _risk_limitations(),
    }


def _clean_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_timestamp: dict[int, dict[str, Any]] = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        timestamp = _int_or_none(item.get("quote_ts_us"))
        if timestamp is None or timestamp <= 0:
            continue
        by_timestamp[timestamp] = item
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def _aligned_interval_returns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for previous, current in zip(rows, rows[1:]):
        elapsed_hours = (
            int(current["quote_ts_us"]) - int(previous["quote_ts_us"])
        ) / 3_600_000_000.0
        if not 0 < elapsed_hours <= 6:
            continue
        long_previous = _history_price(previous, "long")
        long_current = _history_price(current, "long")
        short_previous = _history_price(previous, "short")
        short_current = _history_price(current, "short")
        long_log = _log_return(long_previous, long_current)
        short_log = _log_return(short_previous, short_current)
        basis_previous = _history_basis(previous)
        basis_current = _history_basis(current)
        output.append(
            {
                "long_log_per_sqrt_hour": long_log / math.sqrt(elapsed_hours)
                if long_log is not None
                else None,
                "short_log_per_sqrt_hour": short_log / math.sqrt(elapsed_hours)
                if short_log is not None
                else None,
                "basis_change_pct_points": (
                    basis_current - basis_previous
                    if basis_current is not None and basis_previous is not None
                    else None
                ),
                "elapsed_hours": elapsed_hours,
                "both": long_log is not None and short_log is not None,
            }
        )
    return output


def _rolling_24h_outcomes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Measure maximum adverse excursion inside rolling 24-hour windows.

    Endpoint-to-endpoint returns miss an intraday spike that can liquidate one
    leg even when both legs recover by hour 24.  Each outcome therefore uses
    the worst observed move between the anchor and the end of the window.
    """
    if len(rows) < 2:
        return []
    timestamps = [int(row["quote_ts_us"]) for row in rows]
    output = []
    last_anchor = 0
    for index, row in enumerate(rows[:-1]):
        start = timestamps[index]
        if start < last_anchor:
            continue
        target = start + 24 * 3_600_000_000
        candidate = bisect_left(timestamps, target, lo=index + 1)
        choices = [item for item in (candidate - 1, candidate) if index < item < len(rows)]
        if not choices:
            continue
        end_index = min(choices, key=lambda item: abs(timestamps[item] - target))
        elapsed = (timestamps[end_index] - start) / 3_600_000_000.0
        if not 18 <= elapsed <= 30:
            continue
        window = rows[index + 1 : end_index + 1]
        long_start = _history_price(row, "long")
        short_start = _history_price(row, "short")
        long_returns = [
            value
            for item in window
            if (value := _simple_return_pct(long_start, _history_price(item, "long"))) is not None
        ]
        short_returns = [
            value
            for item in window
            if (value := _simple_return_pct(short_start, _history_price(item, "short"))) is not None
        ]
        start_basis = _history_basis(row)
        basis_changes = [
            value - start_basis
            for item in window
            if start_basis is not None and (value := _history_basis(item)) is not None
        ]
        output.append(
            {
                "long_adverse_pct": max([0.0, *(-value for value in long_returns)])
                if long_returns
                else None,
                "short_adverse_pct": max([0.0, *short_returns]) if short_returns else None,
                "basis_change_pct_points": max([0.0, *basis_changes]) if basis_changes else None,
            }
        )
        last_anchor = start + 6 * 3_600_000_000
    return output


def _history_price(row: dict[str, Any], side: str) -> float | None:
    keys = (
        ("long_ask_vwap_price", "long_ask_price", "long_price", "long_bid_price")
        if side == "long"
        else ("short_bid_vwap_price", "short_bid_price", "short_price", "short_ask_price")
    )
    return _first_number(*(row.get(key) for key in keys))


def _history_basis(row: dict[str, Any]) -> float | None:
    return _first_number(
        row.get("depth_weighted_spread_pct"),
        row.get("executable_spread_pct"),
        row.get("open_spread_pct"),
    )


def _risk_data_quality(rows: list[dict[str, Any]], span_hours: float) -> dict[str, Any]:
    samples = len(rows)
    timestamps = [int(row["quote_ts_us"]) for row in rows]
    gaps = [
        (current - previous) / 3_600_000_000.0
        for previous, current in zip(timestamps, timestamps[1:])
        if current > previous
    ]
    maximum_gap = max(gaps) if gaps else None
    expected_hourly = max(1.0, span_hours + 1.0)
    hourly_coverage = min(1.0, samples / expected_hourly)
    latest_age_hours = (
        max(0.0, (time.time() * 1_000_000 - timestamps[-1]) / 3_600_000_000.0)
        if timestamps
        else None
    )
    if (
        samples >= 168
        and span_hours >= 168
        and hourly_coverage >= 0.5
        and (maximum_gap is None or maximum_gap <= 24)
    ):
        grade, points = "strong", 25
    elif (
        samples >= 72
        and span_hours >= 72
        and hourly_coverage >= 0.35
        and (maximum_gap is None or maximum_gap <= 48)
    ):
        grade, points = "usable", 18
    elif samples >= 24:
        grade, points = "limited", 10
    else:
        grade, points = "sparse", 0
    sources = sorted({str(row.get("sample_source") or "unknown") for row in rows})
    return {
        "grade": grade,
        "samples": samples,
        "span_hours": round(span_hours, 2),
        "hourly_coverage_pct": round(hourly_coverage * 100.0, 1),
        "maximum_gap_hours": round(maximum_gap, 2) if maximum_gap is not None else None,
        "latest_sample_age_hours": (
            round(latest_age_hours, 2) if latest_age_hours is not None else None
        ),
        "sample_sources": sources,
        "confidence_points": points,
    }


def _rolling_calibration(
    adverse: list[float],
    *,
    reserve_addon_pct: float,
) -> dict[str, Any]:
    """Backtest whether prior data plus the same reserve add-ons covered tails."""
    if len(adverse) < 12:
        return {"status": "pending", "observations": max(0, len(adverse) - 8), "coverage_pct": None}
    hits = 0
    observations = 0
    breaches = []
    for index in range(8, len(adverse)):
        training = adverse[:index]
        tail = max(
            15.0,
            _percentile(training, 0.95) or 0.0,
            statistics.pstdev(training) * 2.33,
        )
        forecast = _clamp(tail + reserve_addon_pct, 25.0, 100.0)
        hits += int(adverse[index] <= forecast)
        breaches.append(max(0.0, adverse[index] - forecast))
        observations += 1
    return {
        "status": "ok",
        "observations": observations,
        "coverage_pct": round(hits / observations * 100.0, 1) if observations else None,
        "target_coverage_pct": 95.0,
        "worst_breach_pct": round(max(breaches), 3) if breaches else 0.0,
        "method": "expanding_tail_plus_current_rule_addons",
    }


def _risk_score(risk: dict[str, Any]) -> float:
    if risk.get("status") == "not_applicable":
        return 12.0
    reserve = _number(risk.get("recommended_collateral_pct"))
    if reserve is None:
        return 4.0
    return 20.0 * _clamp((100.0 - reserve) / 65.0, 0.0, 1.0)


def _convergence_evidence(
    history: list[dict[str, Any]],
    *,
    current_basis: float | None,
    horizon_hours: int = 24,
) -> dict[str, Any]:
    """Measure whether a positive opening basis historically compressed.

    A route earns convergence credit only when comparable historical positive
    bases had a later observation close to the 24-hour horizon.  Captures are
    normalized by the opening basis so a single large token does not dominate.
    """
    rows = _clean_history(history)
    if len(rows) < 2:
        return {
            "status": "insufficient_history",
            "horizon_hours": horizon_hours,
            "samples": 0,
            "convergence_probability": None,
            "median_capture_ratio": None,
            "p25_capture_ratio": None,
            "expected_capture_pct": None,
            "median_hours_to_half": None,
        }
    timestamps = [int(row["quote_ts_us"]) for row in rows]
    outcomes: list[float] = []
    half_lives: list[float] = []
    last_anchor_ts = 0
    for index, row in enumerate(rows[:-1]):
        if timestamps[index] < last_anchor_ts + 6 * 3_600_000_000:
            continue
        opening = _history_basis(row)
        # Tiny/negative openings are not comparable entries for the displayed
        # long-cheap / short-rich route direction.
        if opening is None or opening < 0.02:
            continue
        if current_basis is not None and current_basis > 0:
            if not max(0.02, current_basis * 0.25) <= opening <= current_basis * 4.0:
                continue
        target = timestamps[index] + horizon_hours * 3_600_000_000
        candidate = bisect_left(timestamps, target, lo=index + 1)
        choices = [item for item in (candidate - 1, candidate) if index < item < len(rows)]
        if not choices:
            continue
        end_index = min(choices, key=lambda item: abs(timestamps[item] - target))
        elapsed = (timestamps[end_index] - timestamps[index]) / 3_600_000_000.0
        if not horizon_hours * 0.75 <= elapsed <= horizon_hours * 1.25:
            continue
        closing = _history_basis(rows[end_index])
        if closing is None:
            continue
        outcomes.append(_clamp((opening - closing) / opening, -2.0, 2.0))
        last_anchor_ts = timestamps[index]
        half_target = opening * 0.5
        for later in range(index + 1, min(len(rows), end_index + 1)):
            later_basis = _history_basis(rows[later])
            if later_basis is not None and later_basis <= half_target:
                half_lives.append(
                    (timestamps[later] - timestamps[index]) / 3_600_000_000.0
                )
                break
    if not outcomes:
        return {
            "status": "no_comparable_positive_entries",
            "horizon_hours": horizon_hours,
            "samples": 0,
            "convergence_probability": None,
            "median_capture_ratio": None,
            "p25_capture_ratio": None,
            "expected_capture_pct": None,
            "median_hours_to_half": None,
        }
    probability = sum(value > 0 for value in outcomes) / len(outcomes)
    median_ratio = statistics.median(outcomes)
    p25_ratio = _percentile(outcomes, 0.25)
    expected_capture = None
    if current_basis is not None and current_basis > 0 and len(outcomes) >= 3:
        expected_capture = current_basis * _clamp(median_ratio, -1.0, 1.0)
    return {
        "status": "usable" if len(outcomes) >= 8 else "limited",
        "horizon_hours": horizon_hours,
        "samples": len(outcomes),
        "convergence_probability": probability,
        "median_capture_ratio": median_ratio,
        "p25_capture_ratio": p25_ratio,
        "expected_capture_pct": expected_capture,
        "median_hours_to_half": statistics.median(half_lives) if half_lives else None,
    }


def _convergence_score(evidence: dict[str, Any], *, maximum: float) -> float:
    samples = int(evidence.get("samples") or 0)
    probability = _number(evidence.get("convergence_probability"))
    median_ratio = _number(evidence.get("median_capture_ratio"))
    if samples < 3 or probability is None or median_ratio is None:
        return 0.0
    strength = 0.55 * probability + 0.45 * _clamp(median_ratio, 0.0, 1.0)
    sample_factor = _clamp(samples / 12.0, 0.25, 1.0)
    return maximum * strength * sample_factor


def _convergence_detail(evidence: dict[str, Any]) -> str:
    samples = int(evidence.get("samples") or 0)
    probability = _number(evidence.get("convergence_probability"))
    median_ratio = _number(evidence.get("median_capture_ratio"))
    half_life = _number(evidence.get("median_hours_to_half"))
    if samples < 3 or probability is None or median_ratio is None:
        return "No reliable 24h convergence sample yet"
    detail = (
        f"24h convergence in {probability * 100:.0f}% of {samples} comparable entries; "
        f"median capture {median_ratio * 100:+.0f}% of opening basis"
    )
    if half_life is not None:
        detail += f"; median half-life {half_life:.0f}h"
    return detail


def _spread_cross_detail(evidence: dict[str, Any]) -> str:
    capture = _number(evidence.get("expected_capture_pct"))
    if capture is None:
        return "No evidence-backed convergence adjustment; neutral contribution"
    return f"Evidence-backed 24h convergence contribution {capture:+.3f}%"


def _funding_stability_score(stability: dict[str, Any], *, maximum: float) -> float:
    samples = int(stability.get("samples") or 0)
    positive_ratio = _number(stability.get("positive_ratio"))
    average = _number(stability.get("average_24h_pct"))
    volatility = _number(stability.get("volatility_pct"))
    if samples < 3 or positive_ratio is None:
        return 0.0
    scale = max(abs(average or 0.0), 0.05)
    volatility_quality = 1.0 - _clamp((volatility or 0.0) / (scale * 2.0), 0.0, 1.0)
    reversal_quality = 1.0 - _clamp(
        float(stability.get("sign_reversals") or 0) / max(1.0, samples - 1.0),
        0.0,
        1.0,
    )
    sample_factor = _clamp(samples / 24.0, 0.25, 1.0)
    return maximum * (0.55 * positive_ratio + 0.30 * volatility_quality + 0.15 * reversal_quality) * sample_factor


def _funding_stability_detail(stability: dict[str, Any]) -> str:
    samples = int(stability.get("samples") or 0)
    positive_ratio = _number(stability.get("positive_ratio"))
    volatility = _number(stability.get("volatility_pct"))
    reversals = int(stability.get("sign_reversals") or 0)
    if not samples or positive_ratio is None:
        return "Funding stability history unavailable"
    detail = f"{positive_ratio * 100:.0f}% positive across {samples} samples; {reversals} sign reversals"
    if volatility is not None:
        detail += f"; volatility {volatility:.3f}pp"
    return detail


def _funding_confidence(
    *,
    daily: float | None,
    settled_daily: float | None,
    windows: dict[str, Any],
    funding_stability: dict[str, Any],
    depth: float | None,
    min_volume: float | None,
    unresolved_identity: bool,
    executable: float | None,
    exact_symbols: bool,
) -> int:
    points = 20 if daily is not None else 0
    points += 5 if settled_daily is not None else 0
    points += sum(10 for label in ("1d", "7d", "30d") if _number(windows.get(label)) is not None)
    points += 15 if int(funding_stability.get("samples") or 0) >= 8 else 5 if funding_stability.get("samples") else 0
    points += 10 if depth is not None or min_volume is not None else 0
    points += 10 if not unresolved_identity else 0
    points += 5 if executable is not None else 0
    points += 5 if exact_symbols else 0
    return int(round(_clamp(float(points), 0.0, 100.0)))


def _spread_confidence(
    *,
    executable: float | None,
    convergence: dict[str, Any],
    risk: dict[str, Any],
    depth: float | None,
    min_volume: float | None,
    unresolved_identity: bool,
    historical: bool,
    exact_symbols: bool,
) -> int:
    points = 25 if executable is not None else 0
    samples = int(convergence.get("samples") or 0)
    points += 25 if samples >= 12 else 15 if samples >= 3 else 0
    points += min(20, int((risk.get("data_quality") or {}).get("confidence_points") or 0))
    points += 10 if depth is not None or min_volume is not None else 0
    points += 10 if not unresolved_identity else 0
    points += 5 if exact_symbols else 0
    points += 5 if not historical else 0
    return int(round(_clamp(float(points), 0.0, 100.0)))


def _opportunity_label(kind: str, score: float, confidence: int) -> str:
    noun = "funding" if kind == "funding" else "spread"
    if confidence < 45:
        return f"Low-confidence {noun} radar"
    if score >= 75:
        return f"Strong {noun} candidate"
    if score >= 55:
        return f"{noun.title()} candidate for review"
    if score >= 35:
        return f"Speculative {noun} setup"
    return f"Weak current {noun} evidence"


def _unavailable_opportunity(kind: str) -> dict[str, Any]:
    return {
        "score": None,
        "confidence": 0,
        "label": f"Insufficient {kind} data",
        "components": {},
    }


def _risk_limitations() -> list[str]:
    return [
        "Public history cannot see account equity, cross-margin offsets or other positions.",
        "Exact maintenance tiers, liquidation marks, fee tiers and leverage are venue/account specific.",
        "A historical percentile is not a guarantee; regime changes and exchange outages can exceed it.",
        "Depth is measured at the scanner notional and must be repriced at the member's intended size.",
    ]


def _unavailable_risk(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "recommended_collateral_pct": None,
        "research_leverage_ceiling": None,
        "summary": "Risk evidence unavailable",
        "data_quality": {"grade": "missing", "samples": 0, "span_hours": 0, "confidence_points": 0},
        "limitations": _risk_limitations(),
    }


def _component(value: float, maximum: float, detail: str) -> dict[str, Any]:
    return {"value": round(_clamp(value, 0.0, maximum), 1), "max": maximum, "detail": detail}


def _label(score: float, confidence: int) -> str:
    if confidence < 45:
        return "Low-confidence radar"
    if score >= 75:
        return "Strong research candidate"
    if score >= 55:
        return "Worth deeper review"
    if score >= 35:
        return "Speculative / incomplete"
    return "Weak current evidence"


def _log_scale(value: float, *, low: float, high: float) -> float:
    if value <= low:
        return 0.0
    return _clamp(math.log(value / low) / math.log(high / low), 0.0, 1.0)


def _percentile(values: list[float], quantile: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = _clamp(quantile, 0.0, 1.0) * (len(clean) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    weight = position - lower
    return clean[lower] * (1.0 - weight) + clean[upper] * weight


def _correlation(left: list[float], right: list[float]) -> float | None:
    count = min(len(left), len(right))
    if count < 3:
        return None
    x = left[:count]
    y = right[:count]
    mean_x = statistics.fmean(x)
    mean_y = statistics.fmean(y)
    variance_x = sum((value - mean_x) ** 2 for value in x)
    variance_y = sum((value - mean_y) ** 2 for value in y)
    if variance_x <= 0 or variance_y <= 0:
        return None
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    return _clamp(covariance / math.sqrt(variance_x * variance_y), -1.0, 1.0)


def _log_return(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    return math.log(end / start)


def _simple_return_pct(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))
